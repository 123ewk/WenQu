"""带引用的流式问答(M2 核心闭环)。

链路:多轮历史 → 混合检索 → 组装带编号上下文的提示词 → 流式生成 → 落库(含引用)。

引用溯源:M2 用编号方案(简洁且对模型友好)——上下文块标 [1][2]…,要求模型以同样
编号标注;生成结束后把编号映射回真实 chunk_id/文件/摘录,存进 messages.citations。
前端把 [n] 渲染为角标,点击按 citations[n-1].chunk_id 打开抽屉预览原文。
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator

from app.application.service.retrieval import RetrievalService
from app.core.errors import AppError, ErrorCode
from app.domain.enums import Role
from app.domain.interfaces import (
    ChatGateway,
    ConversationRepository,
    MessageRepository,
    SpaceRepository,
)
from app.domain.models import Conversation, Message

logger = logging.getLogger("app.qa")

_MAX_HISTORY_MESSAGES = 10
_NO_RESULT_ANSWER = "知识库中没有检索到与该问题相关的内容,请调整提问或先上传相关文档。"

SYSTEM_PROMPT = (
    "你是企业知识库问答助手。规则:\n"
    "1. 只依据下面提供的资料回答问题,不得编造资料之外的事实;\n"
    "2. 引用资料时用其编号标注,如 [1]、[2];一句话综合多处资料时可标注多个编号;\n"
    "3. 资料不足以回答时,直接说明资料中没有相关信息,不要猜测;\n"
    "4. 回答保持简洁,使用与提问相同的语言。"
)


class QAService:
    def __init__(
        self,
        conversations: ConversationRepository,
        messages: MessageRepository,
        spaces: SpaceRepository,
        retrieval: RetrievalService,
        chat: ChatGateway,
    ) -> None:
        self._conversations = conversations
        self._messages = messages
        self._spaces = spaces
        self._retrieval = retrieval
        self._chat = chat

    def create_conversation(
        self, user_id: uuid.UUID, space_id: uuid.UUID, title: str
    ) -> Conversation:
        self._require_member(space_id, user_id)
        return self._conversations.create(
            Conversation(space_id=space_id, user_id=user_id, title=title or "新会话")
        )

    def list_conversations(self, user_id: uuid.UUID, space_id: uuid.UUID) -> list[Conversation]:
        self._require_member(space_id, user_id)
        return self._conversations.list_for_user(space_id, user_id)

    def get_conversation_messages(
        self, user_id: uuid.UUID, space_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> list[Message]:
        conversation = self._get_conversation(user_id, space_id, conversation_id)
        return self._messages.list_for_conversation(conversation.id)

    def delete_conversation(
        self, user_id: uuid.UUID, space_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> None:
        conversation = self._get_conversation(user_id, space_id, conversation_id)
        self._conversations.delete(conversation)

    def ask_stream(
        self,
        user_id: uuid.UUID,
        space_id: uuid.UUID,
        question: str,
        conversation_id: uuid.UUID | None = None,
        kb_ids: list[uuid.UUID] | None = None,
        top_k: int = 6,
        model_id: str | None = None,
    ) -> Iterator[str]:
        """前置于生成器执行入参/权限校验,再把流交出。

        生成器函数体的代码在首次迭代才跑:若校验写在里面,HTTP 层拿不到 400/403/404
        (流已开始),所以这里用"外层立即校验 + 内层生成"的分段结构。
        """
        question = question.strip()
        if not question:
            raise AppError(ErrorCode.VALIDATION, "提问内容不能为空", http_status=400)
        self._require_member(space_id, user_id)
        conversation = (
            self._get_conversation(user_id, space_id, conversation_id)
            if conversation_id is not None
            else None
        )
        return self._stream(user_id, space_id, question, conversation, kb_ids, top_k, model_id)

    def _stream(
        self,
        user_id: uuid.UUID,
        space_id: uuid.UUID,
        question: str,
        conversation: Conversation | None,
        kb_ids: list[uuid.UUID] | None,
        top_k: int,
        model_id: str | None,
    ) -> Iterator[str]:
        if conversation is None:
            conversation = self._conversations.create(
                Conversation(space_id=space_id, user_id=user_id, title=question[:30])
            )
        history = self._messages.list_for_conversation(conversation.id)
        user_message = self._messages.add(
            Message(
                conversation_id=conversation.id,
                space_id=space_id,
                role="user",
                content=question,
                seq=self._messages.next_seq(conversation.id),
            )
        )

        yield _sse({"type": "meta", "conversation_id": str(conversation.id)})

        hits = self._retrieval.search(
            user_id, space_id, question, top_k=top_k, kb_ids=kb_ids, model_id=model_id
        )
        citations = [
            {
                "index": position,
                "chunk_id": hit.chunk_id,
                "document_id": hit.document_id,
                "kb_id": hit.kb_id,
                "filename": hit.filename,
                "excerpt": hit.content[:300],
                "score": hit.score,
                "breadcrumb": hit.meta.get("breadcrumb", []),
                "page": hit.meta.get("page"),
            }
            for position, hit in enumerate(hits, start=1)
        ]
        yield _sse({"type": "citations", "citations": citations})

        if not hits:
            self._messages.add(
                Message(
                    conversation_id=conversation.id,
                    space_id=space_id,
                    role="assistant",
                    content=_NO_RESULT_ANSWER,
                    seq=self._messages.next_seq(conversation.id),
                    citations=[],
                    model_id=None,
                )
            )
            yield _sse({"type": "delta", "text": _NO_RESULT_ANSWER})
            yield _sse({"type": "done", "message_id": str(user_message.id)})
            return

        messages = _build_messages(history, question, citations)
        pieces: list[str] = []
        try:
            for piece in self._chat.chat_stream(messages, model_id):
                pieces.append(piece)
                yield _sse({"type": "delta", "text": piece})
        except Exception as exc:  # noqa: BLE001 — 上游失败要作为事件告知前端而非断流
            logger.warning("chat stream failed: %s", exc)
            yield _sse({"type": "error", "message": "模型调用失败,请稍后重试"})
            return

        answer = "".join(pieces)
        assistant_message = self._messages.add(
            Message(
                conversation_id=conversation.id,
                space_id=space_id,
                role="assistant",
                content=answer,
                seq=self._messages.next_seq(conversation.id),
                citations=citations,
                model_id=model_id,
            )
        )
        yield _sse(
            {
                "type": "done",
                "message_id": str(assistant_message.id),
                "cited_indexes": _cited_indexes(answer, len(citations)),
            }
        )

    # ---------------------------- 内部 ----------------------------

    def _require_member(self, space_id: uuid.UUID, user_id: uuid.UUID) -> int:
        membership = self._spaces.get_membership(space_id, user_id)
        if membership is None:
            raise AppError(ErrorCode.SPACE_NOT_FOUND, "空间不存在", http_status=404)
        if membership.role < Role.VIEWER:
            raise AppError(ErrorCode.FORBIDDEN, "角色权限不足", http_status=403)
        return membership.role

    def _get_conversation(
        self, user_id: uuid.UUID, space_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> Conversation:
        """会话只对创建者可见:他人会话与不存在同样返回 404(防枚举)。"""
        self._require_member(space_id, user_id)
        conversation = self._conversations.get(conversation_id)
        if (
            conversation is None
            or conversation.space_id != space_id
            or conversation.user_id != user_id
        ):
            raise AppError(ErrorCode.CONVERSATION_NOT_FOUND, "会话不存在", http_status=404)
        return conversation


def _build_messages(
    history: list[Message], question: str, citations: list[dict]
) -> list[dict[str, str]]:
    """系统提示词 + 编号上下文 + 最近若干轮历史 + 本轮提问。"""
    context = "\n\n".join(
        f"[{c['index']}] 来源:{c['filename']}\n{c['excerpt']}" for c in citations
    )
    system_content = f"{SYSTEM_PROMPT}\n\n=== 资料开始 ===\n{context}\n=== 资料结束 ==="
    messages: list[dict[str, str]] = [{"role": "system", "content": system_content}]
    for message in history[-_MAX_HISTORY_MESSAGES:]:
        messages.append({"role": message.role, "content": message.content})
    messages.append({"role": "user", "content": question})
    return messages


def _cited_indexes(answer: str, upper_bound: int) -> list[int]:
    """从答案里提取被真正引用的编号(前端高亮 + 质量观测用)。"""
    import re

    found = {int(m) for m in re.findall(r"\[(\d+)\]", answer)}
    return sorted(i for i in found if 1 <= i <= upper_bound)


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
