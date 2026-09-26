"""实例级灰度开关的读值与拨动服务(OPT-48)。

读值口径(§4.1-1/3):DB 有行用 DB,无行回落 env(默认 False)——库里什么都不配 =
行为与今天逐字节一致。全库唯一读点在 `deps._member_perm_for`,每个带 JWT 的请求都要
拿到这个布尔值,而 `get_settings()` 已被 @lru_cache 固化(env 值进程内本就不热更),
所以 DB 值必须进程内缓存:

- **本进程**:写侧拨动后调用 `clear_enforcement_cache()` 主动失效,下一次读取立刻
  拿到新值(A2:PUT 之后本进程立刻生效,不用重启);
- **其它 worker**:最多 TTL 秒后跟上。开关不是权限本身,晚 60s 只是晚 60s 开始拒绝,
  不会造成越权(滞后方向是 fail-closed 的);
- **无锁**:最坏情况是两个线程同时未命中、各查一次库并写回同一个值,无害。

拨动(D2 修订版,2026-09-26 用户拍板):superuser 或**任意空间 Admin+(role≥30)**可拨,
守卫在路由层;服务层只负责 D3 自锁守卫 —— **开启时**操作者本人必须在自己所在的每个
空间都有生效档位,缺任何一个就 409(防"一开第一个被锁死的是自己");关闭不会锁任何人,
不做该校验。其他成员缺档位只做统计展示,不拦(A6)。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy.orm import Session

from app.application.repository.permission_enforcement import (
    PermissionEnforcementRepositoryImpl,
)
from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.domain.enums import AuditAction
from app.domain.interfaces import AuditRepository, PermissionEnforcementRepository, UserRepository
from app.domain.models import AuditLog, User


@dataclass(frozen=True)
class ResolvedEnforcement:
    """一次解析的开关快照:值 + 来源(契约里的 `effective` / `source`)。"""

    enabled: bool
    source: Literal["db", "env_fallback"]


class EnforcementTtlCache:
    """进程内 TTL 缓存(monotonic 时钟,不受系统对时影响)。"""

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl_seconds = ttl_seconds
        self._value: ResolvedEnforcement | None = None
        self._at: float = 0.0

    def get(self) -> ResolvedEnforcement | None:
        """TTL 内返回缓存值;过期或从未写入返回 None(None = 必须打库)。"""
        if self._value is None:
            return None
        if time.monotonic() - self._at >= self._ttl_seconds:
            return None
        return self._value

    def put(self, value: ResolvedEnforcement) -> None:
        self._value = value
        self._at = time.monotonic()

    def clear(self) -> None:
        self._value = None


_cache = EnforcementTtlCache(get_settings().permission_enforcement_cache_ttl_seconds)


def clear_enforcement_cache() -> None:
    """显式失效入口:写侧拨动后与测试夹具共用;清空后下一次读取必然重新打库。"""
    _cache.clear()


def resolve_enforcement(db: Session) -> ResolvedEnforcement:
    """当前生效值与来源;除 TTL 未命中外不打库。"""
    cached = _cache.get()
    if cached is not None:
        return cached
    row = PermissionEnforcementRepositoryImpl(db).get_row()
    resolved = (
        ResolvedEnforcement(enabled=row.enabled, source="db")
        if row is not None
        else ResolvedEnforcement(
            enabled=get_settings().require_permission_group_for_core, source="env_fallback"
        )
    )
    _cache.put(resolved)
    return resolved


def permission_enforcement_enabled(db: Session) -> bool:
    """人链路读点(deps._member_perm_for)用:当前是否强制"生效档位"。"""
    return resolve_enforcement(db).enabled


@dataclass(frozen=True)
class SpaceGap:
    """操作者有成员身份、但没有生效档位的空间(409 details 与 impact 统计共用)。"""

    space_id: uuid.UUID
    space_name: str


@dataclass(frozen=True)
class EnforcementStatus:
    """GET/PUT 的响应载荷(路由层再映射成契约 Out 模型)。"""

    effective: bool
    source: Literal["db", "env_fallback"]
    updated_at: datetime | None
    updated_by_hint: str | None  # 最后拨动人的用户名(展示用 hint,不是 id)
    spaces: int
    members_total: int
    members_without_active_group: int
    actor_spaces_without_group: list[SpaceGap]

    @property
    def actor_has_active_group(self) -> bool:
        """操作者本人是否处处有生效档(所在每个空间都有 current 挂载;无成员身份 = 真空成立)。"""
        return not self.actor_spaces_without_group


class PermissionEnforcementService:
    """状态读取与拨动;自锁校验(D3)与审计在这里,路由层只做身份守卫。"""

    def __init__(
        self,
        db: Session,
        repo: PermissionEnforcementRepository,
        users: UserRepository,
        audit: AuditRepository,
    ) -> None:
        # db 仅喂给 resolve_enforcement 的缓存读值路径;行/统计一律走注入的 repo
        self._db = db
        self._repo = repo
        self._users = users
        self._audit = audit

    def status(self, actor: User) -> EnforcementStatus:
        resolved = resolve_enforcement(self._db)
        row = self._repo.get_row()
        spaces, members_total, members_without = self._repo.impact_counts()
        gaps = [
            SpaceGap(space_id=sid, space_name=name)
            for sid, name in self._repo.spaces_without_current_group(actor.id)
        ]
        updated_by_hint: str | None = None
        if row is not None and row.updated_by is not None:
            updater = self._users.get(row.updated_by)
            updated_by_hint = updater.username if updater is not None else None
        return EnforcementStatus(
            effective=resolved.enabled,
            source=resolved.source,
            updated_at=row.updated_at if row is not None else None,
            updated_by_hint=updated_by_hint,
            spaces=spaces,
            members_total=members_total,
            members_without_active_group=members_without,
            actor_spaces_without_group=gaps,
        )

    def set_enabled(self, actor: User, enabled: bool, ip: str = "") -> EnforcementStatus:
        """拨动:开启前跑 D3 自锁校验;写行 → 失效本进程缓存 → 审计留痕(含新旧值)。"""
        previous = resolve_enforcement(self._db)
        if enabled and not previous.enabled:
            gaps = self._repo.spaces_without_current_group(actor.id)
            if gaps:
                raise AppError(
                    ErrorCode.PERMISSION_ENFORCEMENT_ACTOR_HAS_NO_GROUP,
                    "打开前先给自己配好生效档位:你在以下空间还没有生效档位,"
                    "打开后会立即被这些空间的核心接口拦截",
                    http_status=409,
                    details={
                        "spaces": [
                            {"space_id": str(sid), "space_name": name} for sid, name in gaps
                        ]
                    },
                )
        self._repo.upsert(enabled=enabled, updated_by=actor.id)
        # A2:本进程立刻生效;其它 worker 最多 TTL 秒后跟上(模块 docstring 的口径)
        clear_enforcement_cache()
        self._audit.add(
            AuditLog(
                actor_id=actor.id,
                space_id=None,  # 全局动作,不属于任何空间(D1)
                action=str(AuditAction.PERMISSION_ENFORCEMENT_TOGGLED),
                target="permission_enforcement",
                detail={"old": previous.enabled, "new": enabled, "old_source": previous.source},
                ip=ip,
            )
        )
        return self.status(actor)
