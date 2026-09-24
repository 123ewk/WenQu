"""带引用的流式问答(M2 核心闭环;OPT-3 起生成与响应解耦,OPT-5 起流水线插件链化)。

链路:多轮历史 → 混合检索 → 组装带编号上下文的提示词 → 流式生成 → 落库(含引用)。
生成流程的阶段拆分见 qa_pipeline(检索/引用/兜底/组装/生成/落库六个阶段插件)。

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
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from app.application.agent.loop import AgentRunner, ToolCallingChat
from app.application.agent.tools import SearchFn, ToolContext
from app.application.repository.conversations import MessageRepositoryImpl
from app.application.repository.knowledge import (
    DocumentRepositoryImpl,
    KnowledgeBaseRepositoryImpl,
)
from app.application.repository.memories import MemoryRepositoryImpl
from app.application.repository.memory_proposals import MemoryProposalRepositoryImpl
from app.application.repository.retrieval import RetrievalRepositoryImpl
from app.application.repository.spaces import SpaceRepositoryImpl
from app.application.service.compaction import (
    CompactionService,
    CompactResult,
    assemble_history,
    read_summary,
)
from app.application.service.memory import MemoryService
from app.application.service.qa_pipeline import (
    DEFAULT_PIPELINE,
    GenerationState,
    Pipeline,
    _cited_indexes,
    build_stages,
)
from app.application.service.retrieval import RetrievalService, RetrievedChunk
from app.application.streaming import GenerationHandle, StreamSupervisor
from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.event_log import EventLog, MemoryEventLog
from app.core.model_usage import ChatTurnUsage
from app.domain.enums import Role
from app.domain.interfaces import (
    ConversationRepository,
    EmbeddingGateway,
    MessageRepository,
    RerankGateway,
    SpaceRepository,
)
from app.domain.models import Conversation, Message

logger = logging.getLogger("app.qa")

# SSE 心跳(OPT-2):读流空闲期发注释帧,防止前置 nginx(proxy_read_timeout 60s)掐断静默流
_PING_FRAME = ": ping\n\n"


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
    agent: bool = False  # OPT-10:true = ReAct 工具循环(检索成为模型可调用的工具)


def _last_context_usage(history: Sequence[Message]) -> tuple[int, int] | None:
    """取最近一条带齐 OPT-29 双快照(used+window)的 assistant 消息,作占用估算依据。"""
    for message in reversed(history):
        if (
            message.role == "assistant"
            and message.context_used_tokens is not None
            and message.context_window_tokens is not None
        ):
            return message.context_used_tokens, message.context_window_tokens
    return None


def _last_cache_usage(history: Sequence[Message]) -> tuple[int, int] | None:
    """取最近一条带缓存快照(cached+prompt 都非空)的 assistant 消息 → (cached, prompt)。

    与窗口快照独立:模型可能回了 cached 却没配窗口,或配了窗口却没回 cached。都缺则 None,
    前端据此显示"当前模型暂不支持缓存命中统计"。
    """
    for message in reversed(history):
        if (
            message.role == "assistant"
            and message.context_cached_tokens is not None
            and message.context_prompt_tokens is not None
        ):
            return message.context_cached_tokens, message.context_prompt_tokens
    return None


@dataclass
class ContextSnapshot:
    """上下文悬浮窗读数(OPT-45 P4/P-cache):进度条 + 压缩可行性 + 缓存命中率。"""

    used_tokens: int | None
    window_tokens: int | None
    compressible_messages: int
    can_compress: bool
    has_summary: bool
    cache_supported: bool
    cache_hit_rate: float | None


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
        reranker_factory: Callable[[Session, uuid.UUID], RerankGateway | None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._embedder = embedder
        self._min_vector_score = min_vector_score
        # 重排网关按"会话所属空间 + 泵线程自己的会话"现建(OPT-27):
        # 未开开关的空间返回 None,检索链路因此完全不碰重排
        self._reranker_factory = reranker_factory

    def _retrieval(self, db: Session, space_id: uuid.UUID) -> RetrievalService:
        return RetrievalService(
            RetrievalRepositoryImpl(db),
            KnowledgeBaseRepositoryImpl(db),
            DocumentRepositoryImpl(db),
            SpaceRepositoryImpl(db),
            self._embedder,
            min_vector_score=self._min_vector_score,
            reranker=None if self._reranker_factory is None
            else self._reranker_factory(db, space_id),
        )

    def search(self, ctx: _AskContext) -> list[RetrievedChunk]:
        with self._session_factory() as db:
            return self._retrieval(db, ctx.space_id).search(
                ctx.user_id,
                ctx.space_id,
                ctx.question,
                top_k=ctx.top_k,
                kb_ids=ctx.kb_ids,
                model_id=ctx.model_id,
            )

    def search_query(self, ctx: ToolContext, query: str, top_k: int) -> list[RetrievedChunk]:
        """Agent 工具检索入口(AgentRunner 的 SearchFn):查询词由模型给出,
        范围仍锁定本次提问的 user/space/kb_ids —— 模型没有扩权通道(§8.3-3)。"""
        with self._session_factory() as db:
            return self._retrieval(db, ctx.space_id).search(
                ctx.user_id,
                ctx.space_id,
                query,
                top_k=top_k,
                kb_ids=ctx.kb_ids,
            )

    def _memory_service(self, db: Session) -> MemoryService:
        return MemoryService(
            MemoryRepositoryImpl(db),
            SpaceRepositoryImpl(db),
            self._embedder,
            get_settings(),
            MemoryProposalRepositoryImpl(db),
        )

    def recall_memories(self, ctx: _AskContext) -> list[str]:
        """记忆召回(OPT-45 P2):泵线程自开会话,按 (space,user) 向量召回本人本空间记忆。

        MemoryService.recall 内部已受 APP_MEMORY_ENABLED 与相似度阈值控制,关闭或空命中
        直接返回 [] —— 与"无记忆"逐事件等价,绝不阻断问答。
        """
        with self._session_factory() as db:
            service = self._memory_service(db)
            return [
                m.content for m in service.recall(ctx.space_id, ctx.user_id, ctx.question)
            ]

    def dismissed_hashes(self, ctx: _AskContext) -> frozenset[str]:
        """本人本空间未过期的"忽略过"提议哈希(P2c):Agent 弹卡前据此跳过。"""
        with self._session_factory() as db:
            return self._memory_service(db).dismissed_hashes(ctx.space_id, ctx.user_id)

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
        chat: ToolCallingChat,
        heartbeat_seconds: float = 15.0,
        event_log: EventLog | None = None,
        supervisor: StreamSupervisor | None = None,
        background_db: BackgroundDb | None = None,
        db: Session | None = None,
        pipeline: Sequence[str] | None = None,
        agent_max_rounds: int = 20,
        turn_usage: ChatTurnUsage | None = None,
        recall: Callable[[_AskContext], list[str]] | None = None,
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
        # 记忆召回协作者(非泵线程路径/单测注入);泵线程优先走 BackgroundDb.recall_memories
        self._recall = recall
        # 流水线插件链(OPT-5):默认序 = M2 硬编码直调的行为;可按名重组
        self._pipeline = Pipeline(
            build_stages(
                pipeline if pipeline is not None else DEFAULT_PIPELINE,
                search=self._search,
                persist=self._persist_assistant,
                chat=chat,
                recall=self._recall_memories,
                context=self._compose_context,
            )
        )
        self._agent_max_rounds = agent_max_rounds  # Agent 工具循环轮数上限(OPT-10)
        # 上下文窗口用量的收集器(OPT-29),由计量外壳逐次调用喂入;单测不传则进度条双 null
        self._turn_usage = turn_usage

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

    def compress_conversation(
        self,
        user_id: uuid.UUID,
        space_id: uuid.UUID,
        conversation_id: uuid.UUID,
        model_id: str | None = None,
    ) -> CompactResult:
        """手动压缩会话历史(OPT-45 P3):滚动摘要写回 meta,原文不动。

        复用 _get_conversation 的归属校验(他人/他空间一律 404 防枚举);同步执行,
        走请求会话的仓储即可(不经泵线程)。
        """
        conversation = self._get_conversation(user_id, space_id, conversation_id)
        service = CompactionService(
            self._conversations, self._messages, self._chat.chat_stream, get_settings()
        )
        return service.compress(conversation, model_id)

    def get_context_snapshot(
        self, user_id: uuid.UUID, space_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> ContextSnapshot:
        """悬浮窗读数:占用快照(复用 OPT-29)+ 还能折几条 + 能否压缩 + 缓存命中率(P-cache)。

        used/window 取上一条带齐快照的 assistant;都没有则 null(首问/未打模型)。
        can_compress 与手动端点同门槛:开启压缩 ∧ 压缩点后候选 ≥ min_messages ∧ 有可折叠项。
        cache_hit_rate = cached/prompt(上一条回报了 cached 的 assistant);模型不支持则
        cache_supported=False、rate=None,前端显示"当前模型暂不支持缓存命中统计"。
        """
        conversation = self._get_conversation(user_id, space_id, conversation_id)
        history = self._messages.list_for_conversation(conversation.id)
        estimate = _last_context_usage(history)
        cache = _last_cache_usage(history)
        settings = get_settings()
        _, through = read_summary(conversation.meta)
        candidates = [m for m in history if m.seq > through]
        foldable = max(0, len(candidates) - settings.compact_keep_recent)
        can_compress = (
            settings.compact_enabled
            and len(candidates) >= settings.compact_min_messages
            and foldable > 0
        )
        cache_supported = cache is not None and cache[1] > 0
        cache_hit_rate = (
            round(cache[0] / cache[1], 3) if cache is not None and cache[1] > 0 else None
        )
        return ContextSnapshot(
            used_tokens=None if estimate is None else estimate[0],
            window_tokens=None if estimate is None else estimate[1],
            compressible_messages=foldable,
            can_compress=can_compress,
            has_summary=through > 0,
            cache_supported=cache_supported,
            cache_hit_rate=cache_hit_rate,
        )

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
        agent: bool = False,
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
            agent=agent,
        )
        # 泵线程用自己的会话检索/落库,必须先提交请求事务让它看得见这批写入
        # (get_db 在响应结束才提交,而泵在响应存续期间就要读);
        # 事务里此刻只有本次写入,提前提交无副作用。
        if self._db is not None:
            self._db.commit()
        # 生成前自动兜底(OPT-45 P3):上一轮上下文占用逼近窗口上限时,先同步压缩再作答,
        # 这一问略慢但绝不超限。改的是内存里的 ctx.conversation.meta,泵直接读得到。
        self._maybe_auto_compact(ctx)
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
        """后台泵:Agent 模式跑 ReAct 循环,否则跑流水线插件链;收尾固定在此兜底。"""
        cid = ctx.conversation.id
        sid = handle.stream_id  # 事件日志的键:一次生成一条流
        state = GenerationState(
            ctx=ctx,
            handle=handle,
            stream_id=sid,
            log=self._event_log,
            turn_usage=self._turn_usage,
        )
        try:
            state.emit("meta", conversation_id=str(cid))
            if ctx.agent:
                self._run_agent(handle, ctx)
            else:
                self._pipeline.run(state)
        except Exception:  # noqa: BLE001 — 泵兜底:绝不让流无声卡死
            logger.exception("ask generation crashed conv=%s", cid)
            try:
                state.emit("error", message="生成失败,请稍后重试")
            except Exception:  # noqa: BLE001 — 日志已被新请求作废等场景,只能记日志
                logger.exception("failed to append error event conv=%s", cid)
        finally:
            self._event_log.mark_finished(sid)
            self._supervisor.finish(cid)

    def _run_agent(self, handle: GenerationHandle, ctx: _AskContext) -> None:
        """Agent 模式(OPT-10):AgentRunner 发工具/正文事件,这里负责落库与 done 收尾。

        done 语义与直检路径一致:部分/空答案不落完整消息,partial 标记如实。
        """
        sid = handle.stream_id

        def emit(event_type: str, payload: dict) -> None:
            self._event_log.append(sid, event_type, payload)

        memories = self._recall_memories(ctx)
        summary, recent_history = self._compose_context(ctx)
        settings = get_settings()
        # 记忆总开关关掉时 max_proposals=0 → 循环不再弹任何提议卡片(工具仍无副作用)
        max_proposals = settings.memory_proposal_max_per_turn if settings.memory_enabled else 0
        outcome = AgentRunner(
            chat=self._chat,
            search=self._agent_search(ctx),
            emit=emit,
            stop_requested=handle.stop_requested,
            model_id=ctx.model_id,
            top_k_default=ctx.top_k,
            max_rounds=self._agent_max_rounds,
            max_proposals=max_proposals,
            dismissed_hashes=self._dismissed_hashes(ctx),
        ).run(
            ToolContext(
                user_id=ctx.user_id,
                space_id=ctx.space_id,
                kb_ids=ctx.kb_ids,
                source_conversation_id=ctx.conversation.id,
                source_message_id=ctx.user_message_id,
            ),
            ctx.question,
            recent_history,
            memories=memories,
            summary=summary,
        )
        if outcome.error is not None:
            self._event_log.append(sid, "error", {"message": outcome.error})
            return
        turn_window = self._turn_usage.window() if self._turn_usage is not None else None
        used = turn_window.used_tokens if turn_window is not None else None
        window_tokens = turn_window.window_tokens if turn_window is not None else None
        if not outcome.answer.strip():
            # 中断无正文 / 模型空答:没有可保存的内容(与直检路径的空部分答案同口径)
            self._event_log.append(
                sid,
                "done",
                {
                    "partial": True,
                    "message_id": None,
                    "context_used_tokens": used,
                    "context_window_tokens": window_tokens,
                },
            )
            return
        saved = self._persist_assistant(
            ctx,
            outcome.answer,
            outcome.citations,
            ctx.model_id,
            used,
            window_tokens,
            agent_steps=outcome.steps,
        )
        if outcome.interrupted:
            self._event_log.append(
                sid,
                "done",
                {
                    "partial": True,
                    "message_id": str(saved.id),
                    "context_used_tokens": used,
                    "context_window_tokens": window_tokens,
                },
            )
            return
        self._event_log.append(
            sid,
            "done",
            {
                "message_id": str(saved.id),
                "cited_indexes": _cited_indexes(outcome.answer, len(outcome.citations)),
                "context_used_tokens": used,
                "context_window_tokens": window_tokens,
            },
        )

    def _agent_search(self, ctx: _AskContext) -> SearchFn:
        """工具检索协作者:范围锁定本次提问,查询词来自模型参数。"""
        if self._background_db is not None:
            return self._background_db.search_query

        def search(ctx: ToolContext, query: str, top_k: int) -> list[RetrievedChunk]:
            return self._retrieval.search(
                ctx.user_id,
                ctx.space_id,
                query,
                top_k=top_k,
                kb_ids=ctx.kb_ids,
            )

        return search

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

    def _recall_memories(self, ctx: _AskContext) -> list[str]:
        """记忆召回(两条路径共用):泵线程用 BackgroundDb 自开会话,否则用注入协作者。

        未注入(单测/无记忆底座)返回 [] —— 与"无记忆"逐事件等价。召回失败绝不冒泡:
        它是增益不是必需路径,吞异常回退空记忆(直检路径另有 RecallMemoryStage 兜底)。
        """
        try:
            if self._background_db is not None:
                return self._background_db.recall_memories(ctx)
            if self._recall is not None:
                return self._recall(ctx)
        except Exception:  # noqa: BLE001 — 记忆召回失败只降级,不中断问答
            logger.exception("memory recall failed, degrade to no-memory")
        return []

    def _dismissed_hashes(self, ctx: _AskContext) -> frozenset[str]:
        """未过期的被忽略提议哈希(Agent 弹卡去重):仅泵线程路径查库,失败即回退空集。"""
        try:
            if self._background_db is not None:
                return self._background_db.dismissed_hashes(ctx)
        except Exception:  # noqa: BLE001 — 去重是增益,查不到就不跳过
            logger.exception("dismissed lookup failed, degrade to none")
        return frozenset()

    def _compose_context(self, ctx: _AskContext) -> tuple[str | None, list[Message]]:
        """上下文选取(OPT-45 P3):按会话 meta 的滚动摘要决定"摘要 + 最近原文"。

        纯读、无副作用:未压缩或压缩关闭时返回 (None, history[-10:]),与旧硬截断逐元素一致;
        已有摘要时返回 (摘要正文, 压缩点之后的最近 keep_recent 条原文)。摘要块由装配处拼入。
        """
        settings = get_settings()
        return assemble_history(
            ctx.history,
            ctx.conversation.meta,
            enabled=settings.compact_enabled,
            keep_recent=settings.compact_keep_recent,
        )

    def _maybe_auto_compact(self, ctx: _AskContext) -> None:
        """生成前同步兜底压缩(OPT-45 P3):占用比 ≥ 阈值才压,仅这一次额外模型调用。

        占用估算复用 OPT-29 落库的上一轮快照(最后一条带齐双值的 assistant 消息),
        不引第三方分词器;窗口未知(window 为 NULL)则跳过 —— 宁可不压也不误判超限。
        压缩失败绝不阻断问答:吞异常,交由后续 compose 用(可能仍偏长但可运行的)上下文。
        """
        settings = get_settings()
        if not settings.compact_enabled:
            return
        estimate = _last_context_usage(ctx.history)
        if estimate is None:
            return
        used, window = estimate
        if window <= 0 or used / window < settings.compact_trigger_ratio:
            return
        try:
            service = CompactionService(
                self._conversations, self._messages, self._chat.chat_stream, settings
            )
            service.compress(ctx.conversation, ctx.model_id)
        except Exception:  # noqa: BLE001 — 兜底压缩是增益,失败只回退不压缩的旧装配
            logger.exception("auto-compaction failed before generate, continue uncompacted")

    def _persist_assistant(
        self,
        ctx: _AskContext,
        content: str,
        citations: list[dict],
        model_id: str | None,
        context_used_tokens: int | None = None,
        context_window_tokens: int | None = None,
        agent_steps: list[dict] | None = None,
    ) -> Message:
        """落库 assistant 消息(完整答案或宽限截止的部分答案;agent 模式附工具轨迹)。

        两列窗口值是**本轮快照**(OPT-29),由调用方从计量外壳取;没打模型则留 null。
        缓存命中快照(OPT-45 P-cache)同样从外壳末次调用读,只在上游回报 cached 时写两列,
        不进 SSE —— 悬浮窗刷新时经 GET /context 读回即可。
        """
        cache = self._turn_usage.cache_snapshot() if self._turn_usage is not None else None
        message = Message(
            conversation_id=ctx.conversation.id,
            space_id=ctx.space_id,
            role="assistant",
            content=content,
            citations=citations,
            model_id=model_id,
            agent_steps=agent_steps,
            context_used_tokens=context_used_tokens,
            context_window_tokens=context_window_tokens,
            context_cached_tokens=None if cache is None else cache[0],
            context_prompt_tokens=None if cache is None else cache[1],
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


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
