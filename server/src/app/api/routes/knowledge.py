"""知识库与文档路由:空间下 KB 容器 + 文档上传/列表/详情/删除。

角色门槛:查看=任意成员,KB/文档增改=Editor+,删 KB=Admin+;上传走 multipart,
文件落对象存储,元数据落 documents 表(status=pending,流水线在 M2-4 接管)。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, Query, UploadFile
from pydantic import BaseModel, Field

from app.api.deps import (
    get_client_ip,
    get_current_user,
    get_knowledge_service,
)
from app.application.service.knowledge import KnowledgeService
from app.domain.models import Document, KnowledgeBase, User

router = APIRouter(prefix="/api/v1/spaces/{space_id}/knowledge-bases", tags=["knowledge"])


class CreateKBRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=512)


class UpdateKBRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=512)


class KnowledgeBaseOut(BaseModel):
    id: str
    space_id: str
    name: str
    description: str
    embedding_model: str
    embedding_dim: int
    created_at: datetime | None = None


class DocumentOut(BaseModel):
    id: str
    kb_id: str
    filename: str
    format: str
    size_bytes: int
    status: str
    error_code: str | None = None
    created_at: datetime | None = None


class DocumentPage(BaseModel):
    items: list[DocumentOut]
    total: int


class ChunkOut(BaseModel):
    id: str
    seq: int
    content: str
    meta: dict


class ChunkPage(BaseModel):
    items: list[ChunkOut]
    total: int


def _kb_out(kb: KnowledgeBase) -> KnowledgeBaseOut:
    return KnowledgeBaseOut(
        id=str(kb.id),
        space_id=str(kb.space_id),
        name=kb.name,
        description=kb.description,
        embedding_model=kb.embedding_model,
        embedding_dim=kb.embedding_dim,
        created_at=kb.created_at,
    )


def _chunk_out(chunk: object) -> ChunkOut:
    return ChunkOut(
        id=str(chunk.id),  # type: ignore[attr-defined]
        seq=chunk.seq,  # type: ignore[attr-defined]
        content=chunk.content,  # type: ignore[attr-defined]
        meta=chunk.meta or {},  # type: ignore[attr-defined]
    )


def _doc_out(document: Document) -> DocumentOut:
    return DocumentOut(
        id=str(document.id),
        kb_id=str(document.kb_id),
        filename=document.filename,
        format=document.format,
        size_bytes=document.size_bytes,
        status=document.status,
        error_code=document.error_code,
        created_at=document.created_at,
    )


# ---------------------------- 知识库 ----------------------------


@router.get("", response_model=list[KnowledgeBaseOut])
def list_kbs(
    space_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: KnowledgeService = Depends(get_knowledge_service),
) -> list[KnowledgeBaseOut]:
    return [_kb_out(kb) for kb in service.list_kbs(user, space_id)]


@router.post("", response_model=KnowledgeBaseOut, status_code=201)
def create_kb(
    space_id: uuid.UUID,
    body: CreateKBRequest,
    user: User = Depends(get_current_user),
    service: KnowledgeService = Depends(get_knowledge_service),
    ip: str = Depends(get_client_ip),
) -> KnowledgeBaseOut:
    kb = service.create_kb(user, space_id, body.name, body.description, ip)
    return _kb_out(kb)


@router.get("/{kb_id}", response_model=KnowledgeBaseOut)
def get_kb(
    space_id: uuid.UUID,
    kb_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: KnowledgeService = Depends(get_knowledge_service),
) -> KnowledgeBaseOut:
    return _kb_out(service.get_kb(user, space_id, kb_id))


@router.patch("/{kb_id}", response_model=KnowledgeBaseOut)
def update_kb(
    space_id: uuid.UUID,
    kb_id: uuid.UUID,
    body: UpdateKBRequest,
    user: User = Depends(get_current_user),
    service: KnowledgeService = Depends(get_knowledge_service),
    ip: str = Depends(get_client_ip),
) -> KnowledgeBaseOut:
    kb = service.update_kb(user, space_id, kb_id, body.name, body.description, ip)
    return _kb_out(kb)


@router.delete("/{kb_id}", status_code=204)
def delete_kb(
    space_id: uuid.UUID,
    kb_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: KnowledgeService = Depends(get_knowledge_service),
    ip: str = Depends(get_client_ip),
) -> None:
    service.delete_kb(user, space_id, kb_id, ip)


# ---------------------------- 文档 ----------------------------


@router.get("/{kb_id}/documents", response_model=DocumentPage)
def list_documents(
    space_id: uuid.UUID,
    kb_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    service: KnowledgeService = Depends(get_knowledge_service),
) -> DocumentPage:
    items, total = service.list_documents(user, space_id, kb_id, limit, offset)
    return DocumentPage(items=[_doc_out(d) for d in items], total=total)


@router.post("/{kb_id}/documents", response_model=DocumentOut, status_code=201)
async def upload_document(
    space_id: uuid.UUID,
    kb_id: uuid.UUID,
    file: UploadFile = File(description="文档文件(pdf/docx/xlsx/pptx/md/txt)"),
    user: User = Depends(get_current_user),
    service: KnowledgeService = Depends(get_knowledge_service),
    ip: str = Depends(get_client_ip),
) -> DocumentOut:
    content = await file.read()
    filename = file.filename or "未命名"
    document = service.upload_document(
        user, space_id, kb_id, filename, content, file.content_type or ""
    )
    return _doc_out(document)


@router.get("/{kb_id}/documents/{document_id}", response_model=DocumentOut)
def get_document(
    space_id: uuid.UUID,
    kb_id: uuid.UUID,
    document_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: KnowledgeService = Depends(get_knowledge_service),
) -> DocumentOut:
    return _doc_out(service.get_document(user, space_id, kb_id, document_id))


@router.delete("/{kb_id}/documents/{document_id}", status_code=204)
def delete_document(
    space_id: uuid.UUID,
    kb_id: uuid.UUID,
    document_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: KnowledgeService = Depends(get_knowledge_service),
    ip: str = Depends(get_client_ip),
) -> None:
    service.delete_document(user, space_id, kb_id, document_id, ip)


@router.get("/{kb_id}/documents/{document_id}/chunks", response_model=ChunkPage)
def list_chunks(
    space_id: uuid.UUID,
    kb_id: uuid.UUID,
    document_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    service: KnowledgeService = Depends(get_knowledge_service),
) -> ChunkPage:
    items, total = service.list_chunks(user, space_id, kb_id, document_id, limit, offset)
    return ChunkPage(items=[_chunk_out(c) for c in items], total=total)
