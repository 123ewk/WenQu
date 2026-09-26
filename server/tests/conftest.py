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

        from app.application.service.permission_group_toggle import clear_space_toggle_cache
        from app.core.config import get_settings
        from app.core.db import get_engine, get_session_factory

        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()
        # OPT-48 v2:空间级开关读值是进程级 TTL 缓存(按 space_id 分键),换库(测试容器)
        # 后必须一并清掉,否则上一进程状态里缓存的开关值会泄漏进本会话的断言。
        clear_space_toggle_cache()

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


# ---------------------------- OPT-48 v2:空间级权限挡位功能开关 ----------------------------


def enable_space_toggle(space_id: str) -> None:
    """直接写库打开某空间的权限挡位功能开关(v2:分配/换当前要求开关已打开)。

    走 DB 而不是 API:调用方往往还没有空间 Admin 身份的令牌语境,测试只关心"开关是开的"。
    """
    import uuid as _uuid

    from app.application.service.permission_group_toggle import clear_space_toggle_cache
    from app.core.db import get_session_factory
    from app.domain.models import SpacePermissionGroupToggle

    with get_session_factory()() as db:
        sid = _uuid.UUID(space_id)
        row = db.get(SpacePermissionGroupToggle, sid)
        if row is None:
            db.add(SpacePermissionGroupToggle(space_id=sid, enabled=True))
        else:
            row.enabled = True
        db.commit()
    clear_space_toggle_cache()


def wipe_space_toggles() -> None:
    """清空全部空间开关行 + 读值缓存(用例级隔离用:session 级共用库不能泄漏开关状态)。"""
    from sqlalchemy import delete

    from app.application.service.permission_group_toggle import clear_space_toggle_cache
    from app.core.db import get_session_factory
    from app.domain.models import SpacePermissionGroupToggle

    with get_session_factory()() as db:
        db.execute(delete(SpacePermissionGroupToggle))
        db.commit()
    clear_space_toggle_cache()


# ---------------------------- OPT-41 两件套:先档位后凭据 ----------------------------
#
# 新契约下 Key 的权限(能力 + 知识库范围)全部来自档位,所以"建一把能用 chat 的 Key"
# 在集成测试里恒为两步。收在这里,免得四个文件各写一遍、改契约时漏改。


def create_group(
    client,
    auth: dict,
    space_id: str,
    *,
    name: str = "档位",
    capabilities: list[str] | tuple[str, ...] = ("chat",),
    kb_ids: list[str] | tuple[str, ...] = (),
    all_kbs: bool | None = None,
    description: str = "",
) -> dict:
    """建一个权限档位并返回详情。

    `all_kbs` 缺省沿用旧直觉"没勾库 = 全部";要测 fail-closed 的"一个库都不给"必须
    显式传 `all_kbs=False`(与生产口径一致:放开全部库是显式动作)。
    """
    kb_list = list(kb_ids)
    body = {
        "name": name,
        "description": description,
        "capabilities": list(capabilities),
        "kb_ids": kb_list,
        "all_kbs": (not kb_list) if all_kbs is None else all_kbs,
    }
    resp = client.post(
        f"/api/v1/spaces/{space_id}/permission-groups", json=body, headers=auth
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def create_key(
    client,
    auth: dict,
    space_id: str,
    group_id: str,
    *,
    name: str = "ci-key",
    owner_user_id: str | None = None,
    description: str = "",
) -> dict:
    """给一个档位配一把凭据(归因成员缺省为调用者本人)。"""
    if owner_user_id is None:
        owner_user_id = client.get("/api/v1/users/me", headers=auth).json()["id"]
    resp = client.post(
        f"/api/v1/spaces/{space_id}/api-keys",
        json={
            "name": name,
            "description": description,
            "group_id": group_id,
            "owner_user_id": owner_user_id,
        },
        headers=auth,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()
