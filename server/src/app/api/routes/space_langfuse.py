"""空间级 Langfuse 端点(OPT-49 v2):配置管理 + trace 产品内查看。

- `GET/PUT /langfuse-config`:本空间 Admin+(role≥30)或 superuser(守卫
  `get_space_langfuse_manager`);私钥三态语义同模型供应商;
- `GET /langfuse-traces`:trace 列表 —— 本空间成员只看自己(userId 强制=自己),
  Admin+/superuser 可传 `user_id` 看指定成员,不传看全空间;
- `GET /langfuse-traces/{trace_id}`:trace 详情 —— 成员只能看自己的(按 metadata
  user_id 校验,他人 trace 归一为 404 防枚举);
- 空间未接入(enabled=false/无行)时列表返回 configured=false,详情 409;
- 只认 JWT;Key 的路由授权表不登记本路由,程序凭据 fail-closed。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.api.deps import (
    get_client_ip,
    get_current_user,
    get_langfuse_config_service,
    get_space_langfuse_manager,
)
from app.api.routing import CommitBeforeSendRoute
from app.application.service.langfuse_config import LangfuseConfigService, LangfuseConfigStatus
from app.domain.models import User

router = APIRouter(
    prefix="/api/v1/spaces/{space_id}",
    tags=["langfuse"],
    route_class=CommitBeforeSendRoute,
)


class LangfuseConfigPut(BaseModel):
    host: str = Field(min_length=1, max_length=255)
    public_key: str = Field(min_length=1, max_length=255)
    # None = 不改动(保留已存密钥);"" = 清除;非空 = 替换(与模型供应商 api_key 同语义)
    secret_key: str | None = Field(default=None, max_length=255)
    enabled: bool


class LangfuseConfigOut(BaseModel):
    enabled: bool
    host: str
    public_key: str  # Langfuse 的 Public Key 设计为可公开标识,非机密
    key_hint: str  # Secret Key 掩码;明文永不回显
    updated_at: datetime | None = None
    updated_by_hint: str | None = None


class LangfuseTraceItem(BaseModel):
    """Langfuse trace 列表项(透传其 API 字段,前端按需取用)。"""

    id: str
    timestamp: datetime | None = None
    name: str | None = None
    input: object | None = None
    output: object | None = None
    latency: float | None = None
    metadata: dict | None = None


class LangfuseTraceListOut(BaseModel):
    configured: bool
    items: list[LangfuseTraceItem]
    meta: dict | None = None


class LangfuseTraceDetailOut(BaseModel):
    """trace 详情(Langfuse 单条对象透传)。"""

    id: str
    data: dict


def _to_out(status: LangfuseConfigStatus) -> LangfuseConfigOut:
    return LangfuseConfigOut(
        enabled=status.enabled,
        host=status.host,
        public_key=status.public_key,
        key_hint=status.key_hint,
        updated_at=status.updated_at,
        updated_by_hint=status.updated_by_hint,
    )


@router.get("/langfuse-config", response_model=LangfuseConfigOut)
def get_space_langfuse_config(
    space_id: uuid.UUID,
    actor: User = Depends(get_space_langfuse_manager),
    service: LangfuseConfigService = Depends(get_langfuse_config_service),
) -> LangfuseConfigOut:
    """读本空间接入配置(未配置时各字段为空/false)。"""
    return _to_out(service.status(actor, space_id))


@router.put("/langfuse-config", response_model=LangfuseConfigOut)
def put_space_langfuse_config(
    space_id: uuid.UUID,
    payload: LangfuseConfigPut,
    actor: User = Depends(get_space_langfuse_manager),
    service: LangfuseConfigService = Depends(get_langfuse_config_service),
    ip: str = Depends(get_client_ip),
) -> LangfuseConfigOut:
    """保存本空间接入配置(写入即加密,审计留痕,本进程即时生效)。"""
    return _to_out(
        service.update(
            actor,
            space_id,
            host=payload.host,
            public_key=payload.public_key,
            secret_key=payload.secret_key,
            enabled=payload.enabled,
            ip=ip,
        )
    )


@router.get("/langfuse-traces", response_model=LangfuseTraceListOut)
def list_space_langfuse_traces(
    space_id: uuid.UUID,
    user_id: uuid.UUID | None = Query(default=None, description="按成员过滤(仅 Admin+ 生效)"),
    limit: int = Query(default=20, ge=1, le=50),
    actor: User = Depends(get_current_user),
    service: LangfuseConfigService = Depends(get_langfuse_config_service),
) -> LangfuseTraceListOut:
    """trace 列表:成员强制只看自己;Admin+/superuser 可看全空间或指定成员。"""
    result = service.list_traces(actor, space_id, user_id_filter=user_id, limit=limit)
    return LangfuseTraceListOut(
        configured=result["configured"],
        items=[
            LangfuseTraceItem(
                id=item.get("id", ""),
                timestamp=item.get("timestamp"),
                name=item.get("name"),
                input=item.get("input"),
                output=item.get("output"),
                latency=item.get("latency"),
                metadata=item.get("metadata"),
            )
            for item in result["items"]
        ],
        meta=result.get("meta"),
    )


@router.get("/langfuse-traces/{trace_id}", response_model=LangfuseTraceDetailOut)
def get_space_langfuse_trace(
    space_id: uuid.UUID,
    trace_id: str,
    actor: User = Depends(get_current_user),
    service: LangfuseConfigService = Depends(get_langfuse_config_service),
) -> LangfuseTraceDetailOut:
    """trace 详情:成员只能看自己的(他人的归一为 404 防枚举)。"""
    data = service.get_trace(actor, space_id, trace_id)
    return LangfuseTraceDetailOut(id=data.get("id", trace_id), data=data)
