"""用户最近登录:last_login_at / last_login_ip 支撑个人中心展示

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("last_login_ip", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "last_login_ip")
    op.drop_column("users", "last_login_at")
