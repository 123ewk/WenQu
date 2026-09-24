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

    def list_for_group(self, group_id: uuid.UUID) -> list[ApiKey]:
        """某档位下的全部凭据(OPT-44:删档守卫与"清空历史凭据"都读这一份,口径不会分叉)。"""
        return list(
            self._db.scalars(
                select(ApiKey)
                .where(ApiKey.group_id == group_id)
                .order_by(ApiKey.created_at)
            )
        )

    def delete(self, api_key: ApiKey) -> None:
        """彻底删除台账行(不可逆)。调用方必须先写出审计快照 —— 行没了归因就只剩审计。"""
        self._db.delete(api_key)
        self._db.flush()
