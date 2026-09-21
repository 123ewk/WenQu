"""空间级模型配置(OPT-27):模型表空间分层 + 空间模型总设置。

- model_providers/custom_models 加 `space_id`(NULL = 全局层,superuser /admin/* 继续管):
  name/model_id 的唯一性从"全库"改为"全局一份 + 每空间一份"(两条 partial unique 分别钉住);
  is_default 的 kind 互斥同样按层拆分(全局一个默认、每空间一个默认);
- 新表 space_model_settings(一空间一行):内置 embedding 模型的空间自有 Key(加密)
  + rerank 启用开关。语义:embedding 模型本体内置锁死(换模型=已入库向量作废),
  空间能配的只有它的钥匙。

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-20
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

_SPACES_FK = "spaces.id"


def upgrade() -> None:
    # ---- model_providers:空间分层 ----
    op.add_column(
        "model_providers",
        sa.Column("space_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_model_providers_space",
        "model_providers",
        "spaces",
        ["space_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint("uq_model_providers_name", "model_providers", type_="unique")
    op.create_index(
        "uq_model_providers_name_global",
        "model_providers",
        ["name"],
        unique=True,
        postgresql_where=sa.text("space_id IS NULL"),
    )
    op.create_index(
        "uq_model_providers_name_space",
        "model_providers",
        ["space_id", "name"],
        unique=True,
        postgresql_where=sa.text("space_id IS NOT NULL"),
    )

    # ---- custom_models:空间分层 ----
    op.add_column(
        "custom_models",
        sa.Column("space_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_custom_models_space",
        "custom_models",
        "spaces",
        ["space_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint("uq_custom_models_model_id", "custom_models", type_="unique")
    op.create_index(
        "uq_custom_models_model_id_global",
        "custom_models",
        ["model_id"],
        unique=True,
        postgresql_where=sa.text("space_id IS NULL"),
    )
    op.create_index(
        "uq_custom_models_model_id_space",
        "custom_models",
        ["space_id", "model_id"],
        unique=True,
        postgresql_where=sa.text("space_id IS NOT NULL"),
    )
    # kind 默认互斥按层拆分:全局一个默认、每空间一个默认
    op.drop_index("uq_custom_models_default_per_kind", table_name="custom_models")
    op.create_index(
        "uq_custom_models_default_per_kind_global",
        "custom_models",
        ["kind"],
        unique=True,
        postgresql_where=sa.text("is_default AND space_id IS NULL"),
    )
    op.create_index(
        "uq_custom_models_default_per_kind_space",
        "custom_models",
        ["space_id", "kind"],
        unique=True,
        postgresql_where=sa.text("is_default AND space_id IS NOT NULL"),
    )

    # ---- 空间模型总设置 ----
    op.create_table(
        "space_model_settings",
        sa.Column(
            "space_id",
            sa.Uuid(),
            sa.ForeignKey(_SPACES_FK, ondelete="CASCADE"),
            primary_key=True,
        ),
        # 内置 embedding 模型的空间自有密钥:AES-GCM 密文(enc:v1:…),空串 = 未配置
        sa.Column(
            "embedding_key_encrypted", sa.Text(), nullable=False, server_default=""
        ),
        sa.Column("embedding_key_hint", sa.String(32), nullable=False, server_default=""),
        sa.Column(
            "rerank_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("space_model_settings")
    # 空间层行随列一起消失;回退前先清掉,避免恢复全局唯一约束时撞名
    op.execute("DELETE FROM custom_models WHERE space_id IS NOT NULL")
    op.execute("DELETE FROM model_providers WHERE space_id IS NOT NULL")
    op.drop_index("uq_custom_models_default_per_kind_space", table_name="custom_models")
    op.drop_index("uq_custom_models_default_per_kind_global", table_name="custom_models")
    op.create_index(
        "uq_custom_models_default_per_kind",
        "custom_models",
        ["kind"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )
    op.drop_index("uq_custom_models_model_id_space", table_name="custom_models")
    op.drop_index("uq_custom_models_model_id_global", table_name="custom_models")
    op.create_unique_constraint("uq_custom_models_model_id", "custom_models", ["model_id"])
    op.drop_constraint("fk_custom_models_space", "custom_models", type_="foreign_key")
    op.drop_column("custom_models", "space_id")
    op.drop_index("uq_model_providers_name_space", table_name="model_providers")
    op.drop_index("uq_model_providers_name_global", table_name="model_providers")
    op.create_unique_constraint("uq_model_providers_name", "model_providers", ["name"])
    op.drop_constraint("fk_model_providers_space", "model_providers", type_="foreign_key")
    op.drop_column("model_providers", "space_id")
