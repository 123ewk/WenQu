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
    Caller,
    get_api_key_service,
    get_client_ip,
    get_current_actor,
    get_current_user,
    get_knowledge_service,
)
from app.application.repository.knowledge import KbStats
from app.application.service.api_keys import ApiKeyService
from app.application.service.knowledge import KnowledgeService
from app.domain.models import ApiKey, Document, KnowledgeBase, User

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
    # 聚合统计(一次查询算齐,见 KbStatsRepository);空库为 0 / empty
    document_count: int = 0
    chunk_count: int = 0
    size_bytes: int = 0
    index_status: str = "empty"  # empty | processing | ready | degraded
    created_at: datetime | None = None


class DocumentOut(BaseModel):
    id: str
    kb_id: str
    filename: str
    format: str
    size_bytes: int
    status: str
    error_code: str | None = None  # 稳定机器码(INGEST_FAILED)
    error_message: str | None = None  # 任务侧真实原因(定位用,不保证面向终端用户友好)
    created_at: datetime | None = None


class DocumentPage(BaseModel):
    items: list[DocumentOut]
    total: int


class ChunkOut(BaseModel):
    id: str
    seq: int
    content: str
    tokens: int | None = None  # 入库时算好的 token 数(分块列表显示用)
    meta: dict


class ChunkPage(BaseModel):
    items: list[ChunkOut]
    total: int


def _kb_out(kb: KnowledgeBase, stats: KbStats | None = None) -> KnowledgeBaseOut:
    return KnowledgeBaseOut(
        id=str(kb.id),
        space_id=str(kb.space_id),
        name=kb.name,
        description=kb.description,
        embedding_model=kb.embedding_model,
        embedding_dim=kb.embedding_dim,
        document_count=stats.document_count if stats else 0,
        chunk_count=stats.chunk_count if stats else 0,
        size_bytes=stats.size_bytes if stats else 0,
        index_status=stats.index_status if stats else "empty",
        created_at=kb.created_at,
    )


def _chunk_out(chunk: object) -> ChunkOut:
    meta = chunk.meta or {}  # type: ignore[attr-defined]
    return ChunkOut(
        id=str(chunk.id),  # type: ignore[attr-defined]
        seq=chunk.seq,  # type: ignore[attr-defined]
        content=chunk.content,  # type: ignore[attr-defined]
        tokens=meta.get("tokens"),
        meta=meta,
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
        error_message=document.error_message,
        created_at=document.created_at,
    )


# ---------------------------- 知识库 ----------------------------


def _check_kb_scope(
    key_service: ApiKeyService, api_key: ApiKey | None, kb_id: uuid.UUID
) -> None:
    """API Key 的 KB 范围收窄:范围外一律 403(JWT 调用 api_key 为 None,不受影响)。"""
    if api_key is not None:
        key_service.resolve_kb_scope(api_key, [kb_id])


@router.get("", response_model=list[KnowledgeBaseOut])
def list_kbs(
    space_id: uuid.UUID,
    caller: Caller = Depends(get_current_actor),
    service: KnowledgeService = Depends(get_knowledge_service),
    key_service: ApiKeyService = Depends(get_api_key_service),
) -> list[KnowledgeBaseOut]:
    user = caller.user
    kbs = service.list_kbs(user, space_id)
    # API Key 调用:KB 列表按 Key 范围过滤(程序化客户端只看得到被授权的库)
    if caller.api_key is not None:
        scope = key_service.resolve_kb_scope(caller.api_key, None)
        if scope is not None:
            allowed = set(scope)
            kbs = [kb for kb in kbs if kb.id in allowed]
    stats = service.kb_stats(space_id, [kb.id for kb in kbs])
    return [_kb_out(kb, stats.get(kb.id)) for kb in kbs]


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
    caller: Caller = Depends(get_current_actor),
    service: KnowledgeService = Depends(get_knowledge_service),
    key_service: ApiKeyService = Depends(get_api_key_service),
) -> KnowledgeBaseOut:
    user = caller.user
    _check_kb_scope(key_service, caller.api_key, kb_id)
    kb = service.get_kb(user, space_id, kb_id)
    return _kb_out(kb, service.kb_stats(space_id, [kb.id]).get(kb.id))


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
    caller: Caller = Depends(get_current_actor),
    service: KnowledgeService = Depends(get_knowledge_service),
    key_service: ApiKeyService = Depends(get_api_key_service),
) -> DocumentPage:
    user = caller.user
    _check_kb_scope(key_service, caller.api_key, kb_id)
    items, total = service.list_documents(user, space_id, kb_id, limit, offset)
    return DocumentPage(items=[_doc_out(d) for d in items], total=total)


