"""Agent 工具体系单元测试(OPT-10,基准 07):Schema 生成、参数校验回填、范围强制、引用登记。"""

from __future__ import annotations

import uuid

from app.application.agent.tools import (
    CitationLedger,
    SearchKnowledgeTool,
    ToolContext,
    ToolResult,
    build_tools,
)
from app.application.service.retrieval import RetrievedChunk

USER = uuid.uuid4()
SPACE = uuid.uuid4()
KB = uuid.uuid4()


def _ctx() -> ToolContext:
    return ToolContext(user_id=USER, space_id=SPACE, kb_ids=[KB])


def _hit(content: str, chunk_id: str | None = None) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id or uuid.uuid4(),
        document_id=uuid.uuid4(),
        kb_id=KB,
        filename="报销制度.pdf",
        content=content,
        score=0.016,
        meta={"breadcrumb": ["报销制度"], "page": 1},
    )


class RecordingSearch:
    """检索替身:记录收到的范围与查询,返回预置命中。"""

    def __init__(self, hits: list[RetrievedChunk] | None = None) -> None:
        self.hits = hits or []
        self.calls: list[tuple[ToolContext, str, int]] = []

    def __call__(self, ctx: ToolContext, query: str, top_k: int) -> list[RetrievedChunk]:
        self.calls.append((ctx, query, top_k))
        return self.hits


def test_schema_is_pydantic_generated() -> None:
    tool = SearchKnowledgeTool(RecordingSearch(), CitationLedger())
    schema = tool.json_schema()
    assert schema["type"] == "object"
    assert schema["properties"]["query"]["type"] == "string"
    assert schema["properties"]["top_k"]["maximum"] == 10
    assert "query" in schema["required"]


def test_execute_validates_args_and_returns_numbered_output() -> None:
    search = RecordingSearch([_hit("上限为每月两千元。")])
    ledger = CitationLedger()
    tool = SearchKnowledgeTool(search, ledger)

    result = tool.execute(_ctx(), {"query": "报销上限", "top_k": 3})

    assert result.success
    assert "[1] 来源:报销制度.pdf" in result.output
    assert "上限为每月两千元" in result.output
    assert result.data["citations"][0]["index"] == 1
    # 范围来自 ToolContext,不在模型参数里(§8.3-3:不存在扩权通道)
    assert search.calls[0][0].kb_ids == [KB]


def test_invalid_args_return_model_readable_error() -> None:
    tool = SearchKnowledgeTool(RecordingSearch(), CitationLedger())
    result = tool.execute(_ctx(), {"top_k": 99})  # 缺 query 且 top_k 越界
    assert not result.success
    assert "query" in result.output  # 逐字段错误说明,模型可自行修正
    assert "top_k" in result.output


def test_no_hits_returns_guidance_not_error() -> None:
    tool = SearchKnowledgeTool(RecordingSearch([]), CitationLedger())
    result = tool.execute(_ctx(), {"query": "不存在的主题"})
    assert result.success
    assert "没有检索到" in result.output
    assert result.data["citations"] == []


def test_ledger_numbers_are_global_across_calls() -> None:
    ledger = CitationLedger()

    first = ledger.register([_hit("第一块。", chunk_id="c1"), _hit("第二块。", chunk_id="c2")])
    second = ledger.register([_hit("第三块。", chunk_id="c3"), _hit("重复块。", chunk_id="c1")])

    assert [c["index"] for c in first] == [1, 2]
    assert [c["index"] for c in second] == [3]  # c1 已登记过,不重复编号
    assert len(ledger.citations) == 3


def test_registry_builds_named_tools_with_declarations() -> None:
    tools = build_tools(RecordingSearch(), CitationLedger())
    assert set(tools) == {"search_knowledge"}
    # 元测试契约:每个注册工具必须显式声明只读性(基准 07,防新增工具漏声明)
    assert all(isinstance(tool.READ_ONLY, bool) for tool in tools.values())


def test_tool_result_shape_is_stable() -> None:
    result = ToolResult(success=True, output="x", data={"citations": []})
    assert set(result.data) == {"citations"}
    assert result.error == ""
