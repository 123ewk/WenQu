"""实例级档位灰度开关端点(OPT-48):把"人链路必须有权位"从环境变量升级成页面可拨。

权限:JWT 且 (is_superuser 或 任意空间 role≥30/ADMIN) —— 守卫 `get_enforcement_manager`
(2026-09-26 用户修订,替代需求单 D2 的"仅 superuser":dev 里 is_superuser 只在种子账号上,
真人管理员永远摸不到入口)。**效果是全局的(D1)**:打开就是整个实例所有空间的人都要有
生效档位,页面措辞必须写明"影响整个实例所有空间"。

路径仍在 `/api/v1/admin/*` 前缀下:路由授权表(`_ROUTE_CAPABILITIES`)不登记 admin 路由,
API Key 打进来一律 403 fail-closed,这条既有门禁原样保留(A7)。

开启时的 409(D3)是服务层守卫:操作者本人所在每个空间都必须已有生效档位;其他成员缺档
只进 `impact` 统计,不拦。运行时语义与 env 时代逐字节一致,本路由只改"值的来源"。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.api.deps import (
    get_client_ip,
    get_enforcement_manager,
    get_permission_enforcement_service,
)
from app.api.routing import CommitBeforeSendRoute
from app.application.service.permission_enforcement import (
    EnforcementStatus,
    PermissionEnforcementService,
)
from app.domain.models import User

router = APIRouter(
    prefix="/api/v1/admin",
    tags=["admin-permission-enforcement"],
    route_class=CommitBeforeSendRoute,
)


class EnforcementToggleIn(BaseModel):
    enabled: bool


class SpaceGapOut(BaseModel):
    space_id: uuid.UUID
    space_name: str


class EnforcementImpactOut(BaseModel):
    """影响面统计(打开前的自查与"会锁多少人"的预览;缺档只展示不拦,D3)。"""

    spaces: int
    members_total: int
    members_without_active_group: int
    actor_has_active_group: bool
    actor_spaces_without_group: list[SpaceGapOut]


class EnforcementStatusOut(BaseModel):
    effective: bool  # 当前生效值(库里没有配置行时 = env 兜底值)
    source: Literal["db", "env_fallback"]  # 值的来源:库里有行 / 回落 env
    updated_at: datetime | None = None  # 库里无行 = 从未在页面上拨过,恒 None
    updated_by_hint: str | None = None  # 最后拨动人用户名(展示用)
    impact: EnforcementImpactOut


def _to_out(status: EnforcementStatus) -> EnforcementStatusOut:
    return EnforcementStatusOut(
        effective=status.effective,
        source=status.source,
        updated_at=status.updated_at,
        updated_by_hint=status.updated_by_hint,
        impact=EnforcementImpactOut(
            spaces=status.spaces,
            members_total=status.members_total,
            members_without_active_group=status.members_without_active_group,
            actor_has_active_group=status.actor_has_active_group,
            actor_spaces_without_group=[
                SpaceGapOut(space_id=gap.space_id, space_name=gap.space_name)
                for gap in status.actor_spaces_without_group
            ],
        ),
    )


@router.get("/permission-enforcement", response_model=EnforcementStatusOut)
def get_permission_enforcement(
    actor: User = Depends(get_enforcement_manager),
    service: PermissionEnforcementService = Depends(get_permission_enforcement_service),
) -> EnforcementStatusOut:
    """读当前生效值、来源与影响面统计。同一个人 GET 恒 200(A5),拦人只发生在 PUT。"""
    return _to_out(service.status(actor))


@router.put("/permission-enforcement", response_model=EnforcementStatusOut)
def put_permission_enforcement(
    payload: EnforcementToggleIn,
    request: Request,
    actor: User = Depends(get_enforcement_manager),
    service: PermissionEnforcementService = Depends(get_permission_enforcement_service),
) -> EnforcementStatusOut:
    """拨动(全局生效);本进程立刻生效,其它 worker 最多缓存 TTL 秒后跟上。"""
    return _to_out(service.set_enabled(actor, payload.enabled, ip=get_client_ip(request)))
