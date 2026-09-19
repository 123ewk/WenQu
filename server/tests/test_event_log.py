"""事件日志单元测试(ADR-3 / OPT-3):seq 单调、回放边界、跟随订阅、环形淘汰。

键是 stream_id(一次生成一条流);`after` 语义 = "给我 seq > after 的事件":
客户端把最后收到的 seq 传回来,续流从断点接着读,不重不丢。
"""

from __future__ import annotations

import threading
import uuid

import pytest

from app.core.event_log import MemoryEventLog


def test_append_assigns_monotonic_seq_per_stream() -> None:
    log = MemoryEventLog()
    sid = uuid.uuid4()
    assert log.append(sid, "meta", {"model": "m"}) == 1
    assert log.append(sid, "delta", {"text": "x"}) == 2
    other = uuid.uuid4()
    assert log.append(other, "meta", {}) == 1  # 流间 seq 独立


def test_replay_after_returns_only_newer_events() -> None:
    log = MemoryEventLog()
    sid = uuid.uuid4()
    for i in range(5):
        log.append(sid, "delta", {"i": i})
    assert [e.seq for e in log.replay_after(sid, 0)] == [1, 2, 3, 4, 5]
    assert [e.seq for e in log.replay_after(sid, 3)] == [4, 5]
    assert log.replay_after(sid, 99) == []


def test_payload_is_copied_on_append() -> None:
    log = MemoryEventLog()
    sid = uuid.uuid4()
    payload = {"text": "x"}
    log.append(sid, "delta", payload)
    payload["text"] = "append 之后被调用方篡改"
    assert log.replay_after(sid, 0)[0].payload == {"text": "x"}


def test_is_resumable_rules() -> None:
    log = MemoryEventLog(max_events_per_conversation=3)
    sid = uuid.uuid4()
    assert log.is_resumable(sid, 0) is False  # 进程内根本没有该流(如重启后)
    for i in range(5):  # 5 条进容量 3 的环 → 最旧的 1、2 被淘汰
        log.append(sid, "delta", {"i": i})
    assert log.is_resumable(sid, 2) is True  # 3 起可完整续
    assert log.is_resumable(sid, 1) is False  # 1→3 之间有断层
    assert log.is_resumable(sid, 0) is False


def test_follow_replays_then_returns_immediately_when_finished() -> None:
    log = MemoryEventLog()
    sid = uuid.uuid4()
    log.append(sid, "meta", {})
    log.append(sid, "done", {})
    log.mark_finished(sid)
    assert [e.type for e in log.follow(sid, 0)] == ["meta", "done"]
    assert [e.type for e in log.follow(sid, 1)] == ["done"]  # 断点续读


def test_follow_waits_for_live_events_until_finished() -> None:
    log = MemoryEventLog()
    sid = uuid.uuid4()
    log.append(sid, "meta", {})

    def produce() -> None:
        log.append(sid, "delta", {"t": 1})
        log.append(sid, "done", {})
        log.mark_finished(sid)

    thread = threading.Thread(target=produce)
    thread.start()
    try:
        assert [e.type for e in log.follow(sid, 0)] == ["meta", "delta", "done"]
    finally:
        thread.join()


def test_mark_finished_is_idempotent_and_blocks_append() -> None:
    log = MemoryEventLog()
    sid = uuid.uuid4()
    log.append(sid, "done", {})
    log.mark_finished(sid)
    log.mark_finished(sid)  # 幂等(进程关闭等场景可能重复触发)
    with pytest.raises(RuntimeError):
        log.append(sid, "delta", {})


def test_follow_unknown_stream_raises() -> None:
    log = MemoryEventLog()
    with pytest.raises(KeyError):
        list(log.follow(uuid.uuid4(), 0))


def test_follow_timeout_yields_none_tick_when_idle() -> None:
    """timeout 只在"无新事件且未结束"时打出 None 节拍;有事件/结束时不打。"""
    log = MemoryEventLog()
    sid = uuid.uuid4()
    log.append(sid, "meta", {})

    follow = log.follow(sid, 0, timeout=0.02)
    assert next(follow).type == "meta"  # 先回放存量
    assert next(follow) is None  # 未结束且无新事件 → 心跳节拍
    assert next(follow) is None
    follow.close()  # 未结束的 follow 不会自己停,调用方必须主动关

    log.append(sid, "done", {})
    log.mark_finished(sid)
    assert [e.type if e is not None else "TICK" for e in log.follow(sid, 0, timeout=0.02)] == [
        "meta",
        "done",
    ]


def test_concurrent_append_produces_unique_ordered_seq() -> None:
    log = MemoryEventLog()
    sid = uuid.uuid4()

    def worker(count: int) -> None:
        for _ in range(count):
            log.append(sid, "delta", {})

    threads = [threading.Thread(target=worker, args=(50,)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    seqs = [e.seq for e in log.replay_after(sid, 0)]
    assert len(seqs) == 200
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == 200
