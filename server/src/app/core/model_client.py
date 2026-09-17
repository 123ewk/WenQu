"""OpenAI 兼容模型客户端(ADR-4):httpx 直连,不引入 SDK;密钥解析 env > .env。

- EmbeddingClient:批量向量化(每批 ≤10 条),可选并发信号量供后台流水线限速;
- ChatClient:非流式与 SSE 流式对话;[DONE] 终止,增量按 OpenAI chunk 协议解析。
调用时才解析密钥(懒 fail-closed):缺配置抛 ModelNotConfigured,网络/上游异常抛
ModelCallError,两者都不带出密钥。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path

import httpx

from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.core.model_catalog import ModelCatalog, ModelEntry, ModelNotConfigured, ProviderEntry

_EMBED_BATCH_SIZE = 10
_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)


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


def resolve_api_key(provider: ProviderEntry, settings: Settings) -> str:
    """密钥解析:环境变量 > .env 文件(pydantic-settings 同源顺序)。"""
    if not provider.api_key_env:
        return "local-no-key"  # 本地 provider(ollama/vllm)无密钥
    import os

    value = os.environ.get(provider.api_key_env, "")
    if not value:
        from dotenv import dotenv_values

        env_path = Path(".env")
        if env_path.exists():
            value = dotenv_values(env_path).get(provider.api_key_env) or ""
    if not value:
        raise ModelNotConfigured(
            f"{provider.api_key_env} 未配置(环境变量或 server/.env 任一处)"
        )
    return value


class EmbeddingClient:
    def __init__(
        self,
        catalog: ModelCatalog,
        settings: Settings,
        background_semaphore: threading.Semaphore | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._catalog = catalog
        self._settings = settings
        self._semaphore = background_semaphore
        self._transport = transport

    def embed(self, texts: list[str], model_id: str | None = None) -> list[list[float]]:
        if not texts:
            return []
        model = (
            self._catalog.get_model(model_id)
            if model_id
            else self._catalog.default_model("embedding")
        )
        if model.kind != "embedding":
            raise ModelNotConfigured(f"模型 {model.id} 不是 embedding 类型")
        provider = self._catalog.provider_of(model)
        api_key = resolve_api_key(provider, self._settings)

        vectors: list[list[float]] = []
        with httpx.Client(
            base_url=provider.base_url, timeout=_TIMEOUT, transport=self._transport
        ) as client:
            for start in range(0, len(texts), _EMBED_BATCH_SIZE):
                batch = texts[start : start + _EMBED_BATCH_SIZE]
                vectors.extend(self._embed_batch(client, model, provider, api_key, batch))
        dims = {len(v) for v in vectors}
        if len(dims) > 1:
            raise ModelCallError(provider.key, None, f"向量维度不一致: {sorted(dims)}")
        return vectors

    def _embed_batch(
        self,
        client: httpx.Client,
        model: ModelEntry,
        provider: ProviderEntry,
        api_key: str,
        batch: list[str],
    ) -> list[list[float]]:
        payload = {"model": model.model, "input": batch}
        if self._semaphore is not None:
            with self._semaphore:
                response = client.post(
                    "/embeddings", json=payload, headers={"Authorization": f"Bearer {api_key}"}
                )
        else:
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


class ChatClient:
    def __init__(
        self,
        catalog: ModelCatalog,
        settings: Settings,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._catalog = catalog
        self._settings = settings
        self._transport = transport

    def chat(
        self,
        messages: list[dict[str, str]],
        model_id: str | None = None,
        temperature: float = 0.7,
    ) -> str:
        return "".join(self.chat_stream(messages, model_id, temperature))

    def chat_stream(
        self,
        messages: list[dict[str, str]],
        model_id: str | None = None,
        temperature: float = 0.7,
    ) -> Iterator[str]:
        model = (
            self._catalog.get_model(model_id)
            if model_id
            else self._catalog.default_model("chat")
        )
        if model.kind != "chat":
            raise ModelNotConfigured(f"模型 {model.id} 不是 chat 类型")
        provider = self._catalog.provider_of(model)
        api_key = resolve_api_key(provider, self._settings)
        payload = {
            "model": model.model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        with httpx.Client(
            base_url=provider.base_url, timeout=_TIMEOUT, transport=self._transport
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
                yield from _iter_sse_deltas(response.iter_lines())


def _iter_sse_deltas(lines: Iterator[str]) -> Iterator[str]:
    for line in lines:
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            return
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        content = delta.get("content")
        if content:
            yield content
