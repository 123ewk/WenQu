"""Langfuse 可观测配置仓储(OPT-49):全局单行(CHECK id=1),upsert 即"保存配置"。"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.domain.models import LangfuseConfig

SINGLETON_ID = 1


class LangfuseConfigRepositoryImpl:
    def __init__(self, db: Session) -> None:
        self._db = db

    def get_row(self) -> LangfuseConfig | None:
        return self._db.get(LangfuseConfig, SINGLETON_ID)

    def upsert(
        self,
        *,
        host: str,
        public_key: str,
        secret_key_encrypted: str,
        key_hint: str,
        enabled: bool,
        updated_by: uuid.UUID,
    ) -> LangfuseConfig:
        row = self._db.get(LangfuseConfig, SINGLETON_ID)
        if row is None:
            row = LangfuseConfig(
                id=SINGLETON_ID,
                host=host,
                public_key=public_key,
                secret_key_encrypted=secret_key_encrypted,
                key_hint=key_hint,
                enabled=enabled,
                updated_by=updated_by,
            )
            self._db.add(row)
        else:
            row.host = host
            row.public_key = public_key
            row.secret_key_encrypted = secret_key_encrypted
            row.key_hint = key_hint
            row.enabled = enabled
            row.updated_by = updated_by
        self._db.flush()
        return row
