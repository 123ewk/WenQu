"""手动压缩端点集成测试(OPT-45 P3a,真 Postgres):meta JSONB 摘要写回可持久化。

覆盖单元假仓储测不到的那一段:`Conversation.meta = 新 dict` + save 之后,重新开会话能读回
摘要与 summary_through_seq(JSONB 脏标记 + 提交)。chat 走桩产确定性摘要文本。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.api.deps import get_chat_gateway
from app.core.db import get_session_factory
from app.domain.models import Conversation, Message
from tests.conftest import register as _register

pytestmark = pytest.mark.integration


class StubChat:
    def __init__(self, reply: str = "这是合并后的前情摘要") -> None:
        self.reply = reply

    def chat_stream(self, messages, model_id=None):
        yield self.reply

    def chat_stream_tools(self, messages, tools, model_id=None):  # pragma: no cover
        yield self.reply
        return []


@pytest.fixture(autouse=True)
def _stub_chat(client):
    client.app.dependency_overrides[get_chat_gateway] = StubChat
    yield
    client.app.dependency_overrides.pop(get_chat_gateway, None)


def _auth(session: dict) -> dict:
    return {"Authorization": f"Bearer {session['access_token']}"}


def _seed_messages(conversation_id: str, space_id: str, count: int) -> None:
    with get_session_factory()() as db:
        for i in range(1, count + 1):
            db.add(
                Message(
                    conversation_id=uuid.UUID(conversation_id),
                    space_id=uuid.UUID(space_id),
                    role="user" if i % 2 else "assistant",
                    content=f"历史消息 {i}",
                    seq=i,
                )
            )
        db.commit()


def test_compress_persists_summary_in_meta(client) -> None:
    session = _register(client, "cmpowner")
    auth = _auth(session)
    space_id = client.post("/api/v1/spaces", json={"name": "cmp-sp"}, headers=auth).json()["id"]
    conversation_id = client.post(
        f"/api/v1/spaces/{space_id}/conversations",
        json={"title": "长会话"},
        headers=auth,
    ).json()["id"]

    _seed_messages(conversation_id, space_id, 20)

    resp = client.post(
        f"/api/v1/spaces/{space_id}/conversations/{conversation_id}/compress",
        json={},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["compressed"] is True
    assert body["summary"] == "这是合并后的前情摘要"
    # 20 条,keep_recent=6 → 折叠 seq 1..14
    assert body["summary_through_seq"] == 14

    # 重开会话读回 meta:证明 JSONB 写回真的落库
    with get_session_factory()() as db:
        conv = db.scalar(select(Conversation).where(Conversation.id == uuid.UUID(conversation_id)))
    assert conv is not None
    assert conv.meta["summary"] == "这是合并后的前情摘要"
    assert conv.meta["summary_through_seq"] == 14


def test_compress_too_short_is_not_error(client) -> None:
    session = _register(client, "cmpshort")
    auth = _auth(session)
    space_id = client.post("/api/v1/spaces", json={"name": "cmp2"}, headers=auth).json()["id"]
    conversation_id = client.post(
        f"/api/v1/spaces/{space_id}/conversations", json={"title": "短"}, headers=auth
    ).json()["id"]
    _seed_messages(conversation_id, space_id, 3)

    resp = client.post(
        f"/api/v1/spaces/{space_id}/conversations/{conversation_id}/compress",
        json={},
        headers=auth,
    )
    assert resp.status_code == 200
    assert resp.json()["compressed"] is False
    assert resp.json()["reason"] == "too_short"


def test_compress_foreign_conversation_is_404(client) -> None:
    owner = _register(client, "cmpo")
    owner_auth = _auth(owner)
    space_id = client.post(
        "/api/v1/spaces", json={"name": "cmpo-sp"}, headers=owner_auth
    ).json()["id"]
    conversation_id = client.post(
        f"/api/v1/spaces/{space_id}/conversations", json={"title": "t"}, headers=owner_auth
    ).json()["id"]

    intruder_session = _register(client, "cmpi")
    intruder_auth = _auth(intruder_session)
    intruder_space = client.post(
        "/api/v1/spaces", json={"name": "cmpi-sp"}, headers=intruder_auth
    ).json()["id"]

    resp = client.post(
        f"/api/v1/spaces/{intruder_space}/conversations/{conversation_id}/compress",
        json={},
        headers=intruder_auth,
    )
    assert resp.status_code == 404
