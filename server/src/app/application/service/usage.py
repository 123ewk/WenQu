"""token 用量记账(OPT-28):每次上游模型调用落一行事件,供空间/成员汇总。

三条硬约束:
1. **旁路写入**:独立会话 + 失败只记日志。用量绝不能打断主链路(问答/入库),
   与 `api/denied_audit.py` 的"失败留痕另开会话"同理 —— 请求事务可能正在回滚,
   而且记账发生在生成器的 finally 里,那里抛异常会咬断断线清理。
2. **从今往后计**:不回填历史。表里没有事件只代表那会儿还没上计量。
3. **两条口径不混算**:`source` 随行落库(厂商回传 = upstream,本地估算 = estimated),
   前端对 estimated 必须标"≈"。

计量点包在网关**外层**而不是埋进各调用点:Agent 模式一次提问要打多次上游
(每个工具轮 + 作答轮),逐点埋一定会漏;包一层则每次真实调用自动各记一行,
且生成器语义原样保留(断线时 close() 仍传到内层释放连接,已吐出的部分照样入账)。

归属:API Key 调用落在 Key 绑定的成员身上(`Caller.user` 即 `api_keys.member_user_id`);
入库用量落在文档上传者身上(`Document.uploaded_by` 可为空 → 该行只进空间合计)。
重排与"探活"调用不计费:前者是配置开关的附带开销且不进单次问答账,后者是管理员在
配置页点按钮,不是成员的业务用量。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Generator
from typing import TYPE_CHECKING, Protocol, TypeVar

from sqlalchemy.orm import Session, sessionmaker

from app.core.model_protocols import ToolCallRequest
from app.core.model_usage import UsageSink, usage_prompt_text
from app.domain.enums import UsageKind, UsageSource
from app.domain.interfaces import EmbeddingGateway
from app.domain.models import TokenUsageEvent

if TYPE_CHECKING:
    from app.application.agent.loop import ToolCallingChat

logger = logging.getLogger("app.usage")

_T = TypeVar("_T")


class UsageAwareChat(Protocol):
    """被计量的 chat 网关形状:比 `ToolCallingChat` 多一个可选 `usage` 出参。

    业务侧协议(ToolCallingChat / EmbeddingGateway)刻意**不含** usage —— 那是要给
    测试假实现去满足的接口,计量是本层的取舍,不该逼每个假网关学会记账。
    真实现(core 的 ChatClient)结构上满足本协议。
    """

    def chat_stream(
        self,
        messages: list[dict[str, str]],
        model_id: str | None = None,
        temperature: float = 0.7,
        *,
        usage: UsageSink | None = None,
    ) -> Generator[str, None, None]: ...

    def chat_stream_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        model_id: str | None = None,
        temperature: float = 0.7,
        *,
        usage: UsageSink | None = None,
    ) -> Generator[str | ToolCallRequest, None, None]: ...


class UsageAwareEmbedding(Protocol):
    """被计量的向量化网关形状(同上:真实现 = core 的 EmbeddingClient)。"""

    def embed(
        self,
        texts: list[str],
        model_id: str | None = None,
        *,
        usage: UsageSink | None = None,
    ) -> list[list[float]]: ...


class UsageRecorder:
    """用量事件的唯一写入口,同时也是计量外壳的工厂。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def chat(
        self,
        inner: UsageAwareChat,
        space_id: uuid.UUID,
        user_id: uuid.UUID | None,
    ) -> ToolCallingChat:
        return _MeteredChat(inner, self, space_id, user_id)

    def embedding(
        self,
        inner: UsageAwareEmbedding,
        space_id: uuid.UUID,
        user_id: uuid.UUID | None,
    ) -> EmbeddingGateway:
        return _MeteredEmbedding(inner, self, space_id, user_id)

    def record(
        self,
        kind: UsageKind,
        space_id: uuid.UUID,
        user_id: uuid.UUID | None,
        sink: UsageSink,
    ) -> None:
        """落一行;任何异常都吞成 warning(见模块头的"旁路写入")。"""
        try:
            with self._session_factory() as db:
                db.add(
                    TokenUsageEvent(
                        space_id=space_id,
                        user_id=user_id,
                        kind=kind.value,
                        model_id=sink.model_id,
                        provider=sink.provider,
                        prompt_tokens=sink.prompt_tokens,
                        completion_tokens=sink.completion_tokens,
                        total_tokens=sink.total_tokens,
                        source=(
                            UsageSource.UPSTREAM
                            if sink.from_upstream
                            else UsageSource.ESTIMATED
                        ).value,
                    )
                )
                db.commit()
        except Exception:  # noqa: BLE001 — 计量失败不许影响业务链路
            logger.warning(
                "token 用量记账失败 kind=%s space=%s model=%s",
                kind.value,
                space_id,
                sink.model_id,
                exc_info=True,
            )


