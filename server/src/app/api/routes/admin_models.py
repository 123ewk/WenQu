"""模型管理端点(OPT-25):superuser 专属,自定义供应商与模型的增删改查。

权限:全部要求 JWT 且 `is_superuser`(平台级配置,与空间无关;API Key 链路根本不
接入本路由前缀 —— 授权表未登记,用 Key 访问一律 403 fail-closed)。

密钥口径:写入即加密,响应只给 `key_hint` 掩码;PATCH 里 `api_key`
**不传 = 不改动,传空串 = 清除密钥(转本地免密钥端点),传非空 = 更换**。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import get_client_ip, get_current_superuser, get_model_admin_service
from app.api.routing import CommitBeforeSendRoute
from app.application.service.model_admin import ModelAdminService
from app.core.model_protocols import registered_protocols
from app.domain.models import CustomModel, ModelProvider

router = APIRouter(prefix="/api/v1/admin", tags=["admin-models"], route_class=CommitBeforeSendRoute)


class ProviderIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    base_url: str = Field(min_length=8, max_length=512)
    protocol: str = Field(default="openai_compatible", max_length=32)
    api_key: str = Field(default="", max_length=512)  # 空 = 免密钥本地端点
    enabled: bool = True


class ProviderUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    base_url: str | None = Field(default=None, min_length=8, max_length=512)
    protocol: str | None = Field(default=None, max_length=32)
    api_key: str | None = Field(default=None, max_length=512)
    enabled: bool | None = None


class ProviderOut(BaseModel):
    id: str
    name: str
    protocol: str
    base_url: str
    key_hint: str  # 掩码(如 "sk-1****9abc");永不回显明文
    enabled: bool
    model_count: int


class ModelIn(BaseModel):
    model_id: str = Field(min_length=1, max_length=128)
    provider_id: uuid.UUID
    kind: Literal["chat", "embedding", "rerank"]
    model_name: str = Field(min_length=1, max_length=128)
    dims: int | None = None  # embedding 必填且当前必须为 1024
    context_tokens: int | None = None
    enabled: bool = True
    is_default: bool = False


class ModelUpdateIn(BaseModel):
    model_name: str | None = Field(default=None, min_length=1, max_length=128)
    dims: int | None = None
    context_tokens: int | None = None
    enabled: bool | None = None
    is_default: bool | None = None


class ModelOut(BaseModel):
    id: str  # 表主键(PATCH/DELETE 寻址用)
    model_id: str  # 对外模型 id(进 /api/v1/models 契约的那个)
    provider_id: str
    provider: str
    kind: str
    model_name: str
    dims: int | None = None
    context_tokens: int | None = None
    enabled: bool
    is_default: bool
    created_at: datetime | None = None


class ProtocolsOut(BaseModel):
    protocols: list[str]  # 注册表键清单,前端渲染 protocol 下拉


def _provider_out(p: ModelProvider, model_count: int) -> ProviderOut:
    return ProviderOut(
        id=str(p.id),
        name=p.name,
        protocol=p.protocol,
        base_url=p.base_url,
        key_hint=p.key_hint,
        enabled=p.enabled,
        model_count=model_count,
    )


def _model_out(m: CustomModel, provider_name: str) -> ModelOut:
    return ModelOut(
        id=str(m.id),
        model_id=m.model_id,
        provider_id=str(m.provider_id),
        provider=provider_name,
        kind=m.kind,
        model_name=m.model_name,
        dims=m.dims,
        context_tokens=m.context_tokens,
        enabled=m.enabled,
        is_default=m.is_default,
        created_at=m.created_at,
    )


@router.get("/model-protocols", response_model=ProtocolsOut)
def list_protocols(
    _actor=Depends(get_current_superuser),
) -> ProtocolsOut:
    """协议注册表键清单(写 provider 前的可选项探测)。"""
    return ProtocolsOut(protocols=registered_protocols())


@router.get("/model-providers", response_model=list[ProviderOut])
def list_providers(
    _actor=Depends(get_current_superuser),
    service: ModelAdminService = Depends(get_model_admin_service),
) -> list[ProviderOut]:
    providers = service.list_providers()
    counts = service.provider_model_counts()
    return [_provider_out(p, counts.get(p.id, 0)) for p in providers]


@router.post("/model-providers", response_model=ProviderOut, status_code=201)
def create_provider(
    body: ProviderIn,
    actor=Depends(get_current_superuser),
    service: ModelAdminService = Depends(get_model_admin_service),
    ip: str = Depends(get_client_ip),
) -> ProviderOut:
    provider = service.create_provider(
        actor,
        name=body.name,
        base_url=body.base_url,
        protocol=body.protocol,
        api_key=body.api_key,
        enabled=body.enabled,
        ip=ip,
    )
    return _provider_out(provider, 0)


@router.patch("/model-providers/{provider_id}", response_model=ProviderOut)
def update_provider(
    provider_id: uuid.UUID,
    body: ProviderUpdateIn,
    actor=Depends(get_current_superuser),
    service: ModelAdminService = Depends(get_model_admin_service),
    ip: str = Depends(get_client_ip),
) -> ProviderOut:
    provider = service.update_provider(
        actor,
        provider_id,
        name=body.name,
        base_url=body.base_url,
        protocol=body.protocol,
        api_key=body.api_key,
        clear_api_key=body.api_key == "",
        enabled=body.enabled,
        ip=ip,
    )
    counts = service.provider_model_counts()
    return _provider_out(provider, counts.get(provider.id, 0))


@router.delete("/model-providers/{provider_id}", status_code=204)
def delete_provider(
    provider_id: uuid.UUID,
    actor=Depends(get_current_superuser),
    service: ModelAdminService = Depends(get_model_admin_service),
    ip: str = Depends(get_client_ip),
) -> None:
    service.delete_provider(actor, provider_id, ip=ip)


@router.get("/models", response_model=list[ModelOut])
def list_models(
    kind: str | None = None,
    _actor=Depends(get_current_superuser),
    service: ModelAdminService = Depends(get_model_admin_service),
) -> list[ModelOut]:
    """全量模型清单(含停用):管理页数据源,与公开 /api/v1/models 不同,这里不隐藏禁用项。"""
    models = service.list_models(kind)
    names = service.provider_names()
    return [_model_out(m, names.get(m.provider_id, "")) for m in models]


@router.post("/models", response_model=ModelOut, status_code=201)
def create_model(
    body: ModelIn,
    actor=Depends(get_current_superuser),
    service: ModelAdminService = Depends(get_model_admin_service),
    ip: str = Depends(get_client_ip),
) -> ModelOut:
    model = service.create_model(
        actor,
        model_id=body.model_id,
        provider_id=body.provider_id,
        kind=body.kind,
        model_name=body.model_name,
        dims=body.dims,
        context_tokens=body.context_tokens,
        enabled=body.enabled,
        is_default=body.is_default,
        ip=ip,
    )
    names = service.provider_names()
    return _model_out(model, names.get(model.provider_id, ""))


@router.patch("/models/{model_id}", response_model=ModelOut)
def update_model(
    model_id: uuid.UUID,
    body: ModelUpdateIn,
    actor=Depends(get_current_superuser),
    service: ModelAdminService = Depends(get_model_admin_service),
    ip: str = Depends(get_client_ip),
) -> ModelOut:
    model = service.update_model(
        actor,
        model_id,
        model_name=body.model_name,
        dims=body.dims,
        context_tokens=body.context_tokens,
        enabled=body.enabled,
        is_default=body.is_default,
        ip=ip,
    )
    names = service.provider_names()
    return _model_out(model, names.get(model.provider_id, ""))


@router.delete("/models/{model_id}", status_code=204)
def delete_model(
    model_id: uuid.UUID,
    actor=Depends(get_current_superuser),
    service: ModelAdminService = Depends(get_model_admin_service),
    ip: str = Depends(get_client_ip),
) -> None:
    service.delete_model(actor, model_id, ip=ip)
