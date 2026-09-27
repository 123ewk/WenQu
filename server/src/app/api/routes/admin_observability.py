"""Langfuse 可观测配置端点(OPT-49):superuser 专属,按"模型配置"标准管理。

权限:JWT 且 `is_superuser`(get_current_superuser,与模型管理一致)——Langfuse 是
全实例一份的可观测配置;API Key 链路不接入本路由前缀,程序凭据 fail-closed。

密钥口径(与模型供应商同款):`secret_key` 写入即 AES-GCM 加密,响应只回 `key_hint`
掩码;PUT 里 `secret_key` **不传 = 不改动,传空串 = 清除**;`enabled=true` 前置
私钥已配置(400 拦下)。变更写 audit_logs(langfuse_config.updated,密钥本体不落审计)。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import (
    get_client_ip,
    get_current_superuser,
    get_langfuse_config_service,
)
from app.api.routing import CommitBeforeSendRoute
from app.application.service.langfuse_config import (
    LangfuseConfigService,
    LangfuseConfigStatus,
)
from app.domain.models import User

router = APIRouter(
    prefix="/api/v1/admin",
    tags=["admin-observability"],
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
    public_key: str  # Langfuse 的 Public Key 本身设计为可公开标识,非机密
    key_hint: str  # Secret Key 掩码(如 "pk-lf-1****ef");明文永不回显
    updated_at: datetime | None = None
    updated_by_hint: str | None = None


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
def get_langfuse_config(
    actor: User = Depends(get_current_superuser),
    service: LangfuseConfigService = Depends(get_langfuse_config_service),
) -> LangfuseConfigOut:
    """读当前接入配置(未配置时各字段为空/false)。"""
    return _to_out(service.status(actor))


@router.put("/langfuse-config", response_model=LangfuseConfigOut)
def put_langfuse_config(
    payload: LangfuseConfigPut,
    actor: User = Depends(get_current_superuser),
    service: LangfuseConfigService = Depends(get_langfuse_config_service),
    ip: str = Depends(get_client_ip),
) -> LangfuseConfigOut:
    """保存接入配置(全局单行;写入即加密,审计留痕,本进程即时生效)。"""
    return _to_out(
        service.update(
            actor,
            host=payload.host,
            public_key=payload.public_key,
            secret_key=payload.secret_key,
            enabled=payload.enabled,
            ip=ip,
        )
    )
