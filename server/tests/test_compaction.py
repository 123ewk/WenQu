"""历史滚动压缩单元测试(OPT-45 P3a):assemble_history 回退等价 + compress 摘要写回。

无真实 DB:假消息仓储 + 假 chat_stream(分片拼回)。核心回归 = 无摘要/关闭时
assemble_history 恰为 history[-10:] 且不附摘要块,与旧硬截断逐元素一致。
"""

from __future__ import annotations

import uuid
from collections.abc import Generator

from app.application.service.compaction import (
    CompactionService,
    CompactResult,
    assemble_history,
    read_summary,
)
from app.core.config import Settings
from app.domain.models import Conversation, Message


def _settings(**overrides) -> Settings:
    base = {
        "compact_enabled": True,
        "compact_keep_recent": 6,
        "compact_min_messages": 8,
        "compact_trigger_ratio": 0.8,
    }
    base.update(overrides)
    return Settings(**base)


def _conv(meta: dict | None = None) -> Conversation:
    return Conversation(
        id=uuid.uuid4(), space_id=uuid.uuid4(), user_id=uuid.uuid4(), meta=dict(meta or {})
    )


def _msgs(count: int, start_seq: int = 1) -> list[Message]:
    out: list[Message] = []
    for i in range(count):
        role = "user" if i % 2 == 0 else "assistant"
        out.append(
            Message(
                conversation_id=uuid.uuid4(),
                seq=start_seq + i,
                role=role,
                content=f"m{start_seq + i}",
            )
        )
    return out


class FakeMessagesRepo:
    def __init__(self, items: list[Message]) -> None:
        self._items = items

    def list_for_conversation(self, conversation_id: uuid.UUID) -> list[Message]:
        return list(self._items)


class FakeConversationsRepo:
    def __init__(self) -> None:
        self.saved: list[Conversation] = []

    def save(self, conversation: Conversation) -> Conversation:
        self.saved.append(conversation)
        return conversation


class FakeChat:
    def __init__(self, reply: str = "摘要正文") -> None:
        self.reply = reply
        self.calls: list[list[dict[str, str]]] = []

    def __call__(self, messages, model_id=None) -> Generator[str, None, None]:
        self.calls.append(messages)
        yield self.reply[:3]
        yield self.reply[3:]


# ---- assemble_history:回归钉子 ----

def test_no_summary_is_legacy_last_ten() -> None:
    history = _msgs(25)
    summary, recent = assemble_history(history, {}, enabled=True, keep_recent=6)
    assert summary is None
    assert [m.seq for m in recent] == [16, 17, 18, 19, 20, 21, 22, 23, 24, 25]


def test_disabled_is_legacy_last_ten() -> None:
    history = _msgs(25)
    meta = {"summary": "旧摘要", "summary_through_seq": 10}
    summary, recent = assemble_history(history, meta, enabled=False, keep_recent=6)
    assert summary is None
    assert [m.seq for m in recent] == list(range(16, 26))


def test_with_summary_keeps_tail_after_through_seq() -> None:
    history = _msgs(30)
    meta = {"summary": "前情", "summary_through_seq": 20}
    summary, recent = assemble_history(history, meta, enabled=True, keep_recent=6)
    assert summary == "前情"
    # 压缩点之后 seq>20 的原文,取最近 6 条 → 25..30
    assert [m.seq for m in recent] == [25, 26, 27, 28, 29, 30]


def test_read_summary_defaults() -> None:
    assert read_summary(None) == ("", 0)
    assert read_summary({}) == ("", 0)
    assert read_summary({"summary": "x", "summary_through_seq": 7}) == ("x", 7)


# ---- compress ----

def test_compress_disabled_noop() -> None:
    conv = _conv()
    settings = _settings(compact_enabled=False)
    svc = CompactionService(
        FakeConversationsRepo(), FakeMessagesRepo(_msgs(50)), FakeChat(), settings
    )
    result = svc.compress(conv)
    assert result == CompactResult(False, "", 0, reason="disabled")
    assert conv.meta == {}


def test_compress_too_short_no_chat_call() -> None:
    conv = _conv()
    chat = FakeChat()
    svc = CompactionService(FakeConversationsRepo(), FakeMessagesRepo(_msgs(5)), chat, _settings())
    result = svc.compress(conv)
    assert result.compressed is False
    assert result.reason == "too_short"
    assert chat.calls == []  # 未达门槛绝不打模型


def test_compress_folds_and_advances_through_seq() -> None:
    conv = _conv()
    chat = FakeChat(reply="A|B 合并后的摘要")
    svc = CompactionService(FakeConversationsRepo(), FakeMessagesRepo(_msgs(20)), chat, _settings())
    result = svc.compress(conv)
    assert result.compressed is True
    assert result.summary == "A|B 合并后的摘要"
    # 20 条,keep=6 → 折叠 seq 1..14,through_seq=14
    assert result.summary_through_seq == 14
    assert len(chat.calls) == 1
    # 折叠的消息进了摘要提示词(含首尾,m14 折叠、m15 保留)
    prompt_text = chat.calls[0][1]["content"]
    assert "m1" in prompt_text and "m14" in prompt_text and "m15" not in prompt_text
    # 写回 meta
    assert conv.meta["summary"] == "A|B 合并后的摘要"
    assert conv.meta["summary_through_seq"] == 14


def test_compress_idempotent_then_too_short() -> None:
    history = _msgs(20)
    conv = _conv()
    repo = FakeConversationsRepo()
    chat = FakeChat()
    svc = CompactionService(repo, FakeMessagesRepo(history), chat, _settings())
    first = svc.compress(conv)
    assert first.compressed
    # 再压一次:候选 = seq>14 的 6 条 < min 8 → too_short,不再打模型
    chat2 = FakeChat()
    svc2 = CompactionService(repo, FakeMessagesRepo(history), chat2, _settings())
    second = svc2.compress(conv)
    assert second.compressed is False
    assert second.reason == "too_short"
    assert chat2.calls == []


def test_compress_empty_summary_noop() -> None:
    conv = _conv()
    chat = FakeChat(reply="")
    svc = CompactionService(FakeConversationsRepo(), FakeMessagesRepo(_msgs(20)), chat, _settings())
    result = svc.compress(conv)
    assert result.compressed is False
    assert result.reason == "empty_summary"
    assert conv.meta == {}
