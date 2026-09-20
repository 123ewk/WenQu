"""数据库会话(SQLAlchemy 2.0 同步 + psycopg3)。

M1 选同步驱动:并发个位数场景下简单可靠、测试零 greenlet 负担;
引擎按 settings.database_url 惰性创建,不在 import 期产生副作用(基准 01)。
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from fastapi import Request
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

# 请求会话在 scope 里的登记键:供 CommitBeforeSendRoute 在响应发送前取到并提交
DB_SESSION_SCOPE_KEY = "wenqu.db_session"


@lru_cache
def get_engine() -> Engine:
    url = get_settings().database_url
    if not url:
        raise RuntimeError("APP_DATABASE_URL 未配置,无法访问数据库(fail-closed)")
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def get_db(request: Request) -> Iterator[Session]:
    """FastAPI 依赖:请求级会话,异常回滚,用毕关闭。

    会话同时登记到 ASGI scope,由 app.api.routing.CommitBeforeSendRoute
    在响应发送前提交(收尾 commit 晚于响应发出,真实 HTTP 下写后立读不可见)。
    """
    session = get_session_factory()()
    request.scope[DB_SESSION_SCOPE_KEY] = session
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
