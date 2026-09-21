"""上下文窗口进度条集成测试(OPT-29,真 Postgres + 打桩上游协议)。

绝不真连厂商:`get_protocol` 换假适配器;CI 无 .env → 主密钥显式注入。

覆盖:
① done 帧两值 = 本轮最后一次上游调用(厂商回传优先,分子是 prompt+completion);
② 厂商不回 usage → 按"1 汉字 0.75 / 1 字母 0.25"本地估算;
③ 模型没声明窗口 → used 有值 + window null(前端"窗口未配置"),回答照常流式;
④ 刷新后 GET messages 给出同一对值,user 行两值 null;
⑤ 断线续播的事件流里 done 帧仍带两值;
⑥ 无检索命中的兜底回答没打模型 → 双 null;
⑦ 多次调用只取最后一次,不累加(Agent 每轮都在重发同一份上下文);
⑧ 工具轮只吐 tool_calls 且厂商没回 usage = "这次调用没发生" → 不进收集器也不落行
   (OPT-28 既有守卫的边界,这里钉住行为而不是当 bug 修)。
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import delete, select

from app.application.service.usage import UsageRecorder
from app.core.crypto import CredentialCipher
from app.core.db import get_session_factory
from app.core.model_catalog import ModelCatalog
from app.core.model_client import ChatClient
from app.core.model_protocols import ToolCallRequest
from app.core.model_usage import (
    ChatTurnUsage,
    ContextWindowUsage,
    estimate_usage_tokens,
    usage_prompt_text,
)
from app.domain.enums import DocumentStatus, UsageKind
from app.domain.models import (
    Chunk,
    CustomModel,
    Document,
    KnowledgeBase,
    ModelProvider,
    SpaceModelSettings,
    TokenUsageEvent,
    User,
)
from tests.conftest import register as _register

pytestmark = pytest.mark.integration

_CIPHER_KEY = "context-window-test-key"
_WINDOW = 64000
_ANSWER = ("根据资料 [1],", "混合检索采用 RRF 融合。")
_UPSTREAM = {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}


@pytest.fixture(autouse=True)
def _cipher(monkeypatch: pytest.MonkeyPatch) -> None:
    def factory() -> CredentialCipher:
        return CredentialCipher(_CIPHER_KEY)

    monkeypatch.setattr(
        "app.application.service.model_admin.get_credential_cipher", factory
    )
    monkeypatch.setattr("app.core.model_client.get_credential_cipher", factory)


@pytest.fixture(autouse=True)
def _reset_tables():
    """全局层模型也会泄漏给其他文件的 503 用例,前后各清一遍。"""

    def _clean() -> None:
        with get_session_factory()() as db:
            db.execute(delete(TokenUsageEvent))
            db.execute(delete(SpaceModelSettings))
            db.execute(delete(CustomModel))
            db.execute(delete(ModelProvider))
            db.commit()

    _clean()
    yield
    _clean()


class _FakeProtocol:
    """假适配器:按预设回传 usage(或不回传),并记下每次的 messages 供估算核对。"""

    def __init__(
        self,
        *,
        chat_usage: dict | None = _UPSTREAM,
        embed_usage: dict | None = None,
    ) -> None:
        self.chat_usage = chat_usage
        self.embed_usage = embed_usage
        self.prompts: list[list[dict]] = []

    def embed(
        self, provider, model, api_key, texts, *, semaphore=None, transport=None, usage=None
    ):
        if usage is not None and self.embed_usage is not None:
            usage.add_upstream(self.embed_usage)
        return [_query_vec() for _ in texts]

    def chat_stream(
        self,
        provider,
        model,
        api_key,
        messages,
        temperature,
        *,
        transport=None,
        usage=None,
    ):
        self.prompts.append(messages)
        if usage is not None and self.chat_usage is not None:
            usage.add_upstream(self.chat_usage)
        yield from _ANSWER

    def chat_stream_tools(
        self,
        provider,
        model,
        api_key,
        messages,
        tools,
        temperature,
        *,
        transport=None,
        usage=None,
    ):
        # 工具轮只吐调用意图,没有正文增量 —— 外壳因此收不到任何字符串
        if usage is not None and self.chat_usage is not None:
            usage.add_upstream(self.chat_usage)
        yield ToolCallRequest(id="call-1", name="search", arguments='{"query":"x"}')


def _patch_protocol(monkeypatch: pytest.MonkeyPatch, protocol: _FakeProtocol) -> None:
    monkeypatch.setattr("app.core.model_client.get_protocol", lambda key: protocol)


def _query_vec() -> list[float]:
    v = [0.0] * 1024
    v[0] = 1.0
    return v


def _auth(session: dict) -> dict:
    return {"Authorization": f"Bearer {session['access_token']}"}


def _space(client, username: str, space_name: str, *, with_chat: bool = True) -> tuple[str, dict]:
    """注册 + 建空间 + 配 embedding Key;with_chat 决定是否配空间层 chat 模型。"""
    session = _register(client, username)
    auth = _auth(session)
    resp = client.post("/api/v1/spaces", json={"name": space_name}, headers=auth)
    assert resp.status_code == 201, resp.text
    space_id = resp.json()["id"]
    base = f"/api/v1/spaces/{space_id}/model-config"
    assert client.put(
        f"{base}/embedding-key", json={"api_key": "sk-a1b2c3d4e5f6g7h8"}, headers=auth
    ).status_code == 200
    if with_chat:
        _space_chat_model(client, space_id, auth)
    return space_id, auth


def _space_chat_model(client, space_id: str, auth: dict) -> None:
    provider = client.post(
        f"/api/v1/spaces/{space_id}/model-config/providers",
        json={
            "provider_type": "deepseek",
            "name": f"窗口厂{uuid.uuid4().hex[:6]}",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-1234567890abcdef",
        },
        headers=auth,
    )
    assert provider.status_code == 201, provider.text
    model = client.post(
        f"/api/v1/spaces/{space_id}/model-config/models",
        json={
            "provider_id": provider.json()["id"],
            "kind": "chat",
            "model_name": "deepseek-chat",
            "context_tokens": _WINDOW,
            "is_default": True,
        },
        headers=auth,
    )
    assert model.status_code == 201, model.text


def _global_chat_model_without_window(client, username: str) -> None:
    """空间层 API 要求 chat 模型必须声明窗口,全局层留空才是"窗口未配置"的真实来源。"""
    root = _register(client, username)
    with get_session_factory()() as db:
        user = db.get(User, uuid.UUID(root["user"]["id"]))
        assert user is not None
        user.is_superuser = True
        db.commit()
    auth = _auth(root)
    provider = client.post(
        "/api/v1/admin/model-providers",
        json={
            "name": f"全局厂{uuid.uuid4().hex[:6]}",
            "protocol": "openai_compatible",
            "base_url": "https://api.example.com/v1/",
            "api_key": "sk-1234567890abcdef",
        },
        headers=auth,
    )
    assert provider.status_code == 201, provider.text
    model = client.post(
        "/api/v1/admin/models",
        json={
            "provider_id": provider.json()["id"],
            "model_id": f"全局厂/chat-{uuid.uuid4().hex[:6]}",
            "kind": "chat",
            "model_name": "chat-v1",
            "is_default": True,
        },
        headers=auth,
    )
    assert model.status_code == 201, model.text


def _seed_chunk(space_id: str) -> None:
    """一条已完成文档 + 与查询向量同向的子块:保证检索命中,流水线走到生成阶段。"""
    import jieba
    from sqlalchemy import func

    with get_session_factory()() as db:
        kb = KnowledgeBase(space_id=uuid.UUID(space_id), name="窗口库")
        db.add(kb)
        db.flush()
        doc = Document(
            kb_id=kb.id,
            space_id=kb.space_id,
            filename="检索手册.md",
            format="md",
            source=f"{space_id}/{kb.id}/d",
            status=DocumentStatus.COMPLETED,
            uploaded_by=None,
        )
        db.add(doc)
        db.flush()
        content = "混合检索算法使用 RRF 融合向量与全文排名。"
        db.add(
            Chunk(
                document_id=doc.id,
                space_id=doc.space_id,
                seq=0,
                content=content,
                embedding=_query_vec(),
                tsv=func.to_tsvector("simple", " ".join(jieba.cut_for_search(content))),
            )
        )
        db.commit()


def _ask(client, space_id: str, auth: dict, question: str = "混合检索算法是什么") -> list[dict]:
    resp = client.post(
        f"/api/v1/spaces/{space_id}/ask",
        json={"question": question, "top_k": 3},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    return [
        json.loads(line[len("data: ") :])
        for line in resp.text.splitlines()
        if line.startswith("data: ")
    ]


def _done(events: list[dict]) -> dict:
    assert events[-1]["type"] == "done", events
    return events[-1]


def _rows() -> list[TokenUsageEvent]:
    with get_session_factory()() as db:
        return list(db.scalars(select(TokenUsageEvent).order_by(TokenUsageEvent.kind)))


def _metered_chat(space_id: str, turn: ChatTurnUsage):
    """真目录 + 真外壳 + 真记账,只是不归属到成员(user_id 为空只进空间合计)。"""
    with get_session_factory()() as db:
        catalog = ModelCatalog.load_from_db(db, uuid.UUID(space_id))
    return UsageRecorder(get_session_factory()).chat(
        ChatClient(catalog), uuid.UUID(space_id), None, turn=turn
    )


# ---------------------------- 直检问答链路 ----------------------------


def test_done_frame_carries_last_call_numbers(client, monkeypatch) -> None:
    """① 分子取厂商回传的 prompt+completion,分母取模型声明的窗口。"""
    space_id, auth = _space(client, "cwqa", "窗口问答空间")
    _seed_chunk(space_id)
    _patch_protocol(monkeypatch, _FakeProtocol())

    done = _done(_ask(client, space_id, auth))
    assert done["context_used_tokens"] == 16
    assert done["context_window_tokens"] == _WINDOW


def test_done_frame_estimates_when_upstream_silent(client, monkeypatch) -> None:
    """② 厂商没回 usage:按 0.75/0.25 口径估,与用量行的估算数字同源。"""
    space_id, auth = _space(client, "cwest", "窗口估算空间")
    _seed_chunk(space_id)
    protocol = _FakeProtocol(chat_usage=None)
    _patch_protocol(monkeypatch, protocol)

    done = _done(_ask(client, space_id, auth))
    expected = estimate_usage_tokens(usage_prompt_text(protocol.prompts[-1]))
    expected += estimate_usage_tokens("".join(_ANSWER))
    assert done["context_used_tokens"] == expected
    assert done["context_used_tokens"] > 0
    assert done["context_window_tokens"] == _WINDOW


def test_window_null_when_model_has_no_context_tokens(client, monkeypatch) -> None:
    """③ 没声明窗口 → used 有值 + window null(绝不双 null,那与"没打模型"撞车)。"""
    space_id, auth = _space(client, "cwnowin", "无窗口空间", with_chat=False)
    _global_chat_model_without_window(client, "cwnowinroot")
    _seed_chunk(space_id)
    _patch_protocol(monkeypatch, _FakeProtocol())

    events = _ask(client, space_id, auth)
    assert [e["type"] for e in events][:4] == ["meta", "citations", "delta", "delta"]
    done = _done(events)
    assert done["context_used_tokens"] == 16
    assert done["context_window_tokens"] is None


def test_messages_endpoint_exposes_the_same_pair(client, monkeypatch) -> None:
    """④ 刷新复原:messages 的两值与 done 帧一致,user 行为 null。"""
    space_id, auth = _space(client, "cwmsgs", "窗口消息空间")
    _seed_chunk(space_id)
    _patch_protocol(monkeypatch, _FakeProtocol())

    events = _ask(client, space_id, auth)
    conversation_id = events[0]["conversation_id"]
    done = _done(events)
    messages = client.get(
        f"/api/v1/spaces/{space_id}/conversations/{conversation_id}/messages", headers=auth
    ).json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["context_used_tokens"] is None
    assert messages[0]["context_window_tokens"] is None
    assert messages[1]["id"] == done["message_id"]
    assert messages[1]["context_used_tokens"] == done["context_used_tokens"]
    assert messages[1]["context_window_tokens"] == done["context_window_tokens"]


def test_resumed_stream_replays_done_with_pair(client, monkeypatch) -> None:
    """⑤ 续播读的是同一本事件日志:done 帧两值不丢。"""
    space_id, auth = _space(client, "cwresume", "窗口续播空间")
    _seed_chunk(space_id)
    _patch_protocol(monkeypatch, _FakeProtocol())

    events = _ask(client, space_id, auth)
    conversation_id = events[0]["conversation_id"]
    done = _done(events)
    resp = client.get(
        f"/api/v1/spaces/{space_id}/conversations/{conversation_id}/stream?after=0",
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    replayed = [
        json.loads(line[len("data: ") :])
        for line in resp.text.splitlines()
        if line.startswith("data: ")
    ]
    assert _done(replayed)["context_used_tokens"] == done["context_used_tokens"]
    assert _done(replayed)["context_window_tokens"] == _WINDOW


def test_fallback_answer_has_no_pair(client, monkeypatch) -> None:
    """⑥ 无检索命中的兜底回答没打模型:两值显式 null(前端整行隐藏)。"""
    space_id, auth = _space(client, "cwfallback", "窗口兜底空间")
    _patch_protocol(monkeypatch, _FakeProtocol())

    done = _done(_ask(client, space_id, auth, question="与库里资料无关的问题"))
    assert done["context_used_tokens"] is None
    assert done["context_window_tokens"] is None
    assert UsageKind.CHAT not in [r.kind for r in _rows()]  # 查询向量化照样记账


# ---------------------------- 收集器口径 ----------------------------


def test_turn_window_takes_last_call_instead_of_summing(client, monkeypatch) -> None:
    """⑦ 两次调用只认最后一次:累加会把重发的上下文报到超出窗口。"""
    space_id, _ = _space(client, "cwmulti", "窗口多轮空间")
    _patch_protocol(monkeypatch, _FakeProtocol(chat_usage=_UPSTREAM))
    turn = ChatTurnUsage()
    chat = _metered_chat(space_id, turn)

    assert "".join(chat.chat_stream([{"role": "user", "content": "第一轮提问"}]))
    assert (
        turn.window() == ContextWindowUsage(used_tokens=16, window_tokens=_WINDOW)
    )

    # 第二轮换一个明显不同的数字:窗口快照必须跟着最后一次调用走
    protocol = _FakeProtocol(chat_usage={"prompt_tokens": 9, "completion_tokens": 2})
    _patch_protocol(monkeypatch, protocol)
    assert "".join(chat.chat_stream([{"role": "user", "content": "第二轮提问"}]))
    assert turn.window() == ContextWindowUsage(used_tokens=11, window_tokens=_WINDOW)
    assert len(turn.sinks) == 2
    assert len([r for r in _rows() if r.kind == UsageKind.CHAT]) == 2


def test_tool_round_without_text_or_usage_is_not_collected(client, monkeypatch) -> None:
    """⑧ 既没回 usage 又只有 tool_calls = 这次调用没发生:不落行也不进收集器。

    这是 OPT-28 已交付口径的边界。后果限定为:宽限截止在工具轮时,done 帧的分子
    取自更早一轮或为 null;而 message_id 为 null 的 partial 不落库,刷新后不复现。
    """
    space_id, _ = _space(client, "cwtool", "窗口工具轮空间")
    _patch_protocol(monkeypatch, _FakeProtocol(chat_usage=None))
    turn = ChatTurnUsage()
    chat = _metered_chat(space_id, turn)

    calls = list(chat.chat_stream_tools([{"role": "user", "content": "问题"}], []))
    assert isinstance(calls[0], ToolCallRequest)
    assert turn.sinks == []
    assert turn.window() is None
    assert _rows() == []
