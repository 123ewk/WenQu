"""模型接入层测试(ADR-4 / OPT-25):httpx MockTransport 假上游,密钥/网络全不真连。

目录不再来自 YAML:测试直接用 ProviderEntry/ModelEntry 组装 ModelCatalog 快照,
密钥走库内 enc:v1: 密文 + CredentialCipher 解密(主密钥用测试专用值)。
"""

from __future__ import annotations

import json
import threading
from dataclasses import replace

import httpx
import pytest

from app.core.crypto import CredentialCipher
from app.core.errors import AppError, ErrorCode
from app.core.model_catalog import (
    ModelCatalog,
    ModelEntry,
    ModelNotConfigured,
    ProviderEntry,
)
from app.core.model_client import (
    ChatClient,
    EmbeddingClient,
    ModelCallError,
    RerankClient,
    ToolCallRequest,
    resolve_api_key,
)
from app.core.model_protocols import get_protocol, get_rerank_protocol, registered_protocols
from app.core.model_usage import UsageSink, estimate_usage_tokens

_CIPHER = CredentialCipher("unit-test-master-key")


def _enc(plaintext: str) -> str:
    return _CIPHER.encrypt(plaintext)


@pytest.fixture(autouse=True)
def _cipher(monkeypatch):
    """解密路径只认测试主密钥,不依赖进程环境里的 APP_MASTER_KEY。"""
    monkeypatch.setattr("app.core.model_client.get_credential_cipher", lambda: _CIPHER)


def _catalog(
    *, with_chat: bool = True, with_embed: bool = True, api_key: str = "sk-test"
) -> ModelCatalog:
    providers: dict[str, ProviderEntry] = {}
    models: dict[str, ModelEntry] = {}
    defaults: dict[str, str] = {}
    if with_embed:
        providers["dash"] = ProviderEntry(
            key="dash",
            protocol="openai_compatible",
            base_url="https://fake.example/v1",
            api_key_encrypted=_enc(api_key),
            enabled=True,
        )
        models["dash/emb"] = ModelEntry(
            id="dash/emb", kind="embedding", provider_key="dash", model="emb-v3", dims=1024
        )
        defaults["embedding"] = "dash/emb"
    if with_chat:
        providers["ds"] = ProviderEntry(
            key="ds",
            protocol="openai_compatible",
            base_url="https://chat.example/v1",
            api_key_encrypted=_enc(api_key),
            enabled=True,
        )
        models["ds/chat"] = ModelEntry(
            id="ds/chat", kind="chat", provider_key="ds", model="chat-v1", context_tokens=64000
        )
        models["ds/off"] = ModelEntry(
            id="ds/off", kind="chat", provider_key="ds", model="off", enabled=False
        )
        defaults["chat"] = "ds/chat"
    return ModelCatalog(providers=providers, models=models, _defaults=defaults)


def _embed_body(vectors: list[list[float]], usage: dict | None) -> dict:
    body: dict = {"data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)]}
    if usage is not None:
        body["usage"] = usage
    return body


def _embed_response(
    vectors: list[list[float]], status: int = 200, usage: dict | None = None
) -> httpx.Response:
    return httpx.Response(
        status,
        json=_embed_body(vectors, usage),
        request=httpx.Request("POST", "https://fake/embeddings"),
    )


def test_embed_batches_and_preserves_order() -> None:
    calls: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body["input"])
        vectors = [[0.1, 0.2] for _ in body["input"]]
        return _embed_response(vectors)

    client = EmbeddingClient(_catalog(), transport=httpx.MockTransport(handler))
    result = client.embed([str(i) for i in range(12)], "dash/emb")

    assert len(result) == 12
    assert result[0] == [0.1, 0.2]
    assert [len(batch) for batch in calls] == [10, 2]  # 每批 ≤10
    assert calls[0][:3] == ["0", "1", "2"]
    assert calls[1] == ["10", "11"]  # 批次顺序即输入顺序


def test_embed_uses_default_model_when_unspecified() -> None:
    seen_models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_models.append(json.loads(request.content)["model"])
        return _embed_response([[0.0]])

    client = EmbeddingClient(_catalog(), transport=httpx.MockTransport(handler))
    client.embed(["文本"])
    assert seen_models == ["emb-v3"]  # defaults.embedding → 上游真实模型名


def test_embed_decrypts_provider_key_for_auth_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sk-test"
        return _embed_response([[0.0]])

    client = EmbeddingClient(_catalog(), transport=httpx.MockTransport(handler))
    assert client.embed(["文本"]) == [[0.0]]


