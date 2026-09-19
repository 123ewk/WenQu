"""分块预览调试接口(ADR-5「调优刚需」):纯函数、无状态、不落库。

正式入库链路的 md/txt 解析以 parser 服务为准;此处的轻量解析仅供粘贴文本即时调参。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import get_current_user
from app.application.chunking import blocks_from_text, parent_child_chunks
from app.domain.models import User

router = APIRouter(prefix="/api/v1/chunks", tags=["chunking"])


class ChunkPreviewRequest(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)
    format: Literal["md", "txt"] = "md"


class ChunkPreviewItem(BaseModel):
    content: str
    tokens: int
    breadcrumb: list[str]
    page: int | None
    kind: str
    # 父子分块(OPT-4):父块内子块预览(检索窗口层);未触发切分时为空
    children: list[ChunkPreviewItem] = []


@router.post("/preview", response_model=list[ChunkPreviewItem])
def preview_chunks(
    body: ChunkPreviewRequest,
    user: User = Depends(get_current_user),
) -> list[ChunkPreviewItem]:
    pairs = parent_child_chunks(blocks_from_text(body.text, body.format))
    return [
        ChunkPreviewItem(
            content=parent.content,
            tokens=parent.tokens,
            breadcrumb=parent.breadcrumb,
            page=parent.page,
            kind=parent.kind,
            children=[
                ChunkPreviewItem(
                    content=c.content,
                    tokens=c.tokens,
                    breadcrumb=c.breadcrumb,
                    page=c.page,
                    kind=c.kind,
                )
                for c in children
            ],
        )
        for parent, children in pairs
    ]
