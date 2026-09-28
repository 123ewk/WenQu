"""问答流水线插件链(OPT-5,ADR 偏离登记 #11):M2 硬编码直调的阶段边界显式化。

设计约束:
- **行为不变**:默认链(retrieve → cite → fallback → recall_memory → compose → generate →
  persist)在原 `_generate` 直调之上只叠一层"记忆召回",召回为空时与旧链逐事件等价,
  SSE 事件形态回归由既有测试钉住;
- 阶段插件只依赖注入的协作者,数据通过共享的 GenerationState 按序读写,
  事件统一走 state.emit(载荷与原实现一致);
- 阶段置 state.stop 即跳过后续阶段(错误/兜底分支收尾);泵外层兜底不变。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from app.application.prompts import (
    QA_SYSTEM_PROMPT,
    memory_block_text,
    render_source_block,
    summary_block_text,
)
from app.application.service.observability import NULL_QA_TRACE, QaTrace
from app.application.service.retrieval import RetrievedChunk
from app.application.streaming import GenerationHandle
from app.core.errors import AppError
from app.core.event_log import EventLog
from app.core.model_usage import ChatTurnUsage
from app.domain.interfaces import ChatGateway
from app.domain.models import Message

if TYPE_CHECKING:
    from app.application.service.qa import _AskContext

logger = logging.getLogger("app.qa")

_MAX_HISTORY_MESSAGES = 10
_NO_RESULT_ANSWER = "知识库中没有检索到与该问题相关的内容,请调整提问或先上传相关文档。"

# 持久化协作者:QAService._persist_assistant 的形状
# (ctx, content, citations, model_id, context_used_tokens, context_window_tokens) → Message
PersistFn = Callable[
    ["_AskContext", str, list[dict], "str | None", "int | None", "int | None"],
    Message,
]
# 检索协作者:QAService._search 的形状(含 BackgroundDb / 直接检索两条路径)
SearchFn = Callable[["_AskContext"], list[RetrievedChunk]]
# 记忆召回协作者:QAService._recall_memories 的形状;返回记忆正文列表(空 = 不注入)
RecallFn = Callable[["_AskContext"], "list[str]"]
# 上下文选取协作者:QAService._compose_context 的形状;返回 (前情摘要 | None, 选中的最近消息)
ContextFn = Callable[["_AskContext"], "tuple[str | None, list[Message]]"]


@dataclass
class GenerationState:
    """一次生成的可变管道状态:阶段插件按序读写,泵持有至收尾。"""

    ctx: _AskContext
    handle: GenerationHandle
    stream_id: uuid.UUID
    log: EventLog
    turn_usage: ChatTurnUsage | None = None
    hits: list[RetrievedChunk] = field(default_factory=list)
    citations: list[dict] = field(default_factory=list)
    memories: list[str] = field(default_factory=list)  # recall_memory 阶段填充(未命中为空)
    prompt: list[dict[str, str]] | None = None
    pieces: list[str] = field(default_factory=list)
    interrupted: bool = False
    stop: bool = False  # 置真 = 错误/兜底分支已收尾,跳过后续阶段
    # OPT-50 富 span:Langfuse 观测句柄(nil-safe 单例缺省,未接入零开销)
    obs: QaTrace = NULL_QA_TRACE

    def emit(self, event_type: str, **payload: object) -> None:
        self.log.append(self.stream_id, event_type, payload)


class Stage(Protocol):
    """流水线阶段插件:有名字,吃共享状态,不返回值(分支用 state.stop 表达)。"""

    name: str

    def run(self, state: GenerationState) -> None: ...


@dataclass
class Pipeline:
    """按序执行阶段;state.stop 为真时跳过剩余阶段。"""

    stages: list[Stage]

    def run(self, state: GenerationState) -> None:
        for stage in self.stages:
            if state.stop:
                return
            stage.run(state)


class RetrieveStage:
    """检索(含查询向量化):失败以 error 事件告知(HTTP 200 已发出,状态码改不了)。"""

    name = "retrieve"

    def __init__(self, search: SearchFn) -> None:
        self._search = search

    def run(self, state: GenerationState) -> None:
        span = state.obs.span("retrieve", input={"question": state.ctx.question})
        try:
            state.hits = self._search(state.ctx)
        except AppError as exc:
            logger.warning("retrieval failed in ask: %s", exc.message)
            state.emit("error", message=exc.message, code=exc.code_str)
            span.end(output={"error": exc.message})
            state.stop = True
            return
        except Exception:  # noqa: BLE001 — 兜底,不让流静默截断
            logger.exception("retrieval crashed in ask")
            state.emit("error", message="检索失败,请稍后重试")
            span.end(output={"error": "检索失败,请稍后重试"})
            state.stop = True
            return
        span.end(
            output={
                "count": len(state.hits),
                "chunks": [_chunk_trace(h) for h in state.hits],
            }
        )

def _chunk_trace(hit: RetrievedChunk) -> dict:
    """检索命中块的 trace 摘要:内容截到 800 字(全文上送口径,防单条过大)。"""
    return {
        "chunk_id": hit.chunk_id,
        "document_id": hit.document_id,
        "kb_id": hit.kb_id,
        "filename": hit.filename,
        "score": hit.score,
        "vector_rank": hit.vector_rank,
        "fulltext_rank": hit.fulltext_rank,
        "breadcrumb": hit.meta.get("breadcrumb", []),
        "page": hit.meta.get("page"),
        "content": hit.content[:800],
    }


class CiteStage:
    """把命中块组装成带编号的引用载荷(载荷形状即前端 citations 契约)。"""

    name = "cite"

    def run(self, state: GenerationState) -> None:
        span = state.obs.span("cite")
        state.citations = [
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
            for position, hit in enumerate(state.hits, start=1)
        ]
        state.emit("citations", citations=state.citations)
        span.end(output={"citations": state.citations})


class FallbackStage:
    """无命中兜底:固定答复直接落库并收尾,不调模型。"""

    name = "fallback"

    def __init__(self, persist: PersistFn) -> None:
        self._persist = persist

    def run(self, state: GenerationState) -> None:
        if state.hits:
            return
        span = state.obs.span("fallback", metadata={"reason": "no_hits"})
        self._persist(state.ctx, _NO_RESULT_ANSWER, [], None, None, None)
        state.emit("delta", text=_NO_RESULT_ANSWER)
        # 兜底回答没打模型 → 进度条两值显式 null(与"老消息"同口径,前端不必猜键缺失)
        state.emit(
            "done",
            message_id=str(state.ctx.user_message_id),
            context_used_tokens=None,
            context_window_tokens=None,
        )
        span.end(output={"answer": _NO_RESULT_ANSWER})
        state.stop = True


class RecallMemoryStage:
    """召回注入:把用户此前确认的相关记忆捞进状态,交给 compose 拼进系统段。

    不发任何 SSE 事件(保证"无记忆"时与现状逐事件等价);召回失败绝不阻断问答,
    吞异常置空即可 —— 记忆是增益不是必需路径。
    """

    name = "recall_memory"

    def __init__(self, recall: RecallFn) -> None:
        self._recall = recall

    def run(self, state: GenerationState) -> None:
        span = state.obs.span("recall_memory")
        try:
            state.memories = self._recall(state.ctx)
        except Exception:  # noqa: BLE001 — 召回是增益,失败即回退"无记忆"
            logger.exception("memory recall failed in ask, degrade to no-memory")
            state.memories = []
        span.end(output={"count": len(state.memories), "memories": state.memories})


class ComposeStage:
    """组装提示词:系统提示 + 编号上下文 + 相关记忆 + 前情摘要 + 最近历史 + 本轮提问。"""

    name = "compose"

    def __init__(self, context: ContextFn) -> None:
        self._context = context

    def run(self, state: GenerationState) -> None:
        summary, recent = self._context(state.ctx)
        state.prompt = _build_messages(
            recent, state.ctx.question, state.citations, state.memories, summary
        )
        state.obs.span("compose").end(
            output={
                "prompt_messages": len(state.prompt),
                "prompt_chars": sum(len(m["content"]) for m in state.prompt),
                "has_summary": summary is not None,
            }
        )


class GenerateStage:
    """流式生成:逐帧 delta 事件;宽限到期(stop 置位)立即释放上游并标记中断。"""

    name = "generate"

    def __init__(self, chat: ChatGateway) -> None:
        self._chat = chat

    def run(self, state: GenerationState) -> None:
        assert state.prompt is not None  # 默认链由 compose 保证;乱序配置在此暴露
        # 全文上送口径:完整 prompt 消息进 generation input(块内容同时在 retrieve span)
        gen = state.obs.generation(
            "generate", model=state.ctx.model_id, input=state.prompt, metadata={"agent": False}
        )
        try:
            upstream = self._chat.chat_stream(state.prompt, state.ctx.model_id)
            try:
                for frame in upstream:
                    if state.handle.stop_requested.is_set():
                        state.interrupted = True
                        gen.end(output="".join(state.pieces), metadata={"interrupted": True})
                        return
                    state.pieces.append(frame)
                    state.emit("delta", text=frame)
            finally:
                if state.interrupted:
                    # 宽限耗尽仍无人回来:立即释放上游模型连接
                    # (close 在当前 yield 点触发实现内部的 with 清理)
                    upstream.close()
        except Exception:  # noqa: BLE001 — 上游失败要作为事件告知而非断流
            logger.warning("chat stream failed")
            state.emit("error", message="模型调用失败,请稍后重试")
            gen.end(metadata={"error": "模型调用失败,请稍后重试"})
            state.stop = True
            return
        gen.end(output="".join(state.pieces), metadata={"interrupted": state.interrupted})


class PersistStage:
    """落库 assistant 消息(完整答案或宽限截止的部分答案)并发 done 事件。"""

    name = "persist"

    def __init__(self, persist: PersistFn) -> None:
        self._persist = persist

    def run(self, state: GenerationState) -> None:
        content = "".join(state.pieces)
        turn_window = state.turn_usage.window() if state.turn_usage is not None else None
        used = turn_window.used_tokens if turn_window is not None else None
        window_tokens = turn_window.window_tokens if turn_window is not None else None
        if state.interrupted:
            # 与完整答案不同:部分答案空内容不落库(没有半截空消息)
            saved = (
                self._persist(
                    state.ctx, content, state.citations, state.ctx.model_id, used, window_tokens
                )
                if content.strip()
                else None
            )
            state.emit(
                "done",
                partial=True,
                message_id=str(saved.id) if saved else None,
                context_used_tokens=used,
                context_window_tokens=window_tokens,
            )
            return
        saved = self._persist(
            state.ctx, content, state.citations, state.ctx.model_id, used, window_tokens
        )
        state.emit(
            "done",
            message_id=str(saved.id),
            cited_indexes=_cited_indexes(content, len(state.citations)),
            context_used_tokens=used,
            context_window_tokens=window_tokens,
        )


# 阶段名 → 构造器(协作者注入后建工厂):链的组成由配置决定,默认序 = M2 直调行为
_STAGE_NAMES = ("retrieve", "cite", "fallback", "recall_memory", "compose", "generate", "persist")

DEFAULT_PIPELINE: tuple[str, ...] = _STAGE_NAMES


def build_stages(
    names: Sequence[str],
    *,
    search: SearchFn,
    persist: PersistFn,
    chat: ChatGateway,
    recall: RecallFn | None = None,
    context: ContextFn | None = None,
) -> list[Stage]:
    """按名字装配流水线;未知名或空链直接报错(fail-closed,启动即暴露配置错误)。

    recall 缺省为空操作(返回 []),context 缺省为旧的"最近 10 条、无摘要",
    使不含相关阶段的旧配置与单测零改动、行为逐元素一致。
    """
    if not names:
        raise ValueError("QA 流水线不能为空")
    recall_fn: RecallFn = recall if recall is not None else (lambda _ctx: [])
    context_fn: ContextFn = (
        context
        if context is not None
        else (lambda ctx: (None, list(ctx.history[-_MAX_HISTORY_MESSAGES:])))
    )
    factories: dict[str, Callable[[], Stage]] = {
        "retrieve": lambda: RetrieveStage(search),
        "cite": lambda: CiteStage(),
        "fallback": lambda: FallbackStage(persist),
        "recall_memory": lambda: RecallMemoryStage(recall_fn),
        "compose": lambda: ComposeStage(context_fn),
        "generate": lambda: GenerateStage(chat),
        "persist": lambda: PersistStage(persist),
    }
    unknown = [name for name in names if name not in factories]
    if unknown:
        raise ValueError(f"未知的 QA 流水线阶段:{', '.join(unknown)}")
    return [factories[name]() for name in names]


def _build_messages(
    history: list[Message],
    question: str,
    citations: list[dict],
    memories: list[str] | None = None,
    summary: str | None = None,
) -> list[dict[str, str]]:
    """L1 静态底座 + 记忆/摘要独立 system 段 + 最近历史 + 尾部 user(问题+资料块)。

    分层(提示词基准 01/05):system 消息只放 L1 静态底座(字节级稳定,前缀可缓存);
    记忆/摘要为低频变化的独立 system 段;检索资料是不可信数据 —— 置尾进最后一条
    user 消息,经标签转义与来源声明(位置=权威:不可信内容不进 system)。
    `history` 是调用方(compose 的 assemble_history)选好的最近若干条,本函数不再截断;
    记忆块、摘要块、资料块都为空时,输出与"无记忆、未压缩、无命中"逐字节一致。
    """
    messages: list[dict[str, str]] = [{"role": "system", "content": QA_SYSTEM_PROMPT}]
    if memories:
        messages.append({"role": "system", "content": memory_block_text(memories)})
    if summary:
        messages.append({"role": "system", "content": summary_block_text(summary)})
    for message in history:
        messages.append({"role": message.role, "content": message.content})
    source_block = render_source_block(citations)
    user_content = f"{question}\n\n{source_block}" if source_block else question
    messages.append({"role": "user", "content": user_content})
    return messages


def _cited_indexes(answer: str, upper_bound: int) -> list[int]:
    """从答案里提取被真正引用的编号(前端高亮 + 质量观测用)。"""
    import re

    found = {int(m) for m in re.findall(r"\[(\d+)\]", answer)}
    return sorted(i for i in found if 1 <= i <= upper_bound)
