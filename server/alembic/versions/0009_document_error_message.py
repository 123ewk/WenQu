"""文档失败原因:documents.error_message(任务侧真实原因,便于定位)

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("error_message", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "error_message")
