"""模型协议注册表(OPT-25):按 `ModelProvider.protocol` 键分发调用实现。

设计:
- **注册表形式**:实现 `ModelProtocol` 的适配器用 `@register_protocol("键名")`
  登记;新增一家不兼容 OpenAI 协议的厂商 = 新增一个适配器文件并注册,
  调用方(ChatClient/EmbeddingClient)零改动;
- **写入口 fail-closed**:管理端保存 provider 时校验 protocol 必须已在注册表中
  (`registered_protocols()`),存进库里的键永远可分发;
- 目前三个适配器:`openai_compatible`(embed/chat)、`cohere_rerank` 与
  `dashscope_rerank`(两种重排线格式,方法同名),按 `get_protocol` /
  `get_rerank_protocol` 分别取;httpx 直连不引 SDK,原 model_client 的
  请求构造/流解析逻辑整体迁入;
- token 用量(OPT-28)在本层解析:调用方传 `UsageSink` 出参,适配器负责"上游回传
  照抄 / 没回传按本地口径估算",重排链路不计量。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol, TypeVar

import httpx

from app.core.errors import AppError, ErrorCode
from app.core.model_catalog import ModelEntry, ProviderEntry
from app.core.model_usage import UsageSink, usage_prompt_text

_EMBED_BATCH_SIZE = 10
_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)
# 重排在问答关键路径上:预算给得紧,超时就 fail-open 回落融合顺序(实施计划 F1-3)
_RERANK_TIMEOUT = httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=3.0)
# 厂商拒收 `stream_options` 时的状态码:去掉这个可选字段重试一次(OPT-28 计量降级)
_STREAM_OPTION_REJECTED = (400, 422)


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
    """协议适配器接口:入参已解析完(供应商/模型/明文密钥),只管发请求。

    `usage` 是可选的用量出参(OPT-28):传进来就在请求收尾时写入 token 计数,不传零开销。
    之所以走出参而不是返回值 —— 流式方法的返回通道是 Generator,那是断线清理契约的一部分。
    """

    def embed(
        self,
        provider: ProviderEntry,
        model: ModelEntry,
        api_key: str,
        texts: list[str],
        *,
        semaphore: threading.Semaphore | None = None,
        transport: httpx.BaseTransport | None = None,
        usage: UsageSink | None = None,
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
        usage: UsageSink | None = None,
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
        usage: UsageSink | None = None,
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
        raise AppError(
            ErrorCode.MODEL_NOT_CONFIGURED,
            f"模型协议 {key} 不支持重排(支持的重排协议:"
            f"{', '.join(registered_rerank_protocols()) or '无'})",
            http_status=503,
        )
    return adapter  # type: ignore[return-value]


def registered_rerank_protocols() -> list[str]:
    """支持重排的协议键(类型表与 kind 匹配判定都取它,别再各处抄一份清单)。"""
    return sorted(name for name in _REGISTRY if hasattr(_REGISTRY[name](), "rerank"))


def registered_protocols() -> list[str]:
    return sorted(_REGISTRY)


_T = TypeVar("_T")


def _metered_stream(
    open_stream: Callable[[bool], Generator[_T, None, None]],
    usage: UsageSink | None,
) -> Generator[_T, None, None]:
    """给流式调用套上用量采集(OPT-28):`open_stream(include_usage)` 负责发请求。

    - 没有收集器时连 `stream_options` 都不带,请求体与计量上线前逐字节相同;
    - 厂商不认这个可选字段(400/422)时**去掉它重试一次**:计量不能把问答弄挂。
      重试只可能发生在第一个增量吐出去之前 —— 协议层唯一的 ModelCallError 抛点就是
      状态码检查,在流式循环之前,所以不存在"说两遍答案"的窗口;
    - 上游没回 usage 就由内层生成器在收尾时按本地口径估算(见 `UsageSink`)。

    用 `yield from` 而不是显式循环:close() 必须原样透传到内层生成器(断线清理契约)。
    """
    if usage is None:
        yield from open_stream(False)
        return
    try:
        yield from open_stream(True)
    except ModelCallError as exc:
        if exc.status not in _STREAM_OPTION_REJECTED:
            raise
        yield from open_stream(False)


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
        usage: UsageSink | None = None,
    ) -> list[list[float]]:
        vectors: list[list[float]] = []
        with httpx.Client(
            base_url=provider.base_url, timeout=_TIMEOUT, transport=transport
        ) as client:
            for start in range(0, len(texts), _EMBED_BATCH_SIZE):
                batch = texts[start : start + _EMBED_BATCH_SIZE]
                if semaphore is not None:
                    with semaphore:
                        vectors.extend(
                            self._embed_batch(client, model, provider, api_key, batch, usage)
                        )
                else:
                    vectors.extend(
                        self._embed_batch(client, model, provider, api_key, batch, usage)
                    )
        dims = {len(v) for v in vectors}
        if len(dims) > 1:
            raise ModelCallError(provider.key, None, f"向量维度不一致: {sorted(dims)}")
        if usage is not None:
            # 批次数不定,估算按整次调用的输入文本一次算完(未认领上游口径时才生效)
            usage.estimate_from("\n".join(texts))
        return vectors

    @staticmethod
    def _embed_batch(
        client: httpx.Client,
        model: ModelEntry,
        provider: ProviderEntry,
        api_key: str,
        batch: list[str],
        usage: UsageSink | None = None,
    ) -> list[list[float]]:
        payload = {"model": model.model, "input": batch}
        response = client.post(
            "/embeddings", json=payload, headers={"Authorization": f"Bearer {api_key}"}
        )
        if response.status_code != 200:
            raise ModelCallError(provider.key, response.status_code, response.text[:200])
        body = response.json()
        if usage is not None:
            usage.add_upstream(body.get("usage"))
        data = body.get("data", [])
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
        usage: UsageSink | None = None,
    ) -> Generator[str, None, None]:
        """返回 Generator 是契约的一部分:断线宽限到点时,泵线程靠 close()
        在当前 yield 点中止生成并触发 with 清理,释放上游连接。"""
        return _metered_stream(
            lambda include_usage: self._chat_text_stream(
                provider,
                api_key,
                self._chat_payload(model, messages, temperature, None, include_usage),
                transport,
                usage,
            ),
            usage,
        )

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
        usage: UsageSink | None = None,
    ) -> Generator[str | ToolCallRequest, None, None]:
        """带工具的流式对话(OpenAI 兼容 function calling,OPT-10)。

        yield 语义:文本增量(str)边收边发;`delta.tool_calls` 的分片跨 chunk 聚合,
        流结束时按 index 序逐个 yield ToolCallRequest(此时流已结束,循环层据此进入
        工具执行段)。
        """
        return _metered_stream(
            lambda include_usage: self._chat_tools_stream(
                provider,
                api_key,
                self._chat_payload(model, messages, temperature, tools, include_usage),
                transport,
                usage,
            ),
            usage,
        )

    @staticmethod
    def _chat_payload(
        model: ModelEntry,
        messages: list[dict],
        temperature: float,
        tools: list[dict] | None,
        include_usage: bool,
    ) -> dict:
        payload: dict = {
            "model": model.model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        if tools is not None:
            payload["tools"] = tools
        if include_usage:
            # OpenAI 默认不在流里回 usage,必须显式要;不认这字段的厂商由 `_metered_stream` 降级
            payload["stream_options"] = {"include_usage": True}
        return payload

    @contextmanager
    def _chat_stream(
        self,
        provider: ProviderEntry,
        api_key: str,
        payload: dict,
        transport: httpx.BaseTransport | None,
        usage: UsageSink | None,
    ) -> Iterator[Iterator[dict]]:
        """打开 SSE 流并交出 delta 迭代器:非 200 在进循环前抛错(重试的安全前提)。"""
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
                yield _iter_sse_deltas(response.iter_lines(), usage)

    def _chat_text_stream(
        self,
        provider: ProviderEntry,
        api_key: str,
        payload: dict,
        transport: httpx.BaseTransport | None,
        usage: UsageSink | None,
    ) -> Generator[str, None, None]:
        pieces: list[str] = []
        try:
            with self._chat_stream(provider, api_key, payload, transport, usage) as deltas:
                for delta in deltas:
                    content = delta.get("content")
                    if content:
                        pieces.append(content)
                        yield content
        finally:
            self._finish_usage(usage, payload, pieces)

    def _chat_tools_stream(
        self,
        provider: ProviderEntry,
        api_key: str,
        payload: dict,
        transport: httpx.BaseTransport | None,
        usage: UsageSink | None,
    ) -> Generator[str | ToolCallRequest, None, None]:
        calls: dict[int, dict] = {}
        pieces: list[str] = []
        try:
            with self._chat_stream(provider, api_key, payload, transport, usage) as deltas:
                for delta in deltas:
                    content = delta.get("content")
                    if content:
                        pieces.append(content)
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
                arguments = "".join(slot["args"])
                # 工具调用的名字与参数同样是模型吐出的 token,估算时按产出计入
                pieces.append(slot["name"] + arguments)
                yield ToolCallRequest(
                    id=slot["id"] or f"call_{index}",
                    name=slot["name"],
                    arguments=arguments,
                )
        finally:
            self._finish_usage(usage, payload, pieces)

    @staticmethod
    def _finish_usage(usage: UsageSink | None, payload: dict, pieces: list[str]) -> None:
        """流收尾(含断线 close)时补估算:上游回了 usage 就什么都不做。"""
        if usage is not None:
            usage.estimate_from(usage_prompt_text(payload["messages"]), "".join(pieces))


@register_protocol("cohere_rerank")
class CohereRerank:
    """Cohere 形态重排:POST {base_url}/rerank(OPT-27)。

    入参 `{model, query, documents, top_n}` → 根级 `results:[{index, relevance_score}]`。
    Jina / SiliconFlow / Cohere 自家端点兼容这一形状;
    **百炼(DashScope)不兼容**,它走 `dashscope_rerank`(见下)。
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
            "top_n": _clamp_top_n(top_n, documents),
        }
        with httpx.Client(
            base_url=provider.base_url, timeout=_RERANK_TIMEOUT, transport=transport
        ) as client:
            response = client.post(
                "/rerank", json=payload, headers={"Authorization": f"Bearer {api_key}"}
            )
        results = _rerank_body(provider, response).get("results")
        return _ranked_pairs(results, provider, documents)


