"""API Key 生效标记(OPT-31)

- api_keys.is_active:同空间同成员至多一把 Key 标为"正在使用"(分配语义,
  Admin+ 经 activate 端点设置);**不参与运行时认证**——未标记的未吊销 Key
  仍可正常调用(成员可换着用),生效性仅用于管理页过滤与辨识;
- 存量回填:每成员按 created_at 最新的未吊销 Key 自动标为使用中,上线即有数据可过滤;
- 部分唯一索引在库层面兜住"一人同时最多一把使用中"。

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-21
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # 存量:每人(COALESCE 口径与 member_of 一致)最近创建的未吊销 Key 标为使用中
    op.execute(
        """
        UPDATE api_keys k
        SET is_active = true
        FROM (
            SELECT id, row_number() OVER (
                PARTITION BY space_id, COALESCE(member_user_id, created_by)
                ORDER BY created_at DESC, id
            ) AS rn
            FROM api_keys
            WHERE revoked_at IS NULL
              AND COALESCE(member_user_id, created_by) IS NOT NULL
        ) t
        WHERE k.id = t.id AND t.rn = 1
        """
    )
    op.create_index(
        "uq_api_keys_one_active_per_member",
        "api_keys",
        ["space_id", "member_user_id"],
        unique=True,
        postgresql_where=sa.text("is_active AND revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_api_keys_one_active_per_member", table_name="api_keys")
    op.drop_column("api_keys", "is_active")
