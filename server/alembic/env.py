"""Alembic 环境:连接串从 APP_DATABASE_URL 读取(防口令入库,基准 02)。

M0 阶段 target_metadata 为 None,迁移全部手写;M1 起接入 SQLAlchemy 元数据后可用 autogenerate。
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

# 本机开发默认指向 docker-compose.dev.yml 的 postgres;CI/生产必须显式设置 APP_DATABASE_URL
DEV_FALLBACK_URL = "postgresql+psycopg://wenqu:wenqu_dev_only@localhost:5432/wenqu"
db_url = os.environ.get("APP_DATABASE_URL", DEV_FALLBACK_URL)
config.set_main_option("sqlalchemy.url", db_url)

target_metadata = None


def run_migrations_offline() -> None:
    context.configure(url=db_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
