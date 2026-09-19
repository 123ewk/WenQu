"""ReAct 循环单元测试(OPT-10,基准 07):假模型脚本驱动,护栏场景零真模型。"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Generator

from app.application.agent.loop import AgentRunner
from app.application.agent.tools import ToolContext
from app.application.service.retrieval import RetrievedChunk
from app.core.model_client import ToolCallRequest

USER = uuid.uuid4()
SPACE = uuid.uuid4()
KB = uuid.uuid4()


class ScriptedAgentChat:
    """脚本假模型:工具轮按脚本回放,作答轮返回固定文本;记录收到的消息供断言。"""

    def __init__(
        self,
        tool_rounds: list[tuple[list[str], list[ToolCallRequest]]],
        answer_pieces: list[str] | None = None,
    ) -> None:
        self.tool_rounds = tool_rounds
        self.answer_pieces = answer_pieces or []
        self.received: list[dict] = []
        self.closed = False

    def chat_stream_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        model_id: str | None = None,
        temperature: float = 0.7,
    ) -> Generator[str | ToolCallRequest, None, None]:
        self.received.append({"messages": messages, "tools": tools})
        script = self.tool_rounds.pop(0) if self.tool_rounds else ([], [])
        yield from script[0]
        yield from script[1]

    def chat_stream(
        self,
        messages: list[dict[str, str]],
        model_id: str | None = None,
        temperature: float = 0.7,
    ) -> Generator[str, None, None]:
        self.received.append({"messages": messages, "tools": None})
        self.closed = True
        yield from self.answer_pieces


def _ctx() -> ToolContext:
    return ToolContext(user_id=USER, space_id=SPACE, kb_ids=[KB])


def _hit(content: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(), document_id=uuid.uuid4(), kb_id=KB,
        filename="报销制度.pdf", content=content, score=0.02, meta={},
    )


def _search(hits: list[RetrievedChunk]):
    def search(ctx: ToolContext, query: str, top_k: int) -> list[RetrievedChunk]:
        return hits

    return search


def _call(name: str = "search_knowledge", args: str = '{"query": "报销上限"}') -> ToolCallRequest:
    return ToolCallRequest(id=f"call_{name}", name=name, arguments=args)


class Collector:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))

    def types(self) -> list[str]:
        return [t for t, _p in self.events]


def _run(chat, search, stop: threading.Event | None = None, max_rounds: int = 20):
    events = Collector()
    runner = AgentRunner(
        chat=chat,
        search=search,
        emit=events,
        stop_requested=stop or threading.Event(),
        max_rounds=max_rounds,
    )
    return runner.run(_ctx(), "报销上限是多少?", []), events


def test_happy_path_tool_round_then_streamed_answer() -> None:
    hit = _hit("上限为每月两千元。")
    chat = ScriptedAgentChat(
        tool_rounds=[([], [_call()])],
        answer_pieces=["报销", "上限为每月两千元。[1]"],
    )
    outcome, events = _run(chat, _search([hit]))

    assert outcome.answer == "报销上限为每月两千元。[1]"
    assert outcome.error is None and not outcome.interrupted
    # 事件顺序:tool_call → tool_result → citations → delta*
    assert events.types() == ["tool_call", "tool_result", "citations", "delta", "delta"]
    assert events.events[0][1]["args"] == {"query": "报销上限"}
    assert events.events[1][1]["ok"] is True
    assert outcome.citations[0]["index"] == 1
    # 意图/结果消息按调用 id 严格配对(函数调用协议)
    tool_round_messages = chat.received[0]["messages"]
    assistant_msg = next(m for m in tool_round_messages if m.get("tool_calls"))
    tool_msg = next(m for m in tool_round_messages if m.get("role") == "tool")
    assert assistant_msg["tool_calls"][0]["id"] == tool_msg["tool_call_id"]
    # 作答轮消息里带登记后的资料与作答指令
    answer_messages = chat.received[1]["messages"]
    assert answer_messages[-1]["role"] == "user" and "检索结束" in answer_messages[-1]["content"]
    assert "[1] 来源:报销制度.pdf" in answer_messages[-2]["content"]
    # 轨迹可落库
    assert outcome.steps[0]["tool_calls"][0]["ok"] is True


def test_round_budget_exhausted_still_answers() -> None:
    """轮数预算耗尽:作答轮兜底收尾,不挂死、不发 error。"""
    chat = ScriptedAgentChat(
        tool_rounds=[([], [_call()]), ([], [_call()]), ([], [_call()])],
        answer_pieces=["预算内作答"],
    )
    outcome, events = _run(chat, _search([_hit("内容")]), max_rounds=2)
    assert outcome.answer == "预算内作答"
    assert len(outcome.steps) == 2  # 只跑了预算内轮数
    assert events.types()[:3] == ["tool_call", "tool_result", "tool_call"]


def test_repeat_stall_forces_answer_pass() -> None:
    """连续 2 轮思考文本相同且仍要调工具 → 判定复读,第 3 轮的调用不再执行。"""
    chat = ScriptedAgentChat(
        tool_rounds=[
            (["先想一下"], [_call()]),
            (["我查一下"], [_call()]),
            (["我查一下"], [_call()]),
        ],
        answer_pieces=["作答"],
    )
    outcome, events = _run(chat, _search([_hit("内容")]), max_rounds=20)
    assert outcome.answer == "作答"
    assert len(outcome.steps) == 3
    # 第 3 轮的调用意图未执行:只有前两轮各产生一对 tool 事件
    assert events.types()[:4] == ["tool_call", "tool_result", "tool_call", "tool_result"]
    assert "tool_call" not in events.types()[4:]


def test_unknown_tool_is_non_fatal() -> None:
    """白名单外工具:回填可读错误,循环继续到下一轮作答(基准 04)。"""
    chat = ScriptedAgentChat(
        tool_rounds=[([], [_call(name="delete_everything")]), ([], [])],
        answer_pieces=["好的"],
    )
    outcome, events = _run(chat, _search([]))
    assert outcome.answer == "好的"
    assert events.events[1][1]["ok"] is False
    assert "未知工具" in events.events[1][1]["output"]


def test_malformed_arguments_reach_validation_error() -> None:
    """参数 JSON 畸形:解析兜底为空 dict → 工具参数校验回填错误,不炸循环。"""
    search_calls: list = []

    def search(ctx: ToolContext, query: str, top_k: int) -> list[RetrievedChunk]:
        search_calls.append(query)
        return []

    chat = ScriptedAgentChat(
        tool_rounds=[([], [_call(args='{"query": 报销未加引号}')]), ([], [])],
        answer_pieces=["已说明"],
    )
    outcome, events = _run(chat, search)
    assert outcome.answer == "已说明"
    assert search_calls == []  # 畸形参数没有触达检索
    assert events.events[1][1]["ok"] is False
    assert "参数校验失败" in events.events[1][1]["output"]


def test_model_failure_is_recoverable_error() -> None:
    class ExplodingChat(ScriptedAgentChat):
        def chat_stream_tools(self, messages, tools, model_id=None, temperature=0.7):
            raise RuntimeError("upstream boom")

    outcome, _events = _run(ExplodingChat([], []), _search([]))
    assert outcome.answer == ""
    assert outcome.error == "模型调用失败,请稍后重试"


def test_stop_before_run_returns_interrupted_empty() -> None:
    chat = ScriptedAgentChat(tool_rounds=[([], [_call()])], answer_pieces=["x"])
    stop = threading.Event()
    stop.set()
    outcome, events = _run(chat, _search([_hit("内容")]), stop=stop)
    assert outcome.interrupted and outcome.answer == ""
    assert events.types() == []  # 未发任何事件


def test_stop_mid_answer_pass_keeps_partial_answer() -> None:
    """停止置位发生在作答轮中途:已生成的片段保留(interrupted=true)。"""
    stop = threading.Event()

    def answer_stream(messages, model_id=None, temperature=0.7) -> Generator[str, None, None]:
        yield "部分"
        stop.set()
        yield "答案"

    chat = ScriptedAgentChat(tool_rounds=[([], [])])
    chat.chat_stream = answer_stream  # type: ignore[method-assign]
    outcome, _events = _run(chat, _search([]), stop=stop)
    assert outcome.interrupted
    assert outcome.answer == "部分"
