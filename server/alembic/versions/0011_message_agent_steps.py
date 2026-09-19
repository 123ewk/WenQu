"""助手消息存 Agent 工具轨迹(OPT-10):agent_steps JSONB,可空(直检消息恒为空)

前端对话页工具卡的真实数据源之一(F5 后按消息回放轨迹;实时走 SSE 工具事件)。

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-19
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("agent_steps", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("messages", "agent_steps")
