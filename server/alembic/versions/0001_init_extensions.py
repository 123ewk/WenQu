"""init:启用 pgvector 与 pg_trgm 扩展(ADR-1 单库三合一的前提)

Revision ID: 0001
Revises:
Create Date: 2026-09-16
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 开发/生产容器均以 postgres 超级用户运行,可创建扩展;
    # 生产若改用受限账号,需 DBA 预先执行本迁移中的语句
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")


def downgrade() -> None:
    # 基准 05:破坏性 down 一律 no-op —— 删扩展会影响未知依赖对象,禁止自动回滚
    pass
