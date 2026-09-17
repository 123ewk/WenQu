"""API Key 仓储(OPT-6):认证热路径按哈希唯一索引等值查找。

查询一律带 space_id 范围(空间是租户边界);是否越权由服务层守卫判定。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models import ApiKey


class ApiKeyRepositoryImpl:
    def __init__(self, db: Session) -> None:
        self._db = db

    def get_by_hash(self, key_hash: str) -> ApiKey | None:
        return self._db.scalar(select(ApiKey).where(ApiKey.key_hash == key_hash))

    def get(self, key_id: uuid.UUID) -> ApiKey | None:
        return self._db.get(ApiKey, key_id)

    def create(self, api_key: ApiKey) -> ApiKey:
        self._db.add(api_key)
        self._db.flush()
        return api_key

    def save(self, api_key: ApiKey) -> ApiKey:
        self._db.flush()
        return api_key

    def list_for_space(self, space_id: uuid.UUID) -> list[ApiKey]:
        return list(
            self._db.scalars(
                select(ApiKey)
                .where(ApiKey.space_id == space_id)
                .order_by(ApiKey.created_at)
            )
        )
