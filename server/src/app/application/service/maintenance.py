"""周期性维护任务(M1 登记、M2 交付):审计保留期清扫 + 过期 refresh 令牌清除。

两者都是"低频、幂等、可重复执行"的清理,放在 worker 循环里按固定间隔触发;
单次执行失败只记日志,不影响主流程(清理延后一轮即可)。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import CursorResult, delete
from sqlalchemy.orm import Session

from app.domain.models import AuditLog, RefreshToken

logger = logging.getLogger("app.maintenance")


def _rowcount(result: object) -> int:
    """DML 的 rowcount 只存在于 CursorResult(ORM 类型签名未暴露)。"""
    if isinstance(result, CursorResult):
        return int(result.rowcount or 0)
    return 0


class MaintenanceService:
    def __init__(self, db: Session, audit_retention_days: int) -> None:
        self._db = db
        self._audit_retention_days = audit_retention_days

    def purge_expired_refresh_tokens(self) -> int:
        """删除已过期令牌:过期行不参与认证,留着只占空间并拖慢按哈希查找。"""
        result = self._db.execute(
            delete(RefreshToken).where(RefreshToken.expires_at < datetime.now(UTC))
        )
        self._db.commit()
        removed = _rowcount(result)
        if removed:
            logger.info("purged %d expired refresh tokens", removed)
        return removed

    def purge_old_audit_logs(self) -> int:
        """按保留期删除审计日志(基准 04:保留期自动清扫,不无限增长)。"""
        cutoff = datetime.now(UTC) - timedelta(days=self._audit_retention_days)
        result = self._db.execute(delete(AuditLog).where(AuditLog.created_at < cutoff))
        self._db.commit()
        removed = _rowcount(result)
        if removed:
            logger.info(
                "purged %d audit logs older than %d days",
                removed,
                self._audit_retention_days,
            )
        return removed

    def run_all(self) -> tuple[int, int]:
        return self.purge_expired_refresh_tokens(), self.purge_old_audit_logs()
