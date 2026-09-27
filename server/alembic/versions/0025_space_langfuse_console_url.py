"""空间级 Langfuse 配置补「控制台地址」字段(OPT-49 补充,2026-09-27 用户反馈)。

console_url = 浏览器访问 Langfuse 用的地址,与 host 的分工:
- host:WenQu 后端服务器可达的地址(自托管经容器内网时是 `http://langfuse-...:3000` 这类名字);
- console_url:运维/管理员**浏览器**可达的地址(自托管经隧道时是 `http://127.0.0.1:3100` 这类)。
前端"创建密钥/看 trace"跳转链接以 console_url 为基座(未填时回落 host,都不填不出链接)。

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "space_langfuse_configs",
        sa.Column("console_url", sa.String(length=255), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("space_langfuse_configs", "console_url")
