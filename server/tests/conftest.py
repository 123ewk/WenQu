"""集成测试共享夹具(testcontainers 真 Postgres)。

放在 conftest 且 session 级:多个测试模块共用同一个容器与迁移,避免重复启动。
外部依赖(Docker)不可用时 skip 而非失败(基准 03)。
"""

from __future__ import annotations

import os
import shutil

import pytest


@pytest.fixture(scope="session")
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

        app = create_app()
        # CI/本地集成环境无 MinIO:换内存替身。必须用**同一个实例** ——
        # 若用类/工厂覆盖,每个请求都会拿到空存储,跨请求的写入读不到,
        # 上传/删除这类流程就变成了"假通过"。
        from app.api.deps import get_storage
        from app.core.storage import MemoryStorage

        shared_storage = MemoryStorage()
        app.dependency_overrides[get_storage] = lambda: shared_storage

        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client
    finally:
        from app.core.db import get_engine, get_session_factory

        get_engine.cache_clear()
        get_session_factory.cache_clear()
        container.stop()


def register(client, username: str) -> dict:
    """注册并返回 AuthResponse(集成测试通用前置)。"""
    resp = client.post(
        "/api/v1/auth/register",
        json={"username": username, "nickname": username, "password": "secret-pass-1"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()
