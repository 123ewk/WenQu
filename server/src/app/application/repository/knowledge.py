"""知识库与文档仓储。

空间是租户边界(基准 04):仓储只按 space_id/kb_id 范围查询,"先成员校验(404)后
角色校验(403)"由服务层守卫负责,这里不做越权判断。
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.models import Document, KnowledgeBase


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
