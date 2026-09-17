"""带引用的流式问答(M2 核心闭环)。

链路:多轮历史 → 混合检索 → 组装带编号上下文的提示词 → 流式生成 → 落库(含引用)。

引用溯源:M2 用编号方案(简洁且对模型友好)——上下文块标 [1][2]…,要求模型以同样
编号标注;生成结束后把编号映射回真实 chunk_id/文件/摘录,存进 messages.citations。
前端把 [n] 渲染为角标,点击按 citations[n-1].chunk_id 打开抽屉预览原文。
"""

from __future__ import annotations

import json
import logging
import queue
import threading
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
# SSE 心跳(OPT-2):生成间隙发注释帧,防止前置 nginx(proxy_read_timeout 60s)掐断静默流
_PING_FRAME = ": ping\n\n"


class _PingSentinel:
    """心跳标记:包装层产出它,_stream 把它翻译成注释行(避免与模型 piece 撞内容)。"""


_PING = _PingSentinel()
_QUEUE_END = object()  # 哨兵:上游产出完毕

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
        heartbeat_seconds: float = 15.0,
    ) -> None:
        self._conversations = conversations
        self._messages = messages
        self._spaces = spaces
        self._retrieval = retrieval
        self._chat = chat
        self._heartbeat_seconds = heartbeat_seconds

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

    def rename_conversation(
        self,
        user_id: uuid.UUID,
        space_id: uuid.UUID,
        conversation_id: uuid.UUID,
        title: str,
    ) -> Conversation:
        conversation = self._get_conversation(user_id, space_id, conversation_id)
        conversation.title = title.strip() or conversation.title
        return self._conversations.save(conversation)

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

        # 检索(含查询向量化)可能因模型未配置/上游失败而中断。此时 HTTP 200 与 meta
        # 事件已发出,状态码改不了,只能靠 error 事件告知 —— 否则前端只看到流突然结束。
        try:
            hits = self._retrieval.search(
                user_id, space_id, question, top_k=top_k, kb_ids=kb_ids, model_id=model_id
            )
        except AppError as exc:
            logger.warning("retrieval failed in ask: %s", exc.message)
            yield _sse({"type": "error", "message": exc.message, "code": exc.code_str})
            return
        except Exception:  # noqa: BLE001 — 兜底,不让流静默截断
            logger.exception("retrieval crashed in ask")
            yield _sse({"type": "error", "message": "检索失败,请稍后重试"})
            return
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
            for frame in _with_heartbeat(
                self._chat.chat_stream(messages, model_id), self._heartbeat_seconds
            ):
                if isinstance(frame, _PingSentinel):
                    yield _PING_FRAME
                    continue
                pieces.append(frame)
                yield _sse({"type": "delta", "text": frame})
        except GeneratorExit:
            # OPT-1:客户端断流(Starlette close 生成器)→ 已生成的部分答案落库,
            # 否则库里只剩 user 消息,用户重连后看到"问题孤零零挂着"。
            # 注意:GeneratorExit 处理中禁止 yield(会 RuntimeError),只做落库后 re-raise。
            self._save_partial_answer(conversation, space_id, citations, pieces, model_id)
            raise
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

    def _save_partial_answer(
        self,
        conversation: Conversation,
        space_id: uuid.UUID,
        citations: list[dict],
        pieces: list[str],
        model_id: str | None,
    ) -> None:
        """断流时保存已生成的部分答案(带引用,与完整答案同构);无产出时不落库。"""
        content = "".join(pieces)
        if not content.strip():
            return
        try:
            self._messages.add(
                Message(
                    conversation_id=conversation.id,
                    space_id=space_id,
                    role="assistant",
                    content=content,
                    seq=self._messages.next_seq(conversation.id),
                    citations=citations,
                    model_id=model_id,
                )
            )
            logger.info(
                "client disconnected, partial answer saved conv=%s chars=%d",
                conversation.id,
                len(content),
            )
        except Exception:  # noqa: BLE001 — 清理路径的失败只记日志,不影响断开流程
            logger.exception("failed to save partial answer conv=%s", conversation.id)

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


def _with_heartbeat(
    upstream: Iterator[str], interval_seconds: float
) -> Iterator[str | _PingSentinel]:
    """给阻塞的上游迭代包一层心跳(OPT-2)。

    上游(模型 SSE)在没有新 token 的等待期会让整条流静默,前置 nginx 默认
    `proxy_read_timeout 60s` 会掐断静默连接。本包装用泵线程把上游搬进队列,
    主循环 `get(timeout=interval)`,超时就发 `: ping` 注释帧(EventSource 客户端
    自动忽略注释行)。

    - **队列 maxsize=1**:泵线程最多比消费端超前一个 piece —— 断线时最多丢
      最后一个增量,而不是把剩余整段答案提前吸进内存后全部丢失;
    - 上游异常经队列原样 re-raise,交给调用方的 except 分支;
    - 已知限制(登记在优化台账 OPT-3):断线后泵线程会继续把上游读完才退出,
      上游模型连接是否及时释放留待事件日志方案一并验证。
    """
    if interval_seconds <= 0:
        yield from upstream
        return

    q: queue.Queue = queue.Queue(maxsize=1)

    def _pump() -> None:
        try:
            for item in upstream:
                q.put(item)  # 队列满时阻塞 → 天然背压,不提前吸干上游
            q.put(_QUEUE_END)
        except BaseException as exc:  # noqa: BLE001 — 异常也要交给消费端
            q.put(exc)

    threading.Thread(target=_pump, daemon=True, name="qa-heartbeat-pump").start()

    while True:
        try:
            item = q.get(timeout=interval_seconds)
        except queue.Empty:
            yield _PING
            continue
        if item is _QUEUE_END:
            return
        if isinstance(item, BaseException):
            raise item
        yield item


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