def test_local_provider_needs_no_key() -> None:
    catalog = _catalog()
    catalog.providers["ollama"] = ProviderEntry(
        key="ollama",
        protocol="openai_compatible",
        base_url="http://localhost:11434/v1",
        api_key_encrypted="",  # 空密文 = 本地免密钥端点
        enabled=True,
    )
    assert resolve_api_key(catalog.providers["ollama"]) == "local-no-key"


def test_corrupted_key_raises_decrypt_error() -> None:
    broken = ModelCatalog(
        providers={
            "dash": ProviderEntry(
                key="dash",
                protocol="openai_compatible",
                base_url="https://fake.example/v1",
                api_key_encrypted="enc:v1:!!!not-base64!!!",
                enabled=True,
            )
        },
        models={
            "dash/emb": ModelEntry(
                id="dash/emb", kind="embedding", provider_key="dash", model="emb-v3"
            )
        },
        _defaults={"embedding": "dash/emb"},
    )
    client = EmbeddingClient(broken, transport=httpx.MockTransport(_embed_response([[0.0]])))
    with pytest.raises(AppError) as exc_info:
        client.embed(["文本"])
    assert exc_info.value.code_str == ErrorCode.CREDENTIAL_DECRYPT_FAILED[1]


def test_no_embedding_configured_raises_model_not_configured() -> None:
    client = EmbeddingClient(
        _catalog(with_embed=False), transport=httpx.MockTransport(_embed_response([[0.0]]))
    )
    with pytest.raises(ModelNotConfigured, match="embedding"):
        client.embed(["文本"])


def test_embed_upstream_error_raises_model_call_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    client = EmbeddingClient(_catalog(), transport=httpx.MockTransport(handler))
    with pytest.raises(ModelCallError) as exc_info:
        client.embed(["文本"])
    assert exc_info.value.status == 429
    assert "sk-test" not in str(exc_info.value)  # 密钥绝不入消息


def test_background_semaphore_limits_concurrency() -> None:
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
        _catalog(), background_semaphore=semaphore,
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


def test_chat_stream_yields_deltas_in_order() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["model"] == "chat-v1"
        return _sse_response(["你好", "，", "世界"])

    client = ChatClient(_catalog(), transport=httpx.MockTransport(handler))
    assert list(client.chat_stream([{"role": "user", "content": "hi"}])) == ["你好", "，", "世界"]


def test_chat_concatenates_stream() -> None:
    client = ChatClient(
        _catalog(),
        transport=httpx.MockTransport(lambda req: _sse_response(["A", "B", "C"])),
    )
    assert client.chat([{"role": "user", "content": "hi"}]) == "ABC"


def test_chat_upstream_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = ChatClient(_catalog(), transport=httpx.MockTransport(handler))
    with pytest.raises(ModelCallError) as exc_info:
        list(client.chat_stream([{"role": "user", "content": "hi"}]))
    assert exc_info.value.status == 500


def test_disabled_or_unknown_model_rejected() -> None:
    chat = ChatClient(_catalog(), transport=httpx.MockTransport(_sse_response(["x"])))
    embed = EmbeddingClient(_catalog(), transport=httpx.MockTransport(_embed_response([[0.0]])))
    with pytest.raises(ModelNotConfigured):
        chat.chat([{"role": "user", "content": "hi"}], "ds/off")  # enabled=False
    with pytest.raises(ModelNotConfigured):
        chat.chat([{"role": "user", "content": "hi"}], "no-such/model")
    with pytest.raises(ModelNotConfigured):
        embed.embed(["x"], "ds/chat")  # 类型不符


def test_disabled_provider_hides_its_models() -> None:
    catalog = _catalog()
    catalog.providers["ds"] = replace(catalog.providers["ds"], enabled=False)
    chat = ChatClient(catalog, transport=httpx.MockTransport(_sse_response(["x"])))
    with pytest.raises(ModelNotConfigured, match="供应商"):
        chat.chat([{"role": "user", "content": "hi"}])  # 默认 chat 模型随供应商失效


# ---------------------------- 协议注册表 ----------------------------


def test_registry_dispatch_and_failclosed() -> None:
    assert "openai_compatible" in registered_protocols()
    assert get_protocol("openai_compatible").__class__.__name__ == "OpenAICompatible"
    with pytest.raises(AppError) as exc_info:
        get_protocol("no_such_protocol")
    assert exc_info.value.code_str == ErrorCode.MODEL_NOT_CONFIGURED[1]
    assert "no_such_protocol" in exc_info.value.message


