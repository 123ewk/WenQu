"""实例级灰度开关落库(OPT-48):人链路"必须有权位"从环境变量升级成页面可拨的运行时开关。

单行表 `permission_enforcement`(CHECK id=1 钉死全局单行,粒度=整个实例,D1):
- `enabled` = 开关当前值。**表为空 = 库里没配过**,读值服务回落到
  `APP_REQUIRE_PERMISSION_GROUP_FOR_CORE`(默认 False)——库里什么都不配,
  行为与今天逐字节一致(fail-closed 底线,§4.1-1);
- `updated_at` / `updated_by` = 行本身的最新状态;谁在什么时候从什么拨到什么
  的完整留痕在 audit_logs(OPT-48 N3),这里不重复记历史。

只加不删,不迁移任何存量数据(表从空开始);downgrade 整表删除即完全回到 0021。

号段:main head=0021(message_context_cache),本片 0022 顺延,down_revision 指回 0021。

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-26
"""

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "permission_enforcement",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.CheckConstraint("id = 1", name="ck_permission_enforcement_singleton"),
    )


def downgrade() -> None:
    op.drop_table("permission_enforcement")
