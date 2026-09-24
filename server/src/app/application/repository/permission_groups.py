"""权限档位仓储(OPT-41):档位本身 + 成员挂载关系。

查询一律带 `space_id` 范围(空间是租户边界);是否越权由服务层守卫判定。
"""

from __future__ import annotations

import uuid

from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy.orm import Session

from app.domain.models import PermissionGroup, PermissionGroupMember


def _rowcount(result: object) -> int:
    """DML 的 rowcount 只存在于 CursorResult(ORM 类型签名未暴露)。"""
    if isinstance(result, CursorResult):
        return int(result.rowcount or 0)
    return 0


class PermissionGroupRepositoryImpl:
    def __init__(self, db: Session) -> None:
        self._db = db

    # ---------------------------- 档位 ----------------------------

    def get(self, group_id: uuid.UUID) -> PermissionGroup | None:
        return self._db.get(PermissionGroup, group_id)

    def create(self, group: PermissionGroup) -> PermissionGroup:
        self._db.add(group)
        self._db.flush()
        return group

    def save(self, group: PermissionGroup) -> PermissionGroup:
        self._db.flush()
        return group

    def delete(self, group_id: uuid.UUID) -> None:
        """只删档位行;成员挂载与凭据的连带风险由服务层的删除门槛管住,不做隐式级联。"""
        group = self._db.get(PermissionGroup, group_id)
        if group is not None:
            self._db.delete(group)
            self._db.flush()

    def list_for_space(self, space_id: uuid.UUID) -> list[PermissionGroup]:
        return list(
            self._db.scalars(
                select(PermissionGroup)
                .where(PermissionGroup.space_id == space_id)
                .order_by(PermissionGroup.created_at)
            )
        )

    def list_by_ids(self, group_ids: list[uuid.UUID]) -> list[PermissionGroup]:
        if not group_ids:
            return []
        return list(
            self._db.scalars(
                select(PermissionGroup).where(PermissionGroup.id.in_(group_ids))
            )
        )

    # ---------------------------- 成员挂载 ----------------------------

    def add_member(self, member: PermissionGroupMember) -> PermissionGroupMember:
        self._db.add(member)
        self._db.flush()
        return member

    def list_members(self, group_id: uuid.UUID) -> list[PermissionGroupMember]:
        return list(
            self._db.scalars(
                select(PermissionGroupMember)
                .where(PermissionGroupMember.group_id == group_id)
                .order_by(PermissionGroupMember.created_at)
            )
        )

    def list_members_for_space(self, space_id: uuid.UUID) -> list[PermissionGroupMember]:
        return list(
            self._db.scalars(
                select(PermissionGroupMember).where(
                    PermissionGroupMember.space_id == space_id
                )
            )
        )

    def get_member(
        self, group_id: uuid.UUID, user_id: uuid.UUID
    ) -> PermissionGroupMember | None:
        return self._db.scalar(
            select(PermissionGroupMember).where(
                PermissionGroupMember.group_id == group_id,
                PermissionGroupMember.user_id == user_id,
            )
        )

    def get_current(
        self, space_id: uuid.UUID, user_id: uuid.UUID
    ) -> PermissionGroupMember | None:
        return self._db.scalar(
            select(PermissionGroupMember).where(
                PermissionGroupMember.space_id == space_id,
                PermissionGroupMember.user_id == user_id,
                PermissionGroupMember.is_current.is_(True),
            )
        )

    def clear_current(self, space_id: uuid.UUID, user_id: uuid.UUID) -> int:
        """把某人当前的档位指针全部清掉(设新指针前调用)。返回影响行数。"""
        return _rowcount(
            self._db.execute(
                update(PermissionGroupMember)
                .where(
                    PermissionGroupMember.space_id == space_id,
                    PermissionGroupMember.user_id == user_id,
                    PermissionGroupMember.is_current.is_(True),
                )
                .values(is_current=False)
            )
        )

    def delete_member(self, member_id: uuid.UUID) -> None:
        member = self._db.get(PermissionGroupMember, member_id)
        if member is not None:
            self._db.delete(member)
            self._db.flush()

    def delete_for_user(self, space_id: uuid.UUID, user_id: uuid.UUID) -> int:
        """成员被移出空间时清理其全部挂载(移出 ≠ 删用户,不能靠 FK 级联)。"""
        return _rowcount(
            self._db.execute(
                delete(PermissionGroupMember).where(
                    PermissionGroupMember.space_id == space_id,
                    PermissionGroupMember.user_id == user_id,
                )
            )
        )
