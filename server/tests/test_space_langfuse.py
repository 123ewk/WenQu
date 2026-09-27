"""空间级 Langfuse 配置与 trace 查看(OPT-49 v2)HTTP 集成测试。

钉住的需求口径:
- **每空间一套配置**,该空间 Admin+(role≥30)或 superuser 可改;成员/非成员 403;
- 私钥三态语义 + 掩码 + enabled 前置;审计归因 space_id 且不含密钥本体;
- **trace 查看**:未接入 → configured=false;接入后成员强制只看自己的 trace,
  Admin+ 可看全空间/指定成员;详情里他人的归一为 404(防枚举);
- 按空间隔离:A 空间配置不影响 B 空间(nil-safe)。
"""

from __future__ import annotations

import uuid

import pytest

import app.application.service.langfuse_config as lf_module
from app.application.service.langfuse_config import (
    clear_langfuse_config_cache,
    langfuse_settings,
)
from app.core.crypto import CredentialCipher
from app.core.db import get_session_factory
from app.domain.models import AuditLog, SpaceLangfuseConfig, User
from tests.conftest import register as _register

pytestmark = pytest.mark.integration

CFG = "/api/v1/spaces/{sid}/langfuse-config"
TRACES = "/api/v1/spaces/{sid}/langfuse-traces"


@pytest.fixture(autouse=True)
def _cipher_and_isolated(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        lf_module,
        "get_credential_cipher",
        lambda: CredentialCipher("langfuse-config-test-key"),
    )
    clear_langfuse_config_cache()
    yield
    with get_session_factory()() as db:
        for row in db.query(SpaceLangfuseConfig).all():
            db.delete(row)
        db.commit()
    clear_langfuse_config_cache()


