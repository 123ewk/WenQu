"""模型调用门面(OPT-25 重构):解析模型/供应商/密钥后按协议注册表分发。

本层只做三件事,HTTP 协议细节全部下沉到 `app.core.model_protocols`:

1. **解析**:model_id 缺省取该 kind 的默认模型;类型/启用/供应商可用性校验在
   `ModelCatalog` 完成(错误语义 ModelNotConfigured 503 不变);
2. **密钥解密**:库内 `enc:v1:` 密文经 `CredentialCipher` 就地解密,只在这一处
   出现明文,失败不外泄密钥内容;内置模型时代的环境变量解析(*_API_KEY)已下线;
3. **分发**:按 `provider.protocol` 从注册表取适配器执行调用。

EmbeddingClient 保留后台并发信号量(入库流水线限速);ChatClient 的
chat_stream/chat_stream_tools 返回 Generator 是断线清理契约的一部分,
生成器语义由适配器保持(本层原样透传,不做 list() 固化)。

token 计量(OPT-28)在本层只是**透传可选的 `UsageSink` 出参**:带收集器才计数,
不带时请求体与计量上线前完全一致;数字的采集与估算全在协议层。
"""

from __future__ import annotations

import threading
from collections.abc import Generator

import httpx

from app.core.crypto import get_credential_cipher
from app.core.model_catalog import ModelCatalog, ModelEntry, ModelNotConfigured, ProviderEntry
from app.core.model_protocols import (
    ModelCallError,
    ToolCallRequest,
    get_protocol,
    get_rerank_protocol,
)
from app.core.model_usage import UsageSink

__all__ = [
    "ChatClient",
    "EmbeddingClient",
    "ModelCallError",
    "RerankClient",
    "ToolCallRequest",
    "resolve_api_key",
]


def resolve_api_key(provider: ProviderEntry) -> str:
    """库内密文 → 明文;空密文 = 本地免密钥端点占位键。"""
    if not provider.api_key_encrypted:
        return "local-no-key"
    return get_credential_cipher().decrypt(provider.api_key_encrypted)


class EmbeddingClient:
    def __init__(
        self,
        catalog: ModelCatalog,
        background_semaphore: threading.Semaphore | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._catalog = catalog
        self._semaphore = background_semaphore
        self._transport = transport

    def embed(
        self,
        texts: list[str],
        model_id: str | None = None,
        *,
        usage: UsageSink | None = None,
    ) -> list[list[float]]:
        if not texts:
            return []
        model = (
            self._catalog.get_model(model_id)
            if model_id
            else self._catalog.default_model("embedding")
        )
        if model.kind != "embedding":
            raise ModelNotConfigured(f"模型 {model.id} 不是 embedding 类型")
        return self._dispatch(model, texts, usage)

    def _dispatch(
        self, model: ModelEntry, texts: list[str], usage: UsageSink | None
    ) -> list[list[float]]:
        provider = self._catalog.provider_of(model)
        api_key = resolve_api_key(provider)
        protocol = get_protocol(provider.protocol)
        if usage is not None:
            usage.bind(model.id, provider.key, model.context_tokens)
        return protocol.embed(
            provider,
            model,
            api_key,
            texts,
            semaphore=self._semaphore,
            transport=self._transport,
            usage=usage,
        )


class ChatClient:
    def __init__(
        self,
        catalog: ModelCatalog,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._catalog = catalog
        self._transport = transport

    def chat(
        self,
        messages: list[dict[str, str]],
        model_id: str | None = None,
        temperature: float = 0.7,
        *,
        usage: UsageSink | None = None,
    ) -> str:
        return "".join(self.chat_stream(messages, model_id, temperature, usage=usage))

    def chat_stream(
        self,
        messages: list[dict[str, str]],
        model_id: str | None = None,
        temperature: float = 0.7,
        *,
        usage: UsageSink | None = None,
    ) -> Generator[str, None, None]:
        model, provider, api_key = self._resolve_chat(model_id, usage)
        protocol = get_protocol(provider.protocol)
        return protocol.chat_stream(
            provider, model, api_key, messages, temperature, transport=self._transport, usage=usage
        )

    def chat_stream_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        model_id: str | None = None,
        temperature: float = 0.7,
        *,
        usage: UsageSink | None = None,
    ) -> Generator[str | ToolCallRequest, None, None]:
        model, provider, api_key = self._resolve_chat(model_id, usage)
        protocol = get_protocol(provider.protocol)
        return protocol.chat_stream_tools(
            provider,
            model,
            api_key,
            messages,
            tools,
            temperature,
            transport=self._transport,
            usage=usage,
        )

    def _resolve_chat(
        self, model_id: str | None, usage: UsageSink | None = None
    ) -> tuple[ModelEntry, ProviderEntry, str]:
        model = (
            self._catalog.get_model(model_id)
            if model_id
            else self._catalog.default_model("chat")
        )
        if model.kind != "chat":
            raise ModelNotConfigured(f"模型 {model.id} 不是 chat 类型")
        provider = self._catalog.provider_of(model)
        if usage is not None:
            usage.bind(model.id, provider.key, model.context_tokens)
        return model, provider, resolve_api_key(provider)


class RerankClient:
    """重排网关(OPT-27):把候选文档交给 rerank 模型按相关性重新排序。

    与另两个网关不同,它只在空间 `rerank_enabled` 为真时才被装配(见 deps),
    未启用时检索链路零开销;上游失败由调用方 fail-open 兜住,不在这里降级。
    """

    def __init__(
        self,
        catalog: ModelCatalog,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._catalog = catalog
        self._transport = transport

    def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int | None = None,
        model_id: str | None = None,
    ) -> list[tuple[int, float]]:
        if not documents:
            return []
        model = (
            self._catalog.get_model(model_id, "rerank")
            if model_id
            else self._catalog.default_model("rerank")
        )
        provider = self._catalog.provider_of(model)
        return get_rerank_protocol(provider.protocol).rerank(
            provider,
            model,
            resolve_api_key(provider),
            query,
            documents,
            top_n if top_n is not None else len(documents),
            transport=self._transport,
        )
