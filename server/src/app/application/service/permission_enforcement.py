"""实例级灰度开关的读值服务(OPT-48):DB 有行用 DB,无行回落 env,进程内 TTL 缓存。

为什么必须有缓存:全库唯一读点在 `deps._member_perm_for` —— 每个带 JWT 的请求都要拿到
这个布尔值;而 `get_settings()` 已被 @lru_cache 固化(env 值进程内本就不热更),DB 值若
不缓存,每次核心请求都要多打一次库。缓存口径(§4.1-3):

- **本进程**:写侧拨动后调用 `clear_enforcement_cache()` 主动失效,下一次读取立刻
  拿到新值(A2:PUT 之后本进程立刻生效,不用重启);
- **其它 worker**:最多 TTL 秒后跟上。开关不是权限本身,晚 60s 只是晚 60s 开始拒绝,
  不会造成越权(滞后方向是 fail-closed 的);
- **无锁**:最坏情况是两个线程同时未命中、各查一次库并写回同一个值,无害。

读值优先级:库里有行 → 用行的 `enabled`;无行 → 回落 `APP_REQUIRE_PERMISSION_GROUP_FOR_CORE`
(默认 False)。库里什么都不配 = 行为与今天逐字节一致(fail-closed 底线)。
"""

from __future__ import annotations

import time

from sqlalchemy.orm import Session

from app.application.repository.permission_enforcement import (
    PermissionEnforcementRepositoryImpl,
)
from app.core.config import get_settings


class EnforcementTtlCache:
    """单个布尔值的进程内 TTL 缓存(monotonic 时钟,不受系统对时影响)。"""

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl_seconds = ttl_seconds
        self._value: bool | None = None
        self._at: float = 0.0

    def get(self) -> bool | None:
        """TTL 内返回缓存值;过期或从未写入返回 None(None = 必须打库)。"""
        if self._value is None:
            return None
        if time.monotonic() - self._at >= self._ttl_seconds:
            return None
        return self._value

    def put(self, value: bool) -> None:
        self._value = value
        self._at = time.monotonic()

    def clear(self) -> None:
        self._value = None


_cache = EnforcementTtlCache(get_settings().permission_enforcement_cache_ttl_seconds)


def clear_enforcement_cache() -> None:
    """显式失效入口:写侧拨动后与测试夹具共用;清空后下一次读取必然重新打库。"""
    _cache.clear()


def permission_enforcement_enabled(db: Session) -> bool:
    """人链路读点用:当前是否强制"生效档位"。除 TTL 未命中外不打库。"""
    cached = _cache.get()
    if cached is not None:
        return cached
    row = PermissionEnforcementRepositoryImpl(db).get_row()
    value = (
        row.enabled if row is not None else get_settings().require_permission_group_for_core
    )
    _cache.put(value)
    return value
