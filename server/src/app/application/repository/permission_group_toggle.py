"""空间级权限挡位功能开关仓储(OPT-48 v2):每空间一行,upsert 即"拨动"。

本空间的影响面统计也在这里:成员关系表是 **memberships**(没有 space_members),
"有生效挡" = permission_group_members 里存在 is_current 挂载行。
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.models import Membership, PermissionGroupMember, SpacePermissionGroupToggle


def _no_current_mount_clause(space_id: uuid.UUID):
    has_mount = select(PermissionGroupMember.id).where(
        PermissionGroupMember.space_id == space_id,
        PermissionGroupMember.user_id == Membership.user_id,
        PermissionGroupMember.is_current.is_(True),
    ).exists()
    return ~has_mount


class SpacePermissionGroupToggleRepositoryImpl:
    def __init__(self, db: Session) -> None:
        self._db = db

    def get_row(self, space_id: uuid.UUID) -> SpacePermissionGroupToggle | None:
        return self._db.get(SpacePermissionGroupToggle, space_id)

    def upsert(
        self, *, space_id: uuid.UUID, enabled: bool, updated_by: uuid.UUID
    ) -> SpacePermissionGroupToggle:
        row = self._db.get(SpacePermissionGroupToggle, space_id)
        if row is None:
            row = SpacePermissionGroupToggle(
                space_id=space_id, enabled=enabled, updated_by=updated_by
            )
            self._db.add(row)
        else:
            row.enabled = enabled
            row.updated_by = updated_by
        self._db.flush()
        return row

    def impact(self, space_id: uuid.UUID) -> tuple[int, int]:
        """(本空间成员关系总数, 其中没有生效挡的数量)。同一人只算本空间这一条。"""
        members_total = self._db.scalar(
            select(func.count())
            .select_from(Membership)
            .where(Membership.space_id == space_id)
        )
        members_without = self._db.scalar(
            select(func.count())
            .select_from(Membership)
            .where(Membership.space_id == space_id, _no_current_mount_clause(space_id))
        )
        return int(members_total or 0), int(members_without or 0)

    def actor_has_active_group(self, space_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """该用户在本空间是否有生效挡(非成员 = False)。"""
        return (
            self._db.scalar(
                select(func.count())
                .select_from(PermissionGroupMember)
                .where(
                    PermissionGroupMember.space_id == space_id,
                    PermissionGroupMember.user_id == user_id,
                    PermissionGroupMember.is_current.is_(True),
                )
            )
            or 0
        ) > 0
