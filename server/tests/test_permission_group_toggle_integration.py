"""空间级权限挡位功能开关(OPT-48 v2)HTTP 集成测试:守卫/拦截范围/配挡时机/缓存/审计。

2026-09-26 用户重述需求后的四项拍板,本文件逐条钉住:
- **只挡核心功能清单**(问答/检索/会话流/知识库与文档,双凭据清单);
  成员/模型配置/审计/用量等管理页继续只看角色 —— 没挡位的 Admin 管理功能照常;
- **谁能拨 = 该空间 Admin+(role≥30)**,superuser 兜底;**别的空间 Admin 拨不动本空间**;
- **自锁不拦**:操作者没挡也能开(开完自己的核心功能被挡,两块管理功能仍可用 = 可自救);
- **配挡时机 = 字面口径**:开关没开的空间,分配/换当前 → 409 PERMISSION_GROUP_TOGGLE_OFF;
  建档/改档不受限;解除分配不拦(逃生口)。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

import app.application.service.permission_group_toggle as toggle_module
from app.application.service.permission_group_toggle import clear_space_toggle_cache
from app.core.db import get_session_factory
from app.domain.models import AuditLog, User
from tests.conftest import create_group, enable_space_toggle, wipe_space_toggles
from tests.conftest import register as _register

pytestmark = pytest.mark.integration


# ---------------------------- 夹具与助手 ----------------------------


@pytest.fixture(autouse=True)
def _isolated_space_toggles(client):
    wipe_space_toggles()
    yield
    wipe_space_toggles()


def _uname(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


def _auth(session: dict) -> dict:
    return {"Authorization": f"Bearer {session['access_token']}"}


def _space(client, prefix: str = "tgl") -> tuple[dict, dict, str]:
    """注册一个用户并建空间(创建者即 OWNER=40);开关保持**关**,由用例自控。"""
    owner = _register(client, _uname(prefix))
    auth = _auth(owner)
    space_id = client.post(
        "/api/v1/spaces", json={"name": f"{prefix}空间{uuid.uuid4().hex[:4]}"}, headers=auth
    ).json()["id"]
    return owner, auth, space_id


def _add_member(client, auth: dict, space_id: str, prefix: str, role: int) -> dict:
    name = _uname(prefix)
    user = _register(client, name)
    resp = client.post(
        f"/api/v1/spaces/{space_id}/members",
        json={"username": name, "role": role},
        headers=auth,
    )
    assert resp.status_code == 201, resp.text
    return {"auth": _auth(user), **user["user"]}


def _superuser(client, prefix: str = "tglroot") -> dict:
    session = _register(client, _uname(prefix))
    with get_session_factory()() as db:
        user = db.get(User, uuid.UUID(session["user"]["id"]))
        assert user is not None
        user.is_superuser = True
        db.commit()
    return session


def _toggle_url(space_id: str) -> str:
    return f"/api/v1/spaces/{space_id}/permission-group-toggle"


def _kb_list(space_id: str) -> str:
    return f"/api/v1/spaces/{space_id}/knowledge-bases"


def _pg(space_id: str) -> str:
    return f"/api/v1/spaces/{space_id}/permission-groups"


def _error(resp) -> dict:
    return resp.json()["error"]


# ---------------------------- 开关关 = 与今天一致 ----------------------------


def test_no_row_means_off_and_status_shape(client) -> None:
    owner, auth, space_id = _space(client)
    viewer = _add_member(client, auth, space_id, "tglviewer", 10)
    # 未开:核心路由只看角色,viewer 照常
    assert client.get(_kb_list(space_id), headers=viewer["auth"]).status_code == 200
    resp = client.get(_toggle_url(space_id), headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["updated_at"] is None and body["updated_by_hint"] is None
    # 影响面只算本空间:owner+viewer 共 2 条成员关系,都没挂挡
    assert body["impact"]["members_total"] == 2
    assert body["impact"]["members_without_active_group"] == 2
    assert body["impact"]["actor_has_active_group"] is False


# ---------------------------- 开=核心功能要挡位,管理页不受影响 ----------------------------


def test_on_requires_group_for_core_routes(client) -> None:
    owner, auth, space_id = _space(client)
    viewer = _add_member(client, auth, space_id, "tglv2", 10)
    enable_space_toggle(space_id)
    # 成员没挡:知识库列表/问答 → 403 PERMISSION_GROUP_REQUIRED(401 会被前端踢成重登录)
    denied = client.get(_kb_list(space_id), headers=viewer["auth"])
    assert denied.status_code == 403
    assert _error(denied)["code"] == "PERMISSION_GROUP_REQUIRED"
    ask = client.post(
        f"/api/v1/spaces/{space_id}/ask", json={"question": "q"}, headers=viewer["auth"]
    )
    assert ask.status_code == 403
    # 没挡的 Owner 也一样被挡:挡位面前人人平等
    assert client.get(_kb_list(space_id), headers=auth).status_code == 403


def test_on_keeps_management_routes_role_only(client) -> None:
    owner, auth, space_id = _space(client)
    admin = _add_member(client, auth, space_id, "tgladmin30", 30)
    enable_space_toggle(space_id)
    # 管理页继续只看角色(核心清单口径):没挡位的 Admin 全部照常
    for path in (
        f"/api/v1/spaces/{space_id}/members",
        f"/api/v1/spaces/{space_id}/audit-logs",
        f"/api/v1/spaces/{space_id}/api-keys",
        _pg(space_id),
    ):
        assert client.get(path, headers=admin["auth"]).status_code == 200, path
    # 核心功能仍要挡位
    assert client.get(_kb_list(space_id), headers=admin["auth"]).status_code == 403


# ---------------------------- 谁能拨:本空间 Admin+,别的空间不行 ----------------------------


def test_toggle_guard_is_space_scoped(client) -> None:
    owner_a, auth_a, sid_a = _space(client, "tgla")
    _owner_b, auth_b, sid_b = _space(client, "tglb")
    admin_a = _add_member(client, auth_a, sid_a, "tgladm", 30)
    editor_a = _add_member(client, auth_a, sid_a, "tgledit", 20)
    # 本空间 Admin(30) 可拨
    assert (
        client.put(_toggle_url(sid_a), json={"enabled": True}, headers=admin_a["auth"]).status_code
        == 200
    )
    # Editor(20) 不行
    denied = client.put(_toggle_url(sid_a), json={"enabled": True}, headers=editor_a["auth"])
    assert denied.status_code == 403
    assert "空间管理员" in _error(denied)["message"]
    assert "模型" not in _error(denied)["message"]  # 文案不误导(B2 原则延续)
    # **别的空间**的 Owner 拨不动本空间(2026-09-26 拍板:管谁的空间拨谁的开关)
    assert (
        client.put(_toggle_url(sid_a), json={"enabled": True}, headers=auth_b).status_code == 403
    )
    # 非 superuser 的 Key 链路不接这组路由:带 Key 不带 Bearer → 401
    assert client.put(_toggle_url(sid_a), json={"enabled": True}).status_code == 401
    # superuser 兜底:拨任意空间
    root = _superuser(client)
    assert (
        client.put(_toggle_url(sid_b), json={"enabled": True}, headers=_auth(root)).status_code
        == 200
    )


# ---------------------------- 自锁不拦 ----------------------------


def test_no_self_lock_interception_but_actor_hinted(client) -> None:
    owner, auth, space_id = _space(client)
    # 操作者没给自己配挡:打开**成功**(不做 409 拦截)
    resp = client.put(_toggle_url(space_id), json={"enabled": True}, headers=auth)
    assert resp.status_code == 200, resp.text
    assert resp.json()["impact"]["actor_has_active_group"] is False
    # 开完自己的核心功能被挡 —— 但两块管理功能仍可用(自救通道)
    assert client.get(_kb_list(space_id), headers=auth).status_code == 403
    assert client.get(_pg(space_id), headers=auth).status_code == 200
    assert client.get(f"/api/v1/spaces/{space_id}/api-keys", headers=auth).status_code == 200


# ---------------------------- 配挡时机:打开后才能分配/换当前 ----------------------------


def test_assignment_requires_toggle_on(client) -> None:
    owner, auth, space_id = _space(client)
    viewer = _add_member(client, auth, space_id, "tglassign", 10)
    group = create_group(client, auth, space_id, capabilities=["chat"], all_kbs=True)
    members_url = f"{_pg(space_id)}/{group['id']}/members"
    current_url = f"{_pg(space_id)}/{group['id']}/current"
    # 开关没开:分配/换当前 → 409 PERMISSION_GROUP_TOGGLE_OFF(建档/改档不受限)
    assign = client.post(members_url, json={"user_id": viewer["id"]}, headers=auth)
    assert assign.status_code == 409
    assert _error(assign)["code"] == "PERMISSION_GROUP_TOGGLE_OFF"
    cur = client.put(current_url, json={"user_id": viewer["id"]}, headers=auth)
    assert cur.status_code == 409
    assert _error(cur)["code"] == "PERMISSION_GROUP_TOGGLE_OFF"
    # 打开后:分配 → 换当前 走通
    enable_space_toggle(space_id)
    assert client.post(members_url, json={"user_id": viewer["id"]}, headers=auth).status_code == 200
    assert client.put(current_url, json={"user_id": viewer["id"]}, headers=auth).status_code == 200
    # 再关掉:解除分配(逃生口)不拦;重新分配重新 409
    off = client.put(_toggle_url(space_id), json={"enabled": False}, headers=auth)
    assert off.status_code == 200
    assert (
        client.delete(f"{members_url}/{viewer['id']}", headers=auth).status_code == 204
    )
    assert client.post(members_url, json={"user_id": viewer["id"]}, headers=auth).status_code == 409


# ---------------------------- 拨动立即生效(本进程) ----------------------------


def test_put_takes_effect_immediately(client) -> None:
    owner, auth, space_id = _space(client)
    viewer = _add_member(client, auth, space_id, "tglimm", 10)
    assert client.get(_kb_list(space_id), headers=viewer["auth"]).status_code == 200
    on = client.put(_toggle_url(space_id), json={"enabled": True}, headers=auth)
    assert on.status_code == 200
    assert client.get(_kb_list(space_id), headers=viewer["auth"]).status_code == 403
    off = client.put(_toggle_url(space_id), json={"enabled": False}, headers=auth)
    assert off.status_code == 200
    assert client.get(_kb_list(space_id), headers=viewer["auth"]).status_code == 200


# ---------------------------- 审计留痕 ----------------------------


def test_toggle_writes_audit_with_old_and_new(client) -> None:
    owner, auth, space_id = _space(client)
    on = client.put(_toggle_url(space_id), json={"enabled": True}, headers=auth)
    assert on.status_code == 200
    assert (
        client.put(_toggle_url(space_id), json={"enabled": False}, headers=auth).status_code == 200
    )
    with get_session_factory()() as db:
        rows = (
            db.execute(
                select(AuditLog).where(AuditLog.action == "permission_group_toggle.set")
            )
            .scalars()
            .all()
        )
    mine = [row for row in rows if str(row.space_id) == space_id]
    assert len(mine) == 2
    flips = {(row.detail["old"], row.detail["new"]) for row in mine}
    assert flips == {(False, True), (True, False)}
    assert all(str(row.actor_id) == owner["user"]["id"] for row in mine)


# ---------------------------- 缓存口径:按空间分键 ----------------------------


def test_cache_is_per_space_and_invalidation_precise(client, monkeypatch) -> None:
    calls = {"n": 0}
    real_repo = toggle_module.SpacePermissionGroupToggleRepositoryImpl

    class CountingRepo(real_repo):
        def get_row(self, space_id):
            calls["n"] += 1
            return super().get_row(space_id)

    monkeypatch.setattr(toggle_module, "SpacePermissionGroupToggleRepositoryImpl", CountingRepo)
    clear_space_toggle_cache()
    _owner, auth, sid_a = _space(client, "tglca")
    _owner2, _auth2, sid_b = _space(client, "tglcb")
    with get_session_factory()() as db:
        assert toggle_module.space_toggle_enabled(db, uuid.UUID(sid_a)) is False  # A 未命中
        assert calls["n"] == 1
        assert toggle_module.space_toggle_enabled(db, uuid.UUID(sid_a)) is False  # TTL 内不打库
        assert calls["n"] == 1
        assert toggle_module.space_toggle_enabled(db, uuid.UUID(sid_b)) is False  # B 独立未命中
        assert calls["n"] == 2
    # 只失效 A:B 的缓存不受影响
    clear_space_toggle_cache(uuid.UUID(sid_a))
    with get_session_factory()() as db:
        toggle_module.space_toggle_enabled(db, uuid.UUID(sid_a))
        assert calls["n"] == 3
        toggle_module.space_toggle_enabled(db, uuid.UUID(sid_b))
        assert calls["n"] == 3
    # 写侧(经 API 拨动)精确失效本空间:同进程下一次读取立刻拿到新值
    assert client.put(_toggle_url(sid_a), json={"enabled": True}, headers=auth).status_code == 200
    with get_session_factory()() as db:
        assert toggle_module.space_toggle_enabled(db, uuid.UUID(sid_a)) is True
        assert calls["n"] == 4
