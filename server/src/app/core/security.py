"""安全原语:密码哈希(bcrypt)、JWT 签发/校验、refresh 令牌哈希(基准 04)。

底线:
- JWT 校验做算法白名单(decode 只接受 HS256,防 alg 混淆);access 带 typ=access,
  refresh 是不透明随机串(天然无法冒充 access),库里只存 SHA-256 哈希。
- JWT secret 缺失时:dev 随机生成(仅本进程有效)并已在启动时告警;prod 由配置
  校验 fail-closed 拒绝启动,这里不可能走到。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

import bcrypt
import jwt

from app.core.config import Settings, get_settings
from app.core.errors import AppError, ErrorCode

_JWT_ALGORITHMS = ["HS256"]  # 白名单:唯一允许的签名算法
_ACCESS_TTL_FALLBACK_MINUTES = 24 * 60


# ---------------------------- 密码 ----------------------------


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("ascii"))
    except ValueError:
        return False


# ---------------------------- JWT ----------------------------


@lru_cache
def _ephemeral_secret() -> str:
    """dev 无 secret 时的进程内随机密钥:重启即失效,迫使用户重新登录。"""
    return secrets.token_urlsafe(48)


def get_jwt_secret(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    if settings.jwt_secret:
        return settings.jwt_secret
    if settings.env == "dev":
        return _ephemeral_secret()
    # prod 缺 secret 已被配置校验拦截;此处兜底大声失败
    raise RuntimeError("APP_JWT_SECRET 缺失,生产环境拒绝签发令牌")


def create_access_token(
    user_id: str, space_id: str | None, settings: Settings | None = None
) -> str:
    settings = settings or get_settings()
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": user_id,
        "sid": space_id,
        "typ": "access",
        "iat": now,
        "exp": now + timedelta(
            minutes=settings.access_token_minutes or _ACCESS_TTL_FALLBACK_MINUTES
        ),
    }
    return jwt.encode(payload, get_jwt_secret(settings), algorithm=_JWT_ALGORITHMS[0])


def decode_access_token(token: str, settings: Settings | None = None) -> dict[str, Any]:
    """校验并解码 access token;任何失败都归一为 AUTH_REQUIRED / TOKEN_EXPIRED。"""
    settings = settings or get_settings()
    try:
        payload: dict[str, Any] = jwt.decode(
            token, get_jwt_secret(settings), algorithms=_JWT_ALGORITHMS
        )
    except jwt.ExpiredSignatureError as exc:
        raise AppError(ErrorCode.TOKEN_EXPIRED, "登录已过期", http_status=401) from exc
    except jwt.InvalidTokenError as exc:
        raise AppError(ErrorCode.AUTH_REQUIRED, "无效的登录凭据", http_status=401) from exc
    if payload.get("typ") != "access":
        raise AppError(ErrorCode.AUTH_REQUIRED, "无效的登录凭据", http_status=401)
    if not payload.get("sub"):
        raise AppError(ErrorCode.AUTH_REQUIRED, "无效的登录凭据", http_status=401)
    return payload


# ---------------------------- refresh 令牌 ----------------------------


def generate_refresh_token() -> str:
    """不透明随机 refresh 令牌:仅返回值入库哈希,明文只在签发时可见一次。"""
    return secrets.token_urlsafe(48)


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
