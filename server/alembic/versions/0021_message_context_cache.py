"""缓存命中快照(OPT-45 P-cache):messages 加两列当轮 cached/prompt 快照。

悬浮窗要显示命中率,而 `GET /context` 是独立请求、只能读已落库的值 —— 沿用 OPT-29 的
"存当轮快照而非现算":把末次 chat 调用上游回报的 cached_tokens 与 prompt_tokens 落在
assistant 行上。两列都可空:上游没回报 cached 字段的模型(多数)整轮留 NULL,前端据此
显示"当前模型暂不支持缓存命中统计",而非硬造 0%。只写 assistant 行,user 行与老行恒 NULL。

号段:本分支 head=0020(memory_proposals),本片 0021 顺延,down_revision 指回 0020。

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-23
"""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("context_cached_tokens", sa.Integer(), nullable=True),
    )
    op.add_column(
        "messages",
        sa.Column("context_prompt_tokens", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("messages", "context_prompt_tokens")
    op.drop_column("messages", "context_cached_tokens")
