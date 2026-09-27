"""Langfuse 配置改为每空间一行(OPT-49 v2,2026-09-26 用户重述需求)。

v1 的"全局单行 + superuser 管理 + 外部面板看 trace"作废,新口径:
- **每空间一套配置**(地址/公钥/私钥/启停),该空间 Admin+(role≥30)可改;
- trace 按提问所在空间上送到该空间配置的 Langfuse;
- **产品内可看**:成员看自己的 trace,Admin+ 看本空间全部(经 Langfuse API 代理,见 N2 路由);
- 未配置的空间 = 未接入,nil-safe 零开销。

本迁移:删除全局单行表(从未被生产写入过配置,无数据可迁),建每空间表。

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("langfuse_config")
    op.create_table(
        "space_langfuse_configs",
        sa.Column(
            "space_id",
            sa.Uuid(),
            sa.ForeignKey("spaces.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("public_key", sa.String(255), nullable=False),
        sa.Column("secret_key_encrypted", sa.String(500), nullable=False, server_default=""),
        sa.Column("key_hint", sa.String(64), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("space_langfuse_configs")
    op.create_table(
        "langfuse_config",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("public_key", sa.String(255), nullable=False),
        sa.Column("secret_key_encrypted", sa.String(500), nullable=False, server_default=""),
        sa.Column("key_hint", sa.String(64), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.CheckConstraint("id = 1", name="ck_langfuse_config_singleton"),
    )
