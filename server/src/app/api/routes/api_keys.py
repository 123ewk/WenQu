"""API Key 路由(OPT-6 签发/列表/吊销;OPT-26 起绑定成员):

安全语义:
- 完整明文 Key **只在创建响应里出现一次**(`plaintext` 字段);列表/详情只给
  `key_hint` 截断串 —— 服务端不存明文,关掉弹窗后无法找回;
- **签发/吊销要求 Admin+**(与拉成员同权),创建 body 必须指定 `member_user_id`
  (该空间的现存成员);列表 Viewer+;
- Key 以绑定成员身份行事:成员被移出 = Key 即时失效,成员降级 = 高权限路由被拒;
- `is_active`「正在使用」标记(OPT-31):Admin+ 经 `PUT /{key_id}/activate` 分配,
  同成员至多一把;**仅管理语义,不参与认证**——未标记的未吊销 Key 照常可用;
- capability 取值:chat(对话检索)/ documents(文档管理);kb_ids 为空 = 全部知识库。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import get_api_key_service, get_client_ip, get_current_user
from app.api.routing import CommitBeforeSendRoute
from app.application.service.api_keys import ApiKeyService, ApiKeyWithMember
from app.domain.models import User

router = APIRouter(
    prefix="/api/v1/spaces/{space_id}/api-keys",
    tags=["api-keys"],
    route_class=CommitBeforeSendRoute,
)


class CreateApiKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=512)
    # 绑定成员:Key 以此人在空间内的身份行事(OPT-26)
    member_user_id: uuid.UUID
    # 能力清单;空数组合法但等于"什么都不能做"(fail-closed),前端默认勾选对话检索
    capabilities: list[str] = Field(default_factory=list, max_length=8)
    # 允许的知识库范围;空数组 = 全部
    kb_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)


class ApiKeyOut(BaseModel):
    id: str
    name: str
    description: str
    key_hint: str  # 形如 sk-live-****ab12,用于列表辨识
    member_user_id: str | None = None  # 绑定成员 id(旧存量回退签发者;成员注销后为 null)
    member_username: str | None = None
    member_nickname: str | None = None
    member_role: int | None = None  # 绑定时点的空间角色(随成员角色实时变化)
    created_by: str | None = None  # 签发操作者 id(仅留痕)
    created_by_name: str | None = None
    capabilities: list[str]
    kb_ids: list[str]
    is_active: bool = False  # 「正在使用」分配标记(OPT-31,Admin+ 设定,成员互斥)
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None
    created_at: datetime | None = None


class ApiKeyCreatedOut(ApiKeyOut):
    """创建响应:比列表多一个 plaintext,且仅此一次。"""

    plaintext: str


def _out(view: ApiKeyWithMember) -> ApiKeyOut:
    api_key = view.api_key
    member = view.member
    return ApiKeyOut(
        id=str(api_key.id),
        name=api_key.name,
        description=api_key.description,
        key_hint=api_key.key_hint,
        member_user_id=str(member.id) if member is not None else (
            str(api_key.member_user_id) if api_key.member_user_id else None
        ),
        member_username=member.username if member is not None else None,
        member_nickname=member.nickname if member is not None else None,
        member_role=view.member_role,
        created_by=str(api_key.created_by) if api_key.created_by else None,
        created_by_name=view.created_by_name,
        capabilities=list(api_key.capabilities or []),
        kb_ids=[str(kb) for kb in (api_key.kb_ids or [])],
        is_active=bool(api_key.is_active),
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
        body.member_user_id,
        name=body.name,
        description=body.description,
        capabilities=body.capabilities,  # type: ignore[arg-type]  # 服务内归一并拒绝未知值
        kb_ids=body.kb_ids,
        ip=ip,
    )
    member, member_role = service.member_of(created.api_key)
    view = ApiKeyWithMember(
        api_key=created.api_key,
        member=member,
        member_role=member_role,
        created_by_name=user.username,
    )
    return ApiKeyCreatedOut(**_out(view).model_dump(), plaintext=created.plaintext)


@router.delete("/{key_id}", status_code=204)
def revoke_api_key(
    space_id: uuid.UUID,
    key_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: ApiKeyService = Depends(get_api_key_service),
    ip: str = Depends(get_client_ip),
) -> None:
    service.revoke(space_id, user, key_id, ip)


@router.put("/{key_id}/activate", response_model=ApiKeyOut)
def activate_api_key(
    space_id: uuid.UUID,
    key_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: ApiKeyService = Depends(get_api_key_service),
    ip: str = Depends(get_client_ip),
) -> ApiKeyOut:
    # 设为「正在使用」(OPT-31):Admin+ 分配,同成员原生效 Key 自动置 false;幂等
    activated = service.activate(space_id, user, key_id, ip)
    member, member_role = service.member_of(activated)
    return _out(
        ApiKeyWithMember(
            api_key=activated,
            member=member,
            member_role=member_role,
            created_by_name=None,
        )
    )