@router.post("/{kb_id}/documents", response_model=DocumentOut, status_code=201)
async def upload_document(
    space_id: uuid.UUID,
    kb_id: uuid.UUID,
    file: UploadFile = File(description="文档文件(pdf/docx/xlsx/pptx/md/txt)"),
    caller: Caller = Depends(get_current_actor),
    service: KnowledgeService = Depends(get_knowledge_service),
    key_service: ApiKeyService = Depends(get_api_key_service),
    ip: str = Depends(get_client_ip),
) -> DocumentOut:
    user = caller.user
    _check_kb_scope(key_service, caller.api_key, kb_id)
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
    caller: Caller = Depends(get_current_actor),
    service: KnowledgeService = Depends(get_knowledge_service),
    key_service: ApiKeyService = Depends(get_api_key_service),
) -> DocumentOut:
    user = caller.user
    _check_kb_scope(key_service, caller.api_key, kb_id)
    return _doc_out(service.get_document(user, space_id, kb_id, document_id))


@router.delete("/{kb_id}/documents/{document_id}", status_code=204)
def delete_document(
    space_id: uuid.UUID,
    kb_id: uuid.UUID,
    document_id: uuid.UUID,
    caller: Caller = Depends(get_current_actor),
    service: KnowledgeService = Depends(get_knowledge_service),
    key_service: ApiKeyService = Depends(get_api_key_service),
    ip: str = Depends(get_client_ip),
) -> None:
    user = caller.user
    _check_kb_scope(key_service, caller.api_key, kb_id)
    service.delete_document(user, space_id, kb_id, document_id, ip)


@router.get("/{kb_id}/documents/{document_id}/chunks", response_model=ChunkPage)
def list_chunks(
    space_id: uuid.UUID,
    kb_id: uuid.UUID,
    document_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    caller: Caller = Depends(get_current_actor),
    service: KnowledgeService = Depends(get_knowledge_service),
    key_service: ApiKeyService = Depends(get_api_key_service),
) -> ChunkPage:
    user = caller.user
    _check_kb_scope(key_service, caller.api_key, kb_id)
    items, total = service.list_chunks(user, space_id, kb_id, document_id, limit, offset)
    return ChunkPage(items=[_chunk_out(c) for c in items], total=total)


@router.post(
    "/{kb_id}/documents/{document_id}/reparse",
    response_model=DocumentOut,
    status_code=200,
)
def reparse_document(
    space_id: uuid.UUID,
    kb_id: uuid.UUID,
    document_id: uuid.UUID,
    caller: Caller = Depends(get_current_actor),
    service: KnowledgeService = Depends(get_knowledge_service),
    key_service: ApiKeyService = Depends(get_api_key_service),
    ip: str = Depends(get_client_ip),
) -> DocumentOut:
    """重新解析/重建索引:重置为 pending 再入队,原文件不变;处理中调用 → 409。"""
    user = caller.user
    _check_kb_scope(key_service, caller.api_key, kb_id)
    return _doc_out(service.reparse_document(user, space_id, kb_id, document_id, ip))


@router.get(
    "/{kb_id}/documents/{document_id}/chunks/{chunk_id}",
    response_model=ChunkOut,
)
def get_chunk(
    space_id: uuid.UUID,
    kb_id: uuid.UUID,
    document_id: uuid.UUID,
    chunk_id: uuid.UUID,
    caller: Caller = Depends(get_current_actor),
    service: KnowledgeService = Depends(get_knowledge_service),
    key_service: ApiKeyService = Depends(get_api_key_service),
) -> ChunkOut:
    """单块全文(引用抽屉"查看完整原文块";不走 excerpt 截断)。"""
    user = caller.user
    _check_kb_scope(key_service, caller.api_key, kb_id)
    chunk = service.get_chunk(user, space_id, kb_id, document_id, chunk_id)
    return _chunk_out(chunk)
