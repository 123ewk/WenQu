"""会话与问答路由:会话 CRUD + SSE 流式问答(M2 核心闭环)。

SSE 事件契约(前端按 type 分派):
- meta      {conversation_id}         首帧,前端据此固定会话(新会话时用它更新路由)
- citations {citations:[{index,chunk_id,document_id,filename,excerpt,score,...}]}
- delta     {text}                    正文增量,累加即答案
- done      {message_id, cited_indexes}
- error     {message}                 上游模型失败(HTTP 仍是 200,流已开始)
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.deps import (
    Caller,
    get_api_key_service,
    get_current_actor,
    get_current_user,
    get_qa_service,
)
from app.application.service.api_keys import ApiKeyService
from app.application.service.qa import QAService
from app.domain.models import Conversation, Message, User

router = APIRouter(prefix="/api/v1/spaces/{space_id}", tags=["conversations"])


class CreateConversationRequest(BaseModel):
    title: str = Field(default="新会话", max_length=128)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    conversation_id: uuid.UUID | None = None
    kb_ids: list[uuid.UUID] | None = None
    top_k: int = Field(default=6, ge=1, le=20)
    model_id: str | None = None


class RenameConversationRequest(BaseModel):
    title: str = Field(min_length=1, max_length=128)


class ConversationOut(BaseModel):
    id: str
    space_id: str
    title: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CitationOut(BaseModel):
    index: int
    chunk_id: str
    document_id: str
    kb_id: str
    filename: str
    excerpt: str
    score: float
    breadcrumb: list[str] = []
    page: int | None = None


class MessageOut(BaseModel):
    id: str
    role: str
    content: str
    seq: int
    citations: list[CitationOut] = []
    model_id: str | None = None
    created_at: datetime | None = None


def _conversation_out(conversation: Conversation) -> ConversationOut:
    return ConversationOut(
        id=str(conversation.id),
        space_id=str(conversation.space_id),
        title=conversation.title,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )


def _message_out(message: Message) -> MessageOut:
    citations = [CitationOut(**item) for item in (message.citations or [])]
    return MessageOut(
        id=str(message.id),
        role=message.role,
        content=message.content,
        seq=message.seq,
        citations=citations,
        model_id=message.model_id,
        created_at=message.created_at,
    )


@router.get("/conversations", response_model=list[ConversationOut])
def list_conversations(
    space_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: QAService = Depends(get_qa_service),
) -> list[ConversationOut]:
    return [_conversation_out(c) for c in service.list_conversations(user.id, space_id)]


@router.post("/conversations", response_model=ConversationOut, status_code=201)
def create_conversation(
    space_id: uuid.UUID,
    body: CreateConversationRequest,
    user: User = Depends(get_current_user),
    service: QAService = Depends(get_qa_service),
) -> ConversationOut:
    return _conversation_out(service.create_conversation(user.id, space_id, body.title))


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageOut])
def list_messages(
    space_id: uuid.UUID,
    conversation_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: QAService = Depends(get_qa_service),
) -> list[MessageOut]:
    messages = service.get_conversation_messages(user.id, space_id, conversation_id)
    return [_message_out(m) for m in messages]


@router.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(
    space_id: uuid.UUID,
    conversation_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: QAService = Depends(get_qa_service),
) -> None:
    service.delete_conversation(user.id, space_id, conversation_id)


@router.post("/ask")
def ask(
    space_id: uuid.UUID,
    body: AskRequest,
    caller: Caller = Depends(get_current_actor),
    service: QAService = Depends(get_qa_service),
    key_service: ApiKeyService = Depends(get_api_key_service),
) -> StreamingResponse:
    """SSE 流式问答。首帧 meta 携带(可能是新建的)conversation_id。"""
    user = caller.user
    # API Key 调用:kb_ids 先按 Key 范围收窄(范围外被拒,绝不静默降级)
    kb_ids = body.kb_ids
    if caller.api_key is not None:
        kb_ids = key_service.resolve_kb_scope(caller.api_key, kb_ids)
    events: Iterator[str] = service.ask_stream(
        user.id,
        space_id,
        body.question,
        conversation_id=body.conversation_id,
        kb_ids=kb_ids,
        top_k=body.top_k,
        model_id=body.model_id,
    )
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
    )


@router.patch("/conversations/{conversation_id}", response_model=ConversationOut)
def rename_conversation(
    space_id: uuid.UUID,
    conversation_id: uuid.UUID,
    body: RenameConversationRequest,
    user: User = Depends(get_current_user),
    service: QAService = Depends(get_qa_service),
) -> ConversationOut:
    return _conversation_out(
        service.rename_conversation(user.id, space_id, conversation_id, body.title)
    )
