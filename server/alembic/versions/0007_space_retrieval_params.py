"""空间级检索参数:RRF k/权重/阈值/默认 topK(设计文档 3 节「按空间可配」)

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 默认值 = 设计文档基准值(ADR-1/ADR-5),既有空间行为不变
    op.add_column(
        "spaces", sa.Column("retrieval_rrf_k", sa.Integer(), nullable=False, server_default="60")
    )
    op.add_column(
        "spaces",
        sa.Column("retrieval_vector_weight", sa.Float(), nullable=False, server_default="0.7"),
    )
    op.add_column(
        "spaces",
        sa.Column("retrieval_fulltext_weight", sa.Float(), nullable=False, server_default="0.3"),
    )
    op.add_column(
        "spaces",
        sa.Column("retrieval_min_score", sa.Float(), nullable=False, server_default="0.3"),
    )
    op.add_column(
        "spaces",
        sa.Column("retrieval_default_top_k", sa.Integer(), nullable=False, server_default="6"),
    )


def downgrade() -> None:
    for column in (
        "retrieval_default_top_k",
        "retrieval_min_score",
        "retrieval_fulltext_weight",
        "retrieval_vector_weight",
        "retrieval_rrf_k",
    ):
        op.drop_column("spaces", column)
