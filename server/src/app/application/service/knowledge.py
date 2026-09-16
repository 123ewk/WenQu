"""知识库与文档服务:空间下的内容容器(RBAC 守卫语义与 SpaceService 一致)。

守卫语义(基准 04):
- 非成员/空间不存在 → 404 SPACE_NOT_FOUND(防枚举);
- 角色不足 → 403;门槛:查看=任意成员,KB/文档增改=EDITOR+,删 KB=ADMIN+。
- KB 删除级联:DB 靠 FK CASCADE,对象存储按 {space_id}/{kb_id}/ 前缀清理(无孤儿)。
"""

from __future__ import annotations

import uuid
from pathlib import PurePosixPath

from app.core.errors import AppError, ErrorCode
from app.core.storage import ObjectStorage
from app.domain.enums import AuditAction, DocumentStatus, Role
from app.domain.interfaces import (
    AuditRepository,
    DocumentRepository,
    KnowledgeBaseRepository,
    SpaceRepository,
)
from app.domain.models import AuditLog, Document, KnowledgeBase, User

SUPPORTED_FORMATS: dict[str, str] = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".pptx": "pptx",
    ".md": "md",
    ".markdown": "md",
    ".txt": "txt",
}


class KnowledgeService:
    def __init__(
        self,
        kbs: KnowledgeBaseRepository,
        documents: DocumentRepository,
        spaces: SpaceRepository,
        audit: AuditRepository,
        storage: ObjectStorage,
        upload_max_mb: int = 50,
    ) -> None:
        self._kbs = kbs
        self._documents = documents
        self._spaces = spaces
        self._audit = audit
        self._storage = storage
        self._upload_max_bytes = upload_max_mb * 1024 * 1024

    # ---------------------------- 知识库 CRUD ----------------------------

    def create_kb(
        self, user: User, space_id: uuid.UUID, name: str, description: str, ip: str = ""
    ) -> KnowledgeBase:
        self._require_role(space_id, user.id, Role.EDITOR)
        if self._kbs.get_by_name(space_id, name) is not None:
            raise AppError(ErrorCode.KB_NAME_TAKEN, "同名知识库已存在", http_status=409)
        kb = self._kbs.create(
            KnowledgeBase(space_id=space_id, name=name, description=description, created_by=user.id)
        )
        self._log(user.id, space_id, AuditAction.KB_CREATED, name, ip, kb_id=str(kb.id))
        return kb

    def list_kbs(self, user: User, space_id: uuid.UUID) -> list[KnowledgeBase]:
        self._require_role(space_id, user.id, Role.VIEWER)
        return self._kbs.list_for_space(space_id)

    def get_kb(self, user: User, space_id: uuid.UUID, kb_id: uuid.UUID) -> KnowledgeBase:
        self._require_role(space_id, user.id, Role.VIEWER)
        return self._get_kb_in_space(space_id, kb_id)

    def update_kb(
        self,
        user: User,
        space_id: uuid.UUID,
        kb_id: uuid.UUID,
        name: str,
        description: str,
        ip: str = "",
    ) -> KnowledgeBase:
        self._require_role(space_id, user.id, Role.EDITOR)
        kb = self._get_kb_in_space(space_id, kb_id)
        existing = self._kbs.get_by_name(space_id, name)
        if existing is not None and existing.id != kb.id:
            raise AppError(ErrorCode.KB_NAME_TAKEN, "同名知识库已存在", http_status=409)
        kb.name = name
        kb.description = description
        self._kbs.save(kb)
        self._log(user.id, space_id, AuditAction.KB_UPDATED, name, ip, kb_id=str(kb.id))
        return kb

    def delete_kb(self, user: User, space_id: uuid.UUID, kb_id: uuid.UUID, ip: str = "") -> None:
        self._require_role(space_id, user.id, Role.ADMIN)
        kb = self._get_kb_in_space(space_id, kb_id)
        self._log(user.id, space_id, AuditAction.KB_DELETED, kb.name, ip, kb_id=str(kb.id))
        self._storage.delete_prefix(f"{space_id}/{kb_id}/")
        self._kbs.delete(kb)  # documents/chunks 由 FK CASCADE 清理

    # ---------------------------- 文档 ----------------------------

    def upload_document(
        self,
        user: User,
        space_id: uuid.UUID,
        kb_id: uuid.UUID,
        filename: str,
        content: bytes,
        content_type: str = "",
        ip: str = "",
    ) -> Document:
        self._require_role(space_id, user.id, Role.EDITOR)
        self._get_kb_in_space(space_id, kb_id)
        if len(content) > self._upload_max_bytes:
            raise AppError(
                ErrorCode.FILE_TOO_LARGE, "文件超过大小上限", http_status=413
            )
        safe_name = PurePosixPath(filename.replace("\\", "/")).name
        suffix = PurePosixPath(safe_name).suffix.lower()
        fmt = SUPPORTED_FORMATS.get(suffix)
        if fmt is None or not safe_name:
            raise AppError(
                ErrorCode.UNSUPPORTED_FORMAT,
                "仅支持 pdf/docx/xlsx/pptx/md/txt(旧版 office 格式请另存为新格式)",
                http_status=415,
            )
        document = self._documents.create(
            Document(
                kb_id=kb_id,
                space_id=space_id,
                filename=safe_name,
                format=fmt,
                size_bytes=len(content),
                source=f"{space_id}/{kb_id}/{uuid.uuid4()}/{safe_name}",
                status=DocumentStatus.PENDING,
                uploaded_by=user.id,
            )
        )
        self._storage.put(document.source, content, content_type)
        self._log(
            user.id, space_id, AuditAction.DOCUMENT_UPLOADED, safe_name, ip,
            kb_id=str(kb_id), document_id=str(document.id),
        )
        return document

    def list_documents(
        self, user: User, space_id: uuid.UUID, kb_id: uuid.UUID, limit: int, offset: int
    ) -> tuple[list[Document], int]:
        self._require_role(space_id, user.id, Role.VIEWER)
        self._get_kb_in_space(space_id, kb_id)
        return self._documents.list_for_kb(kb_id, limit, offset)

    def get_document(
        self, user: User, space_id: uuid.UUID, kb_id: uuid.UUID, document_id: uuid.UUID
    ) -> Document:
        self._require_role(space_id, user.id, Role.VIEWER)
        self._get_kb_in_space(space_id, kb_id)
        document = self._documents.get(document_id)
        if document is None or document.kb_id != kb_id:
            raise AppError(ErrorCode.DOCUMENT_NOT_FOUND, "文档不存在", http_status=404)
        return document

    def delete_document(
        self,
        user: User,
        space_id: uuid.UUID,
        kb_id: uuid.UUID,
        document_id: uuid.UUID,
        ip: str = "",
    ) -> None:
        self._require_role(space_id, user.id, Role.EDITOR)
        self._get_kb_in_space(space_id, kb_id)
        document = self._documents.get(document_id)
        if document is None or document.kb_id != kb_id:
            raise AppError(ErrorCode.DOCUMENT_NOT_FOUND, "文档不存在", http_status=404)
        self._log(
            user.id, space_id, AuditAction.DOCUMENT_DELETED, document.filename, ip,
            kb_id=str(kb_id), document_id=str(document_id),
        )
        self._storage.delete(document.source)
        self._documents.delete(document)  # chunks 由 FK CASCADE 清理

    # ---------------------------- 内部 ----------------------------

    def _require_role(self, space_id: uuid.UUID, user_id: uuid.UUID, minimum: Role) -> int:
        membership = self._spaces.get_membership(space_id, user_id)
        if membership is None:
            raise AppError(ErrorCode.SPACE_NOT_FOUND, "空间不存在", http_status=404)
        if membership.role < minimum:
            raise AppError(ErrorCode.FORBIDDEN, "角色权限不足", http_status=403)
        return membership.role

    def _get_kb_in_space(self, space_id: uuid.UUID, kb_id: uuid.UUID) -> KnowledgeBase:
        kb = self._kbs.get(kb_id)
        if kb is None or kb.space_id != space_id:
            raise AppError(ErrorCode.KB_NOT_FOUND, "知识库不存在", http_status=404)
        return kb

    def _log(
        self,
        actor_id: uuid.UUID,
        space_id: uuid.UUID,
        action: AuditAction,
        target: str,
        ip: str,
        **detail: str,
    ) -> None:
        self._audit.add(
            AuditLog(
                actor_id=actor_id,
                space_id=space_id,
                action=action,
                target=target[:128],
                detail=detail,
                ip=ip,
            )
        )
