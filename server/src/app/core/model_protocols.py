"""模型协议注册表(OPT-25):按 `ModelProvider.protocol` 键分发调用实现。

设计:
- **注册表形式**:实现 `ModelProtocol` 的适配器用 `@register_protocol("键名")`
  登记;新增一家不兼容 OpenAI 协议的厂商 = 新增一个适配器文件并注册,
  调用方(ChatClient/EmbeddingClient)零改动;
- **写入口 fail-closed**:管理端保存 provider 时校验 protocol 必须已在注册表中
  (`registered_protocols()`),存进库里的键永远可分发;
- 目前两个适配器:`openai_compatible`(embed/chat)与 `cohere_rerank`(重排,
  两者方法集不同,按 `get_protocol` / `get_rerank_protocol` 分别取),
  httpx 直连不引 SDK,原 model_client 的请求构造/流解析逻辑整体迁入。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.core.errors import AppError, ErrorCode
from app.core.model_catalog import ModelEntry, ProviderEntry

_EMBED_BATCH_SIZE = 10
_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)
# 重排在问答关键路径上:预算给得紧,超时就 fail-open 回落融合顺序(实施计划 F1-3)
_RERANK_TIMEOUT = httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=3.0)


class ModelCallError(AppError):
    """上游模型调用失败(超时/限流/错误响应)。message 不含密钥与完整请求体。

    502 语义:我们作为调用方,从上游拿到了失败响应。与 ModelNotConfigured(503,
    压根没配好)区分开,前端据此可分别提示"稍后重试"与"联系管理员"。
    """

    def __init__(self, provider: str, status: int | None, detail: str) -> None:
        super().__init__(
            ErrorCode.MODEL_CALL_FAILED,
            f"模型调用失败({provider}{f' HTTP {status}' if status else ''}): {detail}",
            http_status=502,
        )
        self.provider = provider
        self.status = status


@dataclass
class ToolCallRequest:
    """模型发出的一次工具调用意图(流式 fragment 聚合后的完整调用)。

    arguments 保留原始 JSON 文本:解析与畸形修复归循环层(自研运行时基准 04),
    本层只负责协议聚合,不做语义解释。
    """

    id: str
    name: str
    arguments: str


class ModelProtocol(Protocol):
    """协议适配器接口:入参已解析完(供应商/模型/明文密钥),只管发请求。"""

    def embed(
        self,
        provider: ProviderEntry,
        model: ModelEntry,
        api_key: str,
        texts: list[str],
        *,
        semaphore: threading.Semaphore | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> list[list[float]]: ...

    def chat_stream(
        self,
        provider: ProviderEntry,
        model: ModelEntry,
        api_key: str,
        messages: list[dict[str, str]],
        temperature: float,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> Generator[str, None, None]: ...

    def chat_stream_tools(
        self,
        provider: ProviderEntry,
        model: ModelEntry,
        api_key: str,
        messages: list[dict],
        tools: list[dict],
        temperature: float,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> Generator[str | ToolCallRequest, None, None]: ...


class RerankProtocol(Protocol):
    """重排适配器接口:同一注册表,但只有支持重排的协议键能取到它。

    返回 [(原始下标, 相关性分)] 按分数降序;调用方据此重排候选,不改动候选本身。
    """

    def rerank(
        self,
        provider: ProviderEntry,
        model: ModelEntry,
        api_key: str,
        query: str,
        documents: list[str],
        top_n: int,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> list[tuple[int, float]]: ...


_REGISTRY: dict[str, Callable[[], ModelProtocol]] = {}


def register_protocol(key: str) -> Callable[[type], type]:
    """登记协议适配器(存工厂而非实例:适配器无状态,实例化开销可忽略)。"""

    def deco(cls: type) -> type:
        _REGISTRY[key] = cls
        return cls

    return deco


def get_protocol(key: str) -> ModelProtocol:
    factory = _REGISTRY.get(key)
    if factory is None:
        raise AppError(
            ErrorCode.MODEL_NOT_CONFIGURED,
            f"模型协议 {key} 未注册(已注册:{', '.join(sorted(_REGISTRY)) or '无'})",
            http_status=503,
        )
    return factory()


def get_rerank_protocol(key: str) -> RerankProtocol:
    """按协议键取重排实现;注册了但不支持重排的适配器一律拒绝(键空间共用)。"""
    factory = _REGISTRY.get(key)
    adapter = None if factory is None else factory()
    if adapter is None or not hasattr(adapter, "rerank"):
        supported = sorted(
            name for name in _REGISTRY if hasattr(_REGISTRY[name](), "rerank")
        )
        raise AppError(
            ErrorCode.MODEL_NOT_CONFIGURED,
            f"模型协议 {key} 不支持重排(支持的重排协议:{', '.join(supported) or '无'})",
            http_status=503,
        )
    return adapter  # type: ignore[return-value]


def registered_protocols() -> list[str]:
    return sorted(_REGISTRY)


@register_protocol("openai_compatible")
class OpenAICompatible:
    """OpenAI 协议族:POST {base_url}/embeddings 与 /chat/completions(SSE)。"""

    def embed(
        self,
        provider: ProviderEntry,
        model: ModelEntry,
        api_key: str,
        texts: list[str],
        *,
        semaphore: threading.Semaphore | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> list[list[float]]:
        vectors: list[list[float]] = []
        with httpx.Client(
            base_url=provider.base_url, timeout=_TIMEOUT, transport=transport
        ) as client:
            for start in range(0, len(texts), _EMBED_BATCH_SIZE):
                batch = texts[start : start + _EMBED_BATCH_SIZE]
                if semaphore is not None:
                    with semaphore:
                        vectors.extend(self._embed_batch(client, model, provider, api_key, batch))
                else:
                    vectors.extend(self._embed_batch(client, model, provider, api_key, batch))
        dims = {len(v) for v in vectors}
        if len(dims) > 1:
            raise ModelCallError(provider.key, None, f"向量维度不一致: {sorted(dims)}")
        return vectors

    @staticmethod
    def _embed_batch(
        client: httpx.Client,
        model: ModelEntry,
        provider: ProviderEntry,
        api_key: str,
        batch: list[str],
    ) -> list[list[float]]:
        payload = {"model": model.model, "input": batch}
        response = client.post(
            "/embeddings", json=payload, headers={"Authorization": f"Bearer {api_key}"}
        )
        if response.status_code != 200:
            raise ModelCallError(provider.key, response.status_code, response.text[:200])
        data = response.json().get("data", [])
        if len(data) != len(batch):
            raise ModelCallError(provider.key, None, f"返回向量数 {len(data)} != 输入 {len(batch)}")
        ordered = sorted(data, key=lambda item: item.get("index", 0))
        return [item["embedding"] for item in ordered]

    def chat_stream(
        self,
        provider: ProviderEntry,
        model: ModelEntry,
        api_key: str,
        messages: list[dict[str, str]],
        temperature: float,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> Generator[str, None, None]:
        """返回 Generator 是契约的一部分:断线宽限到点时,泵线程靠 close()
        在当前 yield 点中止生成并触发 with 清理,释放上游连接。"""
        payload = {
            "model": model.model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        with httpx.Client(
            base_url=provider.base_url, timeout=_TIMEOUT, transport=transport
        ) as client:
            with client.stream(
                "POST",
                "/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
            ) as response:
                if response.status_code != 200:
                    body = response.read().decode("utf-8", errors="replace")
                    raise ModelCallError(provider.key, response.status_code, body[:200])
                for delta in _iter_sse_deltas(response.iter_lines()):
                    content = delta.get("content")
                    if content:
                        yield content

    def chat_stream_tools(
        self,
        provider: ProviderEntry,
        model: ModelEntry,
        api_key: str,
        messages: list[dict],
        tools: list[dict],
        temperature: float,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> Generator[str | ToolCallRequest, None, None]:
        """带工具的流式对话(OpenAI 兼容 function calling,OPT-10)。

        yield 语义:文本增量(str)边收边发;`delta.tool_calls` 的分片跨 chunk 聚合,
        流结束时按 index 序逐个 yield ToolCallRequest(此时流已结束,循环层据此进入
        工具执行段)。
        """
        payload = {
            "model": model.model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
            "tools": tools,
        }
        with httpx.Client(
            base_url=provider.base_url, timeout=_TIMEOUT, transport=transport
        ) as client:
            with client.stream(
                "POST",
                "/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
            ) as response:
                if response.status_code != 200:
                    body = response.read().decode("utf-8", errors="replace")
                    raise ModelCallError(provider.key, response.status_code, body[:200])
                calls: dict[int, dict] = {}
                for delta in _iter_sse_deltas(response.iter_lines()):
                    content = delta.get("content")
                    if content:
                        yield content
                    for fragment in delta.get("tool_calls") or []:
                        index = int(fragment.get("index", 0))
                        slot = calls.setdefault(index, {"id": "", "name": "", "args": []})
                        if fragment.get("id"):
                            slot["id"] = fragment["id"]
                        function = fragment.get("function") or {}
                        if function.get("name"):
                            slot["name"] = function["name"]
                        if function.get("arguments"):
                            slot["args"].append(function["arguments"])
                for index in sorted(calls):
                    slot = calls[index]
                    yield ToolCallRequest(
                        id=slot["id"] or f"call_{index}",
                        name=slot["name"],
                        arguments="".join(slot["args"]),
                    )


@register_protocol("cohere_rerank")
class CohereRerank:
    """Cohere 形态重排:POST {base_url}/rerank(OPT-27)。

    入参 `{model, query, documents, top_n}` → `results:[{index, relevance_score}]`。
    这一形状已是事实标准(DashScope gte-rerank / Jina / SiliconFlow 等均兼容),
    base_url 由用户自填,故不需要各家变体。
    """

    def rerank(
        self,
        provider: ProviderEntry,
        model: ModelEntry,
        api_key: str,
        query: str,
        documents: list[str],
        top_n: int,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> list[tuple[int, float]]:
        payload = {
            "model": model.model,
            "query": query,
            "documents": documents,
            "top_n": max(1, min(top_n, len(documents))),
        }
        with httpx.Client(
            base_url=provider.base_url, timeout=_RERANK_TIMEOUT, transport=transport
        ) as client:
            response = client.post(
                "/rerank", json=payload, headers={"Authorization": f"Bearer {api_key}"}
            )
        if response.status_code != 200:
            raise ModelCallError(provider.key, response.status_code, response.text[:200])
        results = response.json().get("results")
        if not isinstance(results, list) or not results:
            # 有候选却空返回 = 上游响应不可用(调用方 fail-open 回落融合顺序)
            raise ModelCallError(provider.key, None, "重排响应缺少 results 列表")
        ranked: list[tuple[int, float]] = []
        for item in results:
            try:
                index = int(item["index"])
                score = float(item["relevance_score"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ModelCallError(provider.key, None, f"重排结果字段异常: {exc}") from exc
            if not 0 <= index < len(documents):
                raise ModelCallError(provider.key, None, f"重排结果下标越界: {index}")
            ranked.append((index, score))
        ranked.sort(key=lambda item: item[1], reverse=True)
        return ranked


def _iter_sse_deltas(lines: Iterator[str]) -> Iterator[dict]:
    """解析 SSE chunk 流,产出每个 chunk 的 delta 对象([DONE] 终止,坏行跳过)。"""
    for line in lines:
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if data == "[DONE]":
            return
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        choices = chunk.get("choices") or []
        if not choices:
            continue
        yield choices[0].get("delta") or {}