# 百炼原生重排的固定服务路径:base_url 只填到站点根,路径由适配器钉死
_DASHSCOPE_RERANK_PATH = "/api/v1/services/rerank/text-rerank/text-rerank"


@register_protocol("dashscope_rerank")
class DashScopeRerank:
    """百炼(DashScope)原生重排:POST {base_url}/api/v1/services/rerank/text-rerank/text-rerank。

    与 cohere 形态**两处不同名**,只能单独一个适配器(改 base_url 复用不了):
    请求把 query/documents 包进 `input`、把 top_n 包进 `parameters`;
    响应把结果放在 `output.results`。适用 gte-rerank-v2 一类原生端点,
    上游回的 usage.total_tokens 按既有口径**不计量**(重排不计费)。
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
            "input": {"query": query, "documents": documents},
            "parameters": {"top_n": _clamp_top_n(top_n, documents), "return_documents": False},
        }
        with httpx.Client(
            base_url=provider.base_url, timeout=_RERANK_TIMEOUT, transport=transport
        ) as client:
            response = client.post(
                _DASHSCOPE_RERANK_PATH,
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
            )
        output = _rerank_body(provider, response).get("output")
        results = None if not isinstance(output, dict) else output.get("results")
        return _ranked_pairs(results, provider, documents)


def _clamp_top_n(top_n: int, documents: list[str]) -> int:
    return max(1, min(top_n, len(documents)))


def _rerank_body(provider: ProviderEntry, response: httpx.Response) -> dict:
    """重排响应的共用前置校验:非 200 一律按调用失败,200 必须是 JSON 对象。

    错误消息只带上游响应体前 200 字符(厂商回的内容,不含请求与密钥)。
    """
    if response.status_code != 200:
        raise ModelCallError(provider.key, response.status_code, response.text[:200])
    try:
        body = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ModelCallError(provider.key, None, f"重排响应非 JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise ModelCallError(provider.key, None, "重排响应不是 JSON 对象")
    return body


def _ranked_pairs(
    results: object, provider: ProviderEntry, documents: list[str]
) -> list[tuple[int, float]]:
    """`[{index, relevance_score}]` → 按分数降序的 (原始下标, 分数);两形态共用。"""
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


def _iter_sse_deltas(
    lines: Iterator[str], usage: UsageSink | None = None
) -> Iterator[dict]:
    """解析 SSE chunk 流,产出每个 chunk 的 delta 对象([DONE] 终止,坏行跳过)。

    带 `usage` 时顺手收下 chunk 里的用量:OpenAI 在 `include_usage` 下把 usage 放在
    **choices 为空**的收尾 chunk 里,按"无 choices 即跳过"的老写法会正好丢掉它。
    """
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
        if usage is not None:
            usage.add_upstream(chunk.get("usage"))
        choices = chunk.get("choices") or []
        if not choices:
            continue
        yield choices[0].get("delta") or {}
