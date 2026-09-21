"""上下文窗口使用率(OPT-29):messages 加两列窗口快照。

为什么存"这一轮的结果"而不是每次现算:分母来自 chat 模型的 context_tokens,而模型
随时可能被改配置或删掉 —— 存快照才能让"刷新页面后进度条复原"看到当轮真实数字。
为什么两列都可空:老消息没有当轮数据(从今往后计,不回填);全局层历史模型的
context_tokens 本就允许为空(用户裁决),分母缺失时分子也没意义,双双留空由前端
显示"窗口未配置"。只写 assistant 行,user 行恒为 NULL。

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-21
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("context_used_tokens", sa.Integer(), nullable=True),
    )
    op.add_column(
        "messages",
        sa.Column("context_window_tokens", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("messages", "context_window_tokens")
    op.drop_column("messages", "context_used_tokens")
