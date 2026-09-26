"""空间级权限挡位功能开关端点(OPT-48 v2):每空间一个,该空间 Admin+(role≥30)可拨。

权限:JWT 且 (该空间 role≥30 或 superuser) —— 守卫 `get_space_toggle_manager`
(2026-09-26 用户拍板;初版"superuser 或任意空间 Admin+"随需求重述作废)。

开关语义(见服务模块文档):
- 打开 = 该空间的**人链路核心功能**(问答/检索/会话流/知识库与文档)要求成员有生效挡位;
  成员/模型配置/审计/用量等管理页继续只看角色;
- 分配/换当前挡位只在打开后可用(没开 → 409 PERMISSION_GROUP_TOGGLE_OFF,
  见 deps.ensure_space_toggle_on);解除分配不拦;
- 只认 JWT:与档位管理同闸,Key 的路由授权表不登记本路由,程序凭据 fail-closed 403。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.api.deps import (
    get_client_ip,
    get_permission_group_toggle_service,
    get_space_toggle_manager,
)
from app.api.routing import CommitBeforeSendRoute
from app.application.service.permission_group_toggle import (
    PermissionGroupToggleService,
    SpaceToggleStatus,
)
from app.domain.models import User

router = APIRouter(
    prefix="/api/v1/spaces/{space_id}",
    tags=["permission-group-toggle"],
    route_class=CommitBeforeSendRoute,
)


class SpaceToggleRequest(BaseModel):
    enabled: bool


class SpaceToggleImpactOut(BaseModel):
    """本空间影响面(打开前的自查;缺挡只展示不拦,没有挡位的成员仍可用管理功能自救)。"""

    members_total: int
    members_without_active_group: int
    actor_has_active_group: bool


class SpaceToggleStatusOut(BaseModel):
    enabled: bool
    updated_at: datetime | None = None  # 表无行 = 从未拨过,恒 None
    updated_by_hint: str | None = None  # 最后拨动人用户名(展示用)
    impact: SpaceToggleImpactOut


def _to_out(status: SpaceToggleStatus) -> SpaceToggleStatusOut:
    return SpaceToggleStatusOut(
        enabled=status.enabled,
        updated_at=status.updated_at,
        updated_by_hint=status.updated_by_hint,
        impact=SpaceToggleImpactOut(
            members_total=status.impact.members_total,
            members_without_active_group=status.impact.members_without_active_group,
            actor_has_active_group=status.impact.actor_has_active_group,
        ),
    )


@router.get("/permission-group-toggle", response_model=SpaceToggleStatusOut)
def get_space_permission_group_toggle(
    space_id: uuid.UUID,
    actor: User = Depends(get_space_toggle_manager),
    service: PermissionGroupToggleService = Depends(get_permission_group_toggle_service),
) -> SpaceToggleStatusOut:
    """读本空间开关状态与影响面。有资格的人 GET 恒 200,拦人只发生在写路径。"""
    return _to_out(service.status(actor, space_id))


@router.put("/permission-group-toggle", response_model=SpaceToggleStatusOut)
def put_space_permission_group_toggle(
    space_id: uuid.UUID,
    payload: SpaceToggleRequest,
    request: Request,
    actor: User = Depends(get_space_toggle_manager),
    service: PermissionGroupToggleService = Depends(get_permission_group_toggle_service),
) -> SpaceToggleStatusOut:
    """拨动本空间开关;本进程立刻生效,其它 worker 最多缓存 TTL 秒后跟上。"""
    return _to_out(
        service.set_enabled(actor, space_id, payload.enabled, ip=get_client_ip(request))
    )
