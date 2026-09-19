"""流式生成监督(OPT-3):读者计数 + 断线宽限 + 中止信号 + 资源治理。

策略(产品决策):客户端断开后,生成**最多再跑宽限期**(默认 5 秒,弱网不白烧
token,又不至于网络抖一下就截断回答);期间任何读者(ask 响应 / 续流路由)回来
都取消宽限继续生成;宽限耗尽仍无人回来 → 置位中止信号,泵线程停在下个事件边界,
释放上游连接并落库部分答案。

线程模型:泵线程消费上游模型流;定时器线程在宽限耗尽时置位;读流线程 attach/detach;
清道夫线程周期清理事件日志。所有状态由各 handle 自己的锁保护,Supervisor 只管注册表。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid

from app.core.event_log import EventLog

logger = logging.getLogger("app.streaming")


class GenerationHandle:
    """一次生成的生命周期句柄:泵线程与读流线程的会合点。"""

    def __init__(
        self, conversation_id: uuid.UUID, stream_id: uuid.UUID, grace_seconds: float
    ) -> None:
        self.conversation_id = conversation_id
        self.stream_id = stream_id  # 事件日志的键:一次生成一条流
        self.stop_requested = threading.Event()
        self._grace_seconds = grace_seconds
        self._lock = threading.Lock()
        self._readers = 0
        self._timer: threading.Timer | None = None
        self._finished = False
        self._finished_event = threading.Event()

    def reader_attached(self) -> None:
        """读流线程接入:撤销宽限计时(有人在看,继续生成)。"""
        with self._lock:
            if self._finished:
                return
            self._readers += 1
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def reader_detached(self) -> None:
        """读流线程离开(断开或读完):读者归零时起宽限计时。"""
        with self._lock:
            if self._finished:
                return
            self._readers = max(0, self._readers - 1)
            if self._readers == 0 and self._timer is None:
                self._timer = threading.Timer(self._grace_seconds, self._on_grace_expired)
                self._timer.daemon = True
                self._timer.start()

    def mark_finished(self) -> None:
        """生成结束(正常完成/中止/被新请求取代):撤销计时,句柄终态。"""
        with self._lock:
            self._finished = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._finished_event.set()

    def wait_finished(self, timeout: float) -> bool:
        """有界等待泵收尾(关停兜底用);返回是否在时限内等到。"""
        return self._finished_event.wait(timeout)

    def _on_grace_expired(self) -> None:
        with self._lock:
            if self._finished or self._readers > 0:
                return  # 宽限期内有人回来了(或已自然结束)
            self._timer = None
            self.stop_requested.set()


class StreamSupervisor:
    """按会话登记进行中的生成;同会话新请求会立即作废旧流(防事件交错)。"""

    def __init__(self, grace_seconds: float = 5.0) -> None:
        self._grace_seconds = grace_seconds
        self._active: dict[uuid.UUID, GenerationHandle] = {}
        self._last_stream: dict[uuid.UUID, uuid.UUID] = {}
        self._lock = threading.Lock()

    def start(self, conversation_id: uuid.UUID) -> GenerationHandle:
        with self._lock:
            old = self._active.get(conversation_id)
            if old is not None:
                old.stop_requested.set()
                old.mark_finished()
            handle = GenerationHandle(
                conversation_id, uuid.uuid4(), self._grace_seconds
            )
            self._active[conversation_id] = handle
            self._last_stream[conversation_id] = handle.stream_id
            return handle

    def get(self, conversation_id: uuid.UUID) -> GenerationHandle | None:
        with self._lock:
            return self._active.get(conversation_id)

    def last_stream_id(self, conversation_id: uuid.UUID) -> uuid.UUID | None:
        """该会话最近一次生成的流 id(生成结束后仍可查,供续流回放)。"""
        with self._lock:
            return self._last_stream.get(conversation_id)

    def finish(self, conversation_id: uuid.UUID) -> None:
        with self._lock:
            handle = self._active.pop(conversation_id, None)
        if handle is not None:
            handle.mark_finished()

    def shutdown_all(self, wait_seconds: float = 5.0) -> int:
        """进程关停兜底:置位全部中止信号(泵据此落库部分答案),有界等待收尾。

        返回被通知的生成数。等待有界:泵若阻塞在上游读(最长 read 超时)则放弃
        等待,由守护线程随进程退出 —— 极端场景丢部分答案,不做无限期阻塞。
        """
        with self._lock:
            handles = list(self._active.values())
        for handle in handles:
            handle.stop_requested.set()
        deadline = time.monotonic() + wait_seconds
        for handle in handles:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            handle.wait_finished(remaining)
        return len(handles)


class StreamJanitor:
    """事件日志清道夫:周期 evict 已完成且空闲超时的流(TTL 可配,默认 1 小时)。

    事件日志是 API 进程内单例,清道夫必须在同一进程运行(worker 进程摸不到它);
    因此不进 worker 的维护任务,而是随 API 生命周期启停。
    """

    def __init__(self, event_log: EventLog, interval_seconds: float = 600.0) -> None:
        self._event_log = event_log
        self._interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="stream-janitor"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                evicted = self._event_log.evict_idle()
                if evicted:
                    logger.info("事件日志清理了 %d 条空闲流", evicted)
            except Exception:  # noqa: BLE001 — 清理失败只记日志,不影响主流程
                logger.exception("事件日志清理失败")
