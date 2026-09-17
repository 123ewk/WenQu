"""审计日志仓储:只追加 + 按空间分页查询。保留期清扫任务随 M2 worker 交付。"""

from __future__ import annotations

import uuid
from datetime import datetime

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
        self,
        space_id: uuid.UUID,
        limit: int,
        offset: int,
        action: str | None = None,
        actor_id: uuid.UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> tuple[list[AuditLog], int]:
        # 时间筛选两端均为闭区间(前端日期控件选"到某天"时,当天记录不能被漏掉)
        conditions = [AuditLog.space_id == space_id]
        if action is not None:
            conditions.append(AuditLog.action == action)
        if actor_id is not None:
            conditions.append(AuditLog.actor_id == actor_id)
        if since is not None:
            conditions.append(AuditLog.created_at >= since)
        if until is not None:
            conditions.append(AuditLog.created_at <= until)
        total = self._db.scalar(
            select(func.count()).select_from(AuditLog).where(*conditions)
        )
        rows = self._db.scalars(
            select(AuditLog)
            .where(*conditions)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        return list(rows), int(total or 0)
