"""模型清单端点:供前端渲染模型选择器(对话选模型 / 知识库换嵌入模型)。

只列**已启用**的模型:不可选的条目不该出现在下拉里。
不返回密钥,也不返回"密钥是否已配置" —— 可选清单与服务端能否真正调用是两件事,
前端选中后仍可能在调用时拿到 MODEL_NOT_CONFIGURED,需要能展示该错误。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.core.model_catalog import ModelCatalog
from app.domain.models import User

router = APIRouter(prefix="/api/v1/models", tags=["models"])


class ModelItem(BaseModel):
    id: str
    provider: str  # 供应商显示名
    provider_key: str
    model: str
    dims: int | None = None  # 仅 embedding
    context_tokens: int | None = None  # 仅 chat


class ModelCatalogOut(BaseModel):
    chat: list[ModelItem]
    embedding: list[ModelItem]
    rerank: list[ModelItem]
    defaults: dict[str, str]


@router.get("", response_model=ModelCatalogOut)
def list_models(user: User = Depends(get_current_user)) -> ModelCatalogOut:
    """登录即可读;列出已启用模型与各类默认值。"""
    catalog = ModelCatalog.load()
    grouped: dict[str, list[ModelItem]] = {"chat": [], "embedding": [], "rerank": []}
    for kind, entries in catalog.enabled_models_by_kind().items():
        for entry in entries:
            grouped[kind].append(
                ModelItem(
                    id=entry.id,
                    provider=catalog.provider_display_name(entry.provider_key),
                    provider_key=entry.provider_key,
                    model=entry.model,
                    dims=entry.dims,
                    context_tokens=entry.context_tokens,
                )
            )
    return ModelCatalogOut(
        chat=grouped["chat"],
        embedding=grouped["embedding"],
        rerank=grouped["rerank"],
        defaults=catalog.defaults(),
    )
