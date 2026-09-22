"""ORM 模型(SQLAlchemy 2.0 声明式)。M1 账号与空间域 + M2 RAG 域:

- users / refresh_tokens(refresh 只存 SHA-256 哈希,基准 04 反模式禁令)
- spaces / memberships(数值角色阶梯)
- audit_logs(动作全分类,actor/space 置 NULL 保留日志)
- knowledge_bases / documents / chunks(向量+全文三合一,ADR-1)/ tasks(DB 队列,ADR-2)
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.domain.enums import AuditResult, DocumentStatus, TaskStatus


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(32), unique=True)
    nickname: Mapped[str] = mapped_column(String(32))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # 最近一次成功认证(注册即登录,故注册时也会写入);失败登录不更新
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_login_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    avatar_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # 活动空间随 refresh 存续;切空间=轮换 refresh(基准 04)
    space_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("spaces.id", ondelete="SET NULL"), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Space(Base):
    __tablename__ = "spaces"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # 空间级检索参数(设计文档:RRF 按空间可配;默认值即全局基准)
    retrieval_rrf_k: Mapped[int] = mapped_column(Integer, default=60, server_default="60")
    retrieval_vector_weight: Mapped[float] = mapped_column(
        Float, default=0.7, server_default="0.7"
    )
    retrieval_fulltext_weight: Mapped[float] = mapped_column(
        Float, default=0.3, server_default="0.3"
    )
    retrieval_min_score: Mapped[float] = mapped_column(Float, default=0.3, server_default="0.3")
    retrieval_default_top_k: Mapped[int] = mapped_column(Integer, default=6, server_default="6")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("space_id", "user_id", name="uq_membership"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    space_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("spaces.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[int] = mapped_column(SmallInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_space_created", "space_id", "created_at"),
        Index("ix_audit_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    space_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("spaces.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(128), default="", server_default="")
    result: Mapped[str] = mapped_column(
        String(16), default=AuditResult.SUCCESS, server_default="success"
    )
    detail: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    ip: Mapped[str] = mapped_column(String(64), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class KnowledgeBase(Base):
    """知识库:空间下的文档容器,持有 embedding 模型与维度(换模型 = KB 级重建索引任务)。"""

    __tablename__ = "knowledge_bases"
    __table_args__ = (UniqueConstraint("space_id", "name", name="uq_kb_name"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    space_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("spaces.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    # 建库时固化的嵌入模型 id(对外 model_id);空串 = 建库时尚无默认模型,入库时退回目录默认
    embedding_model: Mapped[str] = mapped_column(String(128), default="", server_default="")
    embedding_dim: Mapped[int] = mapped_column(Integer, default=1024, server_default="1024")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Document(Base):
    """文档:状态机 pending → parsing → chunking → embedding → completed / failed(常量见 enums)。

    space_id 冗余自 kb,租户谓词直查不走 join(基准 04);source 是对象存储 key。
    """

    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_kb_created", "kb_id", "created_at"),
        Index("ix_documents_space_status", "space_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    kb_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True
    )
    space_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("spaces.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(255))
    format: Mapped[str] = mapped_column(String(16))  # pdf | docx | xlsx | pptx | md | txt
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    source: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(
        String(16), default=DocumentStatus.PENDING, index=True
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 任务侧真实失败原因(定位用);error_code 仍是稳定机器码
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Chunk(Base):
    """分块:检索的最小单元。embedding 向量与 tsv 全文列由流水线写入,检索时二路召回融合。

    parent_id 为 M3 父子分块预留(检索命子块、返回父块);metadata 列名带下划线以避开
    Declarative 保留名,ORM 属性用 meta。
    """

    __tablename__ = "chunks"
    __table_args__ = (
        Index("ix_chunks_document_seq", "document_id", "seq"),
        Index("ix_chunks_space", "space_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    space_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("spaces.id", ondelete="CASCADE"), index=True
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("chunks.id", ondelete="SET NULL"), nullable=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024), nullable=True)
    tsv: Mapped[str | None] = mapped_column(TSVECTOR, nullable=True)
    meta: Mapped[dict] = mapped_column("metadata", JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Task(Base):
    """DB 队列任务(ADR-2):SKIP LOCKED 认领 + 陈旧 claim 回收 + 死信,Redis 的替代位。

    payload 含 trace_id 与业务参数;run_after 支持重试退避。
    """

    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_claim", "status", "run_after"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    status: Mapped[str] = mapped_column(String(16), default=TaskStatus.PENDING, index=True)
    max_retry: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    timeout_s: Mapped[int] = mapped_column(Integer, default=600, server_default="600")
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Conversation(Base):
    """会话:空间内的多轮问答容器(知识库范围可限定;M2 单 KB,多 KB 走 kb_ids)。"""

    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_space_created", "space_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    space_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("spaces.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(128), default="新会话", server_default="新会话")
    meta: Mapped[dict] = mapped_column("metadata", JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Message(Base):
    """消息:assistant 消息存引用 JSONB(chunk_id + 摘录 + 分数),支撑引用溯源回链。

    agent_steps 存 Agent 模式(OPT-10)的工具调用轨迹(轮次/思考/调用/结果摘要),
    直检模式恒为 NULL。

    context_used_tokens / context_window_tokens(OPT-29)是**当轮快照**而不是现算值:
    分母所在模型的 context_tokens 可能被改被删,只有存下来才能复原进度条。
    """

    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_conversation_seq", "conversation_id", "seq"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    space_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("spaces.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    seq: Mapped[int] = mapped_column(Integer)
    citations: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    agent_steps: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # OPT-29:当轮窗口使用快照(仅 assistant 行;老行与 user 行恒 NULL)
    context_used_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_window_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ApiKey(Base):
    """程序化接入凭据(OPT-6,OPT-26 起绑定成员):只存 SHA-256 哈希,明文仅创建时回显一次。

    - `key_hash` 唯一索引:认证时按哈希等值查找,不需要遍历解密;
    - `member_user_id`:Key 以**该成员**身份行事(OPT-26)。签发者须有拉成员权限
      (Admin+);校验 = Key 本身有效 ∧ 绑定成员仍在空间,路由权限随成员角色实时变化
      (被移出 = Key 失效,降级 = 高权限路由被服务层拦下)。
      旧数据/旧语义回退 `created_by`(迁移 0012 已回填,列可空仅因 users SET NULL);
    - `created_by`:签发操作者(审计 actor),不再参与运行时身份;
    - `capabilities`:能力级授权(路由授权表 fail-closed),空数组 = 无任何能力;
    - `kb_ids`:二次收窄的知识库范围,空数组 = 全部 KB(仍受绑定成员身份约束);
    - `is_active`:「正在使用」分配标记(OPT-31),Admin+ 为成员设定,同成员至多
      一把生效(部分唯一索引);**仅管理语义,不参与认证**,未标记的未吊销 Key 仍可调用;
    - 吊销 = 置 `revoked_at`(保留行,审计与"最近使用"仍有据可查)。
    """

    __tablename__ = "api_keys"
    __table_args__ = (
        Index("ix_api_keys_space_created", "space_id", "created_at"),
        # 同空间同成员至多一把"正在使用"(分配标记,不参与认证热路径)
        Index(
            "uq_api_keys_one_active_per_member",
            "space_id",
            "member_user_id",
            unique=True,
            postgresql_where=text("is_active AND revoked_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    space_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("spaces.id", ondelete="CASCADE")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    member_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    key_hint: Mapped[str] = mapped_column(String(32))
    # 所属权限档位(OPT-41):能力与知识库范围由档位承载,Key 只是它的程序化凭据
    group_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("permission_groups.id", ondelete="RESTRICT")
    )
    # ⚠️ 以下三列 + is_active 是 OPT-41 待删的旧口径(迁移 0018 移除),现由 group 承载
    capabilities: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    kb_ids: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    # "正在使用"分配标记(OPT-31):Admin+ 设定,成员每把未吊销 Key 仍可调用
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PermissionGroup(Base):
    """权限档位(OPT-41 R6-b):一个空间内的"一种权限"。

    为什么需要它:成员身份只有角色阶梯(Viewer/Editor/Admin/Owner),**分不出
    "能检索哪些知识库**;而 OPT-41 之前那套"一把 Key 绑一个成员"让权限跟着凭据走,
    既不能复用也不能改。档位把两者拆开:
    - 权限(能力 + 知识库范围)只在 `permission_groups` 一处真相;
    - `api_keys` 退化成档位的**程序化凭据**(一个档位可多把,轮换互不影响);
    - 成员通过 `permission_group_members` 挂到档位上,`is_current` 标出他当前在用哪一档
      (取代 OPT-31 的 `api_keys.is_active`;每人每空间至多一档,部分唯一索引兜底)。

    `all_kbs = true` 表示"本空间全部知识库",**每次请求实时展开**,不落快照
    (存快照会让新建的库永远进不了范围)。`all_kbs = false` 时范围就是 `kb_ids`,
    空数组 = 一个都不允许(fail-closed,与 `capabilities` 同构)。

    档位名**不要求唯一**(回填期每把 Key 派生一档,而 Key 名本就可重名);管理按 id 定位。
    """

    __tablename__ = "permission_groups"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    space_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("spaces.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    capabilities: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    kb_ids: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    all_kbs: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PermissionGroupMember(Base):
    """成员 ↔ 档位的挂载关系(OPT-41)。

    - 一行 = "这个成员可以用这个档位";`is_current` 行 = 他**当前生效**的那一档;
    - 每人每空间至多一档 current(部分唯一索引 `uq_permission_group_one_current_per_member`);
    - 切换档位 = Admin+ 改 current 指针(成员不可自助,否则等于自己给自己提权);
    - 成员被移出空间时他的挂载行由服务层显式清理(移出空间 ≠ 删用户,不能靠 FK 级联)。
    """

    __tablename__ = "permission_group_members"
    __table_args__ = (
        UniqueConstraint("group_id", "user_id", name="uq_permission_group_member_user"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    group_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("permission_groups.id", ondelete="CASCADE")
    )
    # 冗余 space_id:租户谓词 + "每人每空间至多一档 current" 的唯一索引都要它
    space_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("spaces.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ModelProvider(Base):
    """自定义模型供应商(OPT-25,OPT-27 空间分层):替代随包 YAML,库内可管理。

    - `protocol` 是协议注册表键(core/model_protocols 注册),调用时按其分发;
    - `api_key_encrypted` 为 AES-GCM 密文(enc:v1:…),明文只在写入瞬间经过内存;
      空串 = 免密钥本地端点;`key_hint` 只供管理端辨识,任何接口不回显明文;
    - `enabled=false` 的供应商,其下模型一律不出现在可选清单(与旧 provider 语义一致);
    - `space_id` NULL = 全局层(superuser 管理),非空 = 该空间私有(成员 Admin+ 管理)。
    """

    __tablename__ = "model_providers"
    __table_args__ = (
        Index(
            "uq_model_providers_name_global",
            "name",
            unique=True,
            postgresql_where=text("space_id IS NULL"),
        ),
        Index(
            "uq_model_providers_name_space",
            "space_id",
            "name",
            unique=True,
            postgresql_where=text("space_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    space_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("spaces.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(64))
    protocol: Mapped[str] = mapped_column(
        String(32), default="openai_compatible", server_default="openai_compatible"
    )
    base_url: Mapped[str] = mapped_column(String(512))
    api_key_encrypted: Mapped[str] = mapped_column(Text, default="", server_default="")
    key_hint: Mapped[str] = mapped_column(String(32), default="", server_default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CustomModel(Base):
    """自定义模型条目(OPT-25,OPT-27 空间分层):kind 三分类与契约 ModelCatalogOut 一致。

    - `model_id` 是契约里的对外模型 id(如 "custom/qwen-plus"),String(128) 与
      messages.model_id / knowledge_bases.embedding_model 对齐;
    - 默认模型按 (层级, kind) 唯一:全局一个默认 + 每空间一个默认(partial unique);
      解析链"空间默认 → 全局默认 → 503"(embedding 的全局回落仅限 dev,见 catalog);
    - `space_id` NULL = 全局层,非空 = 空间私有。
    """

    __tablename__ = "custom_models"
    __table_args__ = (
        Index("ix_custom_models_kind_enabled", "kind", "enabled"),
        Index(
            "uq_custom_models_model_id_global",
            "model_id",
            unique=True,
            postgresql_where=text("space_id IS NULL"),
        ),
        Index(
            "uq_custom_models_model_id_space",
            "space_id",
            "model_id",
            unique=True,
            postgresql_where=text("space_id IS NOT NULL"),
        ),
        Index(
            "uq_custom_models_default_per_kind_global",
            "kind",
            unique=True,
            postgresql_where=text("is_default AND space_id IS NULL"),
        ),
        Index(
            "uq_custom_models_default_per_kind_space",
            "space_id",
            "kind",
            unique=True,
            postgresql_where=text("is_default AND space_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    space_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("spaces.id", ondelete="CASCADE"), nullable=True
    )
    model_id: Mapped[str] = mapped_column(String(128))
    provider_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("model_providers.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(16))
    model_name: Mapped[str] = mapped_column(String(128))
    dims: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SpaceModelSettings(Base):
    """空间模型总设置(OPT-27):一空间一行。

    - embedding 模型本体内置锁死(text-embedding-v3/1024 维,换模型=已入库向量作废),
      空间能配的只有它的钥匙:`embedding_key_encrypted` AES-GCM 密文,空串=未配置
      (dev 回落运维全局 Key;prod 直接 503,配置页红警示);
    - `rerank_enabled`:重排开关,"开了就用",未启用时检索零开销。
    """

    __tablename__ = "space_model_settings"

    space_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("spaces.id", ondelete="CASCADE"), primary_key=True
    )
    embedding_key_encrypted: Mapped[str] = mapped_column(
        Text, default="", server_default=""
    )
    embedding_key_hint: Mapped[str] = mapped_column(
        String(32), default="", server_default=""
    )
    rerank_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TokenUsageEvent(Base):
    """模型 token 用量明细(OPT-28):一次上游调用一行,只追加不改写。

    - `source=upstream` 表示三个数字来自厂商回传的 usage;`estimated` 表示厂商没回传、
      由 `estimate_tokens()` 按提示词/生成文本估出的近似值(前端可打 ≈ 角标)。
      两者口径不同、不可混算,所以必须随行带出;
    - `user_id` 在成员被删后置空(SET NULL):用量是既成事实,不因删人而消失。
      按成员聚合时这类行仍计入 `space_total`,但不进 `members[]`;
    - V1 不预聚合:明细行式 + 下面两个索引直接支撑"按空间按时间"与"按空间按成员"两类汇总。
    """

    __tablename__ = "token_usage_events"
    __table_args__ = (
        Index("ix_usage_space_created", "space_id", "created_at"),
        Index("ix_usage_space_user", "space_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    space_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # chat | embedding(见 domain.enums.UsageKind);列是裸字符串,后续加 rerank 不需改表
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), default="", server_default="")
    provider: Mapped[str] = mapped_column(String(64), default="", server_default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    source: Mapped[str] = mapped_column(
        String(16), default="upstream", server_default="upstream"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
