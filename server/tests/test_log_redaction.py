"""日志脱敏(Go-Live 翻转项 #13)单测。"""

from __future__ import annotations

import json
import logging

from app.core.logging import _JsonFormatter, _RedactingFormatter, redact


def test_redact_key_value_assignments() -> None:
    assert redact('password="hunter2"') == 'password="***"'
    assert redact("api_key=sk-abcdef123456") == "api_key=***"
    assert redact("'refresh_token': 'eyJhbGci'") == "'refresh_token': '***'"
    assert redact("master_key: longsecretvalue") == "master_key: ***"


def test_redact_bearer_and_sk_forms() -> None:
    out = redact("Authorization: Bearer abc.def.ghi")
    assert "abc.def.ghi" not in out
    assert redact("use sk-1234567890abcdef now") == "use sk-*** now"


def test_redact_url_query_params() -> None:
    assert redact("GET /x?token=abc123&y=1") == "GET /x?token=***&y=1"
    assert redact("GET /x?api-key=zzz9") == "GET /x?api-key=***"


def test_redact_strips_control_chars() -> None:
    assert redact("line1\nfake\x1b[31mesc") == "line1\nfake[31mesc"
    assert "\x00" not in redact("nul\x00byte")


def test_redact_keeps_ordinary_text() -> None:
    msg = "denied recorded actor=123 space=45 code=FORBIDDEN path=/api/v1/spaces"
    assert redact(msg) == msg


def _record(msg: str, args: tuple[object, ...] = ()) -> logging.LogRecord:
    return logging.LogRecord("t", logging.INFO, "f.py", 1, msg, args, None)


def test_text_formatter_redacts_at_exit() -> None:
    fmt = _RedactingFormatter("%(message)s")
    assert fmt.format(_record('login ok password="s3cret!!"')) == 'login ok password="***"'


def test_json_formatter_redacts_message_and_exc() -> None:
    try:
        raise ValueError('failed with token="topsecret"')
    except ValueError:
        import sys

        rec = _record('call failed api_key="mykey99"')
        rec.exc_info = sys.exc_info()
    payload = json.loads(_JsonFormatter().format(rec))
    assert payload["msg"] == 'call failed api_key="***"'
    assert "mykey99" not in payload["exc"] and "topsecret" not in payload["exc"]
