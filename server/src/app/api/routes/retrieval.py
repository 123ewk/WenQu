"""检索接口:知识库检索测试(调试)与空间级问答检索共用同一服务。

返回里带 vector_rank / fulltext_rank:调优时能看出命中来自哪一路(RRF 融合可解释)。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_validator

from app.api.deps import get_current_user, get_retrieval_service
from app.application.service.retrieval import RetrievalService, RetrievedChunk
from app.domain.models import User

router = APIRouter(prefix="/api/v1/spaces/{space_id}/retrieval", tags=["retrieval"])


class SearchRequest(BaseModel):
    """检索测试请求。overrides_* 只作用于本次调用(原型 Tab B:不改默认配置)。"""

    query: str = Field(min_length=1, max_length=2000)
    top_k: int | None = Field(default=None, ge=1, le=50, description="缺省用空间配置值")
    kb_ids: list[uuid.UUID] | None = None
    model_id: str | None = Field(default=None, description="覆盖默认 embedding 模型(调优用)")
    # ---- 请求级覆盖(仅本次) ----
    rrf_k: int | None = Field(default=None, ge=1, le=1000)
    vector_weight: float | None = Field(default=None, ge=0.0, le=1.0)
    fulltext_weight: float | None = Field(default=None, ge=0.0, le=1.0)
    min_score: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _weights_paired_and_bounded(self) -> SearchRequest:
        pair_given = self.vector_weight is not None and self.fulltext_weight is not None
        one_given = (self.vector_weight is None) != (self.fulltext_weight is None)
        if one_given:
            raise ValueError("vector_weight 与 fulltext_weight 必须成对提供")
        if pair_given and (self.vector_weight or 0) + (self.fulltext_weight or 0) > 1.0 + 1e-9:
            raise ValueError("vector_weight 与 fulltext_weight 之和不能超过 1")
        return self


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
    overrides = {
        key: value
        for key, value in (
            ("rrf_k", body.rrf_k),
            ("vector_weight", body.vector_weight),
            ("fulltext_weight", body.fulltext_weight),
            ("min_score", body.min_score),
            ("top_k", body.top_k),
        )
        if value is not None
    }
    return [
        _out(chunk)
        for chunk in service.search(
            user.id,
            space_id,
            body.query,
            top_k=body.top_k,
            kb_ids=body.kb_ids,
            model_id=body.model_id,
            overrides=overrides or None,
        )
    ]
