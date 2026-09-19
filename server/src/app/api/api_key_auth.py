"""API Key 的认证与路由授权表(OPT-6)。

设计要点(与 `docs/架构设计.md` §8 对齐):

1. **两个认证入口并存**:JWT(人用)与 API Key(程序用,`X-API-Key` 头)。
   Key 以**创建者身份**行事 —— 空间成员校验、消息归属、审计 actor 都落到创建者;
   创建者被移出空间,Key 在认证阶段即失效(见 ApiKeyService.authenticate)。
2. **授权表 fail-closed**:`_ROUTE_CAPABILITIES` 是"能力 → 允许的路由"白名单。
   用 Key 访问**表里没有的路由**一律 403 —— 新增路由不会自动对 Key 开放,
   必须显式登记。"漏登记"与"误放开"之间刻意选了前一个失败方向。
3. **路径模板匹配**:路由带 `{space_id}` 这类参数,按段数相同 + 参数段通配比较,
   不用正则(易写错且难读)。
4. **KB 范围收窄在服务调用前完成**:调用方拿到收窄后的 `kb_ids` 必须传给
   检索/问答服务,不能只在这里判断而不传递。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.application.repository.api_keys import ApiKeyRepositoryImpl
from app.application.repository.audit import AuditRepositoryImpl
from app.application.repository.knowledge import KnowledgeBaseRepositoryImpl
from app.application.repository.spaces import SpaceRepositoryImpl
from app.application.service.api_keys import ApiKeyService
from app.core.db import get_db
from app.core.errors import AppError, ErrorCode
from app.domain.enums import ApiCapability
from app.domain.models import ApiKey

API_KEY_HEADER = "x-api-key"

# ---------------------------- 路由授权表(fail-closed) ----------------------------

# 能力 → 允许的路由(路径模板, 大写方法)。不在表内的 (路径, 方法) 组合一律拒绝。
# 能力取值与原型页 07 的勾选项一致:「对话检索」= /ask + /search;「文档管理」=
# 文档上传/查看/删除/重解析 + 为定位文档所必需的只读列表接口。
_ROUTE_CAPABILITIES: dict[ApiCapability, set[tuple[str, str]]] = {
    ApiCapability.CHAT: {
        ("/api/v1/spaces/{space_id}/ask", "POST"),
        ("/api/v1/spaces/{space_id}/retrieval/search", "POST"),
        # OPT-3:断线续流与 /ask 同属对话检索能力
        ("/api/v1/spaces/{space_id}/conversations/{conversation_id}/stream", "GET"),
    },
    ApiCapability.DOCUMENTS: {
        ("/api/v1/spaces/{space_id}/knowledge-bases", "GET"),
        ("/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}", "GET"),
        (
            "/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents",
            "GET",
        ),
        (
            "/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents",
            "POST",
        ),
        (
            "/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{document_id}",
            "GET",
        ),
        (
            "/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{document_id}",
            "DELETE",
        ),
        (
            "/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{document_id}"
            "/chunks",
            "GET",
        ),
        (
            "/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{document_id}"
            "/chunks/{chunk_id}",
            "GET",
        ),
        (
            "/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{document_id}"
            "/reparse",
            "POST",
        ),
    },
}


def resolve_route_capability(path: str, method: str) -> ApiCapability | None:
    """查表:该路由允许哪个能力访问;None = 未登记 → 用 Key 访问一律拒绝。"""
    verb = method.upper()
    for capability, allowed in _ROUTE_CAPABILITIES.items():
        for template, allowed_verb in allowed:
            if allowed_verb == verb and _template_matches(template, path):
                return capability
    return None


def _template_matches(template: str, actual: str) -> bool:
    """按段比较:段数相同,模板里 `{...}` 段匹配任意一段。"""
    t_parts = template.strip("/").split("/")
    a_parts = actual.strip("/").split("/")
    if len(t_parts) != len(a_parts):
        return False
    for t_seg, a_seg in zip(t_parts, a_parts, strict=True):
        if t_seg.startswith("{") and t_seg.endswith("}"):
            continue
        if t_seg != a_seg:
            return False
    return True


# ---------------------------- 认证依赖 ----------------------------


@dataclass(frozen=True)
class ApiKeyPrincipal:
    """以 API Key 身份发起的调用主体(Key 以创建者身份行事)。"""

    api_key: ApiKey
    space_id: uuid.UUID
    actor_id: uuid.UUID


def build_api_key_service(db: Session) -> ApiKeyService:
    """组装 API Key 服务(与 deps.py 的构造注入风格一致)。"""
    return ApiKeyService(
        ApiKeyRepositoryImpl(db),
        SpaceRepositoryImpl(db),
        KnowledgeBaseRepositoryImpl(db),
        AuditRepositoryImpl(db),
    )


def get_api_key_principal(
    request: Request,
    db: Session = Depends(get_db),
) -> ApiKeyPrincipal:
    """`X-API-Key` 认证 + 路由授权表判定。

    无凭据/凭据无效 → 401(与"没登录"同构);凭据有效但能力不足/路由未登记 → 403。
    401 会进被拒审计的"仅登录路径"过滤,不会刷成噪声;403 一律落审计。
    """
    raw = request.headers.get(API_KEY_HEADER, "").strip()
    if not raw:
        raise AppError(ErrorCode.API_KEY_INVALID, "缺少 API Key", http_status=401)

    service = build_api_key_service(db)
    api_key = service.authenticate(raw)
    if api_key is None:
        raise AppError(ErrorCode.API_KEY_INVALID, "API Key 无效或已吊销", http_status=401)

    required = resolve_route_capability(request.url.path, request.method)
    if required is None:
        raise AppError(
            ErrorCode.API_KEY_CAPABILITY_DENIED,
            "该 API Key 不允许访问此接口",
            http_status=403,
            details={"route": request.url.path, "reason": "route_not_grantable"},
        )
    service.require_capability(api_key, required)

    if api_key.created_by is None:  # authenticate 已保证非空,这里收紧类型
        raise AppError(ErrorCode.API_KEY_INVALID, "API Key 无效或已吊销", http_status=401)
    return ApiKeyPrincipal(
        api_key=api_key, space_id=api_key.space_id, actor_id=api_key.created_by
    )
