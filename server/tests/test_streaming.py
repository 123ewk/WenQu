"""流监督器单元测试(OPT-3):读者计数、宽限计时、同会话作旧、全量停机。"""

from __future__ import annotations

import threading
import time
import uuid

from app.application.streaming import GenerationHandle, StreamJanitor, StreamSupervisor


def test_reader_detach_arms_grace_then_stop_requested() -> None:
    handle = GenerationHandle(uuid.uuid4(), uuid.uuid4(), grace_seconds=0.05)
    handle.reader_attached()
    handle.reader_detached()
    assert handle.stop_requested.wait(0.5)  # 宽限耗尽且无人回来 → 中止
    assert handle.stop_requested.is_set()


def test_reader_reattach_cancels_grace() -> None:
    handle = GenerationHandle(uuid.uuid4(), uuid.uuid4(), grace_seconds=0.05)
    handle.reader_attached()
    handle.reader_detached()
    time.sleep(0.02)
    handle.reader_attached()  # 宽限期内回来 → 计时撤销,永不置位
    time.sleep(0.1)
    assert not handle.stop_requested.is_set()


def test_grace_expiry_skipped_when_reader_present() -> None:
    handle = GenerationHandle(uuid.uuid4(), uuid.uuid4(), grace_seconds=0.05)
    handle.reader_attached()
    handle.reader_detached()
    handle.reader_attached()  # 计时刚起就回来
    time.sleep(0.15)
    assert not handle.stop_requested.is_set()


def test_mark_finished_is_terminal() -> None:
    handle = GenerationHandle(uuid.uuid4(), uuid.uuid4(), grace_seconds=0.03)
    handle.reader_attached()
    handle.mark_finished()
    handle.reader_detached()  # 终态后 detach 不再起计时
    time.sleep(0.1)
    assert not handle.stop_requested.is_set()


def test_supervisor_start_supersedes_old_handle() -> None:
    supervisor = StreamSupervisor(grace_seconds=5.0)
    cid = uuid.uuid4()
    first = supervisor.start(cid)
    second = supervisor.start(cid)  # 同会话并发的第二次 ask:旧流立即作废
    assert first.stop_requested.is_set()
    assert supervisor.get(cid) is second


def test_supervisor_shutdown_all_requests_stop() -> None:
    supervisor = StreamSupervisor(grace_seconds=5.0)
    handles = [supervisor.start(uuid.uuid4()) for _ in range(3)]
    supervisor.shutdown_all()
    assert all(h.stop_requested.is_set() for h in handles)


def test_supervisor_finish_removes_handle() -> None:
    supervisor = StreamSupervisor()
    cid = uuid.uuid4()
    supervisor.start(cid)
    supervisor.finish(cid)
    assert supervisor.get(cid) is None


def test_shutdown_all_notifies_and_waits_for_finish() -> None:
    """关停兜底:置位中止信号;泵收尾后 wait 返回;未收尾的有界放弃。"""
    supervisor = StreamSupervisor(grace_seconds=5.0)
    finishing = supervisor.start(uuid.uuid4())
    stuck = supervisor.start(uuid.uuid4())

    def finish_soon() -> None:
        time.sleep(0.05)
        supervisor.finish(finishing.conversation_id)

    thread = threading.Thread(target=finish_soon)
    thread.start()
    notified = supervisor.shutdown_all(wait_seconds=2.0)
    thread.join()
    assert notified == 2
    assert finishing.stop_requested.is_set()
    assert finishing.wait_finished(0)  # 已等到收尾
    assert not stuck.wait_finished(0)  # 没收尾的有界放弃


def test_janitor_periodically_evicts() -> None:
    """清道夫按周期调用 evict_idle,stop 后不再调用。"""

    class CountingLog:
        def __init__(self) -> None:
            self.calls = 0

        def evict_idle(self) -> int:
            self.calls += 1
            return 0

    log = CountingLog()
    janitor = StreamJanitor(log, interval_seconds=0.02)
    janitor.start()
    time.sleep(0.1)
    janitor.stop()
    calls_at_stop = log.calls
    assert calls_at_stop >= 2
    time.sleep(0.05)
    assert log.calls == calls_at_stop  # stop 后不再跑


def test_grace_expiry_race_with_reader_attach_is_benign() -> None:
    """定时器已触发后才 attach:中止已置位,attach 不撤销(5 秒边界语义)。"""
    handle = GenerationHandle(uuid.uuid4(), uuid.uuid4(), grace_seconds=0.01)
    handle.reader_attached()
    handle.reader_detached()
    handle.stop_requested.wait(0.5)
    handle.reader_attached()
    assert handle.stop_requested.is_set()
