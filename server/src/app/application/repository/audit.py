"""审计日志仓储:只追加 + 按空间分页查询。保留期清扫任务随 M2 worker 交付。"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.models import AuditLog


class AuditRepositoryImpl:
    def __init__(self, db: Session) -> None:
        self._db = db

    def add(self, log: AuditLog) -> AuditLog:
        self._db.add(log)
        self._db.flush()
        return log

    def list_for_space(
        self, space_id: uuid.UUID, limit: int, offset: int
    ) -> tuple[list[AuditLog], int]:
        total = self._db.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.space_id == space_id)
        )
        rows = self._db.scalars(
            select(AuditLog)
            .where(AuditLog.space_id == space_id)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        return list(rows), int(total or 0)
