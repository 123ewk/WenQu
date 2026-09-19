"""带引用的流式问答(M2 核心闭环;OPT-3 起生成与响应解耦)。

链路:多轮历史 → 混合检索 → 组装带编号上下文的提示词 → 流式生成 → 落库(含引用)。

引用溯源:M2 用编号方案(简洁且对模型友好)——上下文块标 [1][2]…,要求模型以同样
编号标注;生成结束后把编号映射回真实 chunk_id/文件/摘录,存进 messages.citations。
前端把 [n] 渲染为角标,点击按 citations[n-1].chunk_id 打开抽屉预览原文。

流式架构(OPT-3,ADR-3 落地):ask_stream 在请求上下文完成校验与会话/提问落库,
然后把"生成"搬进后台泵线程 —— 逐事件写入事件日志(core/event_log,带单调 seq),
HTTP 响应只是日志的读者。断线(响应生成器被 close)只影响读者,不终止生成:
宽限期(默认 5 秒,APP_STREAM_GRACE_SECONDS)内有人重连就继续生成到完整落库;
耗尽仍无人回来才中止 —— 释放上游连接、落库部分答案、以 done(partial=true) 收尾。
事件载荷带 seq,前端重连时按 seq 续流(续流路由随 OPT-3 交付)。
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from app.application.repository.conversations import MessageRepositoryImpl
from app.application.repository.knowledge import (
    DocumentRepositoryImpl,
    KnowledgeBaseRepositoryImpl,
)
from app.application.repository.retrieval import RetrievalRepositoryImpl
from app.application.repository.spaces import SpaceRepositoryImpl
from app.application.service.retrieval import RetrievalService, RetrievedChunk
from app.application.streaming import GenerationHandle, StreamSupervisor
from app.core.errors import AppError, ErrorCode
from app.core.event_log import EventLog, MemoryEventLog
from app.domain.enums import Role
from app.domain.interfaces import (
    ChatGateway,
    ConversationRepository,
    EmbeddingGateway,
    MessageRepository,
    SpaceRepository,
)
from app.domain.models import Conversation, Message

logger = logging.getLogger("app.qa")

_MAX_HISTORY_MESSAGES = 10
_NO_RESULT_ANSWER = "知识库中没有检索到与该问题相关的内容,请调整提问或先上传相关文档。"
# SSE 心跳(OPT-2):读流空闲期发注释帧,防止前置 nginx(proxy_read_timeout 60s)掐断静默流
_PING_FRAME = ": ping\n\n"

SYSTEM_PROMPT = (
    "你是企业知识库问答助手。规则:\n"
    "1. 只依据下面提供的资料回答问题,不得编造资料之外的事实;\n"
    "2. 引用资料时用其编号标注,如 [1]、[2];一句话综合多处资料时可标注多个编号;\n"
    "3. 资料不足以回答时,直接说明资料中没有相关信息,不要猜测;\n"
    "4. 回答保持简洁,使用与提问相同的语言。"
)


@dataclass
class _AskContext:
    """一次提问的全部输入:请求上下文算好,交给后台泵使用。"""

    conversation: Conversation
    space_id: uuid.UUID
    user_id: uuid.UUID
    question: str
    history: list[Message]
    user_message_id: uuid.UUID
    kb_ids: list[uuid.UUID] | None
    top_k: int
    model_id: str | None


class BackgroundDb:
    """泵线程的独立数据库入口。

    请求会话随响应关闭(断线时更早),而泵线程在宽限期内仍要检索与落库 ——
    必须自开会话(worker.py 同款模式);单元测试传 None,泵直接用注入的仓储。
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        embedder: EmbeddingGateway,
        min_vector_score: float,
    ) -> None:
        self._session_factory = session_factory
        self._embedder = embedder
        self._min_vector_score = min_vector_score

    def search(self, ctx: _AskContext) -> list[RetrievedChunk]:
        with self._session_factory() as db:
            retrieval = RetrievalService(
                RetrievalRepositoryImpl(db),
                KnowledgeBaseRepositoryImpl(db),
                DocumentRepositoryImpl(db),
                SpaceRepositoryImpl(db),
                self._embedder,
                min_vector_score=self._min_vector_score,
            )
            return retrieval.search(
                ctx.user_id,
                ctx.space_id,
                ctx.question,
                top_k=ctx.top_k,
                kb_ids=ctx.kb_ids,
                model_id=ctx.model_id,
            )

    def save_message(self, message: Message) -> Message:
        with self._session_factory() as db:
            messages = MessageRepositoryImpl(db)
            message.seq = messages.next_seq(message.conversation_id)
            messages.add(message)
            db.commit()
            return message


