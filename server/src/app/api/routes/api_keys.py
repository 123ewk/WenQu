"""API Key 路由(OPT-6,原型页 07):签发 / 列表 / 吊销。

安全语义:
- 完整明文 Key **只在创建响应里出现一次**(`plaintext` 字段);列表/详情只给
  `key_hint` 截断串 —— 服务端不存明文,关掉弹窗后无法找回;
- 建与吊销要求 Editor+;列表 Viewer+;
- capability 取值:chat(对话检索)/ documents(文档管理);kb_ids 为空 = 全部知识库。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import get_api_key_service, get_client_ip, get_current_user
from app.application.service.api_keys import ApiKeyService
from app.domain.models import ApiKey, User

router = APIRouter(prefix="/api/v1/spaces/{space_id}/api-keys", tags=["api-keys"])


class CreateApiKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=512)
    # 能力清单;空数组合法但等于"什么都不能做"(fail-closed),前端默认勾选对话检索
    capabilities: list[str] = Field(default_factory=list, max_length=8)
    # 允许的知识库范围;空数组 = 全部
    kb_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)


class ApiKeyOut(BaseModel):
    id: str
    name: str
    description: str
    key_hint: str  # 形如 sk-live-****ab12,用于列表辨识
    capabilities: list[str]
    kb_ids: list[str]
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None
    created_at: datetime | None = None


class ApiKeyCreatedOut(ApiKeyOut):
    """创建响应:比列表多一个 plaintext,且仅此一次。"""

    plaintext: str


def _out(api_key: ApiKey) -> ApiKeyOut:
    return ApiKeyOut(
        id=str(api_key.id),
        name=api_key.name,
        description=api_key.description,
        key_hint=api_key.key_hint,
        capabilities=list(api_key.capabilities or []),
        kb_ids=[str(kb) for kb in (api_key.kb_ids or [])],
        revoked_at=api_key.revoked_at,
        last_used_at=api_key.last_used_at,
        created_at=api_key.created_at,
    )


@router.get("", response_model=list[ApiKeyOut])
def list_api_keys(
    space_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: ApiKeyService = Depends(get_api_key_service),
) -> list[ApiKeyOut]:
    # 含已吊销(前端可置灰展示,审计轨迹不留盲区)
    return [_out(k) for k in service.list_for_space(space_id, user)]


@router.post("", response_model=ApiKeyCreatedOut, status_code=201)
def create_api_key(
    space_id: uuid.UUID,
    body: CreateApiKeyRequest,
    user: User = Depends(get_current_user),
    service: ApiKeyService = Depends(get_api_key_service),
    ip: str = Depends(get_client_ip),
) -> ApiKeyCreatedOut:
    created = service.create(
        space_id,
        user,
        name=body.name,
        description=body.description,
        capabilities=body.capabilities,  # type: ignore[arg-type]  # 服务内归一并拒绝未知值
        kb_ids=body.kb_ids,
        ip=ip,
    )
    return ApiKeyCreatedOut(**_out(created.api_key).model_dump(), plaintext=created.plaintext)


@router.delete("/{key_id}", status_code=204)
def revoke_api_key(
    space_id: uuid.UUID,
    key_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: ApiKeyService = Depends(get_api_key_service),
    ip: str = Depends(get_client_ip),
) -> None:
    service.revoke(space_id, user, key_id, ip)
