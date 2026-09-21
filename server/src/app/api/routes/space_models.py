"""空间模型配置端点(OPT-27):用户在空间里自配对话/重排模型,embedding 只填 Key。

权限:读 = 空间成员(非 Admin 拿到同一份数据,前端按角色隐藏写按钮);
写与【测试连接】= 空间 Admin+(Role 30),门槛判定在服务层,跨空间一律 404。

**API Key 用不了本组接口**:端点只挂 JWT 依赖(`get_current_user`),带 `X-API-Key`
不带 Bearer 的请求拿到的就是 401 `AUTH_REQUIRED`;路径也刻意没有登记进
`api_key_auth` 的授权表 —— 即使将来接入 Key 认证链路,也会 fail-closed 拒绝。
模型配置是"人的决定",不让程序化凭据代持。

密钥口径与 `/admin/*` 一致:写入即 AES-GCM 加密,响应只给 `key_hint` 掩码;
`api_key` 不传 = 不改动,传空串 = 清除(embedding Key 清除后 dev 回落全局、prod 不可用)。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import get_client_ip, get_current_user, get_space_model_admin_service
from app.api.routing import CommitBeforeSendRoute
from app.application.service.space_models import (
    SpaceModelAdminService,
    kinds_for_protocol,
)
from app.domain.models import CustomModel, User

router = APIRouter(
    prefix="/api/v1/spaces/{space_id}/model-config",
    tags=["space-models"],
    route_class=CommitBeforeSendRoute,
)


class EmbeddingCardOut(BaseModel):
    model_name: str  # 内置锁死:text-embedding-v3
    dims: int
    doc_url: str  # 去官网建 API Key 的引导链接
    key_configured: bool
    key_hint: str
    fallback_active: bool  # 未自配 Key 但全局兜底正在生效(仅 dev)


class RerankCardOut(BaseModel):
    enabled: bool
    default_model: str | None  # 开关开着却没有默认模型 = 检索时等同关闭


class SpaceProviderOut(BaseModel):
    id: str
    name: str
    protocol: str
    base_url: str
    key_hint: str
    enabled: bool
    model_count: int
    kinds: list[str]  # 该供应商允许挂载的模型类别;空 = 未知协议


class SpaceModelOut(BaseModel):
    id: str  # 表主键(PATCH/DELETE 寻址用)
    model_id: str  # 对外模型 id(进 /api/v1/models 契约的那个)
    provider_id: str
    provider_name: str
    kind: str
    model_name: str
    context_tokens: int | None
    enabled: bool
    is_default: bool


class SpaceModelConfigOut(BaseModel):
    embedding: EmbeddingCardOut
    rerank: RerankCardOut
    providers: list[SpaceProviderOut]
    models: list[SpaceModelOut]


class ProviderTypeOut(BaseModel):
    key: str
    label: str
    doc_url: str
    default_base_url: str
    protocol: str
    kinds: list[str]
    model_names: list[str]  # 模型名提示清单,不是白名单(后端不校验自定义值)


class OkOut(BaseModel):
    ok: bool = True


class EmbeddingKeyIn(BaseModel):
    api_key: str = Field(default="", max_length=512)  # 空串 = 清除本空间 Key


class EmbeddingKeyTestIn(BaseModel):
    api_key: str | None = Field(default=None, max_length=512)  # 不传 = 测已存 Key


class RerankSwitchIn(BaseModel):
    enabled: bool


class SpaceProviderIn(BaseModel):
    provider_type: str = Field(min_length=1, max_length=64)  # /types 里的 key
    name: str = Field(min_length=1, max_length=64)  # 空间内唯一的显示名
    base_url: str = Field(default="", max_length=512)  # 留空 = 用该供应商默认地址
    api_key: str = Field(default="", max_length=512)  # 空 = 免密钥本地端点
    enabled: bool = True


class SpaceProviderUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    base_url: str | None = Field(default=None, max_length=512)
    api_key: str | None = Field(default=None, max_length=512)
    enabled: bool | None = None


class SpaceModelIn(BaseModel):
    provider_id: uuid.UUID
    kind: str = Field(min_length=1, max_length=16)  # chat | rerank(embedding 内置锁死)
    model_name: str = Field(min_length=1, max_length=128)  # 上游真实模型名/接入点 id
    model_id: str | None = Field(default=None, max_length=128)  # 不传 = 供应商名/模型名
    context_tokens: int | None = Field(default=None, gt=0)  # chat 必填
    enabled: bool = True
    is_default: bool = False


class SpaceModelUpdateIn(BaseModel):
    model_name: str | None = Field(default=None, min_length=1, max_length=128)
    context_tokens: int | None = Field(default=None, gt=0)
    enabled: bool | None = None
    is_default: bool | None = None


@router.get("", response_model=SpaceModelConfigOut)
def get_model_config(
    user: User = Depends(get_current_user),
    service: SpaceModelAdminService = Depends(get_space_model_admin_service),
) -> SpaceModelConfigOut:
    """配置页聚合视图(成员可读;密钥一律掩码)。"""
    return SpaceModelConfigOut(**service.view(user))


@router.get("/types", response_model=list[ProviderTypeOut])
def list_provider_types(
    user: User = Depends(get_current_user),
    service: SpaceModelAdminService = Depends(get_space_model_admin_service),
) -> list[ProviderTypeOut]:
    """供应商名字清单(下拉数据源;base_url 允许改,不是白名单)。"""
    service.require_member(user)
    return [ProviderTypeOut(**item) for item in service.provider_types()]


@router.put("/embedding-key", response_model=OkOut)
def set_embedding_key(
    body: EmbeddingKeyIn,
    user: User = Depends(get_current_user),
    service: SpaceModelAdminService = Depends(get_space_model_admin_service),
    ip: str = Depends(get_client_ip),
) -> OkOut:
    service.set_embedding_key(user, body.api_key, ip=ip)
    return OkOut()


@router.post("/embedding-key/test", response_model=OkOut)
def test_embedding_key(
    body: EmbeddingKeyTestIn,
    user: User = Depends(get_current_user),
    service: SpaceModelAdminService = Depends(get_space_model_admin_service),
    ip: str = Depends(get_client_ip),
) -> OkOut:
    """用候选 Key(不传则用已存 Key)向内置模型发一次最小请求。"""
    service.test_embedding_key(user, body.api_key, ip=ip)
    return OkOut()


@router.put("/rerank-switch", response_model=OkOut)
def set_rerank_switch(
    body: RerankSwitchIn,
    user: User = Depends(get_current_user),
    service: SpaceModelAdminService = Depends(get_space_model_admin_service),
    ip: str = Depends(get_client_ip),
) -> OkOut:
    """重排开关:"开了就用";没有默认 rerank 模型时开着也等同关闭。"""
    service.set_rerank_enabled(user, body.enabled, ip=ip)
    return OkOut()


@router.post("/providers", response_model=SpaceProviderOut, status_code=201)
def create_provider(
    body: SpaceProviderIn,
    user: User = Depends(get_current_user),
    service: SpaceModelAdminService = Depends(get_space_model_admin_service),
    ip: str = Depends(get_client_ip),
) -> SpaceProviderOut:
    provider = service.add_provider(
        user,
        provider_type=body.provider_type,
        name=body.name,
        base_url=body.base_url,
        api_key=body.api_key,
        enabled=body.enabled,
        ip=ip,
    )
    return SpaceProviderOut(
        id=str(provider.id),
        name=provider.name,
        protocol=provider.protocol,
        base_url=provider.base_url,
        key_hint=provider.key_hint,
        enabled=provider.enabled,
        model_count=0,
        kinds=list(kinds_for_protocol(provider.protocol)),
    )


@router.patch("/providers/{provider_id}", response_model=SpaceProviderOut)
def update_provider(
    provider_id: uuid.UUID,
    body: SpaceProviderUpdateIn,
    user: User = Depends(get_current_user),
    service: SpaceModelAdminService = Depends(get_space_model_admin_service),
    ip: str = Depends(get_client_ip),
) -> SpaceProviderOut:
    provider = service.edit_provider(
        user,
        provider_id,
        name=body.name,
        base_url=body.base_url,
        api_key=body.api_key,
        clear_api_key=body.api_key == "",
        enabled=body.enabled,
        ip=ip,
    )
    counts = service.provider_model_counts()
    return SpaceProviderOut(
        id=str(provider.id),
        name=provider.name,
        protocol=provider.protocol,
        base_url=provider.base_url,
        key_hint=provider.key_hint,
        enabled=provider.enabled,
        model_count=counts.get(provider.id, 0),
        kinds=list(kinds_for_protocol(provider.protocol)),
    )


@router.delete("/providers/{provider_id}", status_code=204)
def delete_provider(
    provider_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: SpaceModelAdminService = Depends(get_space_model_admin_service),
    ip: str = Depends(get_client_ip),
) -> None:
    service.remove_provider(user, provider_id, ip=ip)


@router.post("/providers/{provider_id}/test", response_model=OkOut)
def test_provider(
    provider_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: SpaceModelAdminService = Depends(get_space_model_admin_service),
    ip: str = Depends(get_client_ip),
) -> OkOut:
    service.test_provider(user, provider_id, ip=ip)
    return OkOut()


@router.post("/models", response_model=SpaceModelOut, status_code=201)
def create_model(
    body: SpaceModelIn,
    user: User = Depends(get_current_user),
    service: SpaceModelAdminService = Depends(get_space_model_admin_service),
    ip: str = Depends(get_client_ip),
) -> SpaceModelOut:
    model = service.add_model(
        user,
        provider_id=body.provider_id,
        kind=body.kind,
        model_name=body.model_name,
        model_id=body.model_id,
        context_tokens=body.context_tokens,
        enabled=body.enabled,
        is_default=body.is_default,
        ip=ip,
    )
    names = service.provider_names()
    return _model_out(model, names.get(model.provider_id, ""))


@router.patch("/models/{model_pk}", response_model=SpaceModelOut)
def update_model(
    model_pk: uuid.UUID,
    body: SpaceModelUpdateIn,
    user: User = Depends(get_current_user),
    service: SpaceModelAdminService = Depends(get_space_model_admin_service),
    ip: str = Depends(get_client_ip),
) -> SpaceModelOut:
    model = service.edit_model(
        user,
        model_pk,
        model_name=body.model_name,
        context_tokens=body.context_tokens,
        enabled=body.enabled,
        is_default=body.is_default,
        ip=ip,
    )
    names = service.provider_names()
    return _model_out(model, names.get(model.provider_id, ""))


@router.delete("/models/{model_pk}", status_code=204)
def delete_model(
    model_pk: uuid.UUID,
    user: User = Depends(get_current_user),
    service: SpaceModelAdminService = Depends(get_space_model_admin_service),
    ip: str = Depends(get_client_ip),
) -> None:
    service.remove_model(user, model_pk, ip=ip)


@router.post("/models/{model_pk}/test", response_model=OkOut)
def test_model(
    model_pk: uuid.UUID,
    user: User = Depends(get_current_user),
    service: SpaceModelAdminService = Depends(get_space_model_admin_service),
    ip: str = Depends(get_client_ip),
) -> OkOut:
    service.test_model(user, model_pk, ip=ip)
    return OkOut()


def _model_out(model: CustomModel, provider_name: str) -> SpaceModelOut:
    return SpaceModelOut(
        id=str(model.id),
        model_id=model.model_id,
        provider_id=str(model.provider_id),
        provider_name=provider_name,
        kind=model.kind,
        model_name=model.model_name,
        context_tokens=model.context_tokens,
        enabled=model.enabled,
        is_default=model.is_default,
    )
