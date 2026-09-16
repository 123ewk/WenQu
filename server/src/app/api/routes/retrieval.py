"""检索接口:知识库检索测试(调试)与空间级问答检索共用同一服务。

返回里带 vector_rank / fulltext_rank:调优时能看出命中来自哪一路(RRF 融合可解释)。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import get_current_user, get_retrieval_service
from app.application.service.retrieval import RetrievalService, RetrievedChunk
from app.domain.models import User

router = APIRouter(prefix="/api/v1/spaces/{space_id}/retrieval", tags=["retrieval"])


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=8, ge=1, le=50)
    kb_ids: list[uuid.UUID] | None = None
    model_id: str | None = Field(default=None, description="覆盖默认 embedding 模型(调优用)")


class RetrievedChunkOut(BaseModel):
    chunk_id: str
    document_id: str
    kb_id: str
    filename: str
    content: str
    score: float
    vector_rank: int | None
    fulltext_rank: int | None
    meta: dict


def _out(chunk: RetrievedChunk) -> RetrievedChunkOut:
    return RetrievedChunkOut(
        chunk_id=chunk.chunk_id,
        document_id=chunk.document_id,
        kb_id=chunk.kb_id,
        filename=chunk.filename,
        content=chunk.content,
        score=chunk.score,
        vector_rank=chunk.vector_rank,
        fulltext_rank=chunk.fulltext_rank,
        meta=chunk.meta,
    )


@router.post("/search", response_model=list[RetrievedChunkOut])
def search(
    space_id: uuid.UUID,
    body: SearchRequest,
    user: User = Depends(get_current_user),
    service: RetrievalService = Depends(get_retrieval_service),
) -> list[RetrievedChunkOut]:
    return [
        _out(chunk)
        for chunk in service.search(
            user.id, space_id, body.query, top_k=body.top_k, kb_ids=body.kb_ids,
            model_id=body.model_id,
        )
    ]
