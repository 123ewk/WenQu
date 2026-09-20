"""迁移状态检查(Go-Live 翻转项 #12 / 上线七步"迁移后 /system/info 确认非 dirty")。

只读比较 alembic 当前版本与脚本目录 head;供 prod 启动阻断与 /system/info 排障复用。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text

from app.core.db import get_session_factory

# server/alembic(与 alembic.ini 同级的 versions/ 目录)
SCRIPT_LOCATION = str(Path(__file__).resolve().parents[3] / "alembic")


@dataclass(frozen=True)
class MigrationStatus:
    current: str | None
    head: str | None

    @property
    def dirty(self) -> bool:
        """未落库、库版本落后于脚本 head、或多 head,都算 dirty(宁可报错不可放过)。"""
        return self.head is None or self.current != self.head


def migration_status() -> MigrationStatus:
    """查库比较 alembic current 与脚本 head;DB 不可达按 current=None 处理(即 dirty)。"""
    from alembic.script import ScriptDirectory  # 惰性:仅排障/启动路径需要读脚本目录

    heads = ScriptDirectory(SCRIPT_LOCATION).get_heads()
    head = heads[0] if len(heads) == 1 else None  # 多 head 视为未定义,dirty
    try:
        with get_session_factory()() as session:
            current = session.execute(
                text("SELECT version_num FROM alembic_version LIMIT 1")
            ).scalar()
    except Exception:  # noqa: BLE001 - 排障入口与启动自检都不因查库异常而崩,统一归 dirty
        return MigrationStatus(current=None, head=head)
    return MigrationStatus(current=str(current) if current else None, head=head)
