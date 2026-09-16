"""空间路由:CRUD / 成员管理 / 审计查询。

权限映射(基准 04):空间相关操作一律经 path space_id 守卫 ——
非成员 404、角色不足 403;查看成员任意角色,管理操作 Admin+,删除空间 Owner。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from app.api.deps import (
    get_client_ip,
    get_current_user,
    get_space_service,
    get_user_repository,
)
from app.application.service.spaces import SpaceService
from app.domain.enums import AuditAction
from app.domain.interfaces import UserRepository
from app.domain.models import AuditLog, Membership, Space, User

router = APIRouter(prefix="/api/v1/spaces", tags=["spaces"])


# ---------------------------- 模型 ----------------------------


class CreateSpaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=512)


class UpdateSpaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=512)


class AddMemberRequest(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    role: int = Field(description="目标角色:20=Editor / 10=Viewer(Admin 不能授予 Admin)")


class ChangeRoleRequest(BaseModel):
    role: int = Field(description="目标角色;40=Owner 表示转让所有权(自己降为 Admin)")


class SpaceOut(BaseModel):
    id: str
    name: str
    description: str
    role: int
    created_at: datetime | None = None

    @classmethod
    def of(cls, space: Space, role: int) -> SpaceOut:
        return cls(
            id=str(space.id),
            name=space.name,
            description=space.description,
            role=role,
            created_at=space.created_at,
        )


class MemberOut(BaseModel):
    user_id: str
    username: str
    nickname: str
    role: int
    joined_at: datetime | None = None

    @classmethod
    def of(cls, membership: Membership, user: User) -> MemberOut:
        return cls(
            user_id=str(user.id),
            username=user.username,
            nickname=user.nickname,
            role=membership.role,
            joined_at=membership.created_at,
        )


class AuditLogOut(BaseModel):
    id: str
    actor_id: str | None
    actor_name: str | None  # 操作人昵称冗余:操作人退空间后仍可读,前端不必再映射
    action: str
    target: str
    result: str  # success | denied(缺口 #3:前端结果列)
    detail: dict
    ip: str
    created_at: datetime | None = None

    @classmethod
    def of(cls, log: AuditLog, actor_name: str | None = None) -> AuditLogOut:
        return cls(
            id=str(log.id),
            actor_id=str(log.actor_id) if log.actor_id else None,
            actor_name=actor_name,
            action=log.action,
            target=log.target,
            result=log.result,
            detail=log.detail,
            ip=log.ip,
            created_at=log.created_at,
        )


class AuditPage(BaseModel):
    items: list[AuditLogOut]
    total: int


# ---------------------------- 空间 CRUD ----------------------------


@router.get("", response_model=list[SpaceOut])
def list_my_spaces(
    user: User = Depends(get_current_user),
    service: SpaceService = Depends(get_space_service),
) -> list[SpaceOut]:
    return [SpaceOut.of(s, r) for s, r in service.list_for_user(user)]


@router.post("", response_model=SpaceOut, status_code=status.HTTP_201_CREATED)
def create_space(
    body: CreateSpaceRequest,
    user: User = Depends(get_current_user),
    service: SpaceService = Depends(get_space_service),
    ip: str = Depends(get_client_ip),
) -> SpaceOut:
    space, role = service.create(user, body.name, body.description, ip=ip)
    return SpaceOut.of(space, role)


@router.get("/{space_id}", response_model=SpaceOut)
def get_space(
    space_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: SpaceService = Depends(get_space_service),
) -> SpaceOut:
    space, role = service.get_with_role(space_id, user.id)
    return SpaceOut.of(space, role)


@router.patch("/{space_id}", response_model=SpaceOut)
def update_space(
    space_id: uuid.UUID,
    body: UpdateSpaceRequest,
    user: User = Depends(get_current_user),
    service: SpaceService = Depends(get_space_service),
    ip: str = Depends(get_client_ip),
) -> SpaceOut:
    space = service.update(space_id, user, body.name, body.description, ip=ip)
    _, role = service.get_with_role(space_id, user.id)
    return SpaceOut.of(space, role)


@router.delete("/{space_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_space(
    space_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: SpaceService = Depends(get_space_service),
    ip: str = Depends(get_client_ip),
) -> None:
    service.delete(space_id, user, ip=ip)


# ---------------------------- 成员管理 ----------------------------


@router.get("/{space_id}/members", response_model=list[MemberOut])
def list_members(
    space_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: SpaceService = Depends(get_space_service),
) -> list[MemberOut]:
    return [MemberOut.of(m, u) for m, u in service.list_members(space_id, user)]


@router.post("/{space_id}/members", response_model=MemberOut, status_code=status.HTTP_201_CREATED)
def add_member(
    space_id: uuid.UUID,
    body: AddMemberRequest,
    user: User = Depends(get_current_user),
    service: SpaceService = Depends(get_space_service),
    ip: str = Depends(get_client_ip),
) -> MemberOut:
    membership, target = service.add_member(space_id, user, body.username, body.role, ip=ip)
    return MemberOut.of(membership, target)


@router.patch("/{space_id}/members/{member_user_id}", response_model=MemberOut)
def change_role(
    space_id: uuid.UUID,
    member_user_id: uuid.UUID,
    body: ChangeRoleRequest,
    user: User = Depends(get_current_user),
    service: SpaceService = Depends(get_space_service),
    ip: str = Depends(get_client_ip),
) -> MemberOut:
    membership, target_user = service.change_role(space_id, user, member_user_id, body.role, ip=ip)
    return MemberOut.of(membership, target_user)


@router.delete("/{space_id}/members/{member_user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(
    space_id: uuid.UUID,
    member_user_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: SpaceService = Depends(get_space_service),
    ip: str = Depends(get_client_ip),
) -> None:
    service.remove_member(space_id, user, member_user_id, ip=ip)


@router.post("/{space_id}/leave", status_code=status.HTTP_204_NO_CONTENT)
def leave_space(
    space_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: SpaceService = Depends(get_space_service),
    ip: str = Depends(get_client_ip),
) -> None:
    service.leave(space_id, user, ip=ip)


# ---------------------------- 审计 ----------------------------


@router.get("/{space_id}/audit-logs", response_model=AuditPage)
def list_audit_logs(
    space_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    action: AuditAction | None = Query(
        default=None, description="按动作类型精确筛选,如 space.updated(非法值返回 422)"
    ),
    actor_id: uuid.UUID | None = Query(default=None, description="按操作人筛选"),
    since: datetime | None = Query(default=None, description="起始时间(闭区间)"),
    until: datetime | None = Query(default=None, description="结束时间(闭区间)"),
    user: User = Depends(get_current_user),
    service: SpaceService = Depends(get_space_service),
    users: UserRepository = Depends(get_user_repository),
) -> AuditPage:
    logs, total = service.list_audit(
        space_id, user, limit, offset, action=action, actor_id=actor_id, since=since, until=until
    )
    names = _actor_names(users, logs)
    return AuditPage(
        items=[AuditLogOut.of(log, names.get(log.actor_id)) for log in logs], total=total
    )


def _actor_names(users: UserRepository, logs: list[AuditLog]) -> dict[uuid.UUID | None, str]:
    """一次查齐本页操作人昵称,避免逐行查询(N+1)。"""
    names: dict[uuid.UUID | None, str] = {}
    for actor_id in {log.actor_id for log in logs if log.actor_id is not None}:
        actor = users.get(actor_id)
        if actor is not None:
            names[actor_id] = actor.nickname
    return names
