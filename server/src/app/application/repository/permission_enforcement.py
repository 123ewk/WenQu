"""实例级灰度开关仓储(OPT-48):全局单行(CHECK id=1),upsert 即"拨动"。"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.domain.models import PermissionEnforcement

SINGLETON_ID = 1


class PermissionEnforcementRepositoryImpl:
    def __init__(self, db: Session) -> None:
        self._db = db

    def get_row(self) -> PermissionEnforcement | None:
        return self._db.get(PermissionEnforcement, SINGLETON_ID)

    def upsert(self, *, enabled: bool, updated_by: uuid.UUID) -> PermissionEnforcement:
        row = self._db.get(PermissionEnforcement, SINGLETON_ID)
        if row is None:
            row = PermissionEnforcement(id=SINGLETON_ID, enabled=enabled, updated_by=updated_by)
            self._db.add(row)
        else:
            row.enabled = enabled
            row.updated_by = updated_by
        self._db.flush()
        return row
