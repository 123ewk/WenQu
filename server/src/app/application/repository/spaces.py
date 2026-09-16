"""空间与成员仓储。

注意(基准 04):空间是租户边界,服务层守卫负责"先成员校验(404)后角色校验(403)";
仓储只提供按 space_id 范围的查询,不做越权判断。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models import Membership, Space, User


class SpaceRepositoryImpl:
    def __init__(self, db: Session) -> None:
        self._db = db

    def get(self, space_id: uuid.UUID) -> Space | None:
        return self._db.get(Space, space_id)

    def create(self, space: Space) -> Space:
        self._db.add(space)
        self._db.flush()
        return space

    def save(self, space: Space) -> Space:
        self._db.flush()
        return space

    def delete(self, space: Space) -> None:
        self._db.delete(space)
        self._db.flush()

    def list_for_user(self, user_id: uuid.UUID) -> list[tuple[Space, int]]:
        rows = self._db.execute(
            select(Space, Membership.role)
            .join(Membership, Membership.space_id == Space.id)
            .where(Membership.user_id == user_id)
            .order_by(Space.created_at)
        ).all()
        return [(space, role) for space, role in rows]

    def get_membership(self, space_id: uuid.UUID, user_id: uuid.UUID) -> Membership | None:
        return self._db.scalar(
            select(Membership).where(
                Membership.space_id == space_id, Membership.user_id == user_id
            )
        )

    def get_member(self, space_id: uuid.UUID, member_user_id: uuid.UUID) -> Membership | None:
        return self.get_membership(space_id, member_user_id)

    def list_members(self, space_id: uuid.UUID) -> list[tuple[Membership, User]]:
        rows = self._db.execute(
            select(Membership, User)
            .join(User, User.id == Membership.user_id)
            .where(Membership.space_id == space_id)
            .order_by(Membership.created_at)
        ).all()
        return [(membership, user) for membership, user in rows]

    def add_member(self, membership: Membership) -> Membership:
        self._db.add(membership)
        self._db.flush()
        return membership

    def update_member(self, membership: Membership) -> Membership:
        self._db.flush()
        return membership

    def remove_member(self, membership: Membership) -> None:
        self._db.delete(membership)
        self._db.flush()
