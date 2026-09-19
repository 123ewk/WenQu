"""混合检索数据访问(ADR-1):pgvector 向量路 ∥ tsvector 全文路,租户谓词强制直达。

两路各自取 top-K 候选,由服务层用 RRF 融合(Reciprocal Rank Fusion,实现见
application/service/retrieval.py)。此处只做候选召回,不做融合排序。
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.models import Chunk, Document


def build_tsquery(query_tokens: str) -> str:
    """jieba 分词结果 → tsquery:OR 连接各词位,词内不含操作符。"""
    parts = [token for token in query_tokens.split() if token.strip()]
    return " | ".join(parts)


class RetrievalRepositoryImpl:
    def __init__(self, db: Session) -> None:
        self._db = db

    def vector_search(
        self,
        space_id: uuid.UUID,
        embedding: list[float],
        kb_ids: list[uuid.UUID] | None,
        limit: int,
        min_similarity: float = 0.0,
    ) -> list[tuple[Chunk, Document, float]]:
        """余弦距离升序;同时返回相似度(1 - distance)便于展示与阈值过滤。

        min_similarity:低于该余弦相似度的候选视为不相关直接丢弃。没有它时"知识库
        无相关内容"的问题也会拿到 top-K 噪声块,诱发无依据作答(引用可信度受损)。
        """
        distance = Chunk.embedding.cosine_distance(embedding).label("distance")
        stmt = (
            select(Chunk, Document, distance)
            .join(Document, Document.id == Chunk.document_id)
            .where(
                Chunk.space_id == space_id,  # 租户谓词:空间隔离在此强制
                Chunk.embedding.is_not(None),
            )
            .order_by(distance)
            .limit(limit)
        )
        if min_similarity > 0:
            stmt = stmt.where(distance <= 1.0 - min_similarity)
        if kb_ids:
            stmt = stmt.where(Document.kb_id.in_(kb_ids))
        rows = self._db.execute(stmt).all()
        return [(chunk, document, 1.0 - float(dist)) for chunk, document, dist in rows]

    def fulltext_search(
        self,
        space_id: uuid.UUID,
        jieba_tokens: str,
        kb_ids: list[uuid.UUID] | None,
        limit: int,
    ) -> list[tuple[Chunk, Document, float]]:
        """ts_rank 降序;中文由应用层 jieba 切词后写入 tsv(ADR-1)。"""
        tsquery = build_tsquery(jieba_tokens)
        if not tsquery:
            return []
        rank = func.ts_rank(Chunk.tsv, func.to_tsquery("simple", tsquery)).label("rank")
        stmt = (
            select(Chunk, Document, rank)
            .join(Document, Document.id == Chunk.document_id)
            .where(
                Chunk.space_id == space_id,
                Chunk.tsv.op("@@")(func.to_tsquery("simple", tsquery)),
            )
            .order_by(rank.desc())
            .limit(limit)
        )
        if kb_ids:
            stmt = stmt.where(Document.kb_id.in_(kb_ids))
        rows = self._db.execute(stmt).all()
        return [(chunk, document, float(score)) for chunk, document, score in rows]

    def get_many(self, chunk_ids: list[uuid.UUID]) -> list[Chunk]:
        """按 id 批量取块(父子分块回取父块用);入参来自本空间检索结果,免租户谓词。"""
        if not chunk_ids:
            return []
        return list(self._db.scalars(select(Chunk).where(Chunk.id.in_(chunk_ids))))
