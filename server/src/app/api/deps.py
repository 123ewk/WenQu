"""FastAPI 依赖装配:请求级仓储/服务构建 + 认证守卫(构造注入,基准 01)。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from functools import lru_cache

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.api_key_auth import (
    build_api_key_service,
    get_api_key_principal,
    resolve_member_route_capability,
    route_in_dual_list,
)
from app.application.agent.loop import ToolCallingChat
from app.application.repository.api_keys import ApiKeyRepositoryImpl
from app.application.repository.audit import AuditRepositoryImpl
from app.application.repository.conversations import (
    ConversationRepositoryImpl,
    MessageRepositoryImpl,
)
from app.application.repository.knowledge import (
    ChunkQueryRepositoryImpl,
    ChunkRepositoryImpl,
    DocumentRepositoryImpl,
    KbStatsRepositoryImpl,
    KnowledgeBaseRepositoryImpl,
)
from app.application.repository.langfuse_config import SpaceLangfuseConfigRepositoryImpl
from app.application.repository.memories import MemoryRepositoryImpl
from app.application.repository.memory_proposals import MemoryProposalRepositoryImpl
from app.application.repository.permission_group_toggle import (
    SpacePermissionGroupToggleRepositoryImpl,
)
from app.application.repository.permission_groups import PermissionGroupRepositoryImpl
from app.application.repository.retrieval import RetrievalRepositoryImpl
from app.application.repository.spaces import SpaceRepositoryImpl
from app.application.repository.tasks import TaskRepositoryImpl
from app.application.repository.tokens import RefreshTokenRepositoryImpl
from app.application.repository.users import UserRepositoryImpl
from app.application.service.api_keys import ApiKeyService
from app.application.service.auth import AuthService
from app.application.service.effective_permission import (
    EffectivePermission,
    permission_of_member,
)
from app.application.service.knowledge import KnowledgeService
from app.application.service.langfuse_config import LangfuseConfigService
from app.application.service.memory import MemoryService
from app.application.service.model_admin import ModelAdminService
from app.application.service.permission_group_toggle import (
    PermissionGroupToggleService,
    space_toggle_enabled,
)
from app.application.service.permission_groups import PermissionGroupService
from app.application.service.profile import ProfileService
from app.application.service.qa import BackgroundDb, QAService
from app.application.service.retrieval import RetrievalService
from app.application.service.space_models import SpaceModelAdminService
from app.application.service.spaces import SpaceService
from app.application.service.token_usage import UsageService
from app.application.service.usage import UsageRecorder
from app.application.streaming import StreamSupervisor
from app.core.client_ip import resolve_client_ip
from app.core.config import get_settings
from app.core.db import get_db, get_session_factory
from app.core.errors import AppError, ErrorCode
from app.core.event_log import EventLog, MemoryEventLog
from app.core.model_catalog import ModelCatalog
from app.core.model_client import ChatClient, EmbeddingClient, RerankClient
from app.core.model_usage import ChatTurnUsage
from app.core.security import decode_access_token
from app.core.storage import MemoryStorage, MinioStorage, ObjectStorage
from app.domain.enums import Role
from app.domain.interfaces import (
    AuditRepository,
    ChunkQueryRepository,
    ChunkRepository,
    ConversationRepository,
    DocumentRepository,
    EmbeddingGateway,
    KbStatsRepository,
    KnowledgeBaseRepository,
    MemoryProposalRepository,
    MemoryRepository,
    MessageRepository,
    PermissionGroupRepository,
    RefreshTokenRepository,
    RerankGateway,
    RetrievalRepository,
    SpaceRepository,
    TaskRepository,
    UserRepository,
)
from app.domain.models import ApiKey, Membership, User

_bearer_scheme = HTTPBearer(auto_error=False)


# ---------------------------- 认证守卫 ----------------------------


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Bearer access token → 当前用户;任何缺失/无效都归一为 401。"""
    if credentials is None:
        raise AppError(ErrorCode.AUTH_REQUIRED, "请先登录", http_status=401)
    payload = decode_access_token(credentials.credentials)
    user = db.get(User, uuid.UUID(str(payload["sub"])))
    if user is None:
        raise AppError(ErrorCode.AUTH_REQUIRED, "登录状态已失效", http_status=401)
    return user


