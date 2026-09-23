"""API Key 退化为"档位的凭据"(OPT-41 R6-b):删掉 Key 上的旧权限列。

0017 只加不删,是为了让"先加后删"分两次上线;本片把读取口径已切到档位的代码
对应的旧列拆掉:

- `api_keys.capabilities` / `kb_ids` → 真相已在 `permission_groups` 上,Key 上的副本
  留着只会漂移(改档位不改副本 = 两套权限口径,正是 OPT-41 要消灭的东西);
- `api_keys.is_active`(OPT-31)→ 被 `permission_group_members.is_current` 取代:
  一个档位现在可以对多个人生效,"哪把 Key 正在使用"已经不再是"谁当前有权限"的
  正确表达(每人每空间至多一档由 0017 的部分唯一索引兜底);
- `api_keys.member_user_id` → **改名** `owner_user_id`:这一列还在,但语义收窄成
  "以谁的身份行事 + 调用归因",不再承载权限。

**downgrade 对权限是无损的**:`capabilities` / `kb_ids` 从档位原样搬回(`all_kbs=true`
翻译回旧语义的"空 kb_ids = 不限制"),回滚后旧代码(读旧列)看到的权限与升级前逐字相同。
`is_active` **只能重新派生**,不保证与升级前同一行的值一致:它是 OPT-31 的管理页标记
(不参与认证、不卡权限),升级后"每人一把使用中"已并入 current 指针,回滚时按 current
指针 + 旧口径(未吊销、每人取 `created_at` 最新)重算,得到的是同样合法、旧代码跑得通的状态。

顺带把 FK 约束改名与新列一致:Postgres 的 `RENAME COLUMN` 不会带动约束名,留着
`fk_api_keys_member_user`(0012 显式取的名)会让下一个读 schema 的人找错列。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 旧约束依赖 is_active 列,必须在删列之前拆掉
    op.drop_index("uq_api_keys_one_active_per_member", table_name="api_keys")
    op.alter_column(
        "api_keys",
        "member_user_id",
        new_column_name="owner_user_id",
        existing_type=sa.Uuid(),
        existing_nullable=True,
    )
    op.execute(
        "ALTER TABLE api_keys RENAME CONSTRAINT"
        " fk_api_keys_member_user TO fk_api_keys_owner_user"
    )
    op.drop_column("api_keys", "capabilities")
    op.drop_column("api_keys", "kb_ids")
    op.drop_column("api_keys", "is_active")


def downgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column(
            "capabilities", JSONB(), nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "api_keys",
        sa.Column(
            "kb_ids", JSONB(), nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "api_keys",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # 权限从档位原样搬回副本;all_kbs 翻译回旧口径"空列表 = 不限制"
    op.execute(
        """
        UPDATE api_keys k
        SET capabilities = g.capabilities,
            kb_ids = CASE WHEN g.all_kbs THEN '[]'::jsonb ELSE g.kb_ids END
        FROM permission_groups g
        WHERE g.id = k.group_id
        """
    )
    op.execute(
        "ALTER TABLE api_keys RENAME CONSTRAINT"
        " fk_api_keys_owner_user TO fk_api_keys_member_user"
    )
    op.alter_column(
        "api_keys",
        "owner_user_id",
        new_column_name="member_user_id",
        existing_type=sa.Uuid(),
        existing_nullable=True,
    )
    # current 指针 → is_active:同一档位可能对多把 Key 生效,按旧口径每人只留最新一把
    op.execute(
        """
        UPDATE api_keys k
        SET is_active = true
        FROM (
            SELECT k2.id, row_number() OVER (
                PARTITION BY k2.space_id, COALESCE(k2.member_user_id, k2.created_by)
                ORDER BY k2.created_at DESC, k2.id
            ) AS rn
            FROM api_keys k2
            JOIN permission_group_members m
              ON m.group_id = k2.group_id
             AND m.user_id = COALESCE(k2.member_user_id, k2.created_by)
             AND m.is_current
            WHERE k2.revoked_at IS NULL
              AND COALESCE(k2.member_user_id, k2.created_by) IS NOT NULL
        ) t
        WHERE k.id = t.id AND t.rn = 1
        """
    )
    op.create_index(
        "uq_api_keys_one_active_per_member",
        "api_keys",
        ["space_id", "member_user_id"],
        unique=True,
        postgresql_where=sa.text("is_active AND revoked_at IS NULL"),
    )
