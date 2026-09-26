"""权限挡位功能开关落库(OPT-48 v2,2026-09-26 用户重述需求):每空间一个,页面可拨。

v2 与初版的差别(用户拍板,初版全局单行表作废):
- 粒度 = **每空间一个**:一行 = 一个空间把"人链路核心功能要不要按挡位收口"拨到了哪;
  表无行 = 该空间从未拨过 = 只看角色(与今天逐字节一致,fail-closed 底线);
- 只挡**核心功能清单**(问答/检索/会话流/知识库与文档,`route_in_dual_list`);
  成员/模型配置/审计/用量等管理页继续只看角色;
- 谁能拨 = 该空间 Admin+(role≥30),superuser 兜底(守卫在路由层);
- 打开后才能给成员分配挡位(分配/换当前路由挂 ensure_space_toggle_on,没开 → 409);
- 不拦截"操作者自己没挡"(可自救),页面仅提示。

`updated_at`/`updated_by` 是行本身的最新状态;拨动历史走 audit_logs(含新旧值)。
只加不删;downgrade 整表删除即完全回到 0021。

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
        "space_permission_group_toggles",
        sa.Column(
            "space_id",
            sa.Uuid(),
            sa.ForeignKey("spaces.id", ondelete="CASCADE"),
            primary_key=True,
        ),
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
    )


def downgrade() -> None:
    op.drop_table("space_permission_group_toggles")
