"""记忆提议忽略留痕数据访问(OPT-45 P2):写 dismissed 行 + 查未过期哈希集合。

与 memories 同规则:一切读写以 (space_id, user_id) 为强制谓词,不提供无归属入口。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models import MemoryProposal


class MemoryProposalRepositoryImpl:
    def __init__(self, db: Session) -> None:
        self._db = db

    def add(self, proposal: MemoryProposal) -> MemoryProposal:
        self._db.add(proposal)
        self._db.flush()
        return proposal

    def exists(
        self, space_id: uuid.UUID, user_id: uuid.UUID, content_hash: str, now: datetime
    ) -> bool:
        """同归属下是否已有未过期的该哈希(幂等:重复忽略只留一条)。"""
        row = self._db.scalar(
            select(MemoryProposal.id)
            .where(
                MemoryProposal.space_id == space_id,
                MemoryProposal.user_id == user_id,
                MemoryProposal.content_hash == content_hash,
                MemoryProposal.expires_at > now,
            )
            .limit(1)
        )
        return row is not None

    def active_hashes(
        self, space_id: uuid.UUID, user_id: uuid.UUID, now: datetime
    ) -> set[str]:
        """本人在本空间仍未过期的被忽略内容哈希集合(供 Agent 弹卡前跳过)。"""
        rows = self._db.scalars(
            select(MemoryProposal.content_hash).where(
                MemoryProposal.space_id == space_id,
                MemoryProposal.user_id == user_id,
                MemoryProposal.expires_at > now,
            )
        )
        return set(rows)
