"""Langfuse 可观测配置(OPT-49):全局单行表,按"模型配置"标准管理。

- 管理页(superuser)填写 Langfuse 地址与公私钥,私钥 AES-GCM 加密落库
  (CredentialCipher,与模型供应商同款),响应只回 key_hint 掩码;
- 变更写 audit_logs(langfuse_config.updated);
- **表为空 = 未接入**:运行时 Langfuse 客户端为 None,nil-safe 零开销,
  问答/入库链路行为与今天逐字节一致;
- 启用前置:secret_key 必须已配置(服务层校验,未配不允许 enabled=true)。

号段:main head=0022(space_permission_group_toggle),本片 0023 顺延。

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-26
"""

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_table("langfuse_config")
