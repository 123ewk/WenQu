"""自定义模型目录(OPT-25)+ API Key 绑定成员(OPT-26)

- model_providers / custom_models:模型清单从随包 YAML 翻转为库内可管理,
  供应商密钥 AES-GCM 加密落库(api_key_encrypted),内置条目随代码下线,不做迁移播种
  —— 上线后由 superuser 经管理接口自行配置;
- api_keys.member_user_id:Key 从"创建者身份行事"翻转为"绑定成员身份行事";
  存量回填 member_user_id = created_by(旧语义与新列等价,行为零变化)。

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-20
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_providers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        # 供应商显示名,全局唯一(前端下拉分组键)
        sa.Column("name", sa.String(64), nullable=False),
        # 协议注册表键(openai_compatible…);调用时分发依据,新增协议 = 注册适配器
        sa.Column(
            "protocol",
            sa.String(32),
            nullable=False,
            server_default="openai_compatible",
        ),
        sa.Column("base_url", sa.String(512), nullable=False),
        # AES-GCM 密文(enc:v1:…);空串 = 免密钥本地端点(ollama/vllm)
        sa.Column(
            "api_key_encrypted", sa.Text(), nullable=False, server_default=""
        ),
        # 掩码提示(如 "sk-****ab12"),仅管理端辨识用,永不回显明文
        sa.Column("key_hint", sa.String(32), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
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
    op.create_unique_constraint("uq_model_providers_name", "model_providers", ["name"])

    op.create_table(
        "custom_models",
        sa.Column("id", sa.Uuid(), primary_key=True),
        # 对外模型 id(契约里的 ModelItem.id,如 "custom/deepseek-chat");
        # 与 messages.model_id/knowledge_bases.embedding_model 的 String(128) 对齐
        sa.Column("model_id", sa.String(128), nullable=False),
        sa.Column(
            "provider_id",
            sa.Uuid(),
            sa.ForeignKey("model_providers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # chat | embedding | rerank(与既有契约三分类一致)
        sa.Column("kind", sa.String(16), nullable=False),
        # 上游真实模型名/接入点 id
        sa.Column("model_name", sa.String(128), nullable=False),
        sa.Column("dims", sa.Integer(), nullable=True),
        sa.Column("context_tokens", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        # 每 kind 至多一个默认(partial unique);全库无默认时该 kind 调用 503 fail-closed
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
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
    op.create_unique_constraint("uq_custom_models_model_id", "custom_models", ["model_id"])
    op.create_index("ix_custom_models_kind_enabled", "custom_models", ["kind", "enabled"])
    op.create_index(
        "uq_custom_models_default_per_kind",
        "custom_models",
        ["kind"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )

    op.add_column(
        "api_keys",
        sa.Column("member_user_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_api_keys_member_user",
        "api_keys",
        "users",
        ["member_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # 存量 Key:绑定成员 = 原创建者(语义等价,零行为变化)
    op.execute("UPDATE api_keys SET member_user_id = created_by WHERE member_user_id IS NULL")
    # 内置模型下线:KB 的嵌入模型默认值不再指向内置 id,旧内置 id 行清为"退回目录默认"
    op.alter_column(
        "knowledge_bases",
        "embedding_model",
        server_default="",
        existing_type=sa.String(128),
        existing_nullable=False,
    )
    op.execute(
        """
        UPDATE knowledge_bases SET embedding_model = ''
        WHERE embedding_model IN (
            'dashscope/text-embedding-v3', 'dashscope/text-embedding-v4',
            'zhipu/embedding-3', 'ollama/bge-m3'
        )
        """
    )


def downgrade() -> None:
    op.alter_column(
        "knowledge_bases",
        "embedding_model",
        server_default="dashscope/text-embedding-v3",
        existing_type=sa.String(128),
        existing_nullable=False,
    )
    op.drop_column("api_keys", "member_user_id")
    op.drop_index("uq_custom_models_default_per_kind", table_name="custom_models")
    op.drop_index("ix_custom_models_kind_enabled", table_name="custom_models")
    op.drop_constraint("uq_custom_models_model_id", "custom_models", type_="unique")
    op.drop_table("custom_models")
    op.drop_constraint("uq_model_providers_name", "model_providers", type_="unique")
    op.drop_table("model_providers")
