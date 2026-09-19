"""流式事件日志(ADR-3 / OPT-3):断线续流的数据底座。

接口先行(基准 01,memory / Redis 两实现):单副本用 memory;扩多副本时切 Redis
(优化台账 OPT-14),消费方(qa 服务、续流路由)只依赖 EventLog 协议。

键是 **stream_id(一次生成一条流)**,不是会话 —— 同一会话的第二次提问开新流,
旧流继续可回放;会话 → 当前流的映射由 StreamSupervisor 维护。

语义约定(两个实现必须对齐):
- 每条流 append-only,seq 自 1 起按流独立单调递增;
- `after` 语义 = "给我 seq > after 的事件"(客户端把最后收到的 seq 传回来);
- 环形容量:每流最多保留 max_events_per_conversation 条,超限淘汰最旧,
  淘汰造成的断层用 is_resumable 判定(不可续 → 客户端回退拉 messages);
- payload 在 append 时浅拷贝定版,调用方此后修改不影响日志;嵌套值视同不可变;
- mark_finished 之后 append 是程序错误(RuntimeError);mark_finished 自身幂等。
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class StreamEvent:
    """日志中的一条流事件:seq 单调,type 与 SSE 事件类型一致,payload 不含 seq。"""

    seq: int
    type: str
    payload: dict[str, Any]


class EventLog(Protocol):
    def append(
        self, stream_id: uuid.UUID, event_type: str, payload: dict[str, Any]
    ) -> int: ...

    def replay_after(self, stream_id: uuid.UUID, after_seq: int) -> list[StreamEvent]: ...

    def follow(
        self, stream_id: uuid.UUID, after_seq: int, timeout: float | None = None
    ) -> Iterator[StreamEvent | None]: ...

    def mark_finished(self, stream_id: uuid.UUID) -> None: ...

    def is_resumable(self, stream_id: uuid.UUID, after_seq: int) -> bool: ...

    def evict_idle(self) -> int: ...


class _StreamState:
    """单条流的状态:事件环 + seq 计数 + 完成标记,由自己的 Condition 保护。"""

    __slots__ = ("condition", "events", "finished", "last_activity", "next_seq")

    def __init__(self, capacity: int) -> None:
        self.events: deque[StreamEvent] = deque(maxlen=capacity)
        self.next_seq: int = 1
        self.finished: bool = False
        self.last_activity: float = time.monotonic()  # 最近一次 append,供 TTL 清理
        self.condition: threading.Condition = threading.Condition()


class MemoryEventLog:
    """内存实现:dict[stream_id → 流状态],环形容量淘汰最旧事件。"""

    def __init__(
        self,
        max_events_per_conversation: int = 2000,
        idle_ttl_seconds: float = 3600.0,
    ) -> None:
        if max_events_per_conversation < 1:
            raise ValueError("max_events_per_conversation 必须 >= 1")
        self._capacity = max_events_per_conversation
        self._idle_ttl_seconds = idle_ttl_seconds
        self._streams: dict[uuid.UUID, _StreamState] = {}
        self._registry_lock = threading.Lock()

    def _get_stream(self, stream_id: uuid.UUID) -> _StreamState:
        with self._registry_lock:
            try:
                return self._streams[stream_id]
            except KeyError as exc:
                raise KeyError(f"事件日志中没有流 {stream_id}") from exc

    def append(
        self, stream_id: uuid.UUID, event_type: str, payload: dict[str, Any]
    ) -> int:
        with self._registry_lock:
            stream = self._streams.get(stream_id)
            if stream is None:
                stream = _StreamState(self._capacity)
                self._streams[stream_id] = stream
        with stream.condition:
            if stream.finished:
                raise RuntimeError(f"流 {stream_id} 已结束,不能再 append")
            event = StreamEvent(
                seq=stream.next_seq, type=event_type, payload=dict(payload)
            )
            stream.next_seq += 1
            stream.events.append(event)  # deque(maxlen) 满时静默淘汰最旧
            stream.last_activity = time.monotonic()
            stream.condition.notify_all()
        return event.seq

    def replay_after(self, stream_id: uuid.UUID, after_seq: int) -> list[StreamEvent]:
        stream = self._get_stream(stream_id)
        with stream.condition:
            return [e for e in stream.events if e.seq > after_seq]

    def follow(
        self, stream_id: uuid.UUID, after_seq: int, timeout: float | None = None
    ) -> Iterator[StreamEvent | None]:
        """回放 after 之后的存量事件,若流未结束则继续跟随实时事件,直到结束。

        timeout(None = 不限时)是"无新事件"的最长阻塞时长:超时产出 None 让调用方
        有机会发心跳帧/检查断开 —— 读流必须保持有界阻塞,否则 Starlette 关闭响应
        生成器时无法在 yield 点送达 GeneratorExit,线程会一直挂在 wait() 里。
        """
        stream = self._get_stream(stream_id)
        with stream.condition:
            backlog = [e for e in stream.events if e.seq > after_seq]
        last = after_seq
        for event in backlog:
            yield event
            last = event.seq
        while True:
            timed_out = False
            with stream.condition:
                newer = [e for e in stream.events if e.seq > last]
                if not newer and stream.finished:
                    return
                if not newer:
                    if timeout is None:
                        stream.condition.wait()
                        continue
                    if not stream.condition.wait(timeout):
                        timed_out = True
                    else:
                        continue
            if timed_out:
                yield None
                continue
            for event in newer:
                yield event
                last = event.seq

    def mark_finished(self, stream_id: uuid.UUID) -> None:
        stream = self._get_stream(stream_id)
        with stream.condition:
            if stream.finished:
                return
            stream.finished = True
            stream.condition.notify_all()

    def is_resumable(self, stream_id: uuid.UUID, after_seq: int) -> bool:
        """after 位置之后是否可完整续:流存在且不存在淘汰断层。"""
        with self._registry_lock:
            stream = self._streams.get(stream_id)
            if stream is None:
                return False
        with stream.condition:
            if not stream.events:
                return False
            oldest = stream.events[0].seq
        return after_seq >= oldest - 1

    def evict_idle(self) -> int:
        """清理已完成且空闲超过 idle_ttl_seconds 的流(资源治理,OPT-3 收尾)。

        只清 finished 流:进行中的生成不会因空闲被误杀(模型读超时 180s 远小于
        默认 TTL 1h)。last_activity 的跨线程读只用于粗粒度 TTL 判断,无需加锁。
        """
        now = time.monotonic()
        with self._registry_lock:
            stale = [
                sid
                for sid, stream in self._streams.items()
                if stream.finished and now - stream.last_activity > self._idle_ttl_seconds
            ]
            for sid in stale:
                self._streams.pop(sid, None)
        return len(stale)
