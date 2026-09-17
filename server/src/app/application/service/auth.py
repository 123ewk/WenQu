"""认证服务:注册 / 登录 / refresh 轮换 / 登出 / 切换空间 / 改密。

安全底线(基准 04):
- refresh 令牌只存哈希,轮换时旧令牌立即删除(防重放);
- 登录失败不区分"用户不存在/密码错误",统一 INVALID_CREDENTIALS(防枚举);
- 改密成功撤销该用户全部 refresh(踢下线)。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core import security
from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.domain.enums import AuditAction
from app.domain.interfaces import (
    AuditRepository,
    RefreshTokenRepository,
    SpaceRepository,
    UserRepository,
)
from app.domain.models import AuditLog, RefreshToken, Space, User


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = 0
    # 令牌绑定的活动空间(登录/注册时为 None,切空间后为该空间 id)
    space_id: uuid.UUID | None = None


@dataclass(frozen=True)
class AuthSession:
    """一次成功的认证结果:身份 + 可用空间 + 令牌对。"""

    user: User
    spaces: list[tuple[Space, int]]
    tokens: TokenPair


class AuthService:
    def __init__(
        self,
        users: UserRepository,
        tokens: RefreshTokenRepository,
        spaces: SpaceRepository,
        audit: AuditRepository,
        settings: Settings,
    ) -> None:
        self._users = users
        self._tokens = tokens
        self._spaces = spaces
        self._audit = audit
        self._settings = settings

    # ---------------------------- 注册 / 登录 ----------------------------

    def register(self, username: str, nickname: str, password: str, ip: str = "") -> AuthSession:
        if self._users.get_by_username(username) is not None:
            raise AppError(ErrorCode.USERNAME_TAKEN, "该账号已被注册", http_status=409)
        user = User(
            username=username,
            nickname=nickname,
            password_hash=security.hash_password(password),
        )
        self._users.create(user)
        # 注册即登录:此时已建立会话,记为一次成功认证
        self._touch_login(user, ip)
        self._log(actor_id=user.id, action=AuditAction.AUTH_REGISTER, target=username, ip=ip)
        return AuthSession(user=user, spaces=[], tokens=self._issue_pair(user, None))

    def login(self, username: str, password: str, ip: str = "") -> AuthSession:
        user = self._users.get_by_username(username)
        if user is None or not security.verify_password(password, user.password_hash):
            # 不区分"用户不存在/密码错误",防账号枚举(基准 04)
            actor_id = user.id if user else None
            self._log(
                actor_id=actor_id,
                action=AuditAction.AUTH_LOGIN_FAILED,
                target=username,
                ip=ip,
            )
            raise AppError(ErrorCode.INVALID_CREDENTIALS, "账号或密码错误", http_status=401)
        spaces = self._spaces.list_for_user(user.id)
        self._touch_login(user, ip)
        self._log(actor_id=user.id, action=AuditAction.AUTH_LOGIN_SUCCESS, target=username, ip=ip)
        return AuthSession(user=user, spaces=spaces, tokens=self._issue_pair(user, None))

    # ---------------------------- 令牌生命周期 ----------------------------

    def refresh(self, raw_refresh_token: str) -> AuthSession:
        row = self._tokens.get_by_hash(security.hash_refresh_token(raw_refresh_token))
        if row is None:
            raise AppError(ErrorCode.REFRESH_TOKEN_INVALID, "登录状态已失效", http_status=401)
        if row.expires_at < datetime.now(UTC):
            self._tokens.delete(row)
            raise AppError(ErrorCode.REFRESH_TOKEN_INVALID, "登录状态已过期", http_status=401)
        user = self._users.get(row.user_id)
        if user is None:
            self._tokens.delete(row)
            raise AppError(ErrorCode.REFRESH_TOKEN_INVALID, "登录状态已失效", http_status=401)
        self._tokens.delete(row)  # 轮换:旧令牌一次性作废
        return AuthSession(
            user=user,
            spaces=self._spaces.list_for_user(user.id),
            tokens=self._issue_pair(user, row.space_id),
        )

    def logout(self, user_id: uuid.UUID, raw_refresh_token: str, ip: str = "") -> None:
        row = self._tokens.get_by_hash(security.hash_refresh_token(raw_refresh_token))
        if row is not None and row.user_id == user_id:
            self._tokens.delete(row)
        self._log(actor_id=user_id, action=AuditAction.AUTH_LOGOUT, ip=ip)

    def switch_space(
        self, user: User, space_id: uuid.UUID, raw_refresh_token: str, ip: str = ""
    ) -> TokenPair:
        # 非成员与不存在同样返回 404,防空间枚举(基准 04 守卫语义)
        space = self._spaces.get(space_id)
        if space is None or self._spaces.get_membership(space_id, user.id) is None:
            raise AppError(ErrorCode.SPACE_NOT_FOUND, "空间不存在", http_status=404)
        row = self._tokens.get_by_hash(security.hash_refresh_token(raw_refresh_token))
        if row is None or row.user_id != user.id:
            raise AppError(ErrorCode.REFRESH_TOKEN_INVALID, "登录状态已失效", http_status=401)
        self._tokens.delete(row)  # 切空间 = 轮换 refresh(撤销旧对,基准 04)
        return self._issue_pair(user, space_id)

    def list_spaces(self, user: User) -> list[tuple[Space, int]]:
        return self._spaces.list_for_user(user.id)

    def change_password(
        self, user: User, old_password: str, new_password: str, ip: str = ""
    ) -> None:
        if not security.verify_password(old_password, user.password_hash):
            raise AppError(ErrorCode.INVALID_CREDENTIALS, "原密码错误", http_status=401)
        if new_password == old_password:
            raise AppError(
                ErrorCode.VALIDATION, "新密码不能与原密码相同", http_status=400
            )
        user.password_hash = security.hash_password(new_password)
        self._users.save(user)
        self._tokens.delete_by_user(user.id)  # 改密踢下线:全部 refresh 撤销
        self._log(actor_id=user.id, action=AuditAction.PASSWORD_CHANGED, ip=ip)

    # ---------------------------- 内部 ----------------------------

    def _issue_pair(self, user: User, space_id: uuid.UUID | None) -> TokenPair:
        access = security.create_access_token(
            str(user.id), str(space_id) if space_id else None, self._settings
        )
        raw_refresh = security.generate_refresh_token()
        self._tokens.create(
            RefreshToken(
                user_id=user.id,
                token_hash=security.hash_refresh_token(raw_refresh),
                space_id=space_id,
                expires_at=datetime.now(UTC)
                + timedelta(days=self._settings.refresh_token_days),
            )
        )
        return TokenPair(
            access_token=access,
            refresh_token=raw_refresh,
            expires_in=self._settings.access_token_minutes * 60,
            space_id=space_id,
        )

    def _touch_login(self, user: User, ip: str) -> None:
        """记录最近成功认证:失败登录不调用(失败次数由 auth.login_failed 审计承载)。"""
        user.last_login_at = datetime.now(UTC)
        user.last_login_ip = ip
        self._users.save(user)

    def _log(
        self,
        actor_id: uuid.UUID | None,
        action: AuditAction,
        target: str = "",
        detail: dict | None = None,
        space_id: uuid.UUID | None = None,
        ip: str = "",
    ) -> None:
        self._audit.add(
            AuditLog(
                actor_id=actor_id,
                space_id=space_id,
                action=str(action),
                target=target,
                detail=detail or {},
                ip=ip,
            )
        )


__all__ = ["AuthService", "AuthSession", "TokenPair"]
