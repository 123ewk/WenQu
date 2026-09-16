"""FastAPI 依赖装配:请求级仓储/服务构建 + 认证守卫(构造注入,基准 01)。"""

from __future__ import annotations

import uuid

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.application.repository.audit import AuditRepositoryImpl
from app.application.repository.conversations import (
    ConversationRepositoryImpl,
    MessageRepositoryImpl,
)
from app.application.repository.knowledge import (
    ChunkQueryRepositoryImpl,
    ChunkRepositoryImpl,
    DocumentRepositoryImpl,
    KnowledgeBaseRepositoryImpl,
)
from app.application.repository.retrieval import RetrievalRepositoryImpl
from app.application.repository.spaces import SpaceRepositoryImpl
from app.application.repository.tasks import TaskRepositoryImpl
from app.application.repository.tokens import RefreshTokenRepositoryImpl
from app.application.repository.users import UserRepositoryImpl
from app.application.service.auth import AuthService
from app.application.service.knowledge import KnowledgeService
from app.application.service.profile import ProfileService
from app.application.service.qa import QAService
from app.application.service.retrieval import RetrievalService
from app.application.service.spaces import SpaceService
from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import AppError, ErrorCode
from app.core.model_catalog import ModelCatalog
from app.core.model_client import ChatClient, EmbeddingClient
from app.core.security import decode_access_token
from app.core.storage import MemoryStorage, MinioStorage, ObjectStorage
from app.domain.interfaces import (
    AuditRepository,
    ChatGateway,
    ChunkQueryRepository,
    ChunkRepository,
    ConversationRepository,
    DocumentRepository,
    EmbeddingGateway,
    KnowledgeBaseRepository,
    MessageRepository,
    RefreshTokenRepository,
    RetrievalRepository,
    SpaceRepository,
    TaskRepository,
    UserRepository,
)
from app.domain.models import User

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
) -> SpaceService:
    return SpaceService(spaces, users, audit)


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


def get_knowledge_service(
    kbs: KnowledgeBaseRepository = Depends(get_kb_repository),
    documents: DocumentRepository = Depends(get_document_repository),
    spaces: SpaceRepository = Depends(get_space_repository),
    audit: AuditRepository = Depends(get_audit_repository),
    storage: ObjectStorage = Depends(get_storage),
    tasks: TaskRepository = Depends(get_task_repository),
    chunks: ChunkQueryRepository = Depends(get_chunk_query_repository),
) -> KnowledgeService:
    return KnowledgeService(
        kbs, documents, spaces, audit, storage, tasks, chunks, get_settings().upload_max_mb
    )


def get_retrieval_repository(db: Session = Depends(get_db)) -> RetrievalRepository:
    return RetrievalRepositoryImpl(db)


def get_embedding_gateway() -> EmbeddingGateway:
    """向量化网关:检索与流水线共用模型层;测试用 dependency_overrides 换假实现。"""
    settings = get_settings()
    return EmbeddingClient(ModelCatalog.load(), settings)


def get_retrieval_service(
    chunks: RetrievalRepository = Depends(get_retrieval_repository),
    kbs: KnowledgeBaseRepository = Depends(get_kb_repository),
    documents: DocumentRepository = Depends(get_document_repository),
    spaces: SpaceRepository = Depends(get_space_repository),
    embedder: EmbeddingGateway = Depends(get_embedding_gateway),
) -> RetrievalService:
    return RetrievalService(
        chunks, kbs, documents, spaces, embedder,
        min_vector_score=get_settings().retrieval_min_score,
    )


def get_conversation_repository(db: Session = Depends(get_db)) -> ConversationRepository:
    return ConversationRepositoryImpl(db)


def get_message_repository(db: Session = Depends(get_db)) -> MessageRepository:
    return MessageRepositoryImpl(db)


def get_chat_gateway() -> ChatGateway:
    """对话网关:检索与流水线共用模型层;测试用 dependency_overrides 换假实现。"""
    settings = get_settings()
    return ChatClient(ModelCatalog.load(), settings)


def get_qa_service(
    conversations: ConversationRepository = Depends(get_conversation_repository),
    messages: MessageRepository = Depends(get_message_repository),
    spaces: SpaceRepository = Depends(get_space_repository),
    retrieval: RetrievalService = Depends(get_retrieval_service),
    chat: ChatGateway = Depends(get_chat_gateway),
) -> QAService:
    return QAService(conversations, messages, spaces, retrieval, chat)


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
) -> ProfileService:
    return ProfileService(users, storage, get_settings().avatar_max_mb)


def get_client_ip(request: Request) -> str:
    # M2 起:配置 trusted proxies 显式网段后再解析 X-Forwarded-For(翻转项 #8);
    # 当前直接取直连地址,防 XFF 伪造绕过审计与限流
    return request.client.host if request.client else ""
