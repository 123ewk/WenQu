"""空间级「权限挡位功能开关」的读值与拨动服务(OPT-48 v2)。

2026-09-26 用户重述需求后的口径(替代初版"全局单行开关"):
- **粒度 = 每空间一个**:表无行 = 该空间从未拨过 = 只看角色(与今天逐字节一致);
- **只挡核心功能清单**(问答/检索/会话流/知识库与文档,`route_in_dual_list`);
  成员/模型配置/审计/用量等管理页继续只看角色;
- **谁能拨** = 该空间 Admin+(role≥30),superuser 兜底(守卫在路由层 get_space_toggle_manager);
- **不拦截"操作者自己没挡"**(没挡也能开,开完可经两块管理功能自救),页面仅提示;
- **分配/换当前挡位只在开关打开后可用**(字面口径):路由层 ensure_space_toggle_on
  对没开的空间返回 409 PERMISSION_GROUP_TOGGLE_OFF。

读值缓存口径:全库唯一读点在 `deps._member_perm_for`,每个带 JWT 的请求都要拿到
本空间的布尔值,所以按 **space_id** 做**进程内 TTL 缓存**:

- **本进程**:拨动后按 space_id 精确失效,下一次读取立刻拿到新值(不用重启);
- **其它 worker**:最多 TTL 秒后跟上。开关不是权限本身,晚 60s 只是晚 60s 开始拒绝,
  不会造成越权(滞后方向是 fail-closed 的);
- **无锁**:最坏情况是两个线程同时未命中、各查一次库并写回同一个值,无害。

v2 不再有 env 兜底:初版的 `APP_REQUIRE_PERMISSION_GROUP_FOR_CORE` 全局口径随需求重述退役,
"什么都不配 = 只看角色"由"表无行 = 关"直接保证。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.application.repository.permission_group_toggle import (
    SpacePermissionGroupToggleRepositoryImpl,
)
from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.domain.enums import AuditAction
from app.domain.interfaces import (
    AuditRepository,
    SpacePermissionGroupToggleRepository,
    UserRepository,
)
from app.domain.models import AuditLog, Space, User


@dataclass(frozen=True)
class SpaceToggleImpact:
    """本空间的影响面(打开前的自查与"会锁多少人"的预览;缺挡只展示不拦)。"""

    members_total: int
    members_without_active_group: int
    actor_has_active_group: bool


@dataclass(frozen=True)
class SpaceToggleStatus:
    """GET/PUT 的响应载荷(路由层再映射成契约 Out 模型)。"""

    enabled: bool
    updated_at: datetime | None
    updated_by_hint: str | None  # 最后拨动人用户名(展示用);表无行恒 None
    impact: SpaceToggleImpact


class _SpaceToggleTtlCache:
    """按 space_id 分键的进程内 TTL 缓存(monotonic 时钟,不受系统对时影响)。"""

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl_seconds = ttl_seconds
        self._entries: dict[uuid.UUID, tuple[bool, float]] = {}

    def get(self, space_id: uuid.UUID) -> bool | None:
        """TTL 内返回缓存值;过期或从未写入返回 None(None = 必须打库)。"""
        entry = self._entries.get(space_id)
        if entry is None:
            return None
        value, at = entry
        if time.monotonic() - at >= self._ttl_seconds:
            return None
        return value

    def put(self, space_id: uuid.UUID, value: bool) -> None:
        self._entries[space_id] = (value, time.monotonic())

    def clear(self, space_id: uuid.UUID | None = None) -> None:
        if space_id is None:
            self._entries.clear()
        else:
            self._entries.pop(space_id, None)


_cache = _SpaceToggleTtlCache(get_settings().permission_group_toggle_cache_ttl_seconds)


def clear_space_toggle_cache(space_id: uuid.UUID | None = None) -> None:
    """显式失效入口:写侧拨动后按空间失效,测试夹具全清;清空后下一次读取必然打库。"""
    _cache.clear(space_id)


def space_toggle_enabled(db: Session, space_id: uuid.UUID) -> bool:
    """人链路读点(deps._member_perm_for)用:该空间当前是否强制"生效挡位"。"""
    cached = _cache.get(space_id)
    if cached is not None:
        return cached
    row = SpacePermissionGroupToggleRepositoryImpl(db).get_row(space_id)
    value = row.enabled if row is not None else False
    _cache.put(space_id, value)
    return value


class PermissionGroupToggleService:
    """状态读取与拨动;路由层负责"该空间 Admin+"守卫,这里负责写行/失效/审计。"""

    def __init__(
        self,
        db: Session,
        repo: SpacePermissionGroupToggleRepository,
        users: UserRepository,
        audit: AuditRepository,
    ) -> None:
        # db 仅喂缓存读值路径与空间存在性检查;行/统计一律走注入的 repo
        self._db = db
        self._repo = repo
        self._users = users
        self._audit = audit

    def status(self, actor: User, space_id: uuid.UUID) -> SpaceToggleStatus:
        self._ensure_space_exists(space_id)
        row = self._repo.get_row(space_id)
        members_total, members_without = self._repo.impact(space_id)
        updated_by_hint: str | None = None
        if row is not None and row.updated_by is not None:
            updater = self._users.get(row.updated_by)
            updated_by_hint = updater.username if updater is not None else None
        return SpaceToggleStatus(
            enabled=row.enabled if row is not None else False,
            updated_at=row.updated_at if row is not None else None,
            updated_by_hint=updated_by_hint,
            impact=SpaceToggleImpact(
                members_total=members_total,
                members_without_active_group=members_without,
                actor_has_active_group=self._repo.actor_has_active_group(space_id, actor.id),
            ),
        )

    def set_enabled(
        self, actor: User, space_id: uuid.UUID, enabled: bool, ip: str = ""
    ) -> SpaceToggleStatus:
        """拨动:写行 → 按空间失效本进程缓存 → 审计留痕(含新旧值)。不做自锁拦截。"""
        self._ensure_space_exists(space_id)
        previous = space_toggle_enabled(self._db, space_id)
        self._repo.upsert(space_id=space_id, enabled=enabled, updated_by=actor.id)
        # 本进程立刻生效;其它 worker 最多 TTL 秒后跟上(模块 docstring 的口径)
        clear_space_toggle_cache(space_id)
        self._audit.add(
            AuditLog(
                actor_id=actor.id,
                space_id=space_id,  # 空间级动作,归因到空间
                action=str(AuditAction.PERMISSION_GROUP_TOGGLE_SET),
                target="space_permission_group_toggle",
                detail={"old": previous, "new": enabled},
                ip=ip,
            )
        )
        return self.status(actor, space_id)

    def _ensure_space_exists(self, space_id: uuid.UUID) -> None:
        if self._db.get(Space, space_id) is None:
            raise AppError(ErrorCode.SPACE_NOT_FOUND, "空间不存在", http_status=404)
