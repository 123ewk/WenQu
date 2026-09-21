"""token 用量明细(OPT-28):新表 token_usage_events。

一次上游模型调用一行,只追加不改写。为什么要随行存 `source`:厂商不回传 usage 时
只能按文本估算,估算值与实测值口径不同、不可混算,汇总页必须能区分(前端打 ≈ 角标)。
`user_id` 用 SET NULL 而不是 CASCADE:成员被删不应该把既成用量抹掉(空间总量要守恒等式)。
V1 不预聚合,明细 + 下面两个索引直接支撑"按空间按时间"与"按空间按成员"两类汇总。

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-21
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "token_usage_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "space_id",
            sa.Uuid(),
            sa.ForeignKey("spaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # chat | embedding(裸字符串,以后加 rerank 不改表)
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("model_id", sa.String(128), nullable=False, server_default=""),
        sa.Column("provider", sa.String(64), nullable=False, server_default=""),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        # upstream = 厂商 usage 原值;estimated = 后端估算(不可与前者混算)
        sa.Column("source", sa.String(16), nullable=False, server_default="upstream"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_usage_space_created", "token_usage_events", ["space_id", "created_at"])
    op.create_index("ix_usage_space_user", "token_usage_events", ["space_id", "user_id"])


def downgrade() -> None:
    op.drop_table("token_usage_events")