def test_registry_custom_adapter_is_dispatched() -> None:
    """新增适配器 = @register_protocol 即接入,调用方零改动(注册表模式核心卖点)。"""
    from app.core.model_protocols import register_protocol

    @register_protocol("fake_unit_adapter")
    class FakeAdapter:
        def embed(
            self, provider, model, api_key, texts, *, semaphore=None, transport=None, usage=None
        ):
            return [[float(len(t))] for t in texts]

        def chat_stream(
            self, provider, model, api_key, messages, temperature, *, transport=None, usage=None
        ):
            yield "fake"

        def chat_stream_tools(
            self,
            provider,
            model,
            api_key,
            messages,
            tools,
            temperature,
            *,
            transport=None,
            usage=None,
        ):
            yield "fake"

    try:
        catalog = _catalog()
        catalog.providers["dash"] = replace(
            catalog.providers["dash"], protocol="fake_unit_adapter"
        )
        catalog.providers["ds"] = replace(
            catalog.providers["ds"], protocol="fake_unit_adapter"
        )
        client = EmbeddingClient(catalog)
        assert client.embed(["ab"], "dash/emb") == [[2.0]]
        chat = ChatClient(catalog)
        assert list(chat.chat_stream([{"role": "user", "content": "hi"}], "ds/chat")) == ["fake"]
    finally:
        import app.core.model_protocols as mp

        mp._REGISTRY.pop("fake_unit_adapter", None)


# ---------------------------- function calling 流 ----------------------------


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


def test_chat_stream_tools_aggregates_fragmented_call() -> None:
    """function calling 流式分片跨 chunk 聚合:文本先发,意图在流末完整给出。"""
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

    client = ChatClient(_catalog(), transport=httpx.MockTransport(handler))
    frames = list(
        client.chat_stream_tools([{"role": "user", "content": "q"}], _TOOLS)
    )
    assert frames[0] == "先查一下。"
    call = frames[1]
    assert isinstance(call, ToolCallRequest)
    assert (call.id, call.name) == ("call_0", "search_knowledge")
    assert json.loads(call.arguments) == {"query": "报销"}


def test_chat_stream_tools_multiple_calls_in_index_order() -> None:
    fragments = [
        {"index": 1, "id": "call_b", "function": {"name": "t2", "arguments": "{}"}},
        {"index": 0, "function": {"name": "t1", "arguments": "{\"a\":1}"}},  # 无 id,按 index 兜底
    ]
    client = ChatClient(
        _catalog(),
        transport=httpx.MockTransport(lambda req: _sse_tools_response([], fragments)),
    )
    frames = list(client.chat_stream_tools([{"role": "user", "content": "q"}], _TOOLS))
    assert [f.name for f in frames] == ["t1", "t2"]
    assert frames[0].id == "call_0" and frames[1].id == "call_b"


# ---------------------------- token 用量采集(OPT-28) ----------------------------


def _sse_with_usage(pieces: list[str], usage: dict | None = None) -> httpx.Response:
    """造 chat SSE 流;`usage` 放在 choices 为空的收尾 chunk 里(OpenAI 的真实形态)。"""
    lines = [
        f"data: {json.dumps({'choices': [{'delta': {'content': p}}]})}\n\n" for p in pieces
    ]
    if usage is not None:
        lines.append(f"data: {json.dumps({'choices': [], 'usage': usage})}\n\n")
    lines.append("data: [DONE]\n\n")
    return httpx.Response(
        200, content="".join(lines).encode(), headers={"content-type": "text/event-stream"}
    )


def test_estimate_usage_tokens_rules() -> None:
    """口径:1 个汉字 0.75 token、1 个英文字母 0.25 token,向上取整。"""
    assert estimate_usage_tokens("") == 0
    assert estimate_usage_tokens("abcd") == 1
    assert estimate_usage_tokens("hello world") == 3  # 空格不单独计
    assert estimate_usage_tokens("你好世界") == 3
    assert estimate_usage_tokens("你好,world") == 3  # ceil(2*0.75 + 6*0.25)


