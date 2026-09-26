"""实例级灰度开关仓储(OPT-48):全局单行(CHECK id=1),upsert 即"拨动"。

影响面统计(§4.1-2 / D3)也在这里:成员关系表是 **memberships**(没有 space_members),
"有生效档" = permission_group_members 里存在 is_current 挂载行。
"""

from __future__ import annotations

import uuid

from sqlalchemy import ColumnElement, func, not_, select
from sqlalchemy.orm import Session

from app.domain.models import Membership, PermissionEnforcement, PermissionGroupMember, Space

SINGLETON_ID = 1


def _no_current_mount_clause() -> ColumnElement[bool]:
    """(space_id, user_id) 上不存在 is_current 挂载行 = 该成员在本空间没有生效档。"""
    has_mount = select(PermissionGroupMember.id).where(
        PermissionGroupMember.space_id == Membership.space_id,
        PermissionGroupMember.user_id == Membership.user_id,
        PermissionGroupMember.is_current.is_(True),
    ).exists()
    return not_(has_mount)


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

    def impact_counts(self) -> tuple[int, int, int]:
        """(空间总数, 成员关系总数, 其中没有生效档的成员关系数)。

        统计口径是"成员关系"而不是"人":同一人在多个空间各算一条,
        打开开关锁的也是"某空间里的这个人"。
        """
        spaces = self._db.scalar(select(func.count()).select_from(Space))
        members_total = self._db.scalar(select(func.count()).select_from(Membership))
        members_without = self._db.scalar(
            select(func.count()).select_from(Membership).where(_no_current_mount_clause())
        )
        return int(spaces or 0), int(members_total or 0), int(members_without or 0)

    def spaces_without_current_group(self, user_id: uuid.UUID) -> list[tuple[uuid.UUID, str]]:
        """该用户有成员身份、但没有生效档位的空间(id, name),按空间名排序。"""
        rows = self._db.execute(
            select(Space.id, Space.name)
            .join(Membership, Membership.space_id == Space.id)
            .where(Membership.user_id == user_id, _no_current_mount_clause())
            .order_by(Space.name)
        ).all()
        return [(sid, name) for sid, name in rows]
