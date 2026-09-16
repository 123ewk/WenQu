"""PG 集成测试(testcontainers,基准 03):真实 Postgres + 真迁移 + 全链路 API。

外部依赖(Docker)不可用时 skip 而非失败;注册/登录/空间/成员/RBAC/审计一条龙。
"""

from __future__ import annotations

import os
import shutil

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client():
    if shutil.which("docker") is None:
        pytest.skip("docker 不可用,跳过 PG 集成测试")
    try:
        try:  # testcontainers 4.9+ 将社区模块迁移到 community 命名空间
            from testcontainers.community.postgres import PostgresContainer
        except ImportError:
            from testcontainers.postgres import PostgresContainer

        container = PostgresContainer("pgvector/pgvector:pg16", driver="psycopg")
        container.start()
    except Exception as exc:  # noqa: BLE001 — 环境不可用即跳过
        pytest.skip(f"无法启动 postgres 测试容器: {exc}")

    try:
        os.environ["APP_DATABASE_URL"] = container.get_connection_url()
        from alembic import command
        from alembic.config import Config

        from app.core.config import get_settings
        from app.core.db import get_engine, get_session_factory

        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()

        alembic_cfg = Config("alembic.ini")
        command.upgrade(alembic_cfg, "head")

        from fastapi.testclient import TestClient

        from app.main import create_app

        with TestClient(create_app(), raise_server_exceptions=False) as test_client:
            yield test_client
    finally:
        from app.core.db import get_engine, get_session_factory

        get_engine.cache_clear()
        get_session_factory.cache_clear()
        container.stop()


def _register(client, username: str) -> dict:
    resp = client.post(
        "/api/v1/auth/register",
        json={"username": username, "nickname": username, "password": "secret-pass-1"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_full_auth_and_space_flow(client) -> None:
    # ---- 认证门禁 ----
    resp = client.get("/api/v1/users/me")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTH_REQUIRED"

    alice = _register(client, "alice")
    alice_auth = {"Authorization": f"Bearer {alice['access_token']}"}

    resp = client.get("/api/v1/users/me", headers=alice_auth)
    assert resp.status_code == 200 and resp.json()["username"] == "alice"

    # 参数校验走统一错误壳
    resp = client.post(
        "/api/v1/auth/register",
        json={"username": "x", "nickname": "", "password": "short"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    # 重复注册
    resp = client.post(
        "/api/v1/auth/register",
        json={"username": "alice", "nickname": "重复", "password": "secret-pass-1"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "USERNAME_TAKEN"

    # ---- 空间与成员 ----
    resp = client.post(
        "/api/v1/spaces",
        json={"name": "研发空间", "description": "M1 集成测试"},
        headers=alice_auth,
    )
    assert resp.status_code == 201, resp.text
    space_id = resp.json()["id"]

    bob = _register(client, "bob")
    bob_auth = {"Authorization": f"Bearer {bob['access_token']}"}

    # 非成员 404(与不存在同语义)
    resp = client.get(f"/api/v1/spaces/{space_id}", headers=bob_auth)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "SPACE_NOT_FOUND"

    # Owner 拉 Editor
    resp = client.post(
        f"/api/v1/spaces/{space_id}/members",
        json={"username": "bob", "role": 20},
        headers=alice_auth,
    )
    assert resp.status_code == 201, resp.text

    resp = client.get(f"/api/v1/spaces/{space_id}", headers=bob_auth)
    assert resp.status_code == 200 and resp.json()["role"] == 20

    # Editor 访问审计 → 403
    resp = client.get(f"/api/v1/spaces/{space_id}/audit-logs", headers=bob_auth)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"

    # bob 自我提权 → 403(先取成员列表拿 bob 的 user_id)
    resp = client.get(f"/api/v1/spaces/{space_id}/members", headers=alice_auth)
    member = next(m for m in resp.json() if m["username"] == "bob")
    bob_id = member["user_id"]
    resp = client.patch(
        f"/api/v1/spaces/{space_id}/members/{bob_id}", json={"role": 30}, headers=bob_auth
    )
    assert resp.status_code == 403

    # Owner 查审计:建空间/拉成员/拒绝事件都在
    resp = client.get(f"/api/v1/spaces/{space_id}/audit-logs", headers=alice_auth)
    assert resp.status_code == 200
    actions = [item["action"] for item in resp.json()["items"]]
    assert "space.created" in actions and "space.member_added" in actions

    # ---- 切空间轮换 ----
    resp = client.post(
        "/api/v1/auth/switch-space",
        json={"space_id": space_id, "refresh_token": alice["refresh_token"]},
        headers=alice_auth,
    )
    assert resp.status_code == 200, resp.text
    rotated = resp.json()

    resp = client.post("/api/v1/auth/refresh", json={"refresh_token": alice["refresh_token"]})
    assert resp.status_code == 401  # 旧 refresh 已作废
    resp = client.post("/api/v1/auth/refresh", json={"refresh_token": rotated["refresh_token"]})
    assert resp.status_code == 200
    latest = resp.json()

    # ---- 登出与改密踢下线 ----
    resp = client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": latest["refresh_token"]},
        headers={"Authorization": f"Bearer {latest['access_token']}"},
    )
    assert resp.status_code == 204
    resp = client.post("/api/v1/auth/refresh", json={"refresh_token": latest["refresh_token"]})
    assert resp.status_code == 401

    resp = client.post(
        "/api/v1/users/me/password",
        json={"old_password": "secret-pass-1", "new_password": "brand-new-pass"},
        headers=alice_auth,
    )
    assert resp.status_code == 204
    resp = client.post(
        "/api/v1/auth/login", json={"username": "alice", "password": "brand-new-pass"}
    )
    assert resp.status_code == 200
    resp = client.post(
        "/api/v1/auth/login", json={"username": "alice", "password": "secret-pass-1"}
    )
    assert resp.status_code == 401
