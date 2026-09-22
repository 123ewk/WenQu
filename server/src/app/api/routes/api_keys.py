"""API Key 路由(OPT-6 签发/列表/吊销;OPT-26 起绑成员;OPT-41 起权限在档位)。

安全语义:
- 完整明文 Key **只在创建响应里出现一次**(`plaintext` 字段);列表/详情只给
  `key_hint` 截断串 —— 服务端不存明文,关掉弹窗后无法找回;
- **签发/吊销要求 Admin+**(与拉成员同权),创建 body 必须指定 `group_id`(本空间的
  现存档位)与 `owner_user_id`(该空间的现存成员);列表 Viewer+;
- **Key 只是凭据,权限看档位**:`group` 字段是响应里唯一的权限口径(能力 + 知识库范围),
  改档位即时作用于其下全部 Key,不需要重发明文;
- `owner_user_id` 只决定"以谁的身份行事 + 调用归因":本人被移出 = Key 即时失效,
  本人被降级 = 高权限路由被拒;它不再决定权限范围;
- OPT-31 的 `is_active`/`PUT /{key_id}/activate` 已随 OPT-41 删除:"谁当前用哪一档"
  改由 `PUT /permission-groups/{group_id}/current` 表达(见 permission_groups 路由)。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import get_api_key_service, get_client_ip, get_current_user
from app.api.routing import CommitBeforeSendRoute
from app.application.service.api_keys import ApiKeyService, ApiKeyWithOwner
from app.domain.models import User

router = APIRouter(
    prefix="/api/v1/spaces/{space_id}/api-keys",
    tags=["api-keys"],
    route_class=CommitBeforeSendRoute,
)


class CreateApiKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=512)
    # 权限档位:能力清单与知识库范围的全部来源(OPT-41)
    group_id: uuid.UUID
    # 归因成员:Key 以此人在空间内的身份行事(OPT-26)
    owner_user_id: uuid.UUID


class ApiKeyGroupRef(BaseModel):
    """Key 所属档位的权限口径(与 /permission-groups 同源,前端两处共用一套映射)。"""

    id: str
    name: str
    capabilities: list[str]
    kb_ids: list[str]
    all_kbs: bool  # true = 本空间全部库(每次请求实时展开);false 时按 kb_ids,空 = 不给


class ApiKeyOut(BaseModel):
    id: str
    name: str
    description: str
    key_hint: str  # 形如 sk-live-****ab12,用于列表辨识
    group: ApiKeyGroupRef
    owner_user_id: str | None = None  # 归因成员 id(存量回退签发者;成员注销后为 null)
    owner_username: str | None = None
    owner_nickname: str | None = None
    owner_role: int | None = None  # 本空间角色,实时值(随成员角色变化)
    created_by: str | None = None  # 签发操作者 id(仅留痕)
    created_by_name: str | None = None
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None
    created_at: datetime | None = None


class ApiKeyCreatedOut(ApiKeyOut):
    """创建响应:比列表多一个 plaintext,且仅此一次。"""

    plaintext: str


def _out(view: ApiKeyWithOwner) -> ApiKeyOut:
    api_key = view.api_key
    owner = view.owner
    group = view.group
    # 归因口径与服务层的 `owner_of`/`authenticate` 保持一致:存量无 owner_user_id 回退签发者
    owner_id = api_key.owner_user_id or api_key.created_by
    return ApiKeyOut(
        id=str(api_key.id),
        name=api_key.name,
        description=api_key.description,
        key_hint=api_key.key_hint,
        group=ApiKeyGroupRef(
            id=str(group.id),
            name=group.name,
            capabilities=list(group.capabilities or []),
            kb_ids=[str(kb) for kb in (group.kb_ids or [])],
            all_kbs=bool(group.all_kbs),
        ),
        owner_user_id=str(owner_id) if owner_id else None,
        owner_username=owner.username if owner is not None else None,
        owner_nickname=owner.nickname if owner is not None else None,
        owner_role=view.owner_role,
        created_by=str(api_key.created_by) if api_key.created_by else None,
        created_by_name=view.created_by_name,
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
    return [_out(v) for v in service.list_for_space(space_id, user)]


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
        body.owner_user_id,
        body.group_id,
        name=body.name,
        description=body.description,
        ip=ip,
    )
    return ApiKeyCreatedOut(
        **_out(service.describe(created.api_key)).model_dump(),
        plaintext=created.plaintext,
    )


@router.delete("/{key_id}", status_code=204)
def revoke_api_key(
    space_id: uuid.UUID,
    key_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: ApiKeyService = Depends(get_api_key_service),
    ip: str = Depends(get_client_ip),
) -> None:
    service.revoke(space_id, user, key_id, ip)
