"""模型管理端点集成测试(OPT-25,真 Postgres)。

覆盖:superuser 门禁(JWT-only)、供应商/模型 CRUD 的写入口校验、
密钥"写入即加密 + 只回显掩码"、is_default 按 kind 互斥、
删除级联后公开清单回落空目录。每个用例结束清表:
session 级共用库,不能把"已配置模型"泄漏给其他文件的 503 用例。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete

from app.core.crypto import CredentialCipher
from app.core.db import get_session_factory
from app.domain.models import CustomModel, ModelProvider, User
from tests.conftest import register as _register

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _cipher_with_test_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """显式注入加密主密钥:CI 无 .env 时真实入口 fail-closed 会 503(CI 曾误红)。

    本文件只验证"写入即加密、只回显掩码"的形态,不跨进程解密,固定测试密钥即可。
    """
    monkeypatch.setattr(
        "app.application.service.model_admin.get_credential_cipher",
        lambda: CredentialCipher("admin-models-test-key"),
    )


@pytest.fixture(autouse=True)
def _models_table_empty(client):
    yield
    with get_session_factory()() as db:
        db.execute(delete(CustomModel))
        db.execute(delete(ModelProvider))
        db.commit()


def _superuser(client, username: str) -> dict:
    """注册后直接改库置 superuser(访问令牌按用户 id 现取现查,无需重登录)。"""
    session = _register(client, username)
    with get_session_factory()() as db:
        user = db.get(User, uuid.UUID(session["user"]["id"]))
        assert user is not None
        user.is_superuser = True
        db.commit()
    return session


def _auth(session: dict) -> dict:
    return {"Authorization": f"Bearer {session['access_token']}"}


def _provider_payload(**overrides) -> dict:
    params = {
        "name": f"演示厂{uuid.uuid4().hex[:6]}",
        "base_url": "https://api.example.com/v1/",
        "protocol": "openai_compatible",
        "api_key": "sk-1234567890abcdef",
    }
    params.update(overrides)
    return params


def _model_payload(provider_id: str, **overrides) -> dict:
    params = {
        "model_id": f"演示厂/chat-v{uuid.uuid4().hex[:6]}",
        "provider_id": provider_id,
        "kind": "chat",
        "model_name": "chat-v1",
    }
    params.update(overrides)
    return params


# ---------------------------- 门禁 ----------------------------


def test_admin_routes_jwt_only_and_superuser_gated(client) -> None:
    anon = client.get("/api/v1/admin/model-providers")
    assert anon.status_code == 401  # 无 JWT(Bearer)→ 401,API Key 链路根本不接入

    plain = _register(client, "adminplain")
    resp = client.get("/api/v1/admin/model-providers", headers=_auth(plain))
    assert resp.status_code == 403  # 登录了但不是 superuser
    resp = client.post(
        "/api/v1/admin/model-providers", json=_provider_payload(), headers=_auth(plain)
    )
    assert resp.status_code == 403

    root = _superuser(client, "adminroot")
    assert client.get("/api/v1/admin/model-protocols", headers=_auth(root)).status_code == 200


def test_api_key_cannot_touch_admin_routes(client) -> None:
    """Key 访问管理前缀:路由只认 JWT → 401(带 Key 不带 Bearer 即 AUTH_REQUIRED)。"""
    owner = _register(client, "adminkeyowner")
    auth = _auth(owner)
    space_id = client.post("/api/v1/spaces", json={"name": "钥匙空间"}, headers=auth).json()[
        "id"
    ]
    key = client.post(
        f"/api/v1/spaces/{space_id}/api-keys",
        json={
            "name": "k",
            "capabilities": ["chat"],
            "member_user_id": owner["user"]["id"],
        },
        headers=auth,
    ).json()
    resp = client.get(
        "/api/v1/admin/model-providers", headers={"X-API-Key": key["plaintext"]}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTH_REQUIRED"


# ---------------------------- 供应商 CRUD ----------------------------


def test_provider_crud_and_key_never_echoed(client) -> None:
    root = _superuser(client, "adminprov")
    auth = _auth(root)

    created = client.post("/api/v1/admin/model-providers", json=_provider_payload(), headers=auth)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["key_hint"] == "sk-1****cdef"  # 4+****+4 掩码
    assert "sk-1234567890abcdef" not in created.text  # 明文不回显
    assert body["model_count"] == 0

    listed = client.get("/api/v1/admin/model-providers", headers=auth).json()
    assert [p["id"] for p in listed] == [body["id"]]
    assert "api_key_encrypted" not in str(listed)

    # 同名 409
    dup = client.post(
        "/api/v1/admin/model-providers",
        json=_provider_payload(name=body["name"]),
        headers=auth,
    )
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "MODEL_PROVIDER_NAME_TAKEN"

    # base_url 协议校验 + 未知协议 422(fail-closed:入库键必须可分发)
    bad_url = client.post(
        "/api/v1/admin/model-providers",
        json=_provider_payload(base_url="ftp://nope.example/x"),
        headers=auth,
    )
    assert bad_url.status_code == 422
    bad_proto = client.post(
        "/api/v1/admin/model-providers",
        json=_provider_payload(protocol="magic_proto"),
        headers=auth,
    )
    assert bad_proto.status_code == 422
    assert "openai_compatible" in bad_proto.text  # 报错给出已注册清单

    # PATCH 语义:不传 = 不动;传值 = 更换;传空串 = 清除
    pid = body["id"]
    patched = client.patch(
        f"/api/v1/admin/model-providers/{pid}", json={"enabled": False}, headers=auth
    ).json()
    assert patched["enabled"] is False and patched["key_hint"] == "sk-1****cdef"
    patched = client.patch(
        f"/api/v1/admin/model-providers/{pid}", json={"api_key": "sk-newsecret99"}, headers=auth
    ).json()
    assert patched["key_hint"] == "sk-n****et99" and "sk-newsecret99" not in str(patched)
    patched = client.patch(
        f"/api/v1/admin/model-providers/{pid}", json={"api_key": ""}, headers=auth
    ).json()
    assert patched["key_hint"] == ""  # 转本地免密钥端点

    assert client.delete(f"/api/v1/admin/model-providers/{pid}", headers=auth).status_code == 204
    assert client.get("/api/v1/admin/model-providers", headers=auth).json() == []
    assert client.delete(f"/api/v1/admin/model-providers/{pid}", headers=auth).status_code == 404


# ---------------------------- 模型 CRUD 与写入口校验 ----------------------------


def test_model_validation_rules(client) -> None:
    root = _superuser(client, "adminmodel")
    auth = _auth(root)
    created_p = client.post(
        "/api/v1/admin/model-providers", json=_provider_payload(), headers=auth
    )
    pid = created_p.json()["id"]

    # embedding 必须显式 dims 且只能 1024(Vector(1024) 定宽列)
    no_dims = client.post(
        "/api/v1/admin/models", json=_model_payload(pid, kind="embedding"), headers=auth
    )
    assert no_dims.status_code == 422 and "dims" in no_dims.text
    wrong_dims = client.post(
        "/api/v1/admin/models",
        json=_model_payload(pid, kind="embedding", dims=768),
        headers=auth,
    )
    assert wrong_dims.status_code == 422
    ok = client.post(
        "/api/v1/admin/models",
        json=_model_payload(pid, kind="embedding", dims=1024),
        headers=auth,
    )
    assert ok.status_code == 201, ok.text

    # 未知 kind(绕过 pydantic Literal 的路径没有,422 即可);模型 id 撞库 409
    dup = client.post(
        "/api/v1/admin/models",
        json=_model_payload(pid, kind="embedding", dims=1024, model_id=ok.json()["model_id"]),
        headers=auth,
    )
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "MODEL_ID_TAKEN"

    # 不存在的供应商 → 404
    ghost = client.post(
        "/api/v1/admin/models", json=_model_payload(str(uuid.uuid4())), headers=auth
    )
    assert ghost.status_code == 404


def test_default_exclusivity_and_public_catalog(client) -> None:
    root = _superuser(client, "admindef")
    auth = _auth(root)
    user_auth = _auth(_register(client, "admindefreader"))  # 普通用户读公开清单

    created_p = client.post(
        "/api/v1/admin/model-providers", json=_provider_payload(), headers=auth
    )
    pid = created_p.json()["id"]
    first = client.post(
        "/api/v1/admin/models", json=_model_payload(pid, is_default=True), headers=auth
    ).json()
    assert first["is_default"] is True
    public = client.get("/api/v1/models", headers=user_auth).json()
    assert {m["id"] for m in public["chat"]} == {first["model_id"]}
    assert public["defaults"]["chat"] == first["model_id"]

    # 上新默认 → 旧默认自动落牌(同 kind 互斥)
    second = client.post(
        "/api/v1/admin/models", json=_model_payload(pid, is_default=True), headers=auth
    ).json()
    public = client.get("/api/v1/models", headers=user_auth).json()
    assert public["defaults"]["chat"] == second["model_id"]
    listed = client.get("/api/v1/admin/models", headers=auth).json()
    assert sum(1 for m in listed if m["is_default"] and m["kind"] == "chat") == 1

    # 停用模型不能设默认(两种姿势都不行)
    off_id = second["id"]
    off = client.patch(
        f"/api/v1/admin/models/{off_id}", json={"enabled": False}, headers=auth
    )
    assert off.status_code == 200
    bump = client.patch(f"/api/v1/admin/models/{off_id}", json={"is_default": True}, headers=auth)
    assert bump.status_code == 422
    new_off = client.post(
        "/api/v1/admin/models",
        json=_model_payload(pid, enabled=False, is_default=True),
        headers=auth,
    )
    assert new_off.status_code == 422

    # 供应商停用 → 其模型从公开清单消失(管理清单仍在,两次 422 都没插入新行)
    client.patch(f"/api/v1/admin/model-providers/{pid}", json={"enabled": False}, headers=auth)
    public = client.get("/api/v1/models", headers=user_auth).json()
    assert public["chat"] == [] and public["defaults"] == {}
    assert len(client.get("/api/v1/admin/models", headers=auth).json()) == len(listed)

    # 删供应商 → 级联删模型,管理清单归零
    assert client.delete(f"/api/v1/admin/model-providers/{pid}", headers=auth).status_code == 204
    assert client.get("/api/v1/admin/models", headers=auth).json() == []


def test_model_admin_actions_are_audited(client) -> None:
    from sqlalchemy import select

    from app.domain.models import AuditLog

    root = _superuser(client, "adminaudit")
    auth = _auth(root)
    resp = client.post("/api/v1/admin/model-providers", json=_provider_payload(), headers=auth)
    pid = resp.json()["id"]
    client.post("/api/v1/admin/models", json=_model_payload(pid), headers=auth)
    with get_session_factory()() as db:
        actions = set(
            db.scalars(
                select(AuditLog.action).where(AuditLog.actor_id == uuid.UUID(root["user"]["id"]))
            )
        )
    assert {"model_provider.created", "model.created"} <= actions
