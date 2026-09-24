"""外部事实记忆数据访问(OPT-45 P1):get/list/search 一律强制 (space_id, user_id) 谓词。

隔离在此层兜底:仓储不提供"无归属"的读取入口,调用方拿到 None/空即等价于不存在,
从数据通路层杜绝跨用户/跨空间读改写(与 qa._get_conversation 的防枚举同规则)。
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.models import Memory


class MemoryRepositoryImpl:
    def __init__(self, db: Session) -> None:
        self._db = db

    def add(self, memory: Memory) -> Memory:
        self._db.add(memory)
        self._db.flush()
        return memory

    def save(self, memory: Memory) -> Memory:
        self._db.flush()
        return memory

    def get(self, memory_id: uuid.UUID, space_id: uuid.UUID, user_id: uuid.UUID) -> Memory | None:
        memory = self._db.get(Memory, memory_id)
        if memory is None or memory.space_id != space_id or memory.user_id != user_id:
            return None
        return memory

    def list_for_actor(
        self, space_id: uuid.UUID, user_id: uuid.UUID, limit: int, offset: int
    ) -> tuple[list[Memory], int]:
        total = self._db.scalar(
            select(func.count())
            .select_from(Memory)
            .where(Memory.space_id == space_id, Memory.user_id == user_id)
        )
        rows = list(
            self._db.scalars(
                select(Memory)
                .where(Memory.space_id == space_id, Memory.user_id == user_id)
                .order_by(Memory.created_at.desc(), Memory.id)
                .limit(limit)
                .offset(offset)
            )
        )
        return rows, int(total or 0)

    def find_duplicate(
        self, space_id: uuid.UUID, user_id: uuid.UUID, content: str
    ) -> Memory | None:
        """确认落库前的内容去重:同归属下正文完全相同即视为重复(忽略首尾空白)。"""
        return self._db.scalar(
            select(Memory)
            .where(
                Memory.space_id == space_id,
                Memory.user_id == user_id,
                func.btrim(Memory.content) == content.strip(),
            )
            .limit(1)
        )

    def count_for_actor(self, space_id: uuid.UUID, user_id: uuid.UUID) -> int:
        """本人在本空间的记忆条数(召回前的判空闸门):命中 ix_memories_space_user,零行即 0。"""
        total = self._db.scalar(
            select(func.count())
            .select_from(Memory)
            .where(Memory.space_id == space_id, Memory.user_id == user_id)
        )
        return int(total or 0)

    def search_by_embedding(
        self,
        space_id: uuid.UUID,
        user_id: uuid.UUID,
        embedding: list[float],
        limit: int,
        min_similarity: float = 0.0,
    ) -> list[tuple[Memory, float]]:
        """余弦距离升序;相似度 = 1 - distance;低于阈值丢弃,避免注入噪声记忆。"""
        distance = Memory.embedding.cosine_distance(embedding).label("distance")
        stmt = (
            select(Memory, distance)
            .where(
                Memory.space_id == space_id,  # 租户谓词
                Memory.user_id == user_id,  # 归属谓词:仅本人
                Memory.embedding.is_not(None),
            )
            .order_by(distance)
            .limit(limit)
        )
        if min_similarity > 0:
            stmt = stmt.where(distance <= 1.0 - min_similarity)
        rows = self._db.execute(stmt).all()
        return [(memory, 1.0 - float(dist)) for memory, dist in rows]

    def delete(self, memory: Memory) -> None:
        self._db.delete(memory)
        self._db.flush()
