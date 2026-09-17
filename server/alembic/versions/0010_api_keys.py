"""API Key 表:程序化接入凭据(OPT-6)

只存 SHA-256 哈希用于查找,不存明文 —— 原型页 07 明确"仅此一次完整展示",
没有任何接口会读回明文,存了就是无人读取的长期泄露面(见架构设计 §11 偏离登记)。

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-17
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(), primary_key=True),
        # 空间是租户边界:Key 归属空间,跨空间不可见
        sa.Column(
            "space_id",
            sa.Uuid(),
            sa.ForeignKey("spaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # 归属人:Key 以创建者身份行事(成员被移除则 Key 连带失效)
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        # 查找键:SHA-256(明文),唯一;明文本身不落库
        sa.Column("key_hash", sa.String(64), nullable=False),
        # 展示用短标识(如 sk-****4f2a),取自明文首尾,便于用户辨识是哪把 Key
        sa.Column("key_hint", sa.String(32), nullable=False),
        # 能力清单:能力级授权在服务层按路由授权表判定,未声明路由默认拒绝
        sa.Column(
            "capabilities",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        # 允许的知识库范围;空数组 = 全部知识库(仍受创建者成员身份约束)
        sa.Column(
            "kb_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_unique_constraint("uq_api_key_hash", "api_keys", ["key_hash"])
    op.create_index("ix_api_keys_space_created", "api_keys", ["space_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_api_keys_space_created", table_name="api_keys")
    op.drop_constraint("uq_api_key_hash", "api_keys", type_="unique")
    op.drop_table("api_keys")
