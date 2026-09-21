"""token 用量计量写入点集成测试(OPT-28c,真 Postgres)。

覆盖:① 真实网关(不覆盖依赖)走 /ask 时,一次问答落 embedding + chat 两行,
数字与厂商回传一致、口径标记为 upstream、归属到提问成员;
② 厂商不回传 usage 时按本地口径落 estimated 行;
③ 记账本身坏掉(独立会话连不上)不许打断问答主链路;
④ 无归属人(user_id 为空)的行照样落库 —— 汇总时只进空间合计。

上游协议一律打桩(get_protocol 换假适配器),不真连厂商;CI 无 .env,主密钥显式注入。
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import delete, select

from app.application.service.usage import UsageRecorder
from app.core.crypto import CredentialCipher
from app.core.db import get_session_factory
from app.domain.enums import DocumentStatus, UsageKind, UsageSource
from app.domain.models import (
    Chunk,
    CustomModel,
    Document,
    KnowledgeBase,
    ModelProvider,
    SpaceModelSettings,
    TokenUsageEvent,
)
from tests.conftest import register as _register

pytestmark = pytest.mark.integration

_CIPHER_KEY = "usage-metering-test-key"


@pytest.fixture(autouse=True)
def _cipher(monkeypatch: pytest.MonkeyPatch) -> None:
    """解密走真实入口时必须能解:CI 无 .env,真实主密钥会 fail-closed。"""

    def factory() -> CredentialCipher:
        return CredentialCipher(_CIPHER_KEY)

    monkeypatch.setattr(
        "app.application.service.model_admin.get_credential_cipher", factory
    )
    monkeypatch.setattr("app.core.model_client.get_credential_cipher", factory)


@pytest.fixture(autouse=True)
def _reset_tables(client):
    """session 级共用库:用量事件与空间模型前后都清一遍。"""

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
    """假适配器:按预设回传 usage(或不回传),并记录被调用的模型身份。"""

    def __init__(self, *, chat_usage: dict | None = None, embed_usage: dict | None = None) -> None:
        self.chat_usage = chat_usage
        self.embed_usage = embed_usage
        self.calls: list[tuple[str, str]] = []

    def embed(self, provider, model, api_key, texts, *, semaphore=None, transport=None, usage=None):
        self.calls.append((provider.key, model.id))
        if usage is not None and self.embed_usage is not None:
            usage.add_upstream(self.embed_usage)
        return [_query_vec() for _ in texts]

    def chat_stream(
        self, provider, model, api_key, messages, temperature, *, transport=None, usage=None
    ):
        self.calls.append((provider.key, model.id))
        if usage is not None and self.chat_usage is not None:
            usage.add_upstream(self.chat_usage)
        yield "根据资料 [1],"
        yield "混合检索采用 RRF 融合。"

    def chat_stream_tools(self, *a, **kw):  # pragma: no cover - 本文件不用 Agent 模式
        raise AssertionError("not used")


def _patch_protocol(monkeypatch: pytest.MonkeyPatch, protocol: _FakeProtocol) -> None:
    monkeypatch.setattr("app.core.model_client.get_protocol", lambda key: protocol)


def _query_vec() -> list[float]:
    v = [0.0] * 1024
    v[0] = 1.0
    return v


def _auth(session: dict) -> dict:
    return {"Authorization": f"Bearer {session['access_token']}"}


def _owner(client, username: str, space_name: str) -> tuple[str, dict, uuid.UUID]:
    """注册 + 建空间 + 配好空间层模型,返回 (space_id, Authorization, user_id)。"""
    session = _register(client, username)
    auth = _auth(session)
    resp = client.post("/api/v1/spaces", json={"name": space_name}, headers=auth)
    assert resp.status_code == 201, resp.text
    space_id = resp.json()["id"]
    base = f"/api/v1/spaces/{space_id}/model-config"

    # embedding:内置模型 + 空间自配 Key(prod 口径要求,dev 也不依赖全局 Key)
    saved = client.put(
        f"{base}/embedding-key", json={"api_key": "sk-a1b2c3d4e5f6g7h8"}, headers=auth
    )
    assert saved.status_code == 200, saved.text

    provider = client.post(
        f"{base}/providers",
        json={
            "provider_type": "deepseek",
            "name": f"计量厂{uuid.uuid4().hex[:6]}",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-1234567890abcdef",
        },
        headers=auth,
    )
    assert provider.status_code == 201, provider.text
    model = client.post(
        f"{base}/models",
        json={
            "provider_id": provider.json()["id"],
            "kind": "chat",
            "model_name": "deepseek-chat",
            "context_tokens": 64000,
            "is_default": True,
        },
        headers=auth,
    )
    assert model.status_code == 201, model.text
    return space_id, auth, uuid.UUID(session["user"]["id"])


def _seed_chunk(space_id: str) -> None:
    """一条已完成的文档 + 一个与查询向量同向的子块,保证检索命中 → 走生成阶段。"""
    import jieba
    from sqlalchemy import func

    with get_session_factory()() as db:
        kb = KnowledgeBase(space_id=uuid.UUID(space_id), name="计量库")
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


def _rows() -> list[TokenUsageEvent]:
    with get_session_factory()() as db:
        return list(db.scalars(select(TokenUsageEvent).order_by(TokenUsageEvent.kind)))


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


# ---------------------------- 写入点 ----------------------------


def test_ask_writes_upstream_rows_for_both_gateways(client, monkeypatch) -> None:
    """真实网关(不覆盖依赖)下的 /ask:检索一次向量化 + 生成一次对话 = 两行。"""
    space_id, auth, user_id = _owner(client, "meterqa", "计量问答空间")
    _seed_chunk(space_id)
    protocol = _FakeProtocol(
        chat_usage={"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
        embed_usage={"prompt_tokens": 7, "total_tokens": 7},
    )
    _patch_protocol(monkeypatch, protocol)

    events = _ask(client, space_id, auth)
    assert [e["type"] for e in events] == ["meta", "citations", "delta", "delta", "done"], events

    rows = _rows()
    assert [r.kind for r in rows] == [UsageKind.CHAT, UsageKind.EMBEDDING], [
        (r.kind, r.model_id) for r in rows
    ]
    chat, embedding = rows

    # 归属:谁提问记在谁头上;数字是厂商回传的原值,口径标 upstream
    assert chat.user_id == user_id and str(chat.space_id) == space_id
    assert (chat.prompt_tokens, chat.completion_tokens, chat.total_tokens) == (11, 5, 16)
    assert chat.source == UsageSource.UPSTREAM
    assert chat.model_id.endswith("deepseek-chat")
    assert chat.provider.startswith("计量厂")

    # 向量化按内置模型记账(数字只有 prompt 侧)
    assert embedding.user_id == user_id
    assert embedding.model_id == "builtin/text-embedding-v3"
    assert embedding.source == UsageSource.UPSTREAM
    assert (embedding.prompt_tokens, embedding.completion_tokens) == (7, 0)
    assert embedding.total_tokens == 7


def test_estimated_rows_when_upstream_silent(client, monkeypatch) -> None:
    """厂商不回传 usage:仍要落行,口径标 estimated(前端打 ≈)。"""
    space_id, auth, _ = _owner(client, "meterest", "估算空间")
    _seed_chunk(space_id)
    _patch_protocol(monkeypatch, _FakeProtocol())

    events = _ask(client, space_id, auth)
    assert events[-1]["type"] == "done"

    rows = {r.kind: r for r in _rows()}
    assert set(rows) == {UsageKind.CHAT, UsageKind.EMBEDDING}
    for row in rows.values():
        assert row.source == UsageSource.ESTIMATED
        assert row.total_tokens > 0
        assert row.total_tokens == row.prompt_tokens + row.completion_tokens


def test_broken_metering_does_not_break_answer(client, monkeypatch) -> None:
    """记账是旁路:独立会话连不上也只留 warning,问答照样正常收尾。"""

    def _broken_factory():
        raise RuntimeError("数据库挂了")

    monkeypatch.setattr(
        "app.api.deps.get_usage_recorder", lambda: UsageRecorder(_broken_factory)
    )
    space_id, auth, _ = _owner(client, "meterbroken", "记账故障空间")
    _seed_chunk(space_id)
    _patch_protocol(
        monkeypatch,
        _FakeProtocol(chat_usage={"total_tokens": 9}, embed_usage={"total_tokens": 9}),
    )

    events = _ask(client, space_id, auth)
    assert events[-1]["type"] == "done"
    assert events[-1]["cited_indexes"] == [1]
    assert _rows() == []


def test_row_without_user_still_counts_in_space(client, monkeypatch) -> None:
    """无归属人(如上传者已注销的入库链路):行照样写,汇总时只进空间合计。"""
    from app.core.model_catalog import ModelCatalog
    from app.core.model_client import EmbeddingClient

    space_id, auth, _ = _owner(client, "meternull", "空归属空间")
    _patch_protocol(monkeypatch, _FakeProtocol(embed_usage={"prompt_tokens": 4, "total_tokens": 4}))
    with get_session_factory()() as db:
        catalog = ModelCatalog.load_from_db(db, uuid.UUID(space_id))
    recorder = UsageRecorder(get_session_factory())
    gateway = recorder.embedding(EmbeddingClient(catalog), uuid.UUID(space_id), None)

    assert len(gateway.embed(["第一段", "第二段"])) == 2
    rows = _rows()
    assert len(rows) == 1
    assert rows[0].user_id is None and rows[0].kind == UsageKind.EMBEDDING
    assert rows[0].prompt_tokens == 4


def test_metered_chat_bills_one_row_per_upstream_call(client, monkeypatch) -> None:
    """Agent 模式一次提问打多次上游:每次一行,不做合并(V1 口径)。"""
    from app.core.model_catalog import ModelCatalog
    from app.core.model_client import ChatClient

    space_id, auth, user_id = _owner(client, "metermulti", "多轮计量空间")
    _patch_protocol(monkeypatch, _FakeProtocol(chat_usage={"prompt_tokens": 2, "total_tokens": 2}))
    with get_session_factory()() as db:
        catalog = ModelCatalog.load_from_db(db, uuid.UUID(space_id))
    chat = UsageRecorder(get_session_factory()).chat(
        ChatClient(catalog), uuid.UUID(space_id), user_id
    )

    assert "".join(chat.chat_stream([{"role": "user", "content": "问题"}]))
    assert "".join(chat.chat_stream([{"role": "user", "content": "再问一次"}]))
    rows = _rows()
    assert len(rows) == 2
    assert {r.user_id for r in rows} == {user_id}


def test_failed_upstream_call_writes_no_row(client, monkeypatch) -> None:
    """连不上/被上游拒绝 = 这次调用没发生,不许落一行预估(与 embedding 侧同口径)。"""
    from app.core.model_catalog import ModelCatalog
    from app.core.model_client import ChatClient

    class _Dead(_FakeProtocol):
        def chat_stream(self, *a, **kw):  # type: ignore[override]
            raise RuntimeError("connection refused")

    space_id, auth, user_id = _owner(client, "meterdead", "上游失败空间")
    _patch_protocol(monkeypatch, _Dead(chat_usage=None))
    with get_session_factory()() as db:
        catalog = ModelCatalog.load_from_db(db, uuid.UUID(space_id))
    chat = UsageRecorder(get_session_factory()).chat(
        ChatClient(catalog), uuid.UUID(space_id), user_id
    )

    with pytest.raises(RuntimeError):
        list(chat.chat_stream([{"role": "user", "content": "问题"}]))
    assert _rows() == []
