"""问答流水线插件链(OPT-5,ADR 偏离登记 #11):M2 硬编码直调的阶段边界显式化。

设计约束:
- **行为不变**:默认链(retrieve → cite → fallback → compose → generate → persist)
  与原 `_generate` 直调逐事件等价,SSE 事件形态回归由既有测试钉住;
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

from app.application.service.retrieval import RetrievedChunk
from app.application.streaming import GenerationHandle
from app.core.errors import AppError
from app.core.event_log import EventLog
from app.domain.interfaces import ChatGateway
from app.domain.models import Message

if TYPE_CHECKING:
    from app.application.service.qa import _AskContext

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

# 持久化协作者:QAService._persist_assistant 的形状(ctx, content, citations, model_id) → Message
PersistFn = Callable[["_AskContext", str, list[dict], "str | None"], Message]
# 检索协作者:QAService._search 的形状(含 BackgroundDb / 直接检索两条路径)
SearchFn = Callable[["_AskContext"], list[RetrievedChunk]]


@dataclass
class GenerationState:
    """一次生成的可变管道状态:阶段插件按序读写,泵持有至收尾。"""

    ctx: _AskContext
    handle: GenerationHandle
    stream_id: uuid.UUID
    log: EventLog
    hits: list[RetrievedChunk] = field(default_factory=list)
    citations: list[dict] = field(default_factory=list)
    prompt: list[dict[str, str]] | None = None
    pieces: list[str] = field(default_factory=list)
    interrupted: bool = False
    stop: bool = False  # 置真 = 错误/兜底分支已收尾,跳过后续阶段

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
        try:
            state.hits = self._search(state.ctx)
        except AppError as exc:
            logger.warning("retrieval failed in ask: %s", exc.message)
            state.emit("error", message=exc.message, code=exc.code_str)
            state.stop = True
        except Exception:  # noqa: BLE001 — 兜底,不让流静默截断
            logger.exception("retrieval crashed in ask")
            state.emit("error", message="检索失败,请稍后重试")
            state.stop = True


class CiteStage:
    """把命中块组装成带编号的引用载荷(载荷形状即前端 citations 契约)。"""

    name = "cite"

    def run(self, state: GenerationState) -> None:
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


class FallbackStage:
    """无命中兜底:固定答复直接落库并收尾,不调模型。"""

    name = "fallback"

    def __init__(self, persist: PersistFn) -> None:
        self._persist = persist

    def run(self, state: GenerationState) -> None:
        if state.hits:
            return
        self._persist(state.ctx, _NO_RESULT_ANSWER, [], None)
        state.emit("delta", text=_NO_RESULT_ANSWER)
        state.emit("done", message_id=str(state.ctx.user_message_id))
        state.stop = True


class ComposeStage:
    """组装提示词:系统提示 + 编号上下文 + 最近历史 + 本轮提问。"""

    name = "compose"

    def run(self, state: GenerationState) -> None:
        state.prompt = _build_messages(state.ctx.history, state.ctx.question, state.citations)


class GenerateStage:
    """流式生成:逐帧 delta 事件;宽限到期(stop 置位)立即释放上游并标记中断。"""

    name = "generate"

    def __init__(self, chat: ChatGateway) -> None:
        self._chat = chat

    def run(self, state: GenerationState) -> None:
        assert state.prompt is not None  # 默认链由 compose 保证;乱序配置在此暴露
        try:
            upstream = self._chat.chat_stream(state.prompt, state.ctx.model_id)
            try:
                for frame in upstream:
                    if state.handle.stop_requested.is_set():
                        state.interrupted = True
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
            state.stop = True


class PersistStage:
    """落库 assistant 消息(完整答案或宽限截止的部分答案)并发 done 事件。"""

    name = "persist"

    def __init__(self, persist: PersistFn) -> None:
        self._persist = persist

    def run(self, state: GenerationState) -> None:
        content = "".join(state.pieces)
        if state.interrupted:
            # 与完整答案不同:部分答案空内容不落库(没有半截空消息)
            saved = (
                self._persist(state.ctx, content, state.citations, state.ctx.model_id)
                if content.strip()
                else None
            )
            state.emit(
                "done",
                partial=True,
                message_id=str(saved.id) if saved else None,
            )
            return
        saved = self._persist(state.ctx, content, state.citations, state.ctx.model_id)
        state.emit(
            "done",
            message_id=str(saved.id),
            cited_indexes=_cited_indexes(content, len(state.citations)),
        )


# 阶段名 → 构造器(协作者注入后建工厂):链的组成由配置决定,默认序 = M2 直调行为
_STAGE_NAMES = ("retrieve", "cite", "fallback", "compose", "generate", "persist")

DEFAULT_PIPELINE: tuple[str, ...] = _STAGE_NAMES


def build_stages(
    names: Sequence[str], *, search: SearchFn, persist: PersistFn, chat: ChatGateway
) -> list[Stage]:
    """按名字装配流水线;未知名或空链直接报错(fail-closed,启动即暴露配置错误)。"""
    if not names:
        raise ValueError("QA 流水线不能为空")
    factories: dict[str, Callable[[], Stage]] = {
        "retrieve": lambda: RetrieveStage(search),
        "cite": lambda: CiteStage(),
        "fallback": lambda: FallbackStage(persist),
        "compose": lambda: ComposeStage(),
        "generate": lambda: GenerateStage(chat),
        "persist": lambda: PersistStage(persist),
    }
    unknown = [name for name in names if name not in factories]
    if unknown:
        raise ValueError(f"未知的 QA 流水线阶段:{', '.join(unknown)}")
    return [factories[name]() for name in names]


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
