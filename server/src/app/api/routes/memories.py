"""记忆路由(OPT-45 P1):用户确认后的事实记忆 CRUD。

归属 = 当前 JWT 用户 × 路径 space_id;非本人/他空间的记忆一律按 404 返回(防枚举)。
本期仅面向人(JWT),API Key 调用不进本路由(路由未登记进 Key 授权表,fail-closed)。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.api.deps import get_current_user, get_memory_service
from app.application.service.memory import MemoryService
from app.domain.models import Memory, User

router = APIRouter(prefix="/api/v1/spaces/{space_id}", tags=["memories"])


class SaveMemoryRequest(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    kind: str = Field(default="fact", max_length=16)
    topic: str = Field(default="", max_length=255)
    source_conversation_id: uuid.UUID | None = None
    source_message_id: uuid.UUID | None = None


class UpdateMemoryRequest(BaseModel):
    content: str | None = Field(default=None, max_length=2000)
    kind: str | None = Field(default=None, max_length=16)
    topic: str | None = Field(default=None, max_length=255)


class MemoryOut(BaseModel):
    id: str
    space_id: str
    kind: str
    content: str
    topic: str
    source_conversation_id: str | None = None
    created_at: str | None = None


def _out(memory: Memory) -> MemoryOut:
    return MemoryOut(
        id=str(memory.id),
        space_id=str(memory.space_id),
        kind=memory.kind,
        content=memory.content,
        topic=memory.topic,
        source_conversation_id=str(memory.source_conversation_id)
        if memory.source_conversation_id
        else None,
        created_at=memory.created_at.isoformat() if memory.created_at else None,
    )


@router.post("/memories", response_model=MemoryOut, status_code=201)
def save_memory(
    space_id: uuid.UUID,
    body: SaveMemoryRequest,
    user: User = Depends(get_current_user),
    service: MemoryService = Depends(get_memory_service),
) -> MemoryOut:
    memory = service.save_confirmed(
        space_id,
        user.id,
        body.content,
        kind=body.kind,
        topic=body.topic,
        source_conversation_id=body.source_conversation_id,
        source_message_id=body.source_message_id,
    )
    return _out(memory)


@router.get("/memories", response_model=list[MemoryOut])
def list_memories(
    space_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    service: MemoryService = Depends(get_memory_service),
) -> list[MemoryOut]:
    memories, _total = service.list_memories(space_id, user.id, limit, offset)
    return [_out(m) for m in memories]


@router.patch("/memories/{memory_id}", response_model=MemoryOut)
def update_memory(
    space_id: uuid.UUID,
    memory_id: uuid.UUID,
    body: UpdateMemoryRequest,
    user: User = Depends(get_current_user),
    service: MemoryService = Depends(get_memory_service),
) -> MemoryOut:
    memory = service.update(
        space_id, user.id, memory_id, content=body.content, kind=body.kind, topic=body.topic
    )
    return _out(memory)


@router.delete("/memories/{memory_id}", status_code=204)
def delete_memory(
    space_id: uuid.UUID,
    memory_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: MemoryService = Depends(get_memory_service),
) -> None:
    service.delete(space_id, user.id, memory_id)
