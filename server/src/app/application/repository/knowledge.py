"""知识库与文档仓储。

空间是租户边界(基准 04):仓储只按 space_id/kb_id 范围查询,"先成员校验(404)后
角色校验(403)"由服务层守卫负责,这里不做越权判断。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.enums import DocumentStatus
from app.domain.models import Chunk, Document, KnowledgeBase


class KnowledgeBaseRepositoryImpl:
    def __init__(self, db: Session) -> None:
        self._db = db

    def get(self, kb_id: uuid.UUID) -> KnowledgeBase | None:
        return self._db.get(KnowledgeBase, kb_id)

    def get_by_name(self, space_id: uuid.UUID, name: str) -> KnowledgeBase | None:
        return self._db.scalar(
            select(KnowledgeBase).where(
                KnowledgeBase.space_id == space_id, KnowledgeBase.name == name
            )
        )

    def create(self, kb: KnowledgeBase) -> KnowledgeBase:
        self._db.add(kb)
        self._db.flush()
        return kb

    def save(self, kb: KnowledgeBase) -> KnowledgeBase:
        self._db.flush()
        return kb

    def delete(self, kb: KnowledgeBase) -> None:
        self._db.delete(kb)
        self._db.flush()

    def list_for_space(self, space_id: uuid.UUID) -> list[KnowledgeBase]:
        return list(
            self._db.scalars(
                select(KnowledgeBase)
                .where(KnowledgeBase.space_id == space_id)
                .order_by(KnowledgeBase.created_at)
            )
        )


@dataclass(frozen=True)
class KbStats:
    """知识库聚合统计(index_status: empty/processing/degraded/ready)。"""

    document_count: int
    chunk_count: int
    size_bytes: int
    index_status: str


class KbStatsRepositoryImpl:
    """知识库聚合统计:一次查询算齐,避免逐库查询(N+1)。"""

    def __init__(self, db: Session) -> None:
        self._db = db

    def stats_for_kbs(self, kb_ids: list[uuid.UUID]) -> dict[uuid.UUID, KbStats]:
        from app.domain.models import Chunk

        if not kb_ids:
            return {}
        from sqlalchemy import func as sa_func

        doc_rows = self._db.execute(
            select(
                Document.kb_id,
                sa_func.count(Document.id),
                sa_func.coalesce(sa_func.sum(Document.size_bytes), 0),
                sa_func.count(Document.id).filter(Document.status == DocumentStatus.FAILED),
                sa_func.count(Document.id).filter(
                    Document.status.in_(
                        [
                            DocumentStatus.PENDING,
                            DocumentStatus.PARSING,
                            DocumentStatus.CHUNKING,
                            DocumentStatus.EMBEDDING,
                        ]
                    )
                ),
            )
            .where(Document.kb_id.in_(kb_ids))
            .group_by(Document.kb_id)
        ).all()
        # 分块按"本 KB 的文档"聚合:先取本批 KB 的文档 id,再统计其分块
        doc_rows_all: list[tuple[uuid.UUID, uuid.UUID]] = [
            (doc_id, kb_id)
            for doc_id, kb_id in self._db.execute(
                select(Document.id, Document.kb_id).where(Document.kb_id.in_(kb_ids))
            ).all()
        ]
        doc_kb: dict[uuid.UUID, uuid.UUID] = {doc_id: kb_id for doc_id, kb_id in doc_rows_all}
        kb_chunks: dict[uuid.UUID, int] = {}
        if doc_kb:
            chunk_rows: list[tuple[uuid.UUID, int]] = [
                (doc_id, int(count))
                for doc_id, count in self._db.execute(
                    select(Chunk.document_id, sa_func.count(Chunk.id))
                    .where(Chunk.document_id.in_(list(doc_kb)))
                    .group_by(Chunk.document_id)
                ).all()
            ]
            for doc_id, count in chunk_rows:
                kb_id = doc_kb.get(doc_id)
                if kb_id is not None:
                    kb_chunks[kb_id] = kb_chunks.get(kb_id, 0) + count

        stats: dict[uuid.UUID, KbStats] = {}
        for kb_id, docs, size, failed, in_flight in doc_rows:
            chunks = kb_chunks.get(kb_id, 0)
            if docs == 0:
                status = "empty"
            elif in_flight:
                status = "processing"
            elif failed:
                status = "degraded"
            else:
                status = "ready"
            stats[kb_id] = KbStats(
                document_count=int(docs),
                chunk_count=chunks,
                size_bytes=int(size),
                index_status=status,
            )
        return stats

    def list_active_in_space(self, space_id: uuid.UUID, limit: int) -> list[Document]:
        """在途 + 近期失败:入库进度看板的数据源。"""
        return list(
            self._db.scalars(
                select(Document)
                .where(
                    Document.space_id == space_id,
                    Document.status.in_(
                        [
                            DocumentStatus.PENDING,
                            DocumentStatus.PARSING,
                            DocumentStatus.CHUNKING,
                            DocumentStatus.EMBEDDING,
                            DocumentStatus.FAILED,
                        ]
                    ),
                )
                .order_by(Document.updated_at.desc())
                .limit(limit)
            )
        )

    def counts_by_status(self, space_id: uuid.UUID) -> dict[str, int]:
        from sqlalchemy import func as sa_func

        rows = self._db.execute(
            select(Document.status, sa_func.count(Document.id))
            .where(Document.space_id == space_id)
            .group_by(Document.status)
        ).all()
        counts = {status.value: 0 for status in DocumentStatus}
        for status, count in rows:
            counts[str(status)] = int(count)
        return counts


class DocumentRepositoryImpl:
    def __init__(self, db: Session) -> None:
        self._db = db

    def get(self, document_id: uuid.UUID) -> Document | None:
        return self._db.get(Document, document_id)

    def create(self, document: Document) -> Document:
        self._db.add(document)
        self._db.flush()
        return document

    def save(self, document: Document) -> Document:
        self._db.flush()
        return document

    def delete(self, document: Document) -> None:
        self._db.delete(document)
        self._db.flush()

    def list_for_kb(
        self, kb_id: uuid.UUID, limit: int, offset: int
    ) -> tuple[list[Document], int]:
        total = self._db.scalar(
            select(func.count()).select_from(Document).where(Document.kb_id == kb_id)
        )
        items = list(
            self._db.scalars(
                select(Document)
                .where(Document.kb_id == kb_id)
                .order_by(Document.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        return items, int(total or 0)


class ChunkRepositoryImpl:
    """分块仓储:仅流水线使用,幂等重建(先删后插)。"""

    def __init__(self, db: Session) -> None:
        self._db = db

    def replace_for_document(
        self,
        document: Document,
        drafts: list[tuple[int, str, list[float] | None, dict]],
    ) -> None:
        import jieba
        from sqlalchemy import delete
        from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: F401

        from app.domain.models import Chunk

        self._db.execute(delete(Chunk).where(Chunk.document_id == document.id))
        for seq, content, embedding, meta in drafts:
            tokens = " ".join(jieba.cut_for_search(content))
            self._db.add(
                Chunk(
                    document_id=document.id,
                    space_id=document.space_id,
                    seq=seq,
                    content=content,
                    embedding=embedding,
                    tsv=func.to_tsvector("simple", tokens),
                    meta=meta,
                )
            )
        self._db.flush()


class ChunkQueryRepositoryImpl:
    """分块读取:不返回 embedding 列(体积大且前端无用),只给溯源所需字段。"""

    def __init__(self, db: Session) -> None:
        self._db = db

    def get(self, chunk_id: uuid.UUID) -> Chunk | None:
        from app.domain.models import Chunk

        return self._db.get(Chunk, chunk_id)

    def list_for_document(
        self, document_id: uuid.UUID, limit: int, offset: int
    ) -> tuple[list[Chunk], int]:
        from app.domain.models import Chunk

        total = self._db.scalar(
            select(func.count()).select_from(Chunk).where(Chunk.document_id == document_id)
        )
        items = list(
            self._db.scalars(
                select(Chunk)
                .where(Chunk.document_id == document_id)
                .order_by(Chunk.seq)
                .limit(limit)
                .offset(offset)
            )
        )
        return items, int(total or 0)
