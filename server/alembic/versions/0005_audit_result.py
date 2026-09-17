"""审计增强:audit_logs.result(成功/拒绝)支撑前端结果列

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 既有行全部是成功记录(被拒操作此前不落库),默认值即是历史语义
    op.add_column(
        "audit_logs",
        sa.Column("result", sa.String(16), nullable=False, server_default="success"),
    )
    op.create_index("ix_audit_result", "audit_logs", ["result"])


def downgrade() -> None:
    op.drop_index("ix_audit_result", table_name="audit_logs")
    op.drop_column("audit_logs", "result")
