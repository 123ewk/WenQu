"""空间级 Langfuse 配置与 trace 查看服务(OPT-49 v2)。

口径(2026-09-26 用户重述需求):
- **每空间一套配置**(地址/公钥/私钥/启停),该空间 Admin+(role≥30)或 superuser 可改
  (守卫在路由层 get_space_langfuse_manager);
- trace 按提问所在空间上送到该空间配置的 Langfuse(各空间可指向同一服务的不同项目);
- **产品内可看**:本空间成员可查**自己**的 trace,Admin+ 可查**全空间**;未配置空间
  返回 configured=false(nil-safe 零开销);非本空间成员一律 404(防枚举);
- 失败隔离:Langfuse 不可达/配置损坏 → 424 上游错误,绝不进问答主链路。

读取缓存:按 space_id 分键的进程内 TTL 缓存(写侧按空间失效)。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.application.repository.langfuse_config import SpaceLangfuseConfigRepositoryImpl
from app.core.config import get_settings
from app.core.crypto import get_credential_cipher
from app.core.errors import AppError, ErrorCode
from app.domain.enums import AuditAction, Role
from app.domain.interfaces import AuditRepository, SpaceLangfuseConfigRepository, UserRepository
from app.domain.models import AuditLog, Membership, SpaceLangfuseConfig, User


@dataclass(frozen=True)
class LangfuseSettings:
    """运行时接入参数(私钥已解密;只供观测客户端与代理查询使用,不出接口)。"""

    host: str
    public_key: str
    secret_key: str


@dataclass(frozen=True)
class LangfuseConfigStatus:
    """GET/PUT 配置的响应载荷(路由层再映射契约;私钥永不出现)。"""

    enabled: bool
    host: str
    public_key: str
    key_hint: str
    updated_at: datetime | None
    updated_by_hint: str | None
    console_url: str = ""
    project_id: str | None = None


class _Missing:
    """缓存未命中哨兵(与缓存值 None=未接入区分)。"""


class _SpaceTtlCache:
    """按 space_id 分键的进程内 TTL 缓存(None 也缓存,表示"该空间确认未接入")。"""

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl_seconds = ttl_seconds
        self._entries: dict[uuid.UUID, tuple[LangfuseSettings | None, float]] = {}

    def get(self, space_id: uuid.UUID) -> LangfuseSettings | None | _Missing:
        entry = self._entries.get(space_id)
        if entry is None:
            return _Missing()
        value, at = entry
        if time.monotonic() - at >= self._ttl_seconds:
            return _Missing()
        return value

    def put(self, space_id: uuid.UUID, value: LangfuseSettings | None) -> None:
        self._entries[space_id] = (value, time.monotonic())

    def clear(self, space_id: uuid.UUID | None = None) -> None:
        if space_id is None:
            self._entries.clear()
        else:
            self._entries.pop(space_id, None)


_cache = _SpaceTtlCache(get_settings().langfuse_config_cache_ttl_seconds)


def clear_langfuse_config_cache(space_id: uuid.UUID | None = None) -> None:
    """显式失效入口:写侧按空间失效,测试夹具全清。"""
    _cache.clear(space_id)


def _resolve(row: SpaceLangfuseConfig | None) -> LangfuseSettings | None:
    if row is None or not row.enabled or not row.secret_key_encrypted:
        return None
    try:
        secret = get_credential_cipher().decrypt(row.secret_key_encrypted)
    except AppError:
        return None  # 密文损坏/主密钥轮换:按未接入处理,不泄进主链路
    return LangfuseSettings(host=row.host.rstrip("/"), public_key=row.public_key, secret_key=secret)


def langfuse_settings(db: Session | None, space_id: uuid.UUID) -> LangfuseSettings | None:
    """运行时读取(问答观测句柄用):该空间未接入/已停用返回 None。

    `db=None`(部分测试构造)时:缓存命中仍返回缓存,未命中按未接入处理。
    """
    cached = _cache.get(space_id)
    if not isinstance(cached, _Missing):
        return cached
    if db is None:
        return None
    settings = _resolve(SpaceLangfuseConfigRepositoryImpl(db).get_row(space_id))
    _cache.put(space_id, settings)
    return settings


def _hint(key: str) -> str:
    return f"{key[:4]}****{key[-4:]}" if len(key) > 8 else "****"


_project_cache: dict[tuple[str, str, str], tuple[str | None, float]] = {}


def clear_project_id_cache() -> None:
    """测试夹具与写侧共用:密钥换掉后旧解析结果必须作废。"""
    _project_cache.clear()


def resolve_project_id(settings: LangfuseSettings) -> str | None:
    """用 pk/sk 调 Langfuse /api/public/projects 解析所属项目 id(前端直达链接用)。

    按配置指纹缓存(TTL 同配置缓存);不可达/形状不符一律 None,绝不抛 ——
    解析失败只影响"直达链接"的深度,不影响配置读写本身。
    """
    fp = (settings.host, settings.public_key, settings.secret_key)
    hit = _project_cache.get(fp)
    ttl = get_settings().langfuse_config_cache_ttl_seconds
    if hit is not None and time.monotonic() - hit[1] < ttl:
        return hit[0]
    try:
        resp = httpx.get(
            f"{settings.host}/api/public/projects",
            auth=(settings.public_key, settings.secret_key),
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get("data") or []
        pid = str(data[0]["id"]) if data and data[0].get("id") else None
    except Exception:  # noqa: BLE001 — 链接解析是锦上添花,失败不外泄
        pid = None
    _project_cache[fp] = (pid, time.monotonic())
    return pid


class LangfuseConfigService:
    """配置读取/保存(空间 Admin+ 守卫在路由层)+ trace 查看代理(成员看自己/Admin+ 看全空间)。"""

    def __init__(
        self,
        db: Session,
        repo: SpaceLangfuseConfigRepository,
        users: UserRepository,
        audit: AuditRepository,
    ) -> None:
        self._db = db
        self._repo = repo
        self._users = users
        self._audit = audit

    # ---------------------------- 内部 ----------------------------

    def _require_member(self, space_id: uuid.UUID, user_id: uuid.UUID) -> None:
        """非本空间成员一律 404(防枚举)。"""
        if self._db.scalar(
            select(func.count())
            .select_from(Membership)
            .where(Membership.space_id == space_id, Membership.user_id == user_id)
        ):
            return
        raise AppError(ErrorCode.SPACE_NOT_FOUND, "空间不存在", http_status=404)

    def _role_in_space(self, space_id: uuid.UUID, user_id: uuid.UUID) -> int | None:
        return self._db.scalar(
            select(Membership.role).where(
                Membership.space_id == space_id, Membership.user_id == user_id
            )
        )

    def _is_admin(self, actor: User, space_id: uuid.UUID) -> bool:
        if actor.is_superuser:
            return True
        role = self._role_in_space(space_id, actor.id)
        return role is not None and role >= Role.ADMIN.value

    def _lf_get(self, settings: LangfuseSettings, path: str, params: dict) -> dict:
        try:
            resp = httpx.get(
                f"{settings.host}/api/public{path}",
                params=params,
                auth=(settings.public_key, settings.secret_key),
                timeout=10,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise AppError(
                ErrorCode.MODEL_UPSTREAM_ERROR,
                "Langfuse 查询失败,请稍后重试",
                http_status=424,
                details={"reason": str(exc)[:120]},
            ) from exc
        return resp.json()

    # ---------------------------- 配置 ----------------------------

    def status(self, actor: User, space_id: uuid.UUID) -> LangfuseConfigStatus:
        row = self._repo.get_row(space_id)
        updated_by_hint: str | None = None
        if row is not None and row.updated_by is not None:
            updater = self._users.get(row.updated_by)
            updated_by_hint = updater.username if updater is not None else None
        # 行不存在(从未配置过)时回事实例级默认拓扑:表单预填 + 跳转链接开箱即用
        defaults = get_settings()
        host_out = row.host if row is not None else defaults.langfuse_default_host
        console_url = row.console_url if row is not None else defaults.langfuse_console_url
        settings = langfuse_settings(self._db, space_id)
        pid = resolve_project_id(settings) if settings is not None and console_url else None
        return LangfuseConfigStatus(
            enabled=row.enabled if row is not None else False,
            host=host_out,
            public_key=row.public_key if row is not None else "",
            key_hint=row.key_hint if row is not None else "",
            updated_at=row.updated_at if row is not None else None,
            updated_by_hint=updated_by_hint,
            console_url=console_url,
            project_id=pid,
        )

    def update(
        self,
        actor: User,
        space_id: uuid.UUID,
        *,
        host: str,
        console_url: str,
        public_key: str,
        secret_key: str | None,
        enabled: bool,
        ip: str = "",
    ) -> LangfuseConfigStatus:
        """保存本空间配置:secret_key 三态(None=不改/""=清除/非空=替换);启用前置私钥已配置。"""
        host = (host or "").strip().rstrip("/")
        console_url = (console_url or "").strip().rstrip("/")
        public_key = (public_key or "").strip()
        if not host:
            raise AppError(ErrorCode.VALIDATION, "Langfuse 地址(host)不能为空", http_status=400)
        if not public_key:
            raise AppError(ErrorCode.VALIDATION, "Public Key 不能为空", http_status=400)
        row = self._repo.get_row(space_id)
        if secret_key is None:
            enc = row.secret_key_encrypted if row is not None else ""
            hint = row.key_hint if row is not None else ""
        elif secret_key == "":
            enc, hint = "", ""
        else:
            enc = get_credential_cipher().encrypt(secret_key)
            hint = _hint(secret_key)
        if enabled and not enc:
            raise AppError(ErrorCode.VALIDATION, "启用前需先配置 Secret Key", http_status=400)
        self._repo.upsert(
            space_id=space_id,
            host=host,
            console_url=console_url,
            public_key=public_key,
            secret_key_encrypted=enc,
            key_hint=hint,
            enabled=enabled,
            updated_by=actor.id,
        )
        clear_langfuse_config_cache(space_id)
        self._audit.add(
            AuditLog(
                actor_id=actor.id,
                space_id=space_id,
                action=str(AuditAction.LANGFUSE_CONFIG_UPDATED),
                target="space_langfuse_config",
                detail={"host": host, "console_url": console_url, "enabled": enabled,
                        "key_hint": hint},
                ip=ip,
            )
        )
        return self.status(actor, space_id)

    # ---------------------------- trace 查看(产品内代理) ----------------------------

    def list_traces(
        self,
        actor: User,
        space_id: uuid.UUID,
        *,
        user_id_filter: uuid.UUID | None,
        limit: int = 20,
    ) -> dict:
        """成员强制只看自己(userId=自己);Admin+/superuser 可传 user_id 看指定人,不传看全部。"""
        settings = langfuse_settings(self._db, space_id)
        if settings is None:
            return {"configured": False, "items": []}
        self._require_member(space_id, actor.id)
        is_admin = self._is_admin(actor, space_id)
        target_user = user_id_filter if is_admin else actor.id  # 成员强制只看自己
        params: dict = {"limit": max(1, min(limit, 50))}
        if target_user is not None:
            params["userId"] = str(target_user)
        data = self._lf_get(settings, "/traces", params)
        return {"configured": True, "items": data.get("data", []), "meta": data.get("meta", {})}

    def get_trace(self, actor: User, space_id: uuid.UUID, trace_id: str) -> dict:
        settings = langfuse_settings(self._db, space_id)
        if settings is None:
            raise AppError(ErrorCode.VALIDATION, "本空间尚未接入 Langfuse", http_status=409)
        self._require_member(space_id, actor.id)
        data = self._lf_get(settings, f"/traces/{trace_id}", {})
        if not self._is_admin(actor, space_id):
            owner = (data.get("metadata") or {}).get("user_id")
            if owner is not None and str(owner) != str(actor.id):
                raise AppError(ErrorCode.SPACE_NOT_FOUND, "空间不存在", http_status=404)
        return data
