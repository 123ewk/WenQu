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
from dataclasses import dataclass

__all__ = ["UsageSink", "estimate_usage_tokens", "usage_prompt_text"]

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
    # 落到哪条模型/供应商:调用方只传"要不要默认模型",真实身份在解析后才确定
    model_id: str = ""
    provider: str = ""

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

    def estimate_from(self, prompt_text: str, completion_text: str = "") -> None:
        """上游没回 usage 时按本地口径补齐;已认领上游数字则不动。"""
        if self.from_upstream:
            return
        self.prompt_tokens = estimate_usage_tokens(prompt_text)
        self.completion_tokens = estimate_usage_tokens(completion_text)
        self.total_tokens = self.prompt_tokens + self.completion_tokens

    def bind(self, model_id: str, provider: str) -> None:
        """记下这次实际用到的模型与供应商(model_id 缺省时要到解析完才知道)。"""
        self.model_id = model_id
        self.provider = provider
