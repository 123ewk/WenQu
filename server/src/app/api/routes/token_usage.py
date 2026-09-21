"""token 用量端点(OPT-28):空间管理员看成员用量,成员看自己的。

数据来自 `token_usage_events` 明细(每次上游调用一行),这里只做汇总,不换算、不补数。
`estimated_tokens` 是按本地口径估算的那部分用量,前端必须打 "≈" 角标(两种口径不可混算)。

**API Key 用不了本组接口**:端点只挂 JWT 依赖(`get_current_user`),路径也刻意没有登记
进 `api_key_auth` 的授权表 —— 与 `/model-config/*`、`/admin/*` 同一做法:用量与配置都是
"人的账",不让程序化凭据代持。带 `X-API-Key` 不带 Bearer 拿到的就是 401 `AUTH_REQUIRED`。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.api.deps import get_current_user, get_usage_service
from app.api.routing import CommitBeforeSendRoute
from app.application.service.token_usage import DEFAULT_PERIOD, UsageService
from app.domain.models import User

router = APIRouter(
    prefix="/api/v1/spaces/{space_id}",
    tags=["token-usage"],
    route_class=CommitBeforeSendRoute,
)

_Period = Literal["7d", "30d", "all"]


class UsageOut(BaseModel):
    """一份用量:三个 token 数 + 两条链路的调用次数 + 估算占比。"""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    # source=estimated 的行的 total 之和;前端展示为"其中 ≈N"
    estimated_tokens: int
    chat_calls: int
    embedding_calls: int


class MemberUsageOut(UsageOut):
    user_id: str
    username: str
    nickname: str
    role: int


class UsageSummaryOut(BaseModel):
    """Admin+ 视角:空间合计 + 当前花名册逐成员(零用量成员也在列表里)。"""

    period: _Period
    since: datetime | None  # period=all 时为 null
    generated_at: datetime
    space_total: UsageOut
    members: list[MemberUsageOut]


class MyUsageOut(BaseModel):
    period: _Period
    since: datetime | None
    generated_at: datetime
    usage: UsageOut


@router.get("/token-usage", response_model=UsageSummaryOut)
def get_token_usage(
    space_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: UsageService = Depends(get_usage_service),
    period: _Period = Query(default=DEFAULT_PERIOD),
) -> UsageSummaryOut:
    """按成员汇总本空间用量(Admin+;未知归属人的历史行只进 space_total)。"""
    return UsageSummaryOut(**service.summary_for_admin(user.id, space_id, period))


@router.get("/token-usage/me", response_model=MyUsageOut)
def get_my_token_usage(
    space_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: UsageService = Depends(get_usage_service),
    period: _Period = Query(default=DEFAULT_PERIOD),
) -> MyUsageOut:
    """我自己的用量(任意成员;非成员 404,与跨空间访问同壳)。"""
    return MyUsageOut(**service.my_usage(user.id, space_id, period))
