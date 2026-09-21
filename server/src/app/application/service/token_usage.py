"""空间 token 用量汇总(OPT-28 读侧):按成员与空间合计出账。

三条口径:
1. **只汇总不换算**:数字原样来自 `token_usage_events`;`source=estimated` 的行的
   total 单独累加进 `estimated_tokens`,前端据此打 "≈" 角标 —— 厂商回传与本地估算
   两种口径不许混成一个数字对外。
2. **从今往后计**:表里没有事件只代表那会儿还没上计量(不回填历史),所以 `period=all`
   的实际含义是"自计量上线以来"。
3. **归属可空不可丢**:`user_id` 为空(上传者已注销)或归属人已退空间/已注销的既成用量
   照样进 `space_total`,但不进 `members[]` —— 成员表是当前花名册,不是历史归属人。

门槛沿用入库服务的口径(`KnowledgeService._require_role`):非成员与"空间不存在"同壳
404(不泄露存在性),按成员汇总要 Admin+,看自己只要成员。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import AppError, ErrorCode
from app.domain.enums import Role, UsageKind, UsageSource
from app.domain.interfaces import SpaceRepository
from app.domain.models import TokenUsageEvent

Period = Literal["7d", "30d", "all"]
DEFAULT_PERIOD: Period = "30d"

_PERIOD_DAYS: dict[str, int] = {"7d": 7, "30d": 30}


@dataclass
class _Bucket:
    """一组事件的累加器(一个成员,或整个空间合计)。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_tokens: int = 0
    chat_calls: int = 0
    embedding_calls: int = 0

    def add(
        self,
        kind: str,
        source: str,
        prompt: int,
        completion: int,
        total: int,
        calls: int,
    ) -> None:
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.total_tokens += total
        if source == UsageSource.ESTIMATED:
            self.estimated_tokens += total
        if kind == UsageKind.CHAT:
            self.chat_calls += calls
        else:
            self.embedding_calls += calls

    def as_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "estimated_tokens": self.estimated_tokens,
            "chat_calls": self.chat_calls,
            "embedding_calls": self.embedding_calls,
        }


class UsageService:
    def __init__(self, db: Session, spaces: SpaceRepository) -> None:
        self._db = db
        self._spaces = spaces

    # ---------------------------- 端点入口 ----------------------------

    def summary_for_admin(
        self, user_id: uuid.UUID, space_id: uuid.UUID, period: str
    ) -> dict[str, Any]:
        """Admin+:空间合计 + 逐成员明细(看别人的用量是管理员特权)。"""
        self._require(user_id, space_id, Role.ADMIN)
        since = _since(period)
        totals, by_user = self._aggregate(space_id, since)
        members: list[dict[str, Any]] = [
            {
                "user_id": str(membership.user_id),
                "username": user.username,
                "nickname": user.nickname,
                "role": membership.role,
                **by_user.pop(membership.user_id, _Bucket()).as_dict(),
            }
            for membership, user in self._spaces.list_members(space_id)
        ]
        # 花名册外的归属人(已退空间/已注销)只留在 space_total,见模块头口径 3
        members.sort(key=lambda row: (-row["total_tokens"], row["username"]))
        return {
            "period": period,
            "since": since.isoformat() if since is not None else None,
            "generated_at": _now().isoformat(),
            "space_total": totals.as_dict(),
            "members": members,
        }

    def my_usage(
        self, user_id: uuid.UUID, space_id: uuid.UUID, period: str
    ) -> dict[str, Any]:
        """任意成员:只看自己那一行(非成员一律 404)。"""
        self._require(user_id, space_id, Role.VIEWER)
        since = _since(period)
        bucket = self._aggregate(space_id, since, only_user_id=user_id)[1].get(
            user_id, _Bucket()
        )
        return {
            "period": period,
            "since": since.isoformat() if since is not None else None,
            "generated_at": _now().isoformat(),
            "usage": bucket.as_dict(),
        }

    # ---------------------------- 内部 ----------------------------

    def _require(self, user_id: uuid.UUID, space_id: uuid.UUID, minimum: Role) -> None:
        membership = self._spaces.get_membership(space_id, user_id)
        if membership is None:
            raise AppError(ErrorCode.SPACE_NOT_FOUND, "空间不存在", http_status=404)
        if membership.role < minimum:
            raise AppError(ErrorCode.FORBIDDEN, "需要空间管理员权限", http_status=403)

    def _aggregate(
        self,
        space_id: uuid.UUID,
        since: datetime | None,
        only_user_id: uuid.UUID | None = None,
    ) -> tuple[_Bucket, dict[uuid.UUID, _Bucket]]:
        """一次分组查询:按 (归属人, 类别, 口径) 聚合。

        行数上界 = 成员数 × 2 类别 × 2 口径(V1 体量下几十行),明细行式扫描走
        `ix_usage_space_created` / `ix_usage_space_user`,不预聚合。
        """
        stmt = (
            select(
                TokenUsageEvent.user_id,
                TokenUsageEvent.kind,
                TokenUsageEvent.source,
                func.coalesce(func.sum(TokenUsageEvent.prompt_tokens), 0),
                func.coalesce(func.sum(TokenUsageEvent.completion_tokens), 0),
                func.coalesce(func.sum(TokenUsageEvent.total_tokens), 0),
                func.count(),
            )
            .where(TokenUsageEvent.space_id == space_id)
            .group_by(
                TokenUsageEvent.user_id, TokenUsageEvent.kind, TokenUsageEvent.source
            )
        )
        if since is not None:
            stmt = stmt.where(TokenUsageEvent.created_at >= since)
        if only_user_id is not None:
            stmt = stmt.where(TokenUsageEvent.user_id == only_user_id)

        totals = _Bucket()
        by_user: dict[uuid.UUID, _Bucket] = {}
        for user_id, kind, source, prompt, completion, total, calls in self._db.execute(
            stmt
        ):
            totals.add(kind, source, prompt, completion, total, calls)
            if user_id is None:
                continue  # 无归属人的行只进空间合计
            by_user.setdefault(user_id, _Bucket()).add(
                kind, source, prompt, completion, total, calls
            )
        return totals, by_user


def _now() -> datetime:
    return datetime.now(UTC)


def _since(period: str) -> datetime | None:
    days = _PERIOD_DAYS.get(period)
    return None if days is None else _now() - timedelta(days=days)
