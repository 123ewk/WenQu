"""空间级 Langfuse 配置仓储(OPT-49 v2):每空间一行,upsert 即"保存该空间配置"。"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.domain.models import SpaceLangfuseConfig


class SpaceLangfuseConfigRepositoryImpl:
    def __init__(self, db: Session) -> None:
        self._db = db

    def get_row(self, space_id: uuid.UUID) -> SpaceLangfuseConfig | None:
        return self._db.get(SpaceLangfuseConfig, space_id)

    def upsert(
        self,
        *,
        space_id: uuid.UUID,
        host: str,
        console_url: str,
        public_key: str,
        secret_key_encrypted: str,
        key_hint: str,
        enabled: bool,
        updated_by: uuid.UUID,
    ) -> SpaceLangfuseConfig:
        row = self._db.get(SpaceLangfuseConfig, space_id)
        if row is None:
            row = SpaceLangfuseConfig(
                space_id=space_id,
                host=host,
                console_url=console_url,
                public_key=public_key,
                secret_key_encrypted=secret_key_encrypted,
                key_hint=key_hint,
                enabled=enabled,
                updated_by=updated_by,
            )
            self._db.add(row)
        else:
            row.host = host
            row.console_url = console_url
            row.public_key = public_key
            row.secret_key_encrypted = secret_key_encrypted
            row.key_hint = key_hint
            row.enabled = enabled
            row.updated_by = updated_by
        self._db.flush()
        return row