def _uname(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


def _auth(session: dict) -> dict:
    return {"Authorization": f"Bearer {session['access_token']}"}


def _space(client, prefix: str = "lf") -> tuple[dict, dict, str]:
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


def _superuser(client, prefix: str = "lfroot") -> dict:
    session = _register(client, _uname(prefix))
    with get_session_factory()() as db:
        user = db.get(User, uuid.UUID(session["user"]["id"]))
        assert user is not None
        user.is_superuser = True
        db.commit()
    return session


def _enable(client, headers, space_id: str, secret: str = "sk-lf-abcdef9012345678") -> None:
    resp = client.put(
        CFG.format(sid=space_id),
        json={"host": "https://langfuse.internal.example", "public_key": "pk-lf-1234567890",
              "secret_key": secret, "enabled": True},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text


# ---------------------------- 配置:守卫与语义 ----------------------------


def test_config_guard_space_scoped(client) -> None:
    owner_a, auth_a, sid_a = _space(client, "lfa")
    _owner_b, auth_b, sid_b = _space(client, "lfb")
    member_a = _add_member(client, auth_a, sid_a, "lfmember", 10)
    admin_a = _add_member(client, auth_a, sid_a, "lfadmin", 30)
    body = {"host": "https://langfuse.internal.example", "public_key": "pk-lf-1",
            "secret_key": "sk-lf-1", "enabled": True}
    # 本空间 Admin(30) 可保存
    assert client.put(CFG.format(sid=sid_a), json=body, headers=admin_a["auth"]).status_code == 200
    # 成员(10)/别的空间 Owner/匿名 都不行
    assert client.put(CFG.format(sid=sid_a), json=body, headers=member_a["auth"]).status_code == 403
    assert client.put(CFG.format(sid=sid_a), json=body, headers=auth_b).status_code == 403
    assert client.put(CFG.format(sid=sid_a), json=body).status_code == 401
    # GET 同口径:Admin 可读,成员 403
    assert client.get(CFG.format(sid=sid_a), headers=admin_a["auth"]).status_code == 200
    assert client.get(CFG.format(sid=sid_a), headers=member_a["auth"]).status_code == 403


def test_config_secret_semantics_and_space_isolation(client) -> None:
    owner_a, auth_a, sid_a = _space(client, "lfa")
    _o2, auth_b, sid_b = _space(client, "lfb")
    url = CFG.format(sid=sid_a)
    first = client.put(url, json={"host": "https://lf.example/", "public_key": "pk-lf-1234567890",
                                  "secret_key": "sk-lf-abcdef9012345678", "enabled": True},
                       headers=auth_a)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["host"] == "https://lf.example"  # 尾斜杠归一
    assert body["key_hint"] == "sk-l****5678"
    assert "sk-lf-abcdef" not in first.text and "enc:v1:" not in first.text
    # 不传 secret = 不改动
    same = client.put(url, json={"host": "https://lf.example", "public_key": "pk-lf-1234567890",
                                 "secret_key": None, "enabled": True}, headers=auth_a)
    assert same.json()["key_hint"] == "sk-l****5678"
    # 空间隔离:B 空间未配置
    assert client.get(CFG.format(sid=sid_b), headers=auth_b).json()["enabled"] is False
    with get_session_factory()() as db:
        assert langfuse_settings(db, uuid.UUID(sid_a)) is not None
        assert langfuse_settings(db, uuid.UUID(sid_b)) is None


def test_config_audit_attributed_to_space(client) -> None:
    owner, auth, space_id = _space(client, "lfaudit")
    body = {"host": "https://lf.example", "public_key": "pk",
            "secret_key": "sk-lf-secret", "enabled": True}
    assert client.put(CFG.format(sid=space_id), json=body, headers=auth).status_code == 200
    with get_session_factory()() as db:
        rows = (
            db.query(AuditLog)
            .filter(AuditLog.action == "langfuse_config.updated")
            .all()
        )
    mine = [r for r in rows if str(r.space_id) == space_id]
    assert len(mine) == 1
    assert "sk-lf-secret" not in str(mine[0].detail)  # 密钥本体不落审计


# ---------------------------- trace 查看(代理) ----------------------------


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _fake_langfuse(
    monkeypatch: pytest.MonkeyPatch, calls: list, trace_owner_id: str = "owner-id",
) -> None:
    def fake_get(url, params=None, auth=None, timeout=None):
        calls.append({"url": url, "params": dict(params or {}), "auth": auth})
        if url.endswith("/traces/trace-1"):  # 详情端点返回单条对象(真 API 口径)
            return _FakeResp({
                "id": "trace-1",
                "timestamp": "2026-09-27T12:00:00Z",
                "name": "qa",
                "input": {"question": "q"},
                "output": "a",
                "latency": 1.2,
                "metadata": {"user_id": trace_owner_id},
            })
        return _FakeResp({  # 列表端点返回 {data: [...]}
            "data": [{
                "id": "trace-1",
                "timestamp": "2026-09-27T12:00:00Z",
                "name": "qa",
                "input": {"question": "q"},
                "output": "a",
                "latency": 1.2,
                "metadata": {"user_id": trace_owner_id},
            }],
            "meta": {"page": 1},
        })

    monkeypatch.setattr(lf_module.httpx, "get", fake_get)


def test_traces_not_configured_returns_flag(client) -> None:
    owner, auth, space_id = _space(client, "lfoff")
    resp = client.get(TRACES.format(sid=space_id), headers=auth)
    assert resp.status_code == 200
    assert resp.json() == {"configured": False, "items": [], "meta": None}


def test_traces_member_forced_self_admin_sees_all(client, monkeypatch: pytest.MonkeyPatch) -> None:
    owner, auth, space_id = _space(client, "lfview")
    admin = _add_member(client, auth, space_id, "lfvadmin", 30)
    viewer = _add_member(client, auth, space_id, "lfviewer", 10)
    calls: list = []
    _fake_langfuse(monkeypatch, calls)
    _enable(client, auth, space_id)
    # 成员:强制只看自己(userId=自己的 id)
    resp = client.get(TRACES.format(sid=space_id), headers=viewer["auth"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True and len(body["items"]) == 1
    assert calls[-1]["params"]["userId"] == viewer["id"]
    # 即便成员显式传别人的 user_id 也被覆盖成自己
    other = uuid.uuid4()
    client.get(TRACES.format(sid=space_id) + f"?user_id={other}", headers=viewer["auth"])
    assert calls[-1]["params"]["userId"] == viewer["id"]
    # Admin+:不传 user_id = 看全空间(无 userId 过滤)
    client.get(TRACES.format(sid=space_id), headers=admin["auth"])
    assert "userId" not in calls[-1]["params"]
    # Admin+:可指定成员
    client.get(TRACES.format(sid=space_id) + f"?user_id={viewer['id']}", headers=admin["auth"])
    assert calls[-1]["params"]["userId"] == viewer["id"]


def test_trace_detail_member_cannot_see_others(client, monkeypatch: pytest.MonkeyPatch) -> None:
    owner, auth, space_id = _space(client, "lfdetail")
    viewer = _add_member(client, auth, space_id, "lfdv", 10)
    calls: list = []
    _fake_langfuse(monkeypatch, calls)
    _enable(client, auth, space_id)
    # 他人 trace(member_id != viewer) → 404 防枚举
    denied = client.get(TRACES.format(sid=space_id) + "/trace-1", headers=viewer["auth"])
    assert denied.status_code == 404
    # 自己的 trace → 200(把 fake 的 user_id 改成 viewer)
    monkeypatch.setattr(
        lf_module.httpx, "get",
        lambda url, params=None, auth=None, timeout=None: _FakeResp({
            "id": "trace-2", "timestamp": "2026-09-27T12:00:00Z", "name": "qa",
            "input": {"question": "q"}, "output": "a", "latency": 1.0,
            "metadata": {"user_id": viewer["id"]},
        }),
    )
    ok = client.get(TRACES.format(sid=space_id) + "/trace-2", headers=viewer["auth"])
    assert ok.status_code == 200 and ok.json()["id"] == "trace-2"
