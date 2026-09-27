"""迁移状态检查与 prod 启动阻断(Go-Live 翻转项 #12)单测(不依赖真库)。"""

from __future__ import annotations

import pytest

import app.main as main_module
from app.core.config import Settings
from app.core.db import get_engine, get_session_factory
from app.core.migrations import SCRIPT_LOCATION, MigrationStatus, migration_status


def _clear_caches() -> None:
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def test_script_head_is_0024() -> None:
    from alembic.script import ScriptDirectory

    assert ScriptDirectory(SCRIPT_LOCATION).get_heads() == ["0025"]


def test_dirty_semantics() -> None:
    assert MigrationStatus(current="0011", head="0011").dirty is False
    assert MigrationStatus(current="0010", head="0011").dirty is True
    assert MigrationStatus(current=None, head="0011").dirty is True
    assert MigrationStatus(current="0011", head=None).dirty is True  # 多 head 也算


def test_db_unreachable_reports_dirty_with_head(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> object:
        raise RuntimeError("db down")

    monkeypatch.setattr("app.core.migrations.get_session_factory", _boom)
    status = migration_status()
    assert status.current is None
    assert status.head == "0025"
    assert status.dirty is True


def _lifespan(monkeypatch: pytest.MonkeyPatch, status: MigrationStatus):
    # prod fail-closed 校验要求四项显式配置(含数据库串):测试给最小 dummy 值,
    # 不依赖本地 .env —— CI 与干净 worktree 里没有 .env,漏一项就是整条门禁假红。
    monkeypatch.setenv("APP_JWT_SECRET", "x" * 32)
    monkeypatch.setenv("APP_MASTER_KEY", "x" * 32)
    monkeypatch.setenv("PARSER_GRPC_TOKEN", "x" * 32)
    monkeypatch.setenv(
        "APP_DATABASE_URL", "postgresql+psycopg://wenqu:wenqu_test@localhost:5432/wenqu_test"
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(env="prod"))
    monkeypatch.setattr(main_module, "migration_status", lambda: status)
    return main_module.stream_lifespan(main_module.app)


def _enter_lifespan(cm: object) -> None:
    import asyncio

    async def _run() -> None:
        await cm.__aenter__()  # type: ignore[attr-defined]
        await cm.__aexit__(None, None, None)  # type: ignore[attr-defined]

    asyncio.run(_run())


def test_prod_startup_blocked_when_dirty(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_caches()
    with pytest.raises(RuntimeError, match="prod 启动阻断"):
        _enter_lifespan(_lifespan(monkeypatch, MigrationStatus(current="0010", head="0011")))


def test_prod_startup_passes_when_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_caches()
    _enter_lifespan(_lifespan(monkeypatch, MigrationStatus(current="0011", head="0011")))
