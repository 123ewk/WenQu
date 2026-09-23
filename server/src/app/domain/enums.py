"""领域常量:角色数值阶梯与审计动作类型(状态机/枚举一律用常量,禁止裸字符串,基准 01)。"""

from __future__ import annotations

from enum import IntEnum, StrEnum


class Role(IntEnum):
    """空间角色数值阶梯:数值越大权限越高,授权判断只做数值比较(基准 04)。"""

    VIEWER = 10
    EDITOR = 20
    ADMIN = 30
    OWNER = 40

    @classmethod
    def from_value(cls, value: int) -> Role:
        try:
            return cls(value)
        except ValueError as exc:
            raise ValueError(f"非法角色值: {value}") from exc


class AuditResult(StrEnum):
    """审计结果:denied = 有操作意图但被权限/认证拦下(安全审计的关键一半)。"""

    SUCCESS = "success"
    DENIED = "denied"


class UsageKind(StrEnum):
    """token 用量的计量类别(OPT-28):对哪条上游链路记的账。"""

    CHAT = "chat"
    EMBEDDING = "embedding"


class UsageSource(StrEnum):
    """用量数字的出处 —— 两种口径不可混算,所以必须随行落库。

    - `upstream`:厂商 `usage` 回传的原值(精确);
    - `estimated`:厂商不回传时由 `estimate_tokens()` 估算(近似,前端应打 ≈ 角标)。
    """

    UPSTREAM = "upstream"
    ESTIMATED = "estimated"


class AuditAction(StrEnum):
    """审计动作全分类;新增动作必须在此注册(40+ 分类为长期目标,见基准 04)。"""

    AUTH_REGISTER = "auth.register"
    AUTH_LOGIN_SUCCESS = "auth.login_success"
    AUTH_LOGIN_FAILED = "auth.login_failed"
    AUTH_LOGOUT = "auth.logout"
    PASSWORD_CHANGED = "user.password_changed"
    USER_UPDATED = "user.updated"
    AVATAR_UPLOADED = "user.avatar_uploaded"
    AVATAR_DELETED = "user.avatar_deleted"
    SPACE_CREATED = "space.created"
    SPACE_UPDATED = "space.updated"
    SPACE_DELETED = "space.deleted"
    MEMBER_ADDED = "space.member_added"
    MEMBER_REMOVED = "space.member_removed"
    MEMBER_ROLE_CHANGED = "space.member_role_changed"
    KB_CREATED = "kb.created"
    KB_UPDATED = "kb.updated"
    KB_DELETED = "kb.deleted"
    DOCUMENT_UPLOADED = "document.uploaded"
    DOCUMENT_DELETED = "document.deleted"
    DOCUMENT_REPARSED = "document.reparsed"
    API_KEY_CREATED = "api_key.created"
    API_KEY_REVOKED = "api_key.revoked"
    API_KEY_ACTIVATED = "api_key.activated"  # OPT-31 设为"正在使用"(成员互斥切换)
    # OPT-44:行没了,归因只能靠这条快照,所以彻底删除必须逐把留痕
    API_KEY_PURGED = "api_key.purged"  # 已吊销凭据的台账行被彻底删除(不可逆)
    # OPT-41 权限档位:权限只有一处真相,改一次全档生效 → 每一次改动都要能回答"谁改的、改前是什么"
    PERMISSION_GROUP_CREATED = "permission_group.created"
    PERMISSION_GROUP_UPDATED = "permission_group.updated"  # 能力/知识库范围(= N8"修复权限")
    PERMISSION_GROUP_DELETED = "permission_group.deleted"
    PERMISSION_GROUP_MEMBER_ASSIGNED = "permission_group.member_assigned"
    PERMISSION_GROUP_MEMBER_UNASSIGNED = "permission_group.member_unassigned"
    PERMISSION_GROUP_CURRENT_SET = "permission_group.current_set"  # 换档 = 权限开关,不是台账标记
    # OPT-25 自定义模型管理(superuser 操作,全部留痕)
    MODEL_PROVIDER_CREATED = "model_provider.created"
    MODEL_PROVIDER_UPDATED = "model_provider.updated"
    MODEL_PROVIDER_DELETED = "model_provider.deleted"
    MODEL_CREATED = "model.created"
    MODEL_UPDATED = "model.updated"
    MODEL_DELETED = "model.deleted"
    # OPT-27 空间级模型配置:内置 embedding 的钥匙、重排开关、连通探测(动作与全局层同款,
    # 空间层靠 AuditLog.space_id 区分,不另立动作名)
    MODEL_SETTING_EMBEDDING_KEY_UPDATED = "model_setting.embedding_key.updated"
    MODEL_SETTING_RERANK_UPDATED = "model_setting.rerank.updated"
    MODEL_TESTED = "model.tested"
    ACCESS_DENIED = "access.denied"  # 越权尝试(路由级,记录真实意图与目标)


class ApiCapability(StrEnum):
    """API Key 能力(能力级授权的最小单位;字符串值进契约,不可随意改)。

    取值与原型页 07 的勾选项一一对应(两项,不是三个:"对话检索"同时覆盖 /ask 与
    /search)。路由授权表(见 `app/api/api_key_auth.py`)把"能力 → 允许的路由"写死;
    表里没有的路由,用 Key 访问一律拒绝(fail-closed)。
    """

    CHAT = "chat"  # 对话检索:/ask 流式问答 + /search 检索
    DOCUMENTS = "documents"  # 文档管理:上传、删除文档、触发重新解析


class DocumentStatus(StrEnum):
    """文档生命周期状态机:pending → parsing → chunking → embedding → completed / failed(基准 01)。

    流转只允许顺位推进;任何一步失败跳 failed(终态,重解析走新任务)。
    """

    PENDING = "pending"
    PARSING = "parsing"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskStatus(StrEnum):
    """DB 队列任务状态;dead=死信(超过重试上限,人工介入)。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD = "dead"


class TaskType(StrEnum):
    """后台任务类型注册表;新任务类型必须在此登记。"""

    INGEST_DOCUMENT = "ingest_document"  # 解析 → 分块 → 向量化 → 索引
