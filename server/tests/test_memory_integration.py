"""记忆端点集成测试(OPT-45 P1,真 Postgres):迁移落库 + HTTP CRUD + 跨用户 404。

真 embedding 走桩(StubEmbedder 产确定性向量),只验证隔离谓词、去重与列存在性;
docker 不可用时由 conftest 的 client 夹具 skip。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, func, select

from app.api.deps import get_embedding_gateway
from app.core.db import get_session_factory
from app.domain.models import Memory, MemoryProposal
from tests.conftest import register as _register

pytestmark = pytest.mark.integration


class StubEmbedder:
    def embed(self, texts, model_id=None):
        out = []
        for t in texts:
            v = [0.0] * 1024
            v[abs(hash(t)) % 1000] = 1.0
            out.append(v)
        return out


@pytest.fixture(autouse=True)
def _reset(client):
    def _clean() -> None:
        with get_session_factory()() as db:
            db.execute(delete(MemoryProposal))
            db.execute(delete(Memory))
            db.commit()

    _clean()
    client.app.dependency_overrides[get_embedding_gateway] = StubEmbedder
    yield
    client.app.dependency_overrides.pop(get_embedding_gateway, None)
    _clean()


def _auth(session: dict) -> dict:
    return {"Authorization": f"Bearer {session['access_token']}"}


def _owner(client, username: str) -> tuple[str, dict]:
    session = _register(client, username)
    auth = _auth(session)
    space_id = client.post(
        "/api/v1/spaces", json={"name": f"sp-{username}"}, headers=auth
    ).json()["id"]
    return space_id, auth


def test_save_list_update_delete_flow(client) -> None:
    space_id, auth = _owner(client, "memowner")
    resp = client.post(
        f"/api/v1/spaces/{space_id}/memories",
        json={"content": "上线窗口定在周五晚", "kind": "decision", "topic": "发布计划"},
        headers=auth,
    )
    assert resp.status_code == 201, resp.text
    mid = resp.json()["id"]
    assert resp.json()["content"] == "上线窗口定在周五晚"

    # 列表可见
    listed = client.get(f"/api/v1/spaces/{space_id}/memories", headers=auth).json()
    assert len(listed) == 1

    # 编辑
    patched = client.patch(
        f"/api/v1/spaces/{space_id}/memories/{mid}",
        json={"content": "上线窗口改到周四晚"},
        headers=auth,
    )
    assert patched.status_code == 200
    assert patched.json()["content"] == "上线窗口改到周四晚"

    # 删除
    deleted = client.delete(
        f"/api/v1/spaces/{space_id}/memories/{mid}", headers=auth
    )
    assert deleted.status_code == 204
    assert client.get(f"/api/v1/spaces/{space_id}/memories", headers=auth).json() == []


def test_save_dedupes_identical_content(client) -> None:
    space_id, auth = _owner(client, "memdedupe")
    body = {"content": "同一条结论", "kind": "fact"}
    client.post(f"/api/v1/spaces/{space_id}/memories", json=body, headers=auth)
    client.post(f"/api/v1/spaces/{space_id}/memories", json=body, headers=auth)
    assert len(client.get(f"/api/v1/spaces/{space_id}/memories", headers=auth).json()) == 1


def test_cross_user_and_foreign_space_are_404(client) -> None:
    space_a, auth_a = _owner(client, "memb")
    space_b, auth_b = _owner(client, "memb2")
    mid = client.post(
        f"/api/v1/spaces/{space_a}/memories", json={"content": "私有"}, headers=auth_a
    ).json()["id"]

    # 陌生人:同空间非成员 → 空间层 404(SPACE_NOT_FOUND)
    stranger = _auth(_register(client, "memb3"))
    r = client.patch(
        f"/api/v1/spaces/{space_a}/memories/{mid}",
        json={"content": "篡改"},
        headers=stranger,
    )
    assert r.status_code == 404

    # 成员但拿别人的 space 路径查自己 → 列表空,且按 id 删是 404
    assert client.get(f"/api/v1/spaces/{space_b}/memories", headers=auth_b).json() == []
    r2 = client.delete(f"/api/v1/spaces/{space_b}/memories/{mid}", headers=auth_b)
    assert r2.status_code == 404
    # 原记忆仍在
    assert client.get(f"/api/v1/spaces/{space_a}/memories", headers=auth_a).json()[0]["id"] == mid


def test_dismiss_proposal_records_and_is_isolated(client) -> None:
    space_id, auth = _owner(client, "memdismiss")
    resp = client.post(
        f"/api/v1/spaces/{space_id}/memories/proposals/dismiss",
        json={"content": "  上线窗口定在周五晚  ", "kind": "decision"},
        headers=auth,
    )
    assert resp.status_code == 204, resp.text

    with get_session_factory()() as db:
        n = db.scalar(
            select(func.count()).select_from(MemoryProposal).where(
                MemoryProposal.space_id == space_id
            )
        )
    assert n == 1

    # 幂等:同一归一化内容再忽略不新增行
    client.post(
        f"/api/v1/spaces/{space_id}/memories/proposals/dismiss",
        json={"content": "上线窗口定在周五晚", "kind": "decision"},
        headers=auth,
    )
    with get_session_factory()() as db:
        n2 = db.scalar(
            select(func.count()).select_from(MemoryProposal).where(
                MemoryProposal.space_id == space_id
            )
        )
    assert n2 == 1

    # 陌生人非成员 → 空间层 404,且不留痕
    stranger = _auth(_register(client, "memdismiss2"))
    r = client.post(
        f"/api/v1/spaces/{space_id}/memories/proposals/dismiss",
        json={"content": "别人的空间"},
        headers=stranger,
    )
    assert r.status_code == 404


def test_anonymous_is_401(client) -> None:
    resp = client.get(f"/api/v1/spaces/{uuid.uuid4()}/memories")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTH_REQUIRED"
