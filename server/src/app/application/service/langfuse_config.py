"""Langfuse 可观测配置服务(OPT-49):按"模型配置"标准管理接入密钥。

职责边界:
- **本服务**:配置的读写(私钥 AES-GCM 加密落库/响应只回 key_hint)、启停校验
  (enabled=true 前置 secret_key 已配置)、审计留痕(langfuse_config.updated)、
  运行时读取的进程内 TTL 缓存(写时失效,复用 OPT-48 模式);
- **守卫与 UI**:superuser 专属(get_current_superuser,与模型配置一致),在路由层;
- **nil-safe**:表为空 / enabled=false / 私钥未配置时,`langfuse_settings` 返回 None,
  埋点路径短路——问答/入库行为与未接入时逐字节一致;
- **失败隔离**:私钥解密失败按"配置损坏"处理返回 None(运行时不可用),绝不抛进主链路;
  加密只发生在写入路径,主密钥缺失的 503 在保存配置时即暴露,不潜伏到运行时。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.application.repository.langfuse_config import LangfuseConfigRepositoryImpl
from app.core.config import get_settings
from app.core.crypto import get_credential_cipher
from app.core.errors import AppError, ErrorCode
from app.domain.enums import AuditAction
from app.domain.interfaces import AuditRepository, LangfuseConfigRepository, UserRepository
from app.domain.models import AuditLog, LangfuseConfig, User


@dataclass(frozen=True)
class LangfuseSettings:
    """运行时接入参数(私钥已解密;只供 N2 的 Langfuse 客户端使用,不出接口)。"""

    host: str
    public_key: str
    secret_key: str


@dataclass(frozen=True)
class LangfuseConfigStatus:
    """GET/PUT 的响应载荷(路由层再映射成契约 Out 模型;私钥永不出现)。"""

    enabled: bool
    host: str
    public_key: str
    key_hint: str
    updated_at: datetime | None
    updated_by_hint: str | None


class _ConfigTtlCache:
    """进程内 TTL 缓存(键只有一个——全局单行配置;None 也缓存,表示"未接入")。"""

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl_seconds = ttl_seconds
        self._value: LangfuseSettings | None = None
        self._filled: bool = False
        self._at: float = 0.0

    def get(self) -> LangfuseSettings | None | _Missing:  # noqa: UP007
        """TTL 内返回缓存值(可能为 None=确认未接入);过期/未查过返回 _Missing(必须打库)。"""
        if not self._filled:
            return _MISSING
        if time.monotonic() - self._at >= self._ttl_seconds:
            return _MISSING
        return self._value

    def put(self, value: LangfuseSettings | None) -> None:
        self._value = value
        self._filled = True
        self._at = time.monotonic()

    def clear(self) -> None:
        self._filled = False


class _Missing:
    """缓存"确认过未接入/已关闭"的哨兵:与"还没查过"区分,避免未配置时反复打库。"""


_MISSING = _Missing()
_cache = _ConfigTtlCache(get_settings().langfuse_config_cache_ttl_seconds)


def clear_langfuse_config_cache() -> None:
    """显式失效入口:写侧保存后与测试夹具共用。"""
    _cache.clear()


def langfuse_settings(db: Session) -> LangfuseSettings | None:
    """运行时读取(N2 的 Langfuse 客户端用):未接入/已停用返回 None;私钥在此解密。"""
    cached = _cache.get()
    if not isinstance(cached, _Missing):
        return cached
    row = LangfuseConfigRepositoryImpl(db).get_row()
    settings = _resolve(row)
    _cache.put(settings)
    return settings


def _resolve(row: LangfuseConfig | None) -> LangfuseSettings | None:
    if row is None or not row.enabled or not row.secret_key_encrypted:
        return None
    try:
        secret = get_credential_cipher().decrypt(row.secret_key_encrypted)
    except AppError:
        # 密文损坏/主密钥轮换:按"未接入"处理(nil-safe),不把观测配置问题泄进主链路
        return None
    return LangfuseSettings(host=row.host.rstrip("/"), public_key=row.public_key, secret_key=secret)


def _hint(key: str) -> str:
    return f"{key[:4]}****{key[-4:]}" if len(key) > 8 else "****"


class LangfuseConfigService:
    """配置读取与保存;superuser 守卫在路由层(get_current_superuser)。"""

    def __init__(
        self,
        db: Session,
        repo: LangfuseConfigRepository,
        users: UserRepository,
        audit: AuditRepository,
    ) -> None:
        # db 仅喂 langfuse_settings 的缓存读值路径;行操作走注入的 repo
        self._db = db
        self._repo = repo
        self._users = users
        self._audit = audit

    def status(self, actor: User) -> LangfuseConfigStatus:
        row = self._repo.get_row()
        updated_by_hint: str | None = None
        if row is not None and row.updated_by is not None:
            updater = self._users.get(row.updated_by)
            updated_by_hint = updater.username if updater is not None else None
        return LangfuseConfigStatus(
            enabled=row.enabled if row is not None else False,
            host=row.host if row is not None else "",
            public_key=row.public_key if row is not None else "",
            key_hint=row.key_hint if row is not None else "",
            updated_at=row.updated_at if row is not None else None,
            updated_by_hint=updated_by_hint,
        )

    def update(
        self,
        actor: User,
        *,
        host: str,
        public_key: str,
        secret_key: str | None,
        enabled: bool,
        ip: str = "",
    ) -> LangfuseConfigStatus:
        """保存配置:secret_key 三态(None=不改动/""=清除/非空=替换);启用前置私钥已配置。"""
        host = (host or "").strip().rstrip("/")
        public_key = (public_key or "").strip()
        if not host:
            raise AppError(ErrorCode.VALIDATION, "Langfuse 地址(host)不能为空", http_status=400)
        if not public_key:
            raise AppError(ErrorCode.VALIDATION, "Public Key 不能为空", http_status=400)
        row = self._repo.get_row()
        if secret_key is None:  # 不改动
            enc = row.secret_key_encrypted if row is not None else ""
            hint = row.key_hint if row is not None else ""
        elif secret_key == "":  # 显式清除
            enc, hint = "", ""
        else:
            enc = get_credential_cipher().encrypt(secret_key)
            hint = _hint(secret_key)
        if enabled and not enc:
            raise AppError(
                ErrorCode.VALIDATION, "启用前需先配置 Secret Key", http_status=400
            )
        self._repo.upsert(
            host=host,
            public_key=public_key,
            secret_key_encrypted=enc,
            key_hint=hint,
            enabled=enabled,
            updated_by=actor.id,
        )
        clear_langfuse_config_cache()
        self._audit.add(
            AuditLog(
                actor_id=actor.id,
                space_id=None,  # 全局配置,不属于任何空间
                action=str(AuditAction.LANGFUSE_CONFIG_UPDATED),
                target="langfuse_config",
                # 密钥本体(public/secret)一律不落审计,只有掩码与开关状态
                detail={"host": host, "enabled": enabled, "key_hint": hint},
                ip=ip,
            )
        )
        return self.status(actor)
