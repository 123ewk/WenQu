"""权限档位路由(OPT-41):一档 = 一种"能做什么 + 能看哪些库",人和 Key 共用同一份真相。

为什么开这组端点(需求单 §4/R6-b):
- 成员角色只有阶梯(Viewer/Editor/Admin/Owner),**分不出"能检索哪些知识库"**;
  OPT-41 之前权限挂在单把 Key 上,既不能复用也不能改(要改只能吊销重签);
- 拆出档位后:改一次档位 → 其下全部凭据与新范围即时生效,不用重发明文。

安全语义:
- **写入门槛 Admin+**(建/改/删/分配/换档),**读入门槛 VIEWER+** —— 与 API Key 同闸。
  档位是权限源,允许成员自己挂上去 = 允许自己给自己提权;
- **只认 JWT**:这组路由刻意不登记进 Key 的路由授权表(`api_key_auth`),程序凭据
  不能改自己的权限;
- 范围两字段互斥约束、删除门槛(还挂着凭据/成员不许删)、幂等分配与换档的口径都在
  服务层,见 `PermissionGroupService` 的模块文档。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import (
    get_client_ip,
    get_current_user,
    get_permission_group_service,
)
from app.api.routing import CommitBeforeSendRoute
from app.application.service.permission_groups import (
    PermissionGroupService,
    PermissionGroupView,
)
from app.domain.models import User

router = APIRouter(
    prefix="/api/v1/spaces/{space_id}/permission-groups",
    tags=["permission-groups"],
    route_class=CommitBeforeSendRoute,
)


class UpsertPermissionGroupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=512)
    # 能力清单;空数组合法但等于"什么都不能做"(fail-closed)
    capabilities: list[str] = Field(default_factory=list, max_length=8)
    # 知识库范围;与 all_kbs 二选一(all_kbs=true 时必须留空)
    kb_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    # true = 本空间全部知识库(每次请求实时展开,新建的库自动在范围内)
    all_kbs: bool = False


class GroupMemberRequest(BaseModel):
    """分配成员与设定当前档共用:都是"这个档位 ↔ 这个人"的一个动作,只差语义。"""

    user_id: uuid.UUID


class PurgeRevokedKeysOut(BaseModel):
    """清空历史凭据的结果;0 = 该档位下没有已吊销的 Key(不是错误,前端据计数提示即可)。"""

    deleted_count: int


class GroupMemberOut(BaseModel):
    user_id: str
    username: str | None
    nickname: str | None
    role: int | None  # None = 已被移出空间(挂载行留着也无权限,前端据此置灰)
    is_current: bool  # 此人此刻生效的那一档(每人每空间至多一个 true)


class PermissionGroupOut(BaseModel):
    id: str
    name: str
    description: str
    capabilities: list[str]
    kb_ids: list[str]
    all_kbs: bool
    members: list[GroupMemberOut]
    created_at: datetime | None = None


def _out(view: PermissionGroupView) -> PermissionGroupOut:
    group = view.group
    return PermissionGroupOut(
        id=str(group.id),
        name=group.name,
        description=group.description,
        capabilities=list(group.capabilities or []),
        kb_ids=[str(kb) for kb in (group.kb_ids or [])],
        all_kbs=bool(group.all_kbs),
        members=[
            GroupMemberOut(
                user_id=str(m.user_id),
                username=m.username,
                nickname=m.nickname,
                role=m.role,
                is_current=m.is_current,
            )
            for m in view.members
        ],
        created_at=group.created_at,
    )


@router.get("", response_model=list[PermissionGroupOut])
def list_permission_groups(
    space_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: PermissionGroupService = Depends(get_permission_group_service),
) -> list[PermissionGroupOut]:
    return [_out(v) for v in service.list_for_space(space_id, user)]


@router.post("", response_model=PermissionGroupOut, status_code=201)
def create_permission_group(
    space_id: uuid.UUID,
    body: UpsertPermissionGroupRequest,
    user: User = Depends(get_current_user),
    service: PermissionGroupService = Depends(get_permission_group_service),
    ip: str = Depends(get_client_ip),
) -> PermissionGroupOut:
    group = service.create(
        space_id,
        user,
        name=body.name,
        description=body.description,
        capabilities=body.capabilities,  # 服务内归一并拒绝未知值
        kb_ids=body.kb_ids,
        all_kbs=body.all_kbs,
        ip=ip,
    )
    return _out(service.get(space_id, user, group.id))


@router.get("/{group_id}", response_model=PermissionGroupOut)
def get_permission_group(
    space_id: uuid.UUID,
    group_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: PermissionGroupService = Depends(get_permission_group_service),
) -> PermissionGroupOut:
    return _out(service.get(space_id, user, group_id))


@router.patch("/{group_id}", response_model=PermissionGroupOut)
def update_permission_group(
    space_id: uuid.UUID,
    group_id: uuid.UUID,
    body: UpsertPermissionGroupRequest,
    user: User = Depends(get_current_user),
    service: PermissionGroupService = Depends(get_permission_group_service),
    ip: str = Depends(get_client_ip),
) -> PermissionGroupOut:
    # 整组替换(PUT 语义走 PATCH 方法名):半字段 PATCH 会让"能力/范围"两套口径漂移
    service.update(
        space_id,
        user,
        group_id,
        name=body.name,
        description=body.description,
        capabilities=body.capabilities,
        kb_ids=body.kb_ids,
        all_kbs=body.all_kbs,
        ip=ip,
    )
    return _out(service.get(space_id, user, group_id))


@router.delete("/{group_id}", status_code=204)
def delete_permission_group(
    space_id: uuid.UUID,
    group_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: PermissionGroupService = Depends(get_permission_group_service),
    ip: str = Depends(get_client_ip),
) -> None:
    service.delete(space_id, user, group_id, ip)


@router.delete("/{group_id}/revoked-keys", response_model=PurgeRevokedKeysOut)
def purge_revoked_keys(
    space_id: uuid.UUID,
    group_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: PermissionGroupService = Depends(get_permission_group_service),
    ip: str = Depends(get_client_ip),
) -> PurgeRevokedKeysOut:
    """清空该档位**已吊销**凭据的台账行(OPT-44)。

    为什么单独一个端点而不是删档时顺带做:删凭据行是不可逆的,而且只该碰到已吊销的
    Key —— 让"删档位"顺手抹掉在用的凭据,等于给一次误点开了更大的口子。
    """
    deleted = service.purge_revoked_keys(space_id, user, group_id, ip)
    return PurgeRevokedKeysOut(deleted_count=deleted)


@router.post("/{group_id}/members", response_model=PermissionGroupOut)
def assign_group_member(
    space_id: uuid.UUID,
    group_id: uuid.UUID,
    body: GroupMemberRequest,
    user: User = Depends(get_current_user),
    service: PermissionGroupService = Depends(get_permission_group_service),
    ip: str = Depends(get_client_ip),
) -> PermissionGroupOut:
    # 分配 = 让他"可以用这一档",不自动生效(生效要显式 set-current)
    service.assign(space_id, user, group_id, body.user_id, ip)
    return _out(service.get(space_id, user, group_id))


@router.delete("/{group_id}/members/{user_id}", status_code=204)
def unassign_group_member(
    space_id: uuid.UUID,
    group_id: uuid.UUID,
    user_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: PermissionGroupService = Depends(get_permission_group_service),
    ip: str = Depends(get_client_ip),
) -> None:
    service.unassign(space_id, user, group_id, user_id, ip)


@router.put("/{group_id}/current", response_model=PermissionGroupOut)
def set_current_group(
    space_id: uuid.UUID,
    group_id: uuid.UUID,
    body: GroupMemberRequest,
    user: User = Depends(get_current_user),
    service: PermissionGroupService = Depends(get_permission_group_service),
    ip: str = Depends(get_client_ip),
) -> PermissionGroupOut:
    # 换档 = 权限开关(取代 OPT-31 的"激活某把 Key"):同一个人原 current 自动让位
    service.set_current(space_id, user, group_id, body.user_id, ip)
    return _out(service.get(space_id, user, group_id))
