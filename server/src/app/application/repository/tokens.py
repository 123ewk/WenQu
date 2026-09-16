"""refresh 令牌仓储:只读写哈希列,明文令牌不落任何存储(基准 04)。"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.domain.models import RefreshToken


class RefreshTokenRepositoryImpl:
    def __init__(self, db: Session) -> None:
        self._db = db

    def get_by_hash(self, token_hash: str) -> RefreshToken | None:
        return self._db.scalar(select(RefreshToken).where(RefreshToken.token_hash == token_hash))

    def create(self, token: RefreshToken) -> RefreshToken:
        self._db.add(token)
        self._db.flush()
        return token

    def delete(self, token: RefreshToken) -> None:
        self._db.delete(token)
        self._db.flush()

    def delete_by_user(self, user_id: uuid.UUID) -> None:
        self._db.execute(delete(RefreshToken).where(RefreshToken.user_id == user_id))
