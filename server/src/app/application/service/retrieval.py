"""检索服务(ADR-1/ADR-5):查询向量化 → 二路召回 → RRF 融合 → 阈值截断。

RRF(Reciprocal Rank Fusion):score = Σ weight_i / (k + rank_i),对两路各自
的排名而非原始分数融合,天然免疫量纲差异(pgvector 余弦 vs ts_rank 不可比)。
参数 k=60 为公开基准常用值;权重默认向量 0.7 / 全文 0.3,空间级可覆盖(M3 配置面)。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import jieba

from app.core.errors import AppError, ErrorCode
from app.domain.enums import Role
from app.domain.interfaces import (
    DocumentRepository,
    EmbeddingGateway,
    KnowledgeBaseRepository,
    RetrievalRepository,
    SpaceRepository,
)

DEFAULT_RRF_K = 60
DEFAULT_VECTOR_WEIGHT = 0.7
DEFAULT_FULLTEXT_WEIGHT = 0.3
# 向量路最小余弦相似度:低于此值视为不相关。各厂商 embedding 分布不同,
# 可通过 APP_RETRIEVAL_MIN_SCORE 调整;调低=召回更多但噪声上升。
DEFAULT_MIN_VECTOR_SCORE = 0.3


@dataclass
class RetrievedChunk:
    chunk_id: str
    document_id: str
    kb_id: str
    filename: str
    content: str
    score: float
    vector_rank: int | None = None
    fulltext_rank: int | None = None
    meta: dict = field(default_factory=dict)

    @property
    def preview(self) -> str:
        return self.content[:200]


def rrf_fuse(
    vector_hits: list[tuple[str, float]],
    fulltext_hits: list[tuple[str, float]],
    k: int = DEFAULT_RRF_K,
    vector_weight: float = DEFAULT_VECTOR_WEIGHT,
    fulltext_weight: float = DEFAULT_FULLTEXT_WEIGHT,
) -> list[tuple[str, float, int | None, int | None]]:
    """两路排名融合:返回 [(chunk_id, 融合分, 向量排名, 全文排名)] 按融合分降序。"""
    scores: dict[str, float] = {}
    ranks: dict[str, dict[str, int]] = {}
    for path, hits, weight in (
        ("vector", vector_hits, vector_weight),
        ("fulltext", fulltext_hits, fulltext_weight),
    ):
        for position, (chunk_id, _raw) in enumerate(hits, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (k + position)
            ranks.setdefault(chunk_id, {})[path] = position
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return [
        (chunk_id, score, ranks[chunk_id].get("vector"), ranks[chunk_id].get("fulltext"))
        for chunk_id, score in ordered
    ]


class RetrievalService:
    def __init__(
        self,
        chunks: RetrievalRepository,
        kbs: KnowledgeBaseRepository,
        documents: DocumentRepository,
        spaces: SpaceRepository,
        embedder: EmbeddingGateway,
        rrf_k: int = DEFAULT_RRF_K,
        vector_weight: float = DEFAULT_VECTOR_WEIGHT,
        fulltext_weight: float = DEFAULT_FULLTEXT_WEIGHT,
        candidate_multiplier: int = 3,
        min_vector_score: float = DEFAULT_MIN_VECTOR_SCORE,
    ) -> None:
        self._chunks = chunks
        self._kbs = kbs
        self._documents = documents
        self._spaces = spaces
        self._embedder = embedder
        self._rrf_k = rrf_k
        self._vector_weight = vector_weight
        self._fulltext_weight = fulltext_weight
        self._candidate_multiplier = candidate_multiplier
        self._min_vector_score = min_vector_score

    def search(
        self,
        user_id: uuid.UUID,
        space_id: uuid.UUID,
        query: str,
        space_ids: list[uuid.UUID] | None = None,
        top_k: int = 8,
        kb_ids: list[uuid.UUID] | None = None,
        model_id: str | None = None,
    ) -> list[RetrievedChunk]:
        scoped = self._resolve_scope(space_id, space_ids)
        self._require_member(scoped, user_id)
        query = query.strip()
        if not query:
            raise AppError(ErrorCode.VALIDATION, "查询内容不能为空", http_status=400)

        candidates = max(top_k * self._candidate_multiplier, top_k)
        embedding = self._embedder.embed([query], model_id)[0]
        vector_hits = self._chunks.vector_search(
            scoped, embedding, kb_ids, candidates, min_similarity=self._min_vector_score
        )
        tokens = " ".join(jieba.cut_for_search(query))
        fulltext_hits = self._chunks.fulltext_search(scoped, tokens, kb_ids, candidates)

        by_id = {str(chunk.id): (chunk, document) for chunk, document, _ in vector_hits}
        for chunk, document, _ in fulltext_hits:
            by_id.setdefault(str(chunk.id), (chunk, document))

        fused = rrf_fuse(
            [(str(c.id), s) for c, _d, s in vector_hits],
            [(str(c.id), s) for c, _d, s in fulltext_hits],
            k=self._rrf_k,
            vector_weight=self._vector_weight,
            fulltext_weight=self._fulltext_weight,
        )
        results: list[RetrievedChunk] = []
        for chunk_id, score, v_rank, f_rank in fused[:top_k]:
            chunk, document = by_id[chunk_id]
            results.append(
                RetrievedChunk(
                    chunk_id=chunk_id,
                    document_id=str(document.id),
                    kb_id=str(document.kb_id),
                    filename=document.filename,
                    content=chunk.content,
                    score=round(score, 6),
                    vector_rank=v_rank,
                    fulltext_rank=f_rank,
                    meta=chunk.meta or {},
                )
            )
        return results

    # ---------------------------- 内部 ----------------------------

    def _resolve_scope(
        self, space_id: uuid.UUID, space_ids: list[uuid.UUID] | None
    ) -> uuid.UUID:
        """M2 单空间检索;跨空间检索(M3 起)只允许在自身有权空间内取交集。"""
        if not space_ids or space_ids == [space_id]:
            return space_id
        raise AppError(ErrorCode.VALIDATION, "M2 暂不支持跨空间检索", http_status=400)

    def _require_member(self, space_id: uuid.UUID, user_id: uuid.UUID) -> int:
        membership = self._spaces.get_membership(space_id, user_id)
        if membership is None:
            raise AppError(ErrorCode.SPACE_NOT_FOUND, "空间不存在", http_status=404)
        if membership.role < Role.VIEWER:
            raise AppError(ErrorCode.FORBIDDEN, "角色权限不足", http_status=403)
        return membership.role
