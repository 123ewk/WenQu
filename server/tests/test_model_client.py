"""模型接入层测试(ADR-4):httpx MockTransport 假上游,密钥/网络全不真连。"""

from __future__ import annotations

import json
import threading

import httpx
import pytest

from app.core.config import Settings
from app.core.model_catalog import ModelCatalog, ModelNotConfigured
from app.core.model_client import (
    ChatClient,
    EmbeddingClient,
    ModelCallError,
    ToolCallRequest,
    resolve_api_key,
)


@pytest.fixture(autouse=True)
def _isolate_model_env(tmp_path, monkeypatch):
    """隔离真实 server/.env 与环境变量:模型层测试只认测试自己布置的密钥。"""
    monkeypatch.chdir(tmp_path)
    for name in ("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def _catalog() -> ModelCatalog:
    return ModelCatalog.load()


def _settings(**kwargs: str) -> Settings:
    return Settings(_env_file=None, **kwargs)


def _embed_response(vectors: list[list[float]], status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        json={"data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)]},
        request=httpx.Request("POST", "https://fake/embeddings"),
    )


def test_embed_batches_and_preserves_order(monkeypatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    calls: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body["input"])
        vectors = [[0.1, 0.2] for _ in body["input"]]
        return _embed_response(vectors)

    client = EmbeddingClient(_catalog(), _settings(), transport=httpx.MockTransport(handler))
    result = client.embed([str(i) for i in range(12)], "dashscope/text-embedding-v3")

    assert len(result) == 12
    assert result[0] == [0.1, 0.2]
    assert [len(batch) for batch in calls] == [10, 2]  # 每批 ≤10
    assert all(requests[0] == batch[0] for requests, batch in zip(calls, calls, strict=True))


def test_embed_uses_default_model_when_unspecified(monkeypatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    seen_models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_models.append(json.loads(request.content)["model"])
        return _embed_response([[0.0]])

    client = EmbeddingClient(_catalog(), _settings(), transport=httpx.MockTransport(handler))
    client.embed(["文本"])
    assert seen_models == ["text-embedding-v3"]  # defaults.embedding


def test_embed_missing_key_raises_model_not_configured(monkeypatch) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    transport = httpx.MockTransport(_embed_response([[0.0]]))
    client = EmbeddingClient(_catalog(), _settings(), transport=transport)
    with pytest.raises(ModelNotConfigured, match="DASHSCOPE_API_KEY"):
        client.embed(["文本"])


def test_embed_key_resolved_from_env_file(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("DASHSCOPE_API_KEY=sk-from-dotenv\n", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sk-from-dotenv"
        return _embed_response([[0.0]])

    client = EmbeddingClient(_catalog(), _settings(), transport=httpx.MockTransport(handler))
    assert client.embed(["文本"]) == [[0.0]]


def test_embed_upstream_error_raises_model_call_error(monkeypatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    client = EmbeddingClient(_catalog(), _settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(ModelCallError) as exc_info:
        client.embed(["文本"])
    assert exc_info.value.status == 429
    assert "DASHSCOPE" not in str(exc_info.value)  # 密钥绝不入消息


def test_background_semaphore_limits_concurrency(monkeypatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    active, peak = 0, 0
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        threading.Event().wait(0.01)
        with lock:
            active -= 1
        return _embed_response([[0.0]])

    semaphore = threading.Semaphore(1)  # 后台预算=1:同刻最多 1 个在途请求
    client = EmbeddingClient(
        _catalog(), _settings(), background_semaphore=semaphore,
        transport=httpx.MockTransport(handler),
    )
    threads = [
        threading.Thread(target=client.embed, args=([f"t{i}"],)) for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak == 1


def _sse_response(chunks: list[str]) -> httpx.Response:
    lines = []
    for piece in chunks:
        payload = json.dumps({"choices": [{"delta": {"content": piece}}]})
        lines.append(f"data: {payload}\n\n")
    lines.append("data: [DONE]\n\n")
    body = "".join(lines).encode()
    return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})


def test_chat_stream_yields_deltas_in_order(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["model"] == "deepseek-chat"
        return _sse_response(["你好", "，", "世界"])

    client = ChatClient(_catalog(), _settings(), transport=httpx.MockTransport(handler))
    assert list(client.chat_stream([{"role": "user", "content": "hi"}])) == ["你好", "，", "世界"]


def test_chat_concatenates_stream(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    client = ChatClient(
        _catalog(), _settings(),
        transport=httpx.MockTransport(lambda req: _sse_response(["A", "B", "C"])),
    )
    assert client.chat([{"role": "user", "content": "hi"}]) == "ABC"


def test_chat_upstream_error_raises(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = ChatClient(_catalog(), _settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(ModelCallError) as exc_info:
        list(client.chat_stream([{"role": "user", "content": "hi"}]))
    assert exc_info.value.status == 500


def test_disabled_or_unknown_model_rejected(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    chat = ChatClient(_catalog(), _settings(), transport=httpx.MockTransport(_sse_response(["x"])))
    embed_transport = httpx.MockTransport(_embed_response([[0.0]]))
    embed = EmbeddingClient(_catalog(), _settings(), transport=embed_transport)
    with pytest.raises(ModelNotConfigured):
        chat.chat([{"role": "user", "content": "hi"}], "ollama/qwen3")  # enabled: false
    with pytest.raises(ModelNotConfigured):
        chat.chat([{"role": "user", "content": "hi"}], "no-such/model")
    with pytest.raises(ModelNotConfigured):
        embed.embed(["x"], "deepseek/deepseek-chat")  # 类型不符


def test_resolve_api_key_local_provider_needs_no_key() -> None:
    catalog = _catalog()
    provider = catalog.get_provider("ollama")  # 免密钥本地供应商
    assert resolve_api_key(provider, _settings()) == "local-no-key"


def _sse_tools_response(
    text_pieces: list[str], fragments: list[dict]
) -> httpx.Response:
    lines = []
    for piece in text_pieces:
        payload = json.dumps({"choices": [{"delta": {"content": piece}}]})
        lines.append(f"data: {payload}\n\n")
    for fragment in fragments:
        payload = json.dumps({"choices": [{"delta": {"tool_calls": [fragment]}}]})
        lines.append(f"data: {payload}\n\n")
    lines.append("data: [DONE]\n\n")
    return httpx.Response(
        200, content="".join(lines).encode(), headers={"content-type": "text/event-stream"}
    )


_TOOLS = [
    {
        "type": "function",
        "function": {"name": "search_knowledge", "description": "检索", "parameters": {}},
    }
]


def test_chat_stream_tools_aggregates_fragmented_call(monkeypatch) -> None:
    """function calling 流式分片跨 chunk 聚合:文本先发,意图在流末完整给出。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    fragments = [
        {
            "index": 0,
            "id": "call_0",
            "function": {"name": "search_knowledge", "arguments": "{\"que"},
        },
        {"index": 0, "function": {"arguments": "ry\": \"报销\"}"}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["tools"] == _TOOLS  # 工具 Schema 随请求发出
        return _sse_tools_response(["先查一下。"], fragments)

    client = ChatClient(_catalog(), _settings(), transport=httpx.MockTransport(handler))
    frames = list(
        client.chat_stream_tools([{"role": "user", "content": "q"}], _TOOLS)
    )
    assert frames[0] == "先查一下。"
    call = frames[1]
    assert isinstance(call, ToolCallRequest)
    assert (call.id, call.name) == ("call_0", "search_knowledge")
    assert json.loads(call.arguments) == {"query": "报销"}


def test_chat_stream_tools_multiple_calls_in_index_order(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    fragments = [
        {"index": 1, "id": "call_b", "function": {"name": "t2", "arguments": "{}"}},
        {"index": 0, "function": {"name": "t1", "arguments": "{\"a\":1}"}},  # 无 id,按 index 兜底
    ]
    client = ChatClient(
        _catalog(), _settings(),
        transport=httpx.MockTransport(lambda req: _sse_tools_response([], fragments)),
    )
    frames = list(client.chat_stream_tools([{"role": "user", "content": "q"}], _TOOLS))
    assert [f.name for f in frames] == ["t1", "t2"]
    assert frames[0].id == "call_0" and frames[1].id == "call_b"
