"""权限档位(OPT-41 R6-b):把"一把 Key 绑一个成员"升级成"档位承载权限,Key 只是凭据"。

模型变化:
- `permission_groups` = 一个空间内的**权限档位**:能力清单 + 知识库范围(`all_kbs` 或逐库 `kb_ids`);
- `permission_group_members` = 成员挂到哪个档位,`is_current` 标出该成员**当前在用**的那一档
  (部分唯一索引:每人每空间至多一档为 current);
- `api_keys.group_id` = 这把凭据属于哪个档位(一个档位可以有多把明文,轮换互不影响)。

**本片只做加法 + 回填,不删旧列**:`api_keys.capabilities / kb_ids / member_user_id / is_active`
继续存在并被旧代码读取,删列放在 0018(与服务层切读档位同批),这样部署可以"先加后删"分两次上线。

回填口径(1 Key : 1 档位,逐把派生,保证语义零变化):
- `all_kbs = (旧 kb_ids 为空)` —— 旧语义"空范围 = 不限制"如实翻译成显式的"全部";
- `kb_ids` 非空时原样搬,`all_kbs = false`;
- 旧 `member_user_id`(缺失时回退 `created_by`,与 `ApiKeyService.member_of` 同口径)挂到该档位;
- `is_current` 只从**未吊销且旧 is_active** 的行派生,并且按 `created_at DESC` 每人只留一档 ——
  旧的部分唯一索引只约束 `member_user_id`,而这里按 COALESCE 归人,可能出现同一人两把 current。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "permission_groups",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "space_id",
            sa.Uuid(),
            sa.ForeignKey("spaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "capabilities",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "kb_ids",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("all_kbs", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        # 档位名**不要求唯一**:回填阶段每把 Key 派生一档,而 Key 名本来就可以重名;
        # 管理页按 id 定位,重名只影响观感,加唯一约束反而逼出后缀噪声。
    )
    op.create_index(
        "ix_permission_groups_space_created", "permission_groups", ["space_id", "created_at"]
    )

    op.create_table(
        "permission_group_members",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "group_id",
            sa.Uuid(),
            sa.ForeignKey("permission_groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # 冗余 space_id:租户谓词与"每人每空间至多一档 current"的唯一索引都要它
        sa.Column(
            "space_id",
            sa.Uuid(),
            sa.ForeignKey("spaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("group_id", "user_id", name="uq_permission_group_member_user"),
    )
    op.create_index(
        "ix_permission_group_members_space_user", "permission_group_members", ["space_id", "user_id"]
    )
    op.create_index(
        "uq_permission_group_one_current_per_member",
        "permission_group_members",
        ["space_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )

    op.add_column(
        "api_keys",
        sa.Column("group_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_api_keys_group", "api_keys", "permission_groups", ["group_id"], ["id"],
        ondelete="RESTRICT",
    )

    _backfill_groups_from_keys()

    op.alter_column("api_keys", "group_id", existing_type=sa.Uuid(), nullable=False)
    op.create_index("ix_api_keys_group_created", "api_keys", ["group_id", "created_at"])


def _backfill_groups_from_keys() -> None:
    """每把存量 Key 派生一个同名档位,并把绑定成员挂上去。"""
    op.execute(
        """
        INSERT INTO permission_groups (
            id, space_id, name, description, capabilities, kb_ids, all_kbs,
            created_by, created_at, updated_at
        )
        SELECT
            k.id,
            k.space_id,
            k.name,                       -- 档位沿用 Key 名(名不要求唯一,见建表注释)
            k.description,
            k.capabilities,
            k.kb_ids,
            (k.kb_ids = '[]'::jsonb),   -- 旧"空范围 = 不限制" → 显式 all_kbs
            k.created_by,
            k.created_at,
            k.updated_at
        FROM api_keys k
        """
    )
    op.execute("UPDATE api_keys SET group_id = id")
    op.execute(
        """
        INSERT INTO permission_group_members (id, group_id, space_id, user_id, is_current)
        SELECT
            gen_random_uuid(),
            k.id,
            k.space_id,
            COALESCE(k.member_user_id, k.created_by),
            false
        FROM api_keys k
        WHERE COALESCE(k.member_user_id, k.created_by) IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE permission_group_members m
        SET is_current = true
        FROM (
            SELECT k.id AS group_id,
                   COALESCE(k.member_user_id, k.created_by) AS user_id,
                   row_number() OVER (
                       PARTITION BY k.space_id, COALESCE(k.member_user_id, k.created_by)
                       ORDER BY k.created_at DESC, k.id
                   ) AS rn
            FROM api_keys k
            WHERE k.is_active AND k.revoked_at IS NULL
              AND COALESCE(k.member_user_id, k.created_by) IS NOT NULL
        ) t
        WHERE m.group_id = t.group_id AND m.user_id = t.user_id AND t.rn = 1
        """
    )


def downgrade() -> None:
    op.drop_index("ix_api_keys_group_created", table_name="api_keys")
    op.drop_constraint("fk_api_keys_group", "api_keys", type_="foreignkey")
    op.drop_column("api_keys", "group_id")
    op.drop_index("uq_permission_group_one_current_per_member", table_name="permission_group_members")
    op.drop_index("ix_permission_group_members_space_user", table_name="permission_group_members")
    op.drop_table("permission_group_members")
    op.drop_index("ix_permission_groups_space_created", table_name="permission_groups")
    op.drop_table("permission_groups")
