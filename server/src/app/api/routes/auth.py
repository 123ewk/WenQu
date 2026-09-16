"""认证路由:注册 / 登录 / 刷新 / 登出 / 切换空间。

契约(给前端):所有错误统一壳 {success:false, error:{code,message,details}},
稳定字符串码见 core/errors.py;成功直接返回数据体。
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field, field_validator

from app.api.deps import get_auth_service, get_client_ip, get_current_user
from app.application.service.auth import AuthService, AuthSession, TokenPair
from app.domain.models import Space, User

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]{3,32}$")


# ---------------------------- 请求/响应模型 ----------------------------


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32, description="账号,3-32 位字母/数字/_/-")
    nickname: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=8, max_length=72, description="至少 8 位")

    @field_validator("username")
    @classmethod
    def _username_format(cls, v: str) -> str:
        if not _USERNAME_RE.match(v):
            raise ValueError("账号仅限 3-32 位字母、数字、下划线或中划线")
        return v


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=72)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class SwitchSpaceRequest(BaseModel):
    space_id: uuid.UUID
    refresh_token: str = Field(min_length=1)


AVATAR_PATH = "/users/me/avatar"


class UserOut(BaseModel):
    id: str
    username: str
    nickname: str
    created_at: datetime | None = None
    current_space_id: str | None = None  # 令牌绑定的活动空间(缺口 #7)
    last_login_at: datetime | None = None  # 最近一次成功登录(缺口 #9)
    last_login_ip: str | None = None
    # 已设置头像时的读取路径;**不带 /api/v1 前缀**(相对 API 基地址),
    # 前端可直接交给 baseURL=/api/v1 的客户端使用
    avatar_url: str | None = None

    @classmethod
    def of(cls, user: User, current_space_id: str | None = None) -> UserOut:
        return cls(
            id=str(user.id),
            username=user.username,
            nickname=user.nickname,
            created_at=user.created_at,
            current_space_id=current_space_id,
            last_login_at=user.last_login_at,
            last_login_ip=user.last_login_ip,
            avatar_url=AVATAR_PATH if user.avatar_key else None,
        )


class SpaceBriefOut(BaseModel):
    id: str
    name: str
    role: int
    description: str = ""

    @classmethod
    def of(cls, space: Space, role: int) -> SpaceBriefOut:
        return cls(id=str(space.id), name=space.name, description=space.description, role=role)


class AuthResponse(BaseModel):
    user: UserOut
    spaces: list[SpaceBriefOut]
    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int
    current_space_id: str | None = None  # 令牌绑定的活动空间(缺口 #7)


# ---------------------------- 路由 ----------------------------


def _sid(space_id: uuid.UUID | None) -> str | None:
    return str(space_id) if space_id else None


def _auth_response(session: AuthSession) -> AuthResponse:
    return AuthResponse(
        user=UserOut.of(session.user, _sid(session.tokens.space_id)),
        spaces=[SpaceBriefOut.of(s, r) for s, r in session.spaces],
        access_token=session.tokens.access_token,
        refresh_token=session.tokens.refresh_token,
        token_type=session.tokens.token_type,
        expires_in=session.tokens.expires_in,
        current_space_id=_sid(session.tokens.space_id),
    )


@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
def register(
    body: RegisterRequest,
    service: AuthService = Depends(get_auth_service),
    ip: str = Depends(get_client_ip),
) -> AuthResponse:
    session = service.register(body.username, body.nickname, body.password, ip=ip)
    return _auth_response(session)


@router.post("/login", response_model=AuthResponse)
def login(
    body: LoginRequest,
    service: AuthService = Depends(get_auth_service),
    ip: str = Depends(get_client_ip),
) -> AuthResponse:
    session = service.login(body.username, body.password, ip=ip)
    return _auth_response(session)


@router.post("/refresh", response_model=AuthResponse)
def refresh(
    body: RefreshRequest,
    service: AuthService = Depends(get_auth_service),
) -> AuthResponse:
    session = service.refresh(body.refresh_token)
    return _auth_response(session)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    body: RefreshRequest,
    user: User = Depends(get_current_user),
    service: AuthService = Depends(get_auth_service),
    ip: str = Depends(get_client_ip),
) -> None:
    service.logout(user.id, body.refresh_token, ip=ip)


@router.post("/switch-space", response_model=AuthResponse)
def switch_space(
    body: SwitchSpaceRequest,
    user: User = Depends(get_current_user),
    service: AuthService = Depends(get_auth_service),
    ip: str = Depends(get_client_ip),
) -> AuthResponse:
    pair: TokenPair = service.switch_space(user, body.space_id, body.refresh_token, ip=ip)
    spaces = service.list_spaces(user)
    return AuthResponse(
        user=UserOut.of(user, _sid(pair.space_id)),
        spaces=[SpaceBriefOut.of(s, r) for s, r in spaces],
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        token_type=pair.token_type,
        expires_in=pair.expires_in,
        current_space_id=_sid(pair.space_id),
    )