class QAService:
    def __init__(
        self,
        conversations: ConversationRepository,
        messages: MessageRepository,
        spaces: SpaceRepository,
        retrieval: RetrievalService,
        chat: ChatGateway,
        heartbeat_seconds: float = 15.0,
        event_log: EventLog | None = None,
        supervisor: StreamSupervisor | None = None,
        background_db: BackgroundDb | None = None,
        db: Session | None = None,
    ) -> None:
        self._conversations = conversations
        self._messages = messages
        self._spaces = spaces
        self._retrieval = retrieval
        self._chat = chat
        self._heartbeat_seconds = heartbeat_seconds
        self._db = db  # 请求会话:ask_stream 用它提前提交会话/提问,供泵线程可见
        # 进程级单例由 deps 注入;单测不传则各自独立(互不串话)
        self._event_log = event_log if event_log is not None else MemoryEventLog()
        self._supervisor = supervisor if supervisor is not None else StreamSupervisor()
        self._background_db = background_db

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
        """校验与会话/提问落库在请求上下文完成,然后把生成搬进后台泵,返回日志读流。

        校验必须发生在返回之前:生成器体的代码在首次迭代才跑,若校验写在里面,
        HTTP 层拿不到 400/403/404(流已开始)。会话与 user 消息也在这里落库 ——
        泵线程的生命周期可能超出请求会话(断线宽限),它只依赖 BackgroundDb
        自开的会话,不碰请求会话。
        """
        question = question.strip()
        if not question:
            raise AppError(ErrorCode.VALIDATION, "提问内容不能为空", http_status=400)
        self._require_member(space_id, user_id)
        conversation = (
            self._get_conversation(user_id, space_id, conversation_id)
            if conversation_id is not None
            else self._conversations.create(
                Conversation(space_id=space_id, user_id=user_id, title=question[:30])
            )
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
        ctx = _AskContext(
            conversation=conversation,
            space_id=space_id,
            user_id=user_id,
            question=question,
            history=history,
            user_message_id=user_message.id,
            kb_ids=kb_ids,
            top_k=top_k,
            model_id=model_id,
        )
        # 泵线程用自己的会话检索/落库,必须先提交请求事务让它看得见这批写入
        # (get_db 在响应结束才提交,而泵在响应存续期间就要读);
        # 事务里此刻只有本次写入,提前提交无副作用。
        if self._db is not None:
            self._db.commit()
        handle = self._supervisor.start(conversation.id)
        threading.Thread(
            target=self._generate, args=(handle, ctx), daemon=True, name="qa-generate"
        ).start()
        return self._reader(handle.stream_id, handle)

    def resume_stream(
        self,
        user_id: uuid.UUID,
        space_id: uuid.UUID,
        conversation_id: uuid.UUID,
        after: int,
    ) -> Iterator[str]:
        """断线续流(OPT-3):回放 after 之后的存量事件;生成仍在进行则接着实时推。

        不可续(进程重启/事件淘汰/该会话从未生成过)→ 404 STREAM_NOT_RESUMABLE,
        前端回退拉 messages。会话归属校验与 messages 路由同规则(404 防枚举)。
        """
        self._get_conversation(user_id, space_id, conversation_id)
        handle = self._supervisor.get(conversation_id)
        stream_id = (
            handle.stream_id
            if handle is not None
            else self._supervisor.last_stream_id(conversation_id)
        )
        if stream_id is None or not self._event_log.is_resumable(stream_id, after):
            raise AppError(
                ErrorCode.STREAM_NOT_RESUMABLE,
                "没有可续的生成流,请改用消息历史恢复",
                http_status=404,
            )
        return self._reader(stream_id, handle, after=after)

    # ---------------------------- 后台泵 ----------------------------

    def _generate(self, handle: GenerationHandle, ctx: _AskContext) -> None:
        """后台泵:把生成过程逐事件写入日志;宽限到期无人回来则中止并落库部分答案。"""
        cid = ctx.conversation.id
        sid = handle.stream_id  # 事件日志的键:一次生成一条流
        log = self._event_log
        try:
            log.append(sid, "meta", {"conversation_id": str(cid)})

            # 检索(含查询向量化)可能因模型未配置/上游失败而中断:HTTP 200 与 meta
            # 已发出,状态码改不了,只能靠 error 事件告知 —— 否则前端只看到流突然结束。
            try:
                hits = self._search(ctx)
            except AppError as exc:
                logger.warning("retrieval failed in ask: %s", exc.message)
                log.append(sid, "error", {"message": exc.message, "code": exc.code_str})
                return
            except Exception:  # noqa: BLE001 — 兜底,不让流静默截断
                logger.exception("retrieval crashed in ask")
                log.append(sid, "error", {"message": "检索失败,请稍后重试"})
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
            log.append(sid, "citations", {"citations": citations})

            if not hits:
                self._persist_assistant(ctx, _NO_RESULT_ANSWER, [], None)
                log.append(sid, "delta", {"text": _NO_RESULT_ANSWER})
                log.append(sid, "done", {"message_id": str(ctx.user_message_id)})
                return

            prompt = _build_messages(ctx.history, ctx.question, citations)
            pieces: list[str] = []
            interrupted = False
            try:
                upstream = self._chat.chat_stream(prompt, ctx.model_id)
                try:
                    for frame in upstream:
                        if handle.stop_requested.is_set():
                            interrupted = True
                            break
                        pieces.append(frame)
                        log.append(sid, "delta", {"text": frame})
                finally:
                    if interrupted:
                        # 宽限耗尽仍无人回来:立即释放上游模型连接
                        # (close 在当前 yield 点触发实现内部的 with 清理)
                        upstream.close()
            except Exception as exc:  # noqa: BLE001 — 上游失败要作为事件告知而非断流
                logger.warning("chat stream failed: %s", exc)
                log.append(sid, "error", {"message": "模型调用失败,请稍后重试"})
                return

            if interrupted:
                content = "".join(pieces)
                # 与完整答案不同:部分答案空内容不落库(没有半截空消息)
                saved = (
                    self._persist_assistant(ctx, content, citations, ctx.model_id)
                    if content.strip()
                    else None
                )
                log.append(
                    sid,
                    "done",
                    {"partial": True, "message_id": str(saved.id) if saved else None},
                )
                return

            answer = "".join(pieces)
            saved = self._persist_assistant(ctx, answer, citations, ctx.model_id)
            log.append(
                sid,
                "done",
                {
                    "message_id": str(saved.id),
                    "cited_indexes": _cited_indexes(answer, len(citations)),
                },
            )
        except Exception:  # noqa: BLE001 — 泵兜底:绝不让流无声卡死
            logger.exception("ask generation crashed conv=%s", cid)
            try:
                log.append(sid, "error", {"message": "生成失败,请稍后重试"})
            except Exception:  # noqa: BLE001 — 日志已被新请求作废等场景,只能记日志
                logger.exception("failed to append error event conv=%s", cid)
        finally:
            log.mark_finished(sid)
            self._supervisor.finish(cid)

    def _search(self, ctx: _AskContext) -> list[RetrievedChunk]:
        if self._background_db is not None:
            return self._background_db.search(ctx)
        return self._retrieval.search(
            ctx.user_id,
            ctx.space_id,
            ctx.question,
            top_k=ctx.top_k,
            kb_ids=ctx.kb_ids,
            model_id=ctx.model_id,
        )

    def _persist_assistant(
        self,
        ctx: _AskContext,
        content: str,
        citations: list[dict],
        model_id: str | None,
    ) -> Message:
        """落库 assistant 消息(完整答案或宽限截止的部分答案)。"""
        message = Message(
            conversation_id=ctx.conversation.id,
            space_id=ctx.space_id,
            role="assistant",
            content=content,
            citations=citations,
            model_id=model_id,
        )
        if self._background_db is not None:
            return self._background_db.save_message(message)
        message.seq = self._messages.next_seq(ctx.conversation.id)
        return self._messages.add(message)

    # ---------------------------- 日志读者 ----------------------------

    def _reader(
        self, stream_id: uuid.UUID, handle: GenerationHandle | None, *, after: int = 0
    ) -> Iterator[str]:
        """日志读者:事件序列化为 SSE(载荷补 type/seq),空闲期发心跳注释帧。

        follow 的 timeout 保证阻塞有界 —— Starlette 关闭响应生成器时,GeneratorExit
        能在 yield 点送达,finally 的 detach(宽限计时)才有机会执行。
        handle 为 None 表示生成已结束(续流回放场景),无需宽限计时。
        """
        try:
            if handle is not None:
                handle.reader_attached()
            for event in self._event_log.follow(
                stream_id, after, timeout=self._heartbeat_seconds
            ):
                if event is None:
                    yield _PING_FRAME
                else:
                    yield _sse({"type": event.type, **event.payload, "seq": event.seq})
        finally:
            if handle is not None:
                handle.reader_detached()

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
