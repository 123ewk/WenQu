"""FastAPI 依赖装配:请求级仓储/服务构建 + 认证守卫(构造注入,基准 01)。"""

from __future__ import annotations

import uuid

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.application.repository.audit import AuditRepositoryImpl
from app.application.repository.knowledge import (
    ChunkRepositoryImpl,
    DocumentRepositoryImpl,
    KnowledgeBaseRepositoryImpl,
)
from app.application.repository.spaces import SpaceRepositoryImpl
from app.application.repository.tasks import TaskRepositoryImpl
from app.application.repository.tokens import RefreshTokenRepositoryImpl
from app.application.repository.users import UserRepositoryImpl
from app.application.service.auth import AuthService
from app.application.service.knowledge import KnowledgeService
from app.application.service.spaces import SpaceService
from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import AppError, ErrorCode
from app.core.security import decode_access_token
from app.core.storage import MemoryStorage, MinioStorage, ObjectStorage
from app.domain.interfaces import (
    AuditRepository,
    ChunkRepository,
    DocumentRepository,
    KnowledgeBaseRepository,
    RefreshTokenRepository,
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


def get_knowledge_service(
    kbs: KnowledgeBaseRepository = Depends(get_kb_repository),
    documents: DocumentRepository = Depends(get_document_repository),
    spaces: SpaceRepository = Depends(get_space_repository),
    audit: AuditRepository = Depends(get_audit_repository),
    storage: ObjectStorage = Depends(get_storage),
    tasks: TaskRepository = Depends(get_task_repository),
) -> KnowledgeService:
    return KnowledgeService(
        kbs, documents, spaces, audit, storage, tasks, get_settings().upload_max_mb
    )


def get_client_ip(request: Request) -> str:
    # M2 起:配置 trusted proxies 显式网段后再解析 X-Forwarded-For(翻转项 #8);
    # 当前直接取直连地址,防 XFF 伪造绕过审计与限流
    return request.client.host if request.client else ""