def test_usage_sink_never_mixes_the_two_sources() -> None:
    sink = UsageSink()
    for junk in (None, {}, {"prompt_tokens": 0, "completion_tokens": 0}):
        sink.add_upstream(junk)  # 缺字段 / null / 全 0 都不算上游回传
    assert sink.from_upstream is False
    sink.estimate_from("abcd")
    assert (sink.total_tokens, sink.from_upstream) == (1, False)

    billed = UsageSink()
    billed.add_upstream({"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19})
    billed.estimate_from("hello world")  # 已认领上游口径,估算不得覆盖
    assert (billed.prompt_tokens, billed.completion_tokens, billed.total_tokens) == (12, 7, 19)
    assert billed.from_upstream is True


def test_no_usage_sink_keeps_payload_byte_identical() -> None:
    """不计量时连 stream_options 都不带:上线计量不能改变既有请求形态。"""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _sse_response(["A"])

    client = ChatClient(_catalog(), transport=httpx.MockTransport(handler))
    list(client.chat_stream([{"role": "user", "content": "hi"}]))
    assert "stream_options" not in seen


def test_chat_stream_records_upstream_usage() -> None:
    sink = UsageSink()

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream_options"] == {"include_usage": True}
        return _sse_with_usage(
            ["你好", "！"], {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19}
        )

    client = ChatClient(_catalog(), transport=httpx.MockTransport(handler))
    answer = "".join(client.chat_stream([{"role": "user", "content": "你好"}], usage=sink))
    assert answer == "你好！"
    assert (sink.prompt_tokens, sink.completion_tokens, sink.total_tokens) == (12, 7, 19)
    assert sink.from_upstream is True
    # 缺省 model_id 时,实际命中的默认模型也要落到收集器上(用量表要按模型汇总)
    assert (sink.model_id, sink.provider) == ("ds/chat", "ds")


def test_chat_stream_estimates_when_upstream_silent() -> None:
    sink = UsageSink()
    client = ChatClient(
        _catalog(), transport=httpx.MockTransport(lambda req: _sse_with_usage(["ABCDEF"]))
    )
    answer = "".join(
        client.chat_stream([{"role": "user", "content": "hello world"}], usage=sink)
    )
    assert answer == "ABCDEF"
    assert sink.from_upstream is False
    assert (sink.prompt_tokens, sink.completion_tokens) == (3, 2)
    assert sink.total_tokens == 5


def test_chat_stream_retries_without_stream_options_when_rejected() -> None:
    """厂商不认 stream_options(400)时去掉该字段重试一次:计量不许把问答弄挂。"""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "stream_options" in body:
            return httpx.Response(400, json={"error": "unknown field stream_options"})
        return _sse_with_usage(["ABC"])

    sink = UsageSink()
    answer = "".join(
        ChatClient(_catalog(), transport=httpx.MockTransport(handler)).chat_stream(
            [{"role": "user", "content": "hi"}], usage=sink
        )
    )
    assert answer == "ABC"
    assert ["stream_options" in b for b in bodies] == [True, False]
    assert sink.from_upstream is False
    assert (sink.prompt_tokens, sink.completion_tokens) == (1, 1)


def test_other_upstream_errors_are_not_retried() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500, json={"error": "boom"})

    client = ChatClient(_catalog(), transport=httpx.MockTransport(handler))
    with pytest.raises(ModelCallError) as exc_info:
        list(client.chat_stream([{"role": "user", "content": "hi"}], usage=UsageSink()))
    assert exc_info.value.status == 500
    assert len(calls) == 1  # 与计量无关的失败不重发,别把上游打爆


def test_closed_stream_still_bills_what_it_emitted() -> None:
    """断线清理契约不能被计量打破:close() 停在 yield 点也要把已吐出的部分入账。"""
    sink = UsageSink()
    gen = ChatClient(
        _catalog(), transport=httpx.MockTransport(lambda req: _sse_with_usage(["A", "B"]))
    ).chat_stream([{"role": "user", "content": "hi"}], usage=sink)
    assert next(gen) == "A"
    gen.close()
    assert sink.completion_tokens == estimate_usage_tokens("A")


def test_embed_usage_accumulates_across_batches() -> None:
    sink = UsageSink()

    def handler(request: httpx.Request) -> httpx.Response:
        batch = json.loads(request.content)["input"]
        return _embed_response(
            [[0.0] for _ in batch], usage={"prompt_tokens": 4, "total_tokens": 4}
        )

    client = EmbeddingClient(_catalog(), transport=httpx.MockTransport(handler))
    client.embed([f"t{i}" for i in range(12)], "dash/emb", usage=sink)  # 2 批
    assert (sink.prompt_tokens, sink.total_tokens, sink.from_upstream) == (8, 8, True)
    assert (sink.model_id, sink.provider) == ("dash/emb", "dash")


def test_embed_estimates_when_upstream_silent() -> None:
    sink = UsageSink()
    client = EmbeddingClient(
        _catalog(),
        transport=httpx.MockTransport(
            lambda req: _embed_response([[0.0]] * len(json.loads(req.content)["input"]))
        ),
    )
    client.embed(["你好世界", "abcd"], usage=sink)
    assert sink.from_upstream is False
    assert sink.prompt_tokens == 4  # 4 汉字 3.0 + abcd 1.0
    assert sink.completion_tokens == 0


def test_chat_stream_tools_bills_tool_arguments_as_output() -> None:
    fragments = [
        {
            "index": 0,
            "id": "call_0",
            "function": {"name": "search_knowledge", "arguments": '{"query": "报销"}'},
        }
    ]
    sink = UsageSink()
    client = ChatClient(
        _catalog(),
        transport=httpx.MockTransport(lambda req: _sse_tools_response(["先查一下。"], fragments)),
    )
    list(client.chat_stream_tools([{"role": "user", "content": "q"}], _TOOLS, usage=sink))
    assert sink.from_upstream is False
    assert sink.completion_tokens == estimate_usage_tokens(
        "先查一下。search_knowledge" + '{"query": "报销"}'
    )


# ---------------------------- 重排(OPT-27 cohere_rerank) ----------------------------


def _rerank_catalog(protocol: str = "cohere_rerank") -> ModelCatalog:
    providers = {
        "rr": ProviderEntry(
            key="rr",
            protocol=protocol,
            base_url="https://rerank.example/v1",
            api_key_encrypted=_enc("sk-rr"),
            enabled=True,
        )
    }
    models = {
        "rr/gte": ModelEntry(
            id="rr/gte", kind="rerank", provider_key="rr", model="gte-rerank"
        )
    }
    return ModelCatalog(providers=providers, models=models, _defaults={"rerank": "rr/gte"})


def _rerank_response(results: list[dict], status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, json={"results": results}, request=httpx.Request("POST", "https://x/rerank")
    )


def test_rerank_sends_cohere_shape_and_returns_sorted_pairs() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        assert request.url.path == "/v1/rerank"
        assert request.headers["authorization"] == "Bearer sk-rr"
        return _rerank_response(
            [{"index": 2, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.2}]
        )

    client = RerankClient(_rerank_catalog(), transport=httpx.MockTransport(handler))
    ranked = client.rerank("报销政策", ["a", "b", "c"])

    assert seen["model"] == "gte-rerank"
    assert seen["query"] == "报销政策"
    assert seen["documents"] == ["a", "b", "c"]
    assert seen["top_n"] == 3  # 未指定 top_n = 全量候选
    assert ranked == [(2, 0.9), (0, 0.2)]  # 按相关性降序


def test_rerank_respects_top_n_cap() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["top_n"] == 2
        return _rerank_response([{"index": 1, "relevance_score": 0.5}])

    client = RerankClient(_rerank_catalog(), transport=httpx.MockTransport(handler))
    assert client.rerank("q", ["a", "b", "c"], top_n=2) == [(1, 0.5)]


def test_rerank_empty_documents_skips_upstream() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("空候选不该打上游")

    client = RerankClient(_rerank_catalog(), transport=httpx.MockTransport(handler))
    assert client.rerank("q", []) == []


def test_rerank_upstream_error_and_bad_payload_raise_model_call_error() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    with pytest.raises(ModelCallError) as exc_info:
        RerankClient(_rerank_catalog(), transport=httpx.MockTransport(boom)).rerank(
            "q", ["a"]
        )
    assert exc_info.value.status == 429

    def _fails_with(results: list[dict]) -> None:
        with pytest.raises(ModelCallError):
            RerankClient(
                _rerank_catalog(),
                transport=httpx.MockTransport(lambda r: _rerank_response(results)),
            ).rerank("q", ["a", "b"])

    for bad in ([], [{"index": 9, "relevance_score": 0.1}], [{"relevance_score": 0.1}]):
        _fails_with(bad)  # 空 results / 下标越界 / 缺字段


def test_rerank_requires_rerank_kind_model_and_supported_protocol() -> None:
    client = RerankClient(_rerank_catalog(), transport=httpx.MockTransport(
        lambda r: _rerank_response([{"index": 0, "relevance_score": 0.1}])
    ))
    assert client.rerank("q", ["a"]) == [(0, 0.1)]

    # 目录里没有 rerank 默认 → 503(未配置,不是调用失败)
    with pytest.raises(ModelNotConfigured):
        RerankClient(
            ModelCatalog(providers={}, models={}, _defaults={}),
            transport=httpx.MockTransport(lambda r: _rerank_response([])),
        ).rerank("q", ["a"])

    # 协议不支持重排 → 503 且点名可用协议
    with pytest.raises(AppError) as exc_info:
        get_rerank_protocol("openai_compatible")
    assert exc_info.value.code_str == ErrorCode.MODEL_NOT_CONFIGURED[1]
    assert "cohere_rerank" in exc_info.value.message


def test_cohere_rerank_is_registered() -> None:
    assert "cohere_rerank" in registered_protocols()
    assert hasattr(get_protocol("cohere_rerank"), "rerank")