def get_current_superuser(user: User = Depends(get_current_user)) -> User:
    """平台管理员守卫(OPT-25):模型管理是全局配置,只认 is_superuser 的 JWT 身份。"""
    if not user.is_superuser:
        raise AppError(ErrorCode.FORBIDDEN, "仅平台管理员可管理模型", http_status=403)
    return user


def get_space_toggle_manager(
    space_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    """空间级权限挡位功能开关的管理者守卫(OPT-48 v2):该空间 Admin+(role≥30)或 superuser。

    不复用 get_current_superuser 的文案:那条「仅平台管理员可管理模型」是模型管理专用,
    拨档位开关被拒时显示它会误导。粒度是本空间 —— 管谁的空间拨谁的开关(2026-09-26 拍板)。
    """
    if user.is_superuser:
        return user
    membership = db.scalar(
        select(Membership).where(
            Membership.space_id == space_id, Membership.user_id == user.id
        )
    )
    if membership is None or membership.role < Role.ADMIN.value:
        raise AppError(
            ErrorCode.FORBIDDEN,
            "仅空间管理员(Admin)及以上可拨动本空间的权限挡位功能开关",
            http_status=403,
        )
    return user


def ensure_space_toggle_on(
    space_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> None:
    """配挡时机(OPT-48 v2,2026-09-26 用户拍板,字面口径):开关打开后才能分配/换当前。

    挂在 permission-groups 的 分配/换当前 两个写路由上;没开 → 409(角色够,是功能状态没到位)。
    解除分配不拦:清理动作永远放行(逃生口)。
    """
    row = SpacePermissionGroupToggleRepositoryImpl(db).get_row(space_id)
    if row is None or not row.enabled:
        raise AppError(
            ErrorCode.PERMISSION_GROUP_TOGGLE_OFF,
            "本空间的权限挡位功能开关未打开:请先在权限档位页打开开关,再给成员分配挡位",
            http_status=409,
        )


@dataclass(frozen=True)
class Caller:
    """统一调用主体:人(JWT)或程序(API Key,以归因成员身份行事)。

    `perm` = 这次调用的**生效权限**(OPT-41 N4 合成器的唯一输出):
    - 程序链路:一定非 None(一直如此),来自 Key 所属档位;
    - 人链路:只在「双凭据强制清单」内的路由、且灰度开关打开时才非 None;
      None = 这条路由不看档位,只看成员角色(§4.2 边界,管理接口据此不受影响)。
    路由要收窄知识库一律读 `caller.perm`,不要再写 `if caller.api_key is not None`。
    """

    user: User
    api_key: ApiKey | None
    perm: EffectivePermission | None = None


def get_current_actor(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
) -> Caller:
    """双认证入口:优先 X-API-Key(程序化调用),否则 Bearer JWT(人)。

    - API Key 链路:认证 + 路由授权表 + 生效权限都在 get_api_key_principal 里完成;
      Key 只在其授权表登记的路由上可用;
    - 人链路:按 §4.2 只给「双凭据强制清单」内的路由解析档位(见 _member_perm_for);
    - 只带 JWT / 只带 Key / 都带(Key 优先)三种情况行为明确,不静默混合。
    """
    if request.headers.get("x-api-key", "").strip():
        principal = get_api_key_principal(request, db)
        user = db.get(User, principal.actor_id)
        if user is None:  # 创建者被删除;authenticate 已挡 created_by 为空的情况
            raise AppError(ErrorCode.API_KEY_INVALID, "API Key 无效或已吊销", http_status=401)
        return Caller(user=user, api_key=principal.api_key, perm=principal.perm)
    return Caller(
        user=get_current_user(credentials, db),
        api_key=None,
        perm=_member_perm_for(request, credentials, db),
    )


def _member_perm_for(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
    db: Session,
) -> EffectivePermission | None:
    """人这条链路的生效档位:服务端按 JWT 成员解析,明文永不进浏览器(N1)。

    返回 None 的情况都刻意而为(OPT-48 v2 口径,2026-09-26 用户拍板):
    - 非空间路由 —— 开关只作用于空间内路由;
    - 该空间的「权限挡位功能开关」未打开(表无行 = 从未拨过 = 关)—— 全空间只看角色;
    - 路由不在「双凭据强制清单」内 —— 核心功能清单口径:成员/模型配置/审计/用量等
      管理页只看角色,否则 Admin 在还没配档时会连建档位的入口都没有,空间失去管理。
    """
    space_id = _path_space_id(request)
    if space_id is None:
        return None
    if not space_toggle_enabled(db, space_id):
        return None
    if not route_in_dual_list(request.url.path, request.method):
        return None
    user = get_current_user(credentials, db)
    perm = permission_of_member(
        PermissionGroupRepositoryImpl(db), SpaceRepositoryImpl(db), space_id, user.id
    )
    capability = resolve_member_route_capability(request.url.path, request.method)
    if capability is not None:
        perm.require_capability(capability)
    return perm


def _path_space_id(request: Request) -> uuid.UUID | None:
    """路径里的 {space_id}(已匹配路由的 path_params,不是查询串);取不到或畸形 → None。"""
    raw = request.path_params.get("space_id")
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return None


# ---------------------------- 仓储与服务 ----------------------------


def get_user_repository(db: Session = Depends(get_db)) -> UserRepository:
    return UserRepositoryImpl(db)


def get_token_repository(db: Session = Depends(get_db)) -> RefreshTokenRepository:
    return RefreshTokenRepositoryImpl(db)


def get_space_repository(db: Session = Depends(get_db)) -> SpaceRepository:
    return SpaceRepositoryImpl(db)


def get_audit_repository(db: Session = Depends(get_db)) -> AuditRepository:
    return AuditRepositoryImpl(db)


def get_auth_service(
    users: UserRepository = Depends(get_user_repository),
    tokens: RefreshTokenRepository = Depends(get_token_repository),
    spaces: SpaceRepository = Depends(get_space_repository),
    audit: AuditRepository = Depends(get_audit_repository),
) -> AuthService:
    return AuthService(users, tokens, spaces, audit, get_settings())


def get_space_service(
    spaces: SpaceRepository = Depends(get_space_repository),
    users: UserRepository = Depends(get_user_repository),
    audit: AuditRepository = Depends(get_audit_repository),
    db: Session = Depends(get_db),
) -> SpaceService:
    # mounts:移出成员时要一并清掉他的档位挂载(OPT-41),默认不保留
    return SpaceService(spaces, users, audit, mounts=PermissionGroupRepositoryImpl(db))


# ---------------------------- M2 知识域 ----------------------------


def get_kb_repository(db: Session = Depends(get_db)) -> KnowledgeBaseRepository:
    return KnowledgeBaseRepositoryImpl(db)


def get_document_repository(db: Session = Depends(get_db)) -> DocumentRepository:
    return DocumentRepositoryImpl(db)


def get_storage() -> ObjectStorage:
    """生产/dev 走 MinIO;测试用 dependency_overrides 换 MemoryStorage(CI 无 MinIO)。"""
    settings = get_settings()
    return MinioStorage(
        settings.minio_endpoint,
        settings.minio_access_key,
        settings.minio_secret_key,
        settings.minio_bucket,
        settings.minio_secure,
    )


def get_memory_storage() -> ObjectStorage:
    return MemoryStorage()


def get_task_repository(db: Session = Depends(get_db)) -> TaskRepository:
    return TaskRepositoryImpl(db)


def get_chunk_repository(db: Session = Depends(get_db)) -> ChunkRepository:
    return ChunkRepositoryImpl(db)


def get_chunk_query_repository(db: Session = Depends(get_db)) -> ChunkQueryRepository:
    return ChunkQueryRepositoryImpl(db)


def get_kb_stats_repository(db: Session = Depends(get_db)) -> KbStatsRepository:
    return KbStatsRepositoryImpl(db)


def get_knowledge_service(
    kbs: KnowledgeBaseRepository = Depends(get_kb_repository),
    documents: DocumentRepository = Depends(get_document_repository),
    spaces: SpaceRepository = Depends(get_space_repository),
    audit: AuditRepository = Depends(get_audit_repository),
    storage: ObjectStorage = Depends(get_storage),
    tasks: TaskRepository = Depends(get_task_repository),
    chunks: ChunkQueryRepository = Depends(get_chunk_query_repository),
    stats: KbStatsRepository = Depends(get_kb_stats_repository),
) -> KnowledgeService:
    return KnowledgeService(
        kbs, documents, spaces, audit, storage, tasks, chunks, stats,
        get_settings().upload_max_mb,
    )


def get_retrieval_repository(db: Session = Depends(get_db)) -> RetrievalRepository:
    return RetrievalRepositoryImpl(db)


def get_request_space_id(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> uuid.UUID | None:
    """本请求的模型解析空间(OPT-27):路径 {space_id} 优先,其次 access token 的活动空间。

    取不到返回 None = 只看全局层;畸形值同样按 None 处理(路径参数合法性由路由自身把关)。
    """
    raw = request.path_params.get("space_id")
    if raw:
        try:
            return uuid.UUID(str(raw))
        except ValueError:
            return None
    if credentials is None:
        return None
    try:
        payload = decode_access_token(credentials.credentials)
    except AppError:
        return None
    sid = payload.get("sid")
    if not sid:
        return None
    try:
        return uuid.UUID(str(sid))
    except ValueError:
        return None


def get_usage_recorder() -> UsageRecorder:
    """用量记账入口(OPT-28):自己开会话写,绝不打断主链路。

    不做进程级单例 —— 它持有 session_factory,而测试会换库(lru_cache 会把记账
    钉在第一个库上)。对象本身只包一个函数引用,每请求新建零成本。
    """
    return UsageRecorder(get_session_factory())


def get_embedding_gateway(
    db: Session = Depends(get_db),
    space_id: uuid.UUID | None = Depends(get_request_space_id),
    caller: Caller = Depends(get_current_actor),
) -> EmbeddingGateway:
    """向量化网关:目录每请求从库读(自定义模型即时生效);测试用 dependency_overrides 换假实现。

    OPT-27:快照带空间上下文(空间层模型优先;embedding 空间 Key→内置,dev 才回落全局)。
    OPT-28:归属空间已知时套上计量外壳(取不到空间 = 无租户上下文,不记账)。
    """
    client = EmbeddingClient(ModelCatalog.load_from_db(db, space_id))
    if space_id is None:
        return client
    return get_usage_recorder().embedding(client, space_id, caller.user.id)


def get_memory_repository(db: Session = Depends(get_db)) -> MemoryRepository:
    return MemoryRepositoryImpl(db)


def get_memory_proposal_repository(
    db: Session = Depends(get_db),
) -> MemoryProposalRepository:
    return MemoryProposalRepositoryImpl(db)


def get_memory_service(
    memories: MemoryRepository = Depends(get_memory_repository),
    spaces: SpaceRepository = Depends(get_space_repository),
    embedder: EmbeddingGateway = Depends(get_embedding_gateway),
    proposals: MemoryProposalRepository = Depends(get_memory_proposal_repository),
) -> MemoryService:
    return MemoryService(memories, spaces, embedder, get_settings(), proposals)


def _reranker_for(
    session: Session, space_id: uuid.UUID | None
) -> RerankGateway | None:
    """空间开了重排开关才建网关:未启用时检索链路连一次目录解析都不为它多花(N-1 零开销)。"""
    catalog = ModelCatalog.load_from_db(session, space_id)
    return RerankClient(catalog) if catalog.rerank_enabled else None


def get_rerank_gateway(
    db: Session = Depends(get_db),
    space_id: uuid.UUID | None = Depends(get_request_space_id),
) -> RerankGateway | None:
    return _reranker_for(db, space_id)


def get_retrieval_service(
    chunks: RetrievalRepository = Depends(get_retrieval_repository),
    kbs: KnowledgeBaseRepository = Depends(get_kb_repository),
    documents: DocumentRepository = Depends(get_document_repository),
    spaces: SpaceRepository = Depends(get_space_repository),
    embedder: EmbeddingGateway = Depends(get_embedding_gateway),
    reranker: RerankGateway | None = Depends(get_rerank_gateway),
) -> RetrievalService:
    return RetrievalService(
        chunks, kbs, documents, spaces, embedder,
        min_vector_score=get_settings().retrieval_min_score,
        reranker=reranker,
    )


def get_conversation_repository(db: Session = Depends(get_db)) -> ConversationRepository:
    return ConversationRepositoryImpl(db)


def get_message_repository(db: Session = Depends(get_db)) -> MessageRepository:
    return MessageRepositoryImpl(db)


def get_api_key_service(db: Session = Depends(get_db)) -> ApiKeyService:
    return build_api_key_service(db)


def get_permission_group_repository(
    db: Session = Depends(get_db),
) -> PermissionGroupRepository:
    return PermissionGroupRepositoryImpl(db)


def get_permission_group_service(
    db: Session = Depends(get_db),
    groups: PermissionGroupRepository = Depends(get_permission_group_repository),
    spaces: SpaceRepository = Depends(get_space_repository),
    kbs: KnowledgeBaseRepository = Depends(get_kb_repository),
    audit: AuditRepository = Depends(get_audit_repository),
    users: UserRepository = Depends(get_user_repository),
) -> PermissionGroupService:
    """权限档位服务(OPT-41):档位是能力与知识库范围的唯一来源,Key 只引用它。

    `api_keys` 依赖是给"删档位前先看还有几把凭据挂着"用的(不做静默级联)。
    """
    return PermissionGroupService(
        groups, spaces, kbs, audit, ApiKeyRepositoryImpl(db), users
    )


def get_permission_group_toggle_service(
    db: Session = Depends(get_db),
    audit: AuditRepository = Depends(get_audit_repository),
    users: UserRepository = Depends(get_user_repository),
) -> PermissionGroupToggleService:
    """空间级权限挡位功能开关服务(OPT-48 v2):db 只喂缓存读值路径,行与统计走仓储。"""
    return PermissionGroupToggleService(
        db, SpacePermissionGroupToggleRepositoryImpl(db), users, audit
    )


def get_langfuse_config_service(
    db: Session = Depends(get_db),
    audit: AuditRepository = Depends(get_audit_repository),
    users: UserRepository = Depends(get_user_repository),
) -> LangfuseConfigService:
    """空间级 Langfuse 配置与 trace 查看服务(OPT-49 v2);空间 Admin+ 守卫在路由层。"""
    return LangfuseConfigService(db, SpaceLangfuseConfigRepositoryImpl(db), users, audit)


def get_space_langfuse_manager(
    space_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    """空间级 Langfuse 配置的管理者守卫(OPT-49 v2):该空间 Admin+(role≥30)或 superuser。"""
    if user.is_superuser:
        return user
    membership = db.scalar(
        select(Membership).where(
            Membership.space_id == space_id, Membership.user_id == user.id
        )
    )
    if membership is None or membership.role < Role.ADMIN.value:
        raise AppError(
            ErrorCode.FORBIDDEN,
            "仅空间管理员(Admin)及以上可管理本空间的 Langfuse 配置",
            http_status=403,
        )
    return user


def get_model_catalog(
    db: Session = Depends(get_db),
    space_id: uuid.UUID | None = Depends(get_request_space_id),
) -> ModelCatalog:
    """模型目录只读快照:公开清单、建 KB 解析默认嵌入模型、管理端共用(OPT-27 空间分层)。"""
    return ModelCatalog.load_from_db(db, space_id)


def get_model_admin_service(db: Session = Depends(get_db)) -> ModelAdminService:
    return ModelAdminService(db)


def get_usage_service(
    db: Session = Depends(get_db),
    spaces: SpaceRepository = Depends(get_space_repository),
) -> UsageService:
    """token 用量汇总(OPT-28 读侧):明细表 + 空间花名册,门槛在服务内判。"""
    return UsageService(db, spaces)


def get_space_model_admin_service(
    space_id: uuid.UUID,
    db: Session = Depends(get_db),
    spaces: SpaceRepository = Depends(get_space_repository),
) -> SpaceModelAdminService:
    """空间模型配置服务(OPT-27):实例即绑定空间,服务内不再传 space_id。

    `space_id` 在此声明为路径参数,非法值在依赖解析阶段就 422,不会退化成
    "绑定到 None = 全局层"那种越权。
    """
    return SpaceModelAdminService(db, space_id, spaces)


def get_chat_turn_usage() -> ChatTurnUsage:
    """本轮上下文窗口用量的收集器(OPT-29 进度条):每请求一份,绝不能做进程级单例。

    缓存它等于让 A 用户的进度条读到 B 用户的用量,且列表随请求无界增长。
    FastAPI 会缓存同请求内的子依赖,所以网关与 QAService 拿到的是同一个实例。
    """
    return ChatTurnUsage()


def get_chat_gateway(
    db: Session = Depends(get_db),
    space_id: uuid.UUID | None = Depends(get_request_space_id),
    caller: Caller = Depends(get_current_actor),
    turn_usage: ChatTurnUsage = Depends(get_chat_turn_usage),
) -> ToolCallingChat:
    """对话网关:问答直检走 chat_stream,Agent 模式(OPT-10)走 chat_stream_tools;
    目录每请求从库读(OPT-25),OPT-27 起快照带空间上下文(空间默认 chat 优先);
    OPT-28 起套计量外壳(每次上游调用一行用量,Agent 的多个轮次各记一行);
    OPT-29 起外壳同时喂上下文窗口收集器;
    测试用 dependency_overrides 换假实现。"""
    client = ChatClient(ModelCatalog.load_from_db(db, space_id))
    if space_id is None:
        return client
    return get_usage_recorder().chat(client, space_id, caller.user.id, turn=turn_usage)


@lru_cache
def get_event_log() -> EventLog:
    """进程级单例(OPT-3):后台泵写入,ask 响应与续流路由读取共用一本日志。"""
    return MemoryEventLog(idle_ttl_seconds=get_settings().stream_log_ttl_seconds)


@lru_cache
def get_stream_supervisor() -> StreamSupervisor:
    """进程级单例:登记进行中的生成,执行断线宽限策略(宽限秒数可配)。"""
    return StreamSupervisor(grace_seconds=get_settings().stream_grace_seconds)


def get_qa_service(
    db: Session = Depends(get_db),
    conversations: ConversationRepository = Depends(get_conversation_repository),
    messages: MessageRepository = Depends(get_message_repository),
    spaces: SpaceRepository = Depends(get_space_repository),
    retrieval: RetrievalService = Depends(get_retrieval_service),
    chat: ToolCallingChat = Depends(get_chat_gateway),
    embedder: EmbeddingGateway = Depends(get_embedding_gateway),
    turn_usage: ChatTurnUsage = Depends(get_chat_turn_usage),
) -> QAService:
    return QAService(
        conversations,
        messages,
        spaces,
        retrieval,
        chat,
        turn_usage=turn_usage,
        event_log=get_event_log(),
        supervisor=get_stream_supervisor(),
        background_db=BackgroundDb(
            get_session_factory(),
            embedder,
            get_settings().retrieval_min_score,
            reranker_factory=_reranker_for,
        ),
        db=db,
        # 流水线阶段序由配置决定(OPT-5);未知名在 build_stages 启动即失败
        pipeline=[name.strip() for name in get_settings().qa_pipeline.split(",") if name.strip()],
        agent_max_rounds=get_settings().agent_max_rounds,
    )


def get_current_space_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> uuid.UUID | None:
    """当前活动空间 = access token 的 sid 声明(缺口 #7)。

    未选空间时为 None;无效令牌交给 get_current_user 统一报 401,这里不重复报错。
    """
    if credentials is None:
        return None
    try:
        payload = decode_access_token(credentials.credentials)
    except AppError:
        return None
    raw = payload.get("sid")
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return None


def get_profile_service(
    users: UserRepository = Depends(get_user_repository),
    storage: ObjectStorage = Depends(get_storage),
    audit: AuditRepository = Depends(get_audit_repository),
) -> ProfileService:
    return ProfileService(users, storage, audit, get_settings().avatar_max_mb)


def get_client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    return resolve_client_ip(peer, request.headers, get_settings().trusted_proxies)
