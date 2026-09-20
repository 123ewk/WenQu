"""日志机制(基准 02):request_id 经 contextvars 注入全链路;生产 JSON,开发文本。

脱敏(Go-Live 翻转项 #13)在 formatter 出口统一做:密钥类字段赋值 / Bearer / sk- 形态 /
URL query 中的密钥参数 → ***;控制字符过滤(防日志注入换行伪造)。
落盘与轮转由容器 logging 驱动承担(json-file max-size),进程只写 stdout。
LLM 调试日志默认关(代码中不存在请求体日志路径,新增时必须过本模块 redact)。
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import time

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

_SECRET_KEYS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "access_key",
    "secret_key",
    "refresh_token",
    "access_token",
    "master_key",
)

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # 形态规则先行(摘走整值,避免键规则只吃掉 "Bearer" 一词留下残值)
    (re.compile(r"\bBearer\s+[a-zA-Z0-9\.\-_~+/]+=*", re.IGNORECASE), "Bearer***"),
    (re.compile(r"\bsk-[A-Za-z0-9]{8,}"), "sk-***"),
    # 引号赋值:password="x" / 'token': 'x'(键名不区分大小写;模板 \g<1>,\1*** 会成组 10)
    *(
        (re.compile(rf"""(?i)(["']?\b{k}\b["']?\s*[:=]\s*)(["'])[^"']*\2"""), r"\g<1>\g<2>***\g<2>")
        for k in _SECRET_KEYS
    ),
    # 无引号赋值:password=x / api-key: x(到空白/逗号/&/引号/行尾)
    *(
        (re.compile(rf"(?i)(\b{k}\b\s*[:=]\s*)[^\s,&\"']+"), r"\g<1>***")
        for k in _SECRET_KEYS
    ),
    # URL query 参数(?token=…&api-key=…)
    (
        re.compile(r"(?i)([?&](?:password|secret|token|api[-_]key|key)=)[^&\s]+"),
        r"\g<1>***",
    ),
)

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def redact(text_value: str) -> str:
    """对任意日志文本做密钥脱敏 + 控制字符过滤;无法解析的结构也不应泄漏原值。"""
    out = _CONTROL_CHARS.sub("", text_value)
    for pattern, repl in _REDACTIONS:
        out = pattern.sub(repl, out)
    return out


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def _redact_record(record: logging.LogRecord) -> None:
    record.msg = redact(record.getMessage())
    record.args = ()


class _RedactingFormatter(logging.Formatter):
    """所有 formatter 的公共基座:出口统一脱敏,漏配格式化模式也不会输出原文。"""

    def format(self, record: logging.LogRecord) -> str:
        _redact_record(record)
        return super().format(record)


class _JsonFormatter(_RedactingFormatter):
    def format(self, record: logging.LogRecord) -> str:
        _redact_record(record)
        payload: dict[str, object] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.msg,
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(env: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_RequestIdFilter())
    if env == "prod":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            _RedactingFormatter(
                "%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s"
            )
        )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
