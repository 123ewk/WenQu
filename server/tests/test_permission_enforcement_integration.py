"""实例级档位灰度开关(OPT-48)HTTP 集成测试:守卫、自锁 409、立即生效、缓存与审计。

覆盖需求单 §8 里可自动化的部分:
- **A3 回落优先级**:库里无行 = env 兜底(关=关、开=开);有行以库为准(行压过 env);
- **A7 权限**:superuser 或任意空间 Admin+(role≥30)可读可拨;Editor/普通成员/无成员身份
  一律 403,文案不含"管理模型"那种误导(B2);X-API-Key 打 /admin/* 沿用既有 fail-closed;
- **A5 自锁守卫**:开启者本人有空间缺生效档 → 409 并列出缺的空间;同一个人 GET 恒 200;
  关闭不做该校验(关闭锁不了任何人);
- **A6 影响面只统计不拦**:别人缺档 PUT 照常成功,数字与库里裸 SQL 对得上;
- **A2 立即生效**:PUT 后本进程下一次核心请求即变,不需要重启;
- **A4 缓存口径**:TTL 内不打库、显式失效立刻重新打库(钉住 §4.1-3);
- **A8 审计**:拨一次留一条,detail 含新旧值与旧值来源。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select, text

import app.application.service.permission_enforcement as pe_module
from app.application.service.permission_enforcement import clear_enforcement_cache
from app.core.db import get_session_factory
from app.domain.models import AuditLog, PermissionEnforcement, User
from tests.conftest import create_group, create_key
from tests.conftest import register as _register

pytestmark = pytest.mark.integration

ENFORCEMENT_URL = "/api/v1/admin/permission-enforcement"


# ---------------------------- 夹具与助手 ----------------------------


def _wipe_enforcement() -> None:
    """开关行 + 读值缓存一并清:session 级共用库,本文件不能把开关状态泄漏给别人。"""
    with get_session_factory()() as db:
        db.execute(delete(PermissionEnforcement))
        db.commit()
    clear_enforcement_cache()


@pytest.fixture(autouse=True)
def _enforcement_isolated(client):
    _wipe_enforcement()
    yield
    _wipe_enforcement()


def _uname(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


def _auth(session: dict) -> dict:
    return {"Authorization": f"Bearer {session['access_token']}"}


def _space(client, prefix: str) -> tuple[dict, dict, str]:
    """注册一个用户并建空间(创建者即 OWNER=40,天然满足"任意空间 Admin+"守卫)。"""
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


def _give_current_group(client, auth: dict, space_id: str, user_id: str) -> None:
    """给某人配"生效档"的完整三步:建档位 → 分配 → 置当前(D3 校验的就是这个)。"""
    group = create_group(client, auth, space_id, capabilities=["chat"], all_kbs=True)
    base = f"/api/v1/spaces/{space_id}/permission-groups/{group['id']}"
    assert (
        client.post(f"{base}/members", json={"user_id": user_id}, headers=auth).status_code == 200
    )
    assert client.put(f"{base}/current", json={"user_id": user_id}, headers=auth).status_code == 200


def _superuser(client, prefix: str) -> dict:
    """注册后直接改库置 superuser(与 test_admin_models_integration 同口径)。"""
    session = _register(client, _uname(prefix))
    with get_session_factory()() as db:
        user = db.get(User, uuid.UUID(session["user"]["id"]))
        assert user is not None
        user.is_superuser = True
        db.commit()
    return session


def _gate_env(monkeypatch, on: bool) -> None:
    """钉死 env 回落值:替换服务模块的 get_settings 接缝并清缓存。

    生产里 env 值被 @lru_cache 固化在进程内;测试用同一接缝模拟"库里无行 + env=on/off"。
    """
    from app.core.config import Settings

    monkeypatch.setattr(
        pe_module,
        "get_settings",
        lambda: Settings(require_permission_group_for_core=on),
    )
    clear_enforcement_cache()


# ---------------------------- A3 回落优先级 ----------------------------


def test_no_row_falls_back_to_env_off(client, monkeypatch) -> None:
    owner, auth, _space_id = _space(client, "peoff")
    _gate_env(monkeypatch, on=False)
    resp = client.get(ENFORCEMENT_URL, headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    assert body["effective"] is False
    assert body["source"] == "env_fallback"
    assert body["updated_at"] is None and body["updated_by_hint"] is None
    # 操作者自己的空间没挂档 → 自查为 False(A5 的 GET 半边)
    assert body["impact"]["actor_has_active_group"] is False


def test_no_row_env_on_means_open(client, monkeypatch) -> None:
    owner, auth, _space_id = _space(client, "peon")
    _gate_env(monkeypatch, on=True)
    resp = client.get(ENFORCEMENT_URL, headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    assert body["effective"] is True
    assert body["source"] == "env_fallback"


def test_row_beats_env(client, monkeypatch) -> None:
    owner, auth, space_id = _space(client, "pedb")
    _gate_env(monkeypatch, on=True)  # env 说是"开"……
    _give_current_group(client, auth, space_id, owner["user"]["id"])  # 过 D3 自锁校验
    resp = client.put(ENFORCEMENT_URL, json={"enabled": False}, headers=auth)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # ……但库里有了行,以库为准
    assert body["effective"] is False
    assert body["source"] == "db"
    assert body["updated_by_hint"] == owner["user"]["username"]


# ---------------------------- A7 权限与文案 ----------------------------


def test_plain_member_forbidden_message_not_misleading(client) -> None:
    plain = _register(client, _uname("peplain"))
    resp = client.get(ENFORCEMENT_URL, headers=_auth(plain))
    assert resp.status_code == 403
    err = resp.json()["error"]
    assert err["code"] == "FORBIDDEN"
    assert "管理员" in err["message"]
    assert "模型" not in err["message"]  # B2:不能把"拨开关被拒"说成"不能管理模型"
    assert client.put(
        ENFORCEMENT_URL, json={"enabled": True}, headers=_auth(plain)
    ).status_code == 403


def test_editor_denied_but_admin_of_any_space_allowed(client) -> None:
    owner, auth, space_id = _space(client, "peladder")
    editor = _add_member(client, auth, space_id, "peeditor", 20)
    admin = _add_member(client, auth, space_id, "peadmin30", 30)
    assert client.get(ENFORCEMENT_URL, headers=editor["auth"]).status_code == 403
    # D2 修订口径:任意空间 Admin+(这里 role=30)即可读;效果仍是全局
    assert client.get(ENFORCEMENT_URL, headers=admin["auth"]).status_code == 200


def test_superuser_without_memberships_can_toggle(client) -> None:
    root = _superuser(client, "peroot")
    resp = client.put(ENFORCEMENT_URL, json={"enabled": True}, headers=_auth(root))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["effective"] is True and body["source"] == "db"
    assert body["impact"]["actor_has_active_group"] is True  # 无成员身份 → 真空成立


def test_api_key_cannot_touch_enforcement(client) -> None:
    """A7 fail-closed:Key 链路根本不接 /admin/*,带 Key 不带 Bearer → 401 AUTH_REQUIRED。"""
    owner, auth, space_id = _space(client, "pekey")
    group = create_group(client, auth, space_id, capabilities=["chat"])
    key = create_key(client, auth, space_id, group["id"], owner_user_id=owner["user"]["id"])
    resp = client.get(ENFORCEMENT_URL, headers={"X-API-Key": key["plaintext"]})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTH_REQUIRED"


# ---------------------------- A5 自锁守卫 ----------------------------


def test_enable_blocked_when_actor_lacks_group_but_get_ok(client, monkeypatch) -> None:
    owner, auth, space_id = _space(client, "peselflock")
    _gate_env(monkeypatch, on=False)
    # GET 恒 200,自查里列出了自己缺档的空间
    got = client.get(ENFORCEMENT_URL, headers=auth)
    assert got.status_code == 200
    gaps = got.json()["impact"]["actor_spaces_without_group"]
    assert any(gap["space_id"] == space_id for gap in gaps)
    # PUT 开启 → 409,details 列出缺档空间
    resp = client.put(ENFORCEMENT_URL, json={"enabled": True}, headers=auth)
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "PERMISSION_ENFORCEMENT_ACTOR_HAS_NO_GROUP"
    assert any(gap["space_name"] for gap in err["details"]["spaces"])
    # 同一个人 GET 依然正常;关闭不做自锁校验(关闭锁不了任何人)
    assert client.get(ENFORCEMENT_URL, headers=auth).status_code == 200
    assert client.put(ENFORCEMENT_URL, json={"enabled": False}, headers=auth).status_code == 200


# ---------------------------- A6 影响面只统计不拦 ----------------------------


def test_others_without_group_counted_not_blocked(client, monkeypatch) -> None:
    owner, auth, space_id = _space(client, "peimpact")
    _gate_env(monkeypatch, on=False)
    _give_current_group(client, auth, space_id, owner["user"]["id"])
    _add_member(client, auth, space_id, "peplain1", 10)
    _add_member(client, auth, space_id, "peplain2", 20)
    # 独立口径:裸 SQL 直查共享库(session 级容器里有其它用例的存量,数字是全实例的)
    with get_session_factory()() as db:
        oracle = db.execute(
            text(
                "SELECT (SELECT count(*) FROM spaces) AS spaces,"
                " (SELECT count(*) FROM memberships) AS members_total,"
                " (SELECT count(*) FROM memberships m WHERE NOT EXISTS ("
                "    SELECT 1 FROM permission_group_members g WHERE g.space_id = m.space_id"
                "    AND g.user_id = m.user_id AND g.is_current)) AS members_without"
            )
        ).one()
    resp = client.put(ENFORCEMENT_URL, json={"enabled": True}, headers=auth)
    assert resp.status_code == 200, resp.text  # 别人缺档不拦
    impact = resp.json()["impact"]
    assert impact["spaces"] == oracle.spaces
    assert impact["members_total"] == oracle.members_total
    assert impact["members_without_active_group"] == oracle.members_without
    assert impact["actor_has_active_group"] is True


# ---------------------------- A2 立即生效 ----------------------------


def test_put_takes_effect_immediately_for_core_routes(client, monkeypatch) -> None:
    owner, auth, space_id = _space(client, "pea2")
    _gate_env(monkeypatch, on=False)
    _give_current_group(client, auth, space_id, owner["user"]["id"])
    viewer = _add_member(client, auth, space_id, "peviewer", 10)
    list_url = f"/api/v1/spaces/{space_id}/knowledge-bases"
    # 开关关:人只看角色,viewer 能列库
    assert client.get(list_url, headers=viewer["auth"]).status_code == 200
    # PUT 开 → 同一进程下一次请求立即 403(不重启)
    assert client.put(ENFORCEMENT_URL, json={"enabled": True}, headers=auth).status_code == 200
    denied = client.get(list_url, headers=viewer["auth"])
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "PERMISSION_GROUP_REQUIRED"
    # PUT 关 → 立即恢复
    assert client.put(ENFORCEMENT_URL, json={"enabled": False}, headers=auth).status_code == 200
    assert client.get(list_url, headers=viewer["auth"]).status_code == 200


# ---------------------------- A4 缓存口径 ----------------------------


def test_cache_within_ttl_no_db_hit_and_invalidation_hits(client, monkeypatch) -> None:
    calls = {"n": 0}
    real_repo = pe_module.PermissionEnforcementRepositoryImpl

    class CountingRepo(real_repo):
        def get_row(self):
            calls["n"] += 1
            return super().get_row()

    monkeypatch.setattr(pe_module, "PermissionEnforcementRepositoryImpl", CountingRepo)
    clear_enforcement_cache()
    with get_session_factory()() as db:
        assert pe_module.permission_enforcement_enabled(db) is False  # 首次:打库
        assert calls["n"] == 1
        assert pe_module.permission_enforcement_enabled(db) is False  # TTL 内:不再打库
        assert calls["n"] == 1
    clear_enforcement_cache()
    with get_session_factory()() as db:
        pe_module.permission_enforcement_enabled(db)  # 显式失效:立刻重新打库
        assert calls["n"] == 2


# ---------------------------- A8 审计留痕 ----------------------------


def test_toggle_writes_audit_with_old_and_new(client, monkeypatch) -> None:
    owner, auth, space_id = _space(client, "peaudit")
    _gate_env(monkeypatch, on=False)
    _give_current_group(client, auth, space_id, owner["user"]["id"])
    assert client.put(ENFORCEMENT_URL, json={"enabled": True}, headers=auth).status_code == 200
    assert client.put(ENFORCEMENT_URL, json={"enabled": False}, headers=auth).status_code == 200
    with get_session_factory()() as db:
        rows = (
            db.execute(
                select(AuditLog).where(AuditLog.action == "permission_enforcement.toggled")
            )
            .scalars()
            .all()
        )
    mine = [row for row in rows if str(row.actor_id) == owner["user"]["id"]]
    assert len(mine) == 2
    flips = {(row.detail["old"], row.detail["new"]) for row in mine}
    assert flips == {(False, True), (True, False)}
    # 第一次拨动的旧值来自 env 回落,第二次的旧值已来自库里的行
    by_new = {row.detail["new"]: row for row in mine}
    assert by_new[True].detail["old_source"] == "env_fallback"
    assert by_new[False].detail["old_source"] == "db"
    assert all(row.space_id is None for row in mine)  # 全局动作不属于任何空间
