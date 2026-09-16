"""用户路由:个人资料与改密。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.api.deps import (
    get_auth_service,
    get_client_ip,
    get_current_space_id,
    get_current_user,
    get_profile_service,
)
from app.api.routes.auth import UserOut
from app.application.service.auth import AuthService
from app.application.service.profile import ProfileService
from app.domain.models import User

router = APIRouter(prefix="/api/v1/users", tags=["users"])


class UpdateProfileRequest(BaseModel):
    nickname: str = Field(min_length=1, max_length=32)


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=1, max_length=72)
    new_password: str = Field(min_length=8, max_length=72)


@router.get("/me", response_model=UserOut)
def me(
    user: User = Depends(get_current_user),
    space_id: uuid.UUID | None = Depends(get_current_space_id),
) -> UserOut:
    return UserOut.of(user, str(space_id) if space_id else None)


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


# ---------------------------- 头像(缺口 #6) ----------------------------


@router.post("/me/avatar", response_model=UserOut)
async def upload_avatar(
    file: UploadFile = File(description="头像图片(PNG/JPEG/WEBP,≤2MB)"),
    user: User = Depends(get_current_user),
    service: ProfileService = Depends(get_profile_service),
    space_id: uuid.UUID | None = Depends(get_current_space_id),
) -> UserOut:
    """上传即替换;内容经真实解码校验,不信任扩展名。"""
    content = await file.read()
    service.set_avatar(user, content, file.content_type or "")
    return UserOut.of(user, str(space_id) if space_id else None)


@router.get("/me/avatar")
def get_avatar(
    user: User = Depends(get_current_user),
    service: ProfileService = Depends(get_profile_service),
) -> Response:
    """本人头像读取;不走公开静态目录(内部系统不对匿名分发)。"""
    data, content_type = service.get_avatar(user)
    return Response(content=data, media_type=content_type)


@router.delete("/me/avatar", status_code=status.HTTP_204_NO_CONTENT)
def delete_avatar(
    user: User = Depends(get_current_user),
    service: ProfileService = Depends(get_profile_service),
) -> None:
    service.delete_avatar(user)
