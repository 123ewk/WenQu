"""token 用量采集(OPT-28):协议层往出参里写,调用方在请求收尾后读。

**为什么是出参而不是返回值**:`ChatClient.chat_stream` 返回 Generator 是断线清理契约
的一部分(客户端断线时泵线程靠 `close()` 在当前 yield 点中止生成器,释放上游连接)。
返回值通道被生成器本身占住,要携带用量只能由调用方传一个收集器进来。

**两条口径绝不混算**,所以 `from_upstream` 必须随数字一起落地:
- 厂商回了 `usage` → 照抄(`UPSTREAM`);
- 厂商没回 → 本地按 `estimate_usage_tokens` 估(`ESTIMATED`),前端展示要标"≈"。

估算口径与分块用的 `application.chunking.estimate_tokens` 是**两套**:分块要保守
(宁可切小也要保证不超上下文上限),计量要贴近账单。两者互相独立,改任何一套都不
该牵动另一套的既有测试与已入库分块。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

__all__ = [
    "ChatTurnUsage",
    "ContextWindowUsage",
    "UsageSink",
    "estimate_usage_tokens",
    "usage_prompt_text",
]

# 汉字 + 假名 + 谚文:这些字符按 0.75 token/字估(中文 BPE 大致 1 字 ≈ 0.6~1 token)
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\u3040-\u30ff\uac00-\ud7af]")


def estimate_usage_tokens(text: str) -> int:
    """计量口径的估算:1 个汉字 0.75 token,1 个英文字母(及其他非空白字符)0.25 token。

    向上取整 —— 估算宁可多报也不把有用量的一次调用报成 0。空白不单独成词(BPE 里
    空格跟着下一个词走),故不计数。
    """
    cjk = len(_CJK_RE.findall(text))
    other = sum(1 for ch in text if not ch.isspace()) - cjk
    return math.ceil(cjk * 0.75 + other * 0.25)


def usage_prompt_text(messages: list[dict]) -> str:
    """把一次对话请求的可见文本拼起来作为估算输入(多轮上下文按全量计)。"""
    parts: list[str] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
    return "\n".join(parts)


def _as_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _extract_cached_tokens(raw: dict) -> int | None:
    """从上游 usage 里取缓存命中 token 数,兼容多家字段名;取不到返回 None(不支持)。

    优先级:OpenAI/百炼兼容 `prompt_tokens_details.cached_tokens` → Anthropic
    `cache_read_input_tokens` → 裸 `cached_tokens`。**键存在且是整数**才算"上游报了这个字段"
    (哪怕值为 0 = 命中 0 次),缺失则 None —— 用它区分"不支持缓存统计"与"支持但没命中"。
    """
    details = raw.get("prompt_tokens_details")
    if isinstance(details, dict) and isinstance(details.get("cached_tokens"), int):
        return details["cached_tokens"]
    for key in ("cache_read_input_tokens", "cached_tokens"):
        if isinstance(raw.get(key), int):
            return raw[key]
    return None


@dataclass
class UsageSink:
    """一次上游调用的 token 计数收集器。

    上游回传走 `add_upstream`(跨批次**累加**,embedding 会打多个 batch);
    没回传时由 `estimate_from` 按本地口径**覆盖**一次(两条口径不混算,故不用累加)。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    from_upstream: bool = False
    # 缓存命中(P-cache):上游报了几次 cached_tokens(跨批次累加)与"是否报过该字段"
    cached_tokens: int = 0
    cache_reported: bool = False
    # 落到哪条模型/供应商:调用方只传"要不要默认模型",真实身份在解析后才确定
    model_id: str = ""
    provider: str = ""
    # 该模型声明的上下文窗口上限(OPT-29);目录里允许留空
    context_tokens: int | None = None

    def add_upstream(self, raw: object) -> None:
        """累加厂商响应里的 usage 对象;没有可用数字(缺失/null/全 0)时不认领上游口径。"""
        if not isinstance(raw, dict):
            return
        prompt = _as_int(raw.get("prompt_tokens"))
        completion = _as_int(raw.get("completion_tokens"))
        total = _as_int(raw.get("total_tokens")) or prompt + completion
        if not (prompt or completion or total):
            return
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.total_tokens += total
        self.from_upstream = True
        cached = _extract_cached_tokens(raw)
        if cached is not None:
            # 只在上游确实带了 cached 字段时才记;值 0 也算"带了",用于区分不支持与零命中
            self.cached_tokens += cached
            self.cache_reported = True

    def estimate_from(self, prompt_text: str, completion_text: str = "") -> None:
        """上游没回 usage 时按本地口径补齐;已认领上游数字则不动。"""
        if self.from_upstream:
            return
        self.prompt_tokens = estimate_usage_tokens(prompt_text)
        self.completion_tokens = estimate_usage_tokens(completion_text)
        self.total_tokens = self.prompt_tokens + self.completion_tokens

    def bind(
        self,
        model_id: str,
        provider: str,
        context_tokens: int | None = None,
    ) -> None:
        """记下这次实际用到的模型与供应商(model_id 缺省时要到解析完才知道)。"""
        self.model_id = model_id
        self.provider = provider
        self.context_tokens = context_tokens


@dataclass(frozen=True)
class ContextWindowUsage:
    """本轮上下文的占用快照:分子 used_tokens,分母 window_tokens(未配置时为 None)。"""

    used_tokens: int
    window_tokens: int | None


@dataclass
class ChatTurnUsage:
    """一次问答内所有上游 chat 调用的收集器(OPT-29 进度条的分子来源)。

    **为什么挂在计量外壳上而不是流水线里**:Agent 模式的 prompt 由 AgentRunner 逐轮
    组装,问答流水线根本看不见;而外壳看得到每一次真正发给上游的调用。
    """

    sinks: list[UsageSink] = field(default_factory=list)

    def add(self, sink: UsageSink) -> None:
        self.sinks.append(sink)

    def window(self) -> ContextWindowUsage | None:
        """取**最后一次**调用的分子/分母;一次都没发生则为 None。

        不累加:Agent 每轮都在重发同一份上下文,累加会把用量报到超出窗口。
        """
        if not self.sinks:
            return None
        last = self.sinks[-1]
        return ContextWindowUsage(
            used_tokens=last.prompt_tokens + last.completion_tokens,
            window_tokens=last.context_tokens,
        )

    def cache_snapshot(self) -> tuple[int, int] | None:
        """末次调用若上游回报了 cached_tokens,返回 (cached, prompt) 供算命中率;否则 None。

        与 `window()` 同取最后一次调用(Agent 多轮重发同一上下文,累加无意义)。
        prompt<=0 或未报该字段一律 None = "当前模型暂不支持缓存命中统计"。
        """
        if not self.sinks:
            return None
        last = self.sinks[-1]
        if not last.from_upstream or not last.cache_reported or last.prompt_tokens <= 0:
            return None
        return (last.cached_tokens, last.prompt_tokens)
