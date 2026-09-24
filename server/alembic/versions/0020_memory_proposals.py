"""记忆提议忽略留痕(OPT-45 P2):memory_proposals 表(仅存 dismissed,供去重再提议)

- 归属 = (space_id, user_id) 双维度隔离,与 memories 同规则;
- 每行是一条"用户忽略过某条提议"的记录,按 content_hash 匹配未过期项以跳过重复弹卡;
- expires_at = created_at + TTL(默认 7 天),过期行靠查询条件自然失效;
- 号段:本分支 head=0019(memories),本片 0020 顺延,down_revision 指回 0019。

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-23
"""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_proposals",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "space_id",
            sa.Uuid(),
            sa.ForeignKey("spaces.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "conversation_id",
            sa.Uuid(),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default="fact"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_mprops_lookup",
        "memory_proposals",
        ["space_id", "user_id", "content_hash", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_mprops_lookup", table_name="memory_proposals")
    op.drop_table("memory_proposals")
