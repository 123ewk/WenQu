"""用户路由:个人资料与改密。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from app.api.deps import get_auth_service, get_client_ip, get_current_user
from app.api.routes.auth import UserOut
from app.application.service.auth import AuthService
from app.domain.models import User

router = APIRouter(prefix="/api/v1/users", tags=["users"])


class UpdateProfileRequest(BaseModel):
    nickname: str = Field(min_length=1, max_length=32)


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=1, max_length=72)
    new_password: str = Field(min_length=8, max_length=72)


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut.of(user)


@router.patch("/me", response_model=UserOut)
def update_profile(
    body: UpdateProfileRequest,
    user: User = Depends(get_current_user),
) -> UserOut:
    user.nickname = body.nickname
    return UserOut.of(user)


@router.post("/me/password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    body: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    service: AuthService = Depends(get_auth_service),
    ip: str = Depends(get_client_ip),
) -> None:
    service.change_password(user, body.old_password, body.new_password, ip=ip)
