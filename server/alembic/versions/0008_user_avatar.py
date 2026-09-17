"""用户头像:avatar_key 指向对象存储中的头像对象

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("avatar_key", sa.String(512), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "avatar_key")
