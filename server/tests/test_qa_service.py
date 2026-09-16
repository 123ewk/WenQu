"""问答服务单元测试(假检索/假网关,基准 03):SSE 事件时序、引用映射、会话归属。"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app.application.service.qa import QAService
from app.core.errors import AppError
from app.domain.enums import Role
from app.domain.models import Conversation, Membership, Message, Space
from tests.fakes import FakeSpaceRepository, FakeUserRepository, make_user


class FakeConvRepo:
    def __init__(self) -> None:
        self.items: dict[uuid.UUID, Conversation] = {}

    def create(self, conversation: Conversation) -> Conversation:
        if conversation.id is None:
            conversation.id = uuid.uuid4()
        self.items[conversation.id] = conversation
        return conversation

    def get(self, conversation_id: uuid.UUID) -> Conversation | None:
        return self.items.get(conversation_id)

    def save(self, conversation: Conversation) -> Conversation:
        self.items[conversation.id] = conversation
        return conversation

    def delete(self, conversation: Conversation) -> None:
        self.items.pop(conversation.id, None)

    def list_for_user(self, space_id, user_id) -> list[Conversation]:
        return [
            c for c in self.items.values() if c.space_id == space_id and c.user_id == user_id
        ]


class FakeMsgRepo:
    def __init__(self) -> None:
        self.items: dict[uuid.UUID, Message] = {}

    def add(self, message: Message) -> Message:
        if message.id is None:
            message.id = uuid.uuid4()
        if message.citations is None:
            message.citations = []
        self.items[message.id] = message
        return message

    def list_for_conversation(self, conversation_id) -> list[Message]:
        return sorted(
            (m for m in self.items.values() if m.conversation_id == conversation_id),
            key=lambda m: m.seq,
        )

    def next_seq(self, conversation_id) -> int:
        seqs = [m.seq for m in self.items.values() if m.conversation_id == conversation_id]
        return max(seqs or [0]) + 1


class FakeRetrieval:
    def __init__(self, hits: list | None = None) -> None:
        self.hits = hits if hits is not None else []
        self.queries: list[str] = []

    def search(self, user_id, space_id, query, space_ids=None, top_k=8, kb_ids=None, model_id=None):
        self.queries.append(query)
        return self.hits[:top_k]


class FakeChat:
    """可编程假网关:按 pieces 产出,或抛预置异常。"""

    def __init__(self, pieces: list[str] | None = None, exc: Exception | None = None) -> None:
        self.pieces = pieces or ["答案"]
        self.exc = exc
        self.messages: list[list[dict[str, str]]] = []

    def chat_stream(self, messages, model_id=None):
        self.messages.append(messages)
        if self.exc is not None:
            raise self.exc
        yield from self.pieces


def _hit(index_text: str):
    return SimpleNamespace(
        chunk_id=str(uuid.uuid4()),
        document_id=str(uuid.uuid4()),
        kb_id=str(uuid.uuid4()),
        filename="手册.md",
        content=index_text,
        score=0.42,
        vector_rank=1,
        fulltext_rank=1,
        meta={"breadcrumb": ["第一章"], "page": 3},
    )


def build_env(hits=None, chat=None, role=Role.VIEWER):
    users = FakeUserRepository()
    spaces = FakeSpaceRepository(users_ref=users.users)
    user = users.create(make_user("u"))
    space = spaces.create(Space(name="s", description=""))
    spaces.add_member(Membership(space_id=space.id, user_id=user.id, role=role))
    svc = QAService(FakeConvRepo(), FakeMsgRepo(), spaces, FakeRetrieval(hits), chat or FakeChat())
    return SimpleNamespace(service=svc, user=user, space=space, chat=chat or FakeChat())


def _events(stream) -> list[dict]:
    return [json.loads(line[len("data: ") :]) for line in stream if line.startswith("data: ")]


def test_ask_stream_event_order_and_citation_mapping() -> None:
    hits = [_hit("第一段资料"), _hit("第二段资料")]
    chat = FakeChat(["根据资料 [1],", "结论如下 [2]。"])
    env = build_env(hits=hits, chat=chat)

    events = _events(env.service.ask_stream(env.user.id, env.space.id, "问题是什么"))
    assert [e["type"] for e in events] == ["meta", "citations", "delta", "delta", "done"]

    # 引用编号与检索结果一一对应,且携带可回链字段 + 溯源信息
    citations = events[1]["citations"]
    assert [c["index"] for c in citations] == [1, 2]
    assert citations[0]["chunk_id"] == hits[0].chunk_id
    assert citations[0]["filename"] == "手册.md"
    assert citations[0]["breadcrumb"] == ["第一章"] and citations[0]["page"] == 3
    assert events[4]["cited_indexes"] == [1, 2]

    # 提示词:系统消息带编号资料,末尾是用户提问
    sent = chat.messages[0]
    assert sent[0]["role"] == "system"
    assert "[1] 来源:手册.md" in sent[0]["content"]
    assert sent[0]["content"].index("=== 资料开始 ===") < sent[0]["content"].index("第一段资料")
    assert sent[-1] == {"role": "user", "content": "问题是什么"}


def test_ask_stream_persists_messages_with_citations() -> None:
    hits = [_hit("资料")]
    env = build_env(hits=hits, chat=FakeChat(["答案 [1]"]))
    events = _events(env.service.ask_stream(env.user.id, env.space.id, "提问"))
    conversation_id = uuid.UUID(events[0]["conversation_id"])

    stored = env.service.get_conversation_messages(env.user.id, env.space.id, conversation_id)
    assert [m.role for m in stored] == ["user", "assistant"]
    assert stored[0].content == "提问" and stored[1].content == "答案 [1]"
    assert stored[1].citations[0]["chunk_id"] == hits[0].chunk_id  # 引用可回溯落库


def test_ask_without_hits_returns_notice_and_no_model_call() -> None:
    chat = FakeChat(["不应被调用"])
    env = build_env(hits=[], chat=chat)
    events = _events(env.service.ask_stream(env.user.id, env.space.id, "冷门问题"))
    types = [e["type"] for e in events]
    assert types == ["meta", "citations", "delta", "done"]
    assert events[1]["citations"] == []
    assert "没有检索到" in events[2]["text"]
    assert chat.messages == []  # 无资料不调模型


def test_ask_model_failure_emits_error_event() -> None:
    env = build_env(hits=[_hit("资料")], chat=FakeChat(exc=RuntimeError("upstream 500")))
    events = _events(env.service.ask_stream(env.user.id, env.space.id, "提问"))
    assert events[-1]["type"] == "error"
    assert "模型调用失败" in events[-1]["message"]


def test_ask_reuses_existing_conversation_and_history_in_prompt() -> None:
    chat = FakeChat(["第二次回答"])
    env = build_env(hits=[_hit("资料")], chat=chat)
    first = _events(env.service.ask_stream(env.user.id, env.space.id, "第一个问题"))
    conv_id = uuid.UUID(first[0]["conversation_id"])

    second_events = _events(
        env.service.ask_stream(env.user.id, env.space.id, "第二个问题", conversation_id=conv_id)
    )
    assert second_events[0]["conversation_id"] == str(conv_id)  # 复用同一会话
    assert [e["type"] for e in second_events] == ["meta", "citations", "delta", "done"]

    # 第二次的 prompt 必须含上一轮问答(多轮上下文)
    second_prompt = chat.messages[1]
    roles = [(m["role"], m["content"][:12]) for m in second_prompt]
    assert ("user", "第一个问题") in roles
    assert ("assistant", "第二次回答") in roles


def test_ask_requires_membership_and_own_conversation() -> None:
    env = build_env()
    outsider = uuid.uuid4()
    with pytest.raises(AppError) as exc_info:
        env.service.ask_stream(outsider, env.space.id, "提问")
    assert exc_info.value.code_str == "SPACE_NOT_FOUND"

    conv = env.service.create_conversation(env.user.id, env.space.id, "我的会话")
    other = uuid.uuid4()
    with pytest.raises(AppError) as not_found:
        env.service.get_conversation_messages(other, env.space.id, conv.id)
    assert not_found.value.code_str == "SPACE_NOT_FOUND"


def test_ask_empty_question_rejected() -> None:
    env = build_env()
    with pytest.raises(AppError) as exc_info:
        env.service.ask_stream(env.user.id, env.space.id, "   ")
    assert exc_info.value.code_str == "VALIDATION_ERROR"