class _MeteredChat:
    """chat 网关的计量外壳:一次上游调用 = 一行 chat 用量事件。"""

    def __init__(
        self,
        inner: UsageAwareChat,
        recorder: UsageRecorder,
        space_id: uuid.UUID,
        user_id: uuid.UUID | None,
    ) -> None:
        self._inner = inner
        self._recorder = recorder
        self._space_id = space_id
        self._user_id = user_id

    def chat_stream(
        self,
        messages: list[dict[str, str]],
        model_id: str | None = None,
        temperature: float = 0.7,
    ) -> Generator[str, None, None]:
        return self._metered(
            messages,
            lambda usage: self._inner.chat_stream(
                messages, model_id, temperature, usage=usage
            ),
        )

    def chat_stream_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        model_id: str | None = None,
        temperature: float = 0.7,
    ) -> Generator[str | ToolCallRequest, None, None]:
        return self._metered(
            messages,
            lambda usage: self._inner.chat_stream_tools(
                messages, tools, model_id, temperature, usage=usage
            ),
        )

    def _metered(
        self,
        messages: list[dict],
        open_stream: Callable[[UsageSink], Generator[_T, None, None]],
    ) -> Generator[_T, None, None]:
        sink = UsageSink()
        seen: list[str] = []
        try:
            yield from _observe(open_stream(sink), seen)
        finally:
            # 收口写在被调方法里而不是这里 `return`:`finally` 里 return 会**吞掉正在
            # 传播的上游异常**,把连不上模型悄悄变成"正常答完但没内容"
            self._settle(messages, sink, seen)

    def _settle(self, messages: list[dict], sink: UsageSink, seen: list[str]) -> None:
        if not seen and not sink.from_upstream:
            # 一字节没回、也没交回用量数字 = 这次调用根本没发生(连不上/参数被拒),
            # 记一行预估就是虚增。与 embedding 侧"只在成功时记账"同一口径;
            # 中途断流时 seen 已有内容,那部分确实算了钱,照样入账。
            return
        # 网关没交回上游数字(不支持 usage 的适配器)时按本地口径兜底;
        # 真适配器已在流收尾时估好,这里同文本重算是幂等的
        sink.estimate_from(usage_prompt_text(messages), "".join(seen))
        self._recorder.record(UsageKind.CHAT, self._space_id, self._user_id, sink)


class _MeteredEmbedding:
    """向量化网关的计量外壳:一次 embed(可能含多个批次)= 一行 embedding 用量事件。"""

    def __init__(
        self,
        inner: UsageAwareEmbedding,
        recorder: UsageRecorder,
        space_id: uuid.UUID,
        user_id: uuid.UUID | None,
    ) -> None:
        self._inner = inner
        self._recorder = recorder
        self._space_id = space_id
        self._user_id = user_id

    def embed(
        self, texts: list[str], model_id: str | None = None
    ) -> list[list[float]]:
        if not texts:
            return self._inner.embed(texts, model_id)
        sink = UsageSink()
        vectors = self._inner.embed(texts, model_id, usage=sink)
        # 只在成功时记账:上游失败没有消耗 token,记了就是虚增
        sink.estimate_from("\n".join(texts))
        self._recorder.record(UsageKind.EMBEDDING, self._space_id, self._user_id, sink)
        return vectors


def _observe(
    stream: Generator[_T, None, None], seen: list[str]
) -> Generator[_T, None, None]:
    """原样透传流,同时把其中的文本增量收下来供收尾估算。

    `finally` 里显式 close 内层:外层被 close()(断线)时,内层的 with 清理必须
    照样执行 —— 那是释放上游模型连接的契约,不能等垃圾回收。
    """
    try:
        for item in stream:
            if isinstance(item, str):
                seen.append(item)
            yield item
    finally:
        stream.close()
