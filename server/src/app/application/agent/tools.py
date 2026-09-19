"""Agent 工具体系(OPT-10,自研运行时基准 03):Protocol + Pydantic 参数模型 + 显式注册表。

设计要点(与基准的对应):
- 模型生成的参数不可信:Schema 用 Pydantic `model_json_schema()` 生成,执行前
  `model_validate` 强校验;校验失败回填模型可读错误(含字段名与修正建议),不终止循环;
- ToolContext 显式携带数据范围(用户/空间/KB 列表):租户与范围谓词强制在工具内,
  模型参数里没有"范围"概念,不存在扩权通道(架构设计 §8.3-3);
- ToolResult 双通道:output 给模型(带全局引用编号,回填前由运行时再截断),
  data 给程序(citations 等结构化数据);
- 注册表为**显式 dict**(名字 → 工具),每次请求构建;白名单外工具不发 Schema。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel, Field, ValidationError

from app.application.service.retrieval import RetrievedChunk


@dataclass
class ToolContext:
    """工具执行的环境包:一切数据访问按此范围过滤,不拿全局单例。"""

    user_id: uuid.UUID
    space_id: uuid.UUID
    kb_ids: list[uuid.UUID] | None  # 本次提问的检索范围;None = 范围内全部知识库


@dataclass
class ToolResult:
    """统一执行产物:output(给模型,已按预算处理)与 data(给程序)分离。"""

    success: bool
    output: str = ""
    data: dict = field(default_factory=dict)
    error: str = ""


class Tool(Protocol):
    """工具协议:类属性 name/description + Schema + 执行;元测试强制声明 READ_ONLY。"""

    name: str
    description: str
    READ_ONLY: bool

    def json_schema(self) -> dict: ...

    def execute(self, ctx: ToolContext, args: dict) -> ToolResult: ...


class CitationLedger:
    """全局引用登记:检索命中 → 带序号的 citations 载荷(编号跨工具调用连续)。"""

    def __init__(self) -> None:
        self.citations: list[dict] = []
        self._seen: set[str] = set()

    def register(self, hits: list[RetrievedChunk]) -> list[dict]:
        registered: list[dict] = []
        for hit in hits:
            if hit.chunk_id in self._seen:
                continue  # 同一引用块跨多次调用只登记一次
            self._seen.add(hit.chunk_id)
            self.citations.append(
                {
                    "index": len(self.citations) + 1,
                    "chunk_id": hit.chunk_id,
                    "document_id": hit.document_id,
                    "kb_id": hit.kb_id,
                    "filename": hit.filename,
                    "excerpt": hit.content[:300],
                    "score": hit.score,
                    "breadcrumb": hit.meta.get("breadcrumb", []),
                    "page": hit.meta.get("page"),
                }
            )
            registered.append(self.citations[-1])
        return registered

    def render_for_model(self, registered: list[dict]) -> str:
        """把登记结果渲染成模型可读的编号资料块。"""
        lines = [
            f"[{c['index']}] 来源:{c['filename']}\n{c['excerpt']}" for c in registered
        ]
        return "\n\n".join(lines)


class SearchKnowledgeArgs(BaseModel):
    """search_knowledge 参数模型:Schema 即发给模型的工具说明,描述写在 Field 上。"""

    query: str = Field(
        min_length=1,
        max_length=200,
        description="检索查询词:具体的关键词或短句,不要把整个问题原样塞进来",
    )
    top_k: int = Field(
        default=5, ge=1, le=10, description="返回条数;默认 5,一般不需要调大"
    )


class SearchKnowledgeTool:
    """知识库检索(只读):强制走 ToolContext 的空间/KB 范围,模型不可指定范围。"""

    name = "search_knowledge"
    description = (
        "在用户的知识库中检索资料。当回答需要事实依据(制度条款、数字、流程、"
        "技术细节)时调用;寒暄、纯创作或确定无疑的常识问题不要调用。"
        "可以多次调用,换不同 query 覆盖问题的不同侧面。"
    )
    READ_ONLY = True

    def __init__(
        self,
        search: SearchFn,
        ledger: CitationLedger,
    ) -> None:
        self._search = search
        self._ledger = ledger

    def json_schema(self) -> dict:
        return SearchKnowledgeArgs.model_json_schema()

    def execute(self, ctx: ToolContext, args: dict) -> ToolResult:
        try:
            validated = SearchKnowledgeArgs.model_validate(args)
        except ValidationError as exc:
            # 参数校验失败不终止循环:回填模型可读的逐字段错误,模型下一轮自行修正
            detail = ";".join(
                f"参数 {err['loc'][0]} 错误:{err['msg']}" for err in exc.errors()
            )
            return ToolResult(
                success=False,
                output=f"参数校验失败:{detail}。请按工具说明修正参数后重试。",
                error=detail,
            )
        hits = self._search(ctx, validated.query, validated.top_k)
        if not hits:
            return ToolResult(
                success=True,
                output="知识库中没有检索到与该查询相关的内容。可换关键词重试,"
                "或基于已有资料如实回答。",
                data={"citations": []},
            )
        registered = self._ledger.register(hits)
        return ToolResult(
            success=True,
            output=self._ledger.render_for_model(registered),
            data={"citations": registered},
        )


class SearchFn(Protocol):
    """检索协作者:由问答层注入(实现强制空间/KB 范围与租户谓词)。"""

    def __call__(
        self, ctx: ToolContext, query: str, top_k: int
    ) -> list[RetrievedChunk]: ...


def build_tools(search: SearchFn, ledger: CitationLedger) -> dict[str, Tool]:
    """显式注册表:每次请求构建;新增工具在此加一行并声明 READ_ONLY(元测试钉住)。"""
    return {"search_knowledge": SearchKnowledgeTool(search, ledger)}
