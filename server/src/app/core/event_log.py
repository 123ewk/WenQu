"""流式事件日志(ADR-3 / OPT-3):断线续流的数据底座。

接口先行(基准 01,memory / Redis 两实现):单副本用 memory;扩多副本时切 Redis
(优化台账 OPT-14),消费方(qa 服务、续流路由)只依赖 EventLog 协议。

语义约定(两个实现必须对齐):
- 每个会话一条 append-only 流,seq 自 1 起按会话独立单调递增;
- `after` 语义 = "给我 seq > after 的事件"(客户端把最后收到的 seq 传回来);
- 环形容量:每会话最多保留 max_events_per_conversation 条,超限淘汰最旧,
  淘汰造成的断层用 is_resumable 判定(不可续 → 客户端回退拉 messages);
- payload 在 append 时浅拷贝定版,调用方此后修改不影响日志;嵌套值视同不可变;
- mark_finished 之后 append 是程序错误(RuntimeError);mark_finished 自身幂等。
"""

from __future__ import annotations

import threading
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
        self, conversation_id: uuid.UUID, event_type: str, payload: dict[str, Any]
    ) -> int: ...

    def replay_after(self, conversation_id: uuid.UUID, after_seq: int) -> list[StreamEvent]: ...

    def follow(self, conversation_id: uuid.UUID, after_seq: int) -> Iterator[StreamEvent]: ...

    def mark_finished(self, conversation_id: uuid.UUID) -> None: ...

    def is_resumable(self, conversation_id: uuid.UUID, after_seq: int) -> bool: ...


class _ConversationStream:
    """单会话的流状态:事件环 + seq 计数 + 完成标记,由自己的 Condition 保护。"""

    __slots__ = ("condition", "events", "finished", "next_seq")

    def __init__(self, capacity: int) -> None:
        self.events: deque[StreamEvent] = deque(maxlen=capacity)
        self.next_seq: int = 1
        self.finished: bool = False
        self.condition: threading.Condition = threading.Condition()


class MemoryEventLog:
    """内存实现:dict[conversation_id → 流状态],环形容量淘汰最旧事件。"""

    def __init__(self, max_events_per_conversation: int = 2000) -> None:
        if max_events_per_conversation < 1:
            raise ValueError("max_events_per_conversation 必须 >= 1")
        self._capacity = max_events_per_conversation
        self._streams: dict[uuid.UUID, _ConversationStream] = {}
        self._registry_lock = threading.Lock()

    def _get_stream(self, conversation_id: uuid.UUID) -> _ConversationStream:
        with self._registry_lock:
            try:
                return self._streams[conversation_id]
            except KeyError as exc:
                raise KeyError(f"事件日志中没有会话 {conversation_id}") from exc

    def append(
        self, conversation_id: uuid.UUID, event_type: str, payload: dict[str, Any]
    ) -> int:
        with self._registry_lock:
            stream = self._streams.get(conversation_id)
            if stream is None:
                stream = _ConversationStream(self._capacity)
                self._streams[conversation_id] = stream
        with stream.condition:
            if stream.finished:
                raise RuntimeError(f"会话 {conversation_id} 的流已结束,不能再 append")
            event = StreamEvent(
                seq=stream.next_seq, type=event_type, payload=dict(payload)
            )
            stream.next_seq += 1
            stream.events.append(event)  # deque(maxlen) 满时静默淘汰最旧
            stream.condition.notify_all()
        return event.seq

    def replay_after(self, conversation_id: uuid.UUID, after_seq: int) -> list[StreamEvent]:
        stream = self._get_stream(conversation_id)
        with stream.condition:
            return [e for e in stream.events if e.seq > after_seq]

    def follow(self, conversation_id: uuid.UUID, after_seq: int) -> Iterator[StreamEvent]:
        """回放 after 之后的存量事件,若流未结束则继续跟随实时事件,直到结束。"""
        stream = self._get_stream(conversation_id)
        with stream.condition:
            backlog = [e for e in stream.events if e.seq > after_seq]
        last = after_seq
        for event in backlog:
            yield event
            last = event.seq
        while True:
            with stream.condition:
                newer = [e for e in stream.events if e.seq > last]
                if not newer:
                    if stream.finished:
                        return
                    stream.condition.wait()
                    continue
            for event in newer:
                yield event
                last = event.seq

    def mark_finished(self, conversation_id: uuid.UUID) -> None:
        stream = self._get_stream(conversation_id)
        with stream.condition:
            if stream.finished:
                return
            stream.finished = True
            stream.condition.notify_all()

    def is_resumable(self, conversation_id: uuid.UUID, after_seq: int) -> bool:
        """after 位置之后是否可完整续:会话存在且不存在淘汰断层。"""
        with self._registry_lock:
            stream = self._streams.get(conversation_id)
            if stream is None:
                return False
        with stream.condition:
            if not stream.events:
                return False
            oldest = stream.events[0].seq
        return after_seq >= oldest - 1
