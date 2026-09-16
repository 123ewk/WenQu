"""用户仓储。所有查询只按主键/唯一键定位,无跨租户数据(用户是全局资源)。"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models import User


class UserRepositoryImpl:
    def __init__(self, db: Session) -> None:
        self._db = db

    def get(self, user_id: uuid.UUID) -> User | None:
        return self._db.get(User, user_id)

    def get_by_username(self, username: str) -> User | None:
        return self._db.scalar(select(User).where(User.username == username))

    def create(self, user: User) -> User:
        self._db.add(user)
        self._db.flush()
        return user

    def save(self, user: User) -> User:
        self._db.flush()
        return user
