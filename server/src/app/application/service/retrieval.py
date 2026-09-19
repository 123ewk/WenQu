"""检索服务(ADR-1/ADR-5):查询向量化 → 二路召回 → RRF 融合 → 阈值截断。

RRF(Reciprocal Rank Fusion):score = Σ weight_i / (k + rank_i),对两路各自
的排名而非原始分数融合,天然免疫量纲差异(pgvector 余弦 vs ts_rank 不可比)。
参数 k=60 为公开基准常用值;权重默认向量 0.7 / 全文 0.3,空间级可覆盖(M3 配置面)。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import NamedTuple

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
DEFAULT_TOP_K = 6


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


class SearchOverrides(NamedTuple):
    """请求级覆盖(检索测试调参用):只作用于本次调用,绝不写回空间配置。"""

    rrf_k: int | None = None
    vector_weight: float | None = None
    fulltext_weight: float | None = None
    min_score: float | None = None
    top_k: int | None = None


def _apply_overrides(
    params: EffectiveParams, overrides: SearchOverrides | dict[str, float | int] | None
) -> EffectiveParams:
    if overrides is None:
        return params
    # 允许传 dict(HTTP 层直接透传请求体);只认已知键,未知键忽略
    if isinstance(overrides, dict):
        overrides = SearchOverrides(
            rrf_k=_opt_int(overrides.get("rrf_k")),
            vector_weight=_opt_float(overrides.get("vector_weight")),
            fulltext_weight=_opt_float(overrides.get("fulltext_weight")),
            min_score=_opt_float(overrides.get("min_score")),
            top_k=_opt_int(overrides.get("top_k")),
        )
    return EffectiveParams(
        rrf_k=params.rrf_k if overrides.rrf_k is None else overrides.rrf_k,
        vector_weight=(
            params.vector_weight
            if overrides.vector_weight is None
            else overrides.vector_weight
        ),
        fulltext_weight=(
            params.fulltext_weight
            if overrides.fulltext_weight is None
            else overrides.fulltext_weight
        ),
        min_score=params.min_score if overrides.min_score is None else overrides.min_score,
        top_k=params.top_k if overrides.top_k is None else overrides.top_k,
    )


def _opt_int(value: float | int | None) -> int | None:
    return None if value is None else int(value)


def _opt_float(value: float | int | None) -> float | None:
    return None if value is None else float(value)


class EffectiveParams(NamedTuple):
    """一次检索的实际生效参数:空间级配置覆盖服务级默认(缺口 #5)。"""

    rrf_k: int
    vector_weight: float
    fulltext_weight: float
    min_score: float
    top_k: int


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
        top_k: int | None = None,
        kb_ids: list[uuid.UUID] | None = None,
        # 已废弃(OPT-3 收尾):检索向量取 KB 的 embedding_model,此参数不再使用,
        # 保留仅为契约兼容(SearchRequest.model_id)
        model_id: str | None = None,
        overrides: SearchOverrides | dict[str, float | int] | None = None,
    ) -> list[RetrievedChunk]:
        scoped = self._resolve_scope(space_id, space_ids)
        self._require_member(scoped, user_id)
        query = query.strip()
        if not query:
            raise AppError(ErrorCode.VALIDATION, "查询内容不能为空", http_status=400)

        params = self._effective_params(scoped, top_k, overrides)
        top_k = params.top_k
        candidates = max(top_k * self._candidate_multiplier, top_k)
        embedding = self._embedder.embed(
            [query], self._resolve_embedding_model(scoped, kb_ids)
        )[0]
        vector_hits = self._chunks.vector_search(
            scoped, embedding, kb_ids, candidates, min_similarity=params.min_score
        )
        tokens = " ".join(jieba.cut_for_search(query))
        fulltext_hits = self._chunks.fulltext_search(scoped, tokens, kb_ids, candidates)

        by_id = {str(chunk.id): (chunk, document) for chunk, document, _ in vector_hits}
        for chunk, document, _ in fulltext_hits:
            by_id.setdefault(str(chunk.id), (chunk, document))

        fused = rrf_fuse(
            [(str(c.id), s) for c, _d, s in vector_hits],
            [(str(c.id), s) for c, _d, s in fulltext_hits],
            k=params.rrf_k,
            vector_weight=params.vector_weight,
            fulltext_weight=params.fulltext_weight,
        )
        # 父子分块(OPT-4):命中子块回取父块;同一父块的多个子块只保留融合分最高的一条
        parent_ids: set[uuid.UUID] = set()
        for chunk_id, *_ranks in fused:
            parent_id = by_id[chunk_id][0].parent_id
            if parent_id is not None:
                parent_ids.add(parent_id)
        parents = (
            {parent.id: parent for parent in self._chunks.get_many(list(parent_ids))}
            if parent_ids
            else {}
        )
        results: list[RetrievedChunk] = []
        seen_parents: set[str] = set()
        for chunk_id, score, v_rank, f_rank in fused:
            if len(results) == top_k:
                break
            chunk, document = by_id[chunk_id]
            hit_parent = chunk.parent_id
            source = parents.get(hit_parent, chunk) if hit_parent is not None else chunk
            key = str(source.id)
            if key in seen_parents:
                continue
            seen_parents.add(key)
            results.append(
                RetrievedChunk(
                    chunk_id=key,
                    document_id=str(document.id),
                    kb_id=str(document.kb_id),
                    filename=document.filename,
                    content=source.content,
                    score=round(score, 6),
                    vector_rank=v_rank,
                    fulltext_rank=f_rank,
                    meta=source.meta or {},
                )
            )
        return results

    def _effective_params(
        self,
        space_id: uuid.UUID,
        top_k: int | None,
        overrides: SearchOverrides | dict[str, float | int] | None = None,
    ) -> EffectiveParams:
        """空间配置覆盖全局默认;空间已删或字段缺失(None)时回退到服务级默认。

        None 也算缺失:ORM 列默认值在 flush 时才生效,未落库的实例该字段是 None,
        直接参与运算会 TypeError,所以统一按"未配置"处理。
        """
        space = self._spaces.get(space_id)

        def pick(attr: str, fallback: float | int) -> float | int:
            value = getattr(space, attr, None)
            return fallback if value is None else value

        params = EffectiveParams(
            rrf_k=int(pick("retrieval_rrf_k", self._rrf_k)),
            vector_weight=float(pick("retrieval_vector_weight", self._vector_weight)),
            fulltext_weight=float(
                pick("retrieval_fulltext_weight", self._fulltext_weight)
            ),
            min_score=float(pick("retrieval_min_score", self._min_vector_score)),
            top_k=(
                top_k
                if top_k is not None
                else int(pick("retrieval_default_top_k", DEFAULT_TOP_K))
            ),
        )
        return _apply_overrides(params, overrides)

    # ---------------------------- 内部 ----------------------------

    def _resolve_embedding_model(
        self, space_id: uuid.UUID, kb_ids: list[uuid.UUID] | None
    ) -> str | None:
        """查询向量必须与库内向量的嵌入模型一致:取目标 KB 的 embedding_model。

        - 指定 kb_ids → 用第一个 KB 的模型(检索范围内所有 KB 应同源;V1 不支持
          一个空间混用多种嵌入模型,见优化台账 OPT-23);
        - 未指定 → 用空间内第一个 KB 的模型;空间还没有 KB → None(网关回退目录
          默认 embedding 模型,保持"未配密钥 → 503"的可观测语义)。
        请求里的 `model_id`(回答模型)不参与检索 —— 此前把 /ask 的对话模型 id
        喂给查询向量化,导致显式选任何对话模型检索必 503(前端缺口台账 §11.2-1)。
        """
        if kb_ids:
            kb = self._kbs.get(kb_ids[0])
            return kb.embedding_model if kb is not None else None
        kbs = self._kbs.list_for_space(space_id)
        return kbs[0].embedding_model if kbs else None

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
