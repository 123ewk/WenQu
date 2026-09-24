"""外部事实记忆(OPT-45 P1):memories 表 + 向量近邻索引

- 归属 = (space_id, user_id) 双维度隔离;跨用户/跨空间一律不可见;
- embedding 走空间层 key,与 chunks 同维度(1024),供向量相关性召回;
- source_* 溯源列:来源会话删除(SET NULL)不影响记忆本身。
- 号段说明:OPT-41 已并入 main(head 0018),本片 down_revision 指向 0018,
  本功能链为 0018→0019→0020→0021,单 head 合流。

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-23
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memories",
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
        sa.Column("kind", sa.String(16), nullable=False, server_default="fact"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("topic", sa.String(255), nullable=False, server_default=""),
        sa.Column(
            "source_conversation_id",
            sa.Uuid(),
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source_message_id", sa.Uuid(), nullable=True),
        sa.Column("embedding", Vector(1024), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
    )
    op.create_index("ix_memories_space_user", "memories", ["space_id", "user_id"])
    op.create_index(
        "ix_memories_embedding_hnsw",
        "memories",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_memories_embedding_hnsw", table_name="memories")
    op.drop_index("ix_memories_space_user", table_name="memories")
    op.drop_table("memories")
