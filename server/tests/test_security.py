"""安全原语契约:bcrypt 往返、JWT 白名单算法、过期/篡改拒绝(基准 04)。"""

from __future__ import annotations

import uuid

import jwt
import pytest

from app.core.config import Settings
from app.core.errors import AppError
from app.core.security import (
    create_access_token,
    decode_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)

SECRET = "unit-test-secret-0123456789abcdef0123456789abcdef"
SETTINGS = Settings(jwt_secret=SECRET, access_token_minutes=60)


def test_password_hash_roundtrip() -> None:
    hashed = hash_password("secret-pass-1")
    assert hashed != "secret-pass-1"
    assert verify_password("secret-pass-1", hashed)
    assert not verify_password("wrong-password", hashed)


def test_access_token_roundtrip() -> None:
    user_id, space_id = str(uuid.uuid4()), str(uuid.uuid4())
    token = create_access_token(user_id, space_id, SETTINGS)
    payload = decode_access_token(token, SETTINGS)
    assert payload["sub"] == user_id
    assert payload["sid"] == space_id
    assert payload["typ"] == "access"


def test_expired_token_rejected() -> None:
    import datetime as dt

    now = dt.datetime.now(dt.UTC)
    expired = jwt.encode(
        {"sub": str(uuid.uuid4()), "typ": "access", "exp": now - dt.timedelta(minutes=1)},
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(AppError) as exc_info:
        decode_access_token(expired, SETTINGS)
    assert exc_info.value.code_str == "TOKEN_EXPIRED"


def test_tampered_signature_rejected() -> None:
    token = create_access_token(str(uuid.uuid4()), None, SETTINGS)
    tampered = token[:-4] + ("aaaa" if token[-4:] != "aaaa" else "bbbb")
    with pytest.raises(AppError) as exc_info:
        decode_access_token(tampered, SETTINGS)
    assert exc_info.value.code_str == "AUTH_REQUIRED"


def test_algorithm_whitelist() -> None:
    """HS512 签名的 token 即使签名有效也必须被拒(alg 混淆防护,基准 04)。"""
    payload = {"sub": str(uuid.uuid4()), "typ": "access"}
    hs512_token = jwt.encode(payload, SECRET, algorithm="HS512")
    with pytest.raises(AppError) as exc_info:
        decode_access_token(hs512_token, SETTINGS)
    assert exc_info.value.code_str == "AUTH_REQUIRED"


def test_refresh_token_is_opaque_and_hashable() -> None:
    a, b = generate_refresh_token(), generate_refresh_token()
    assert a != b
    assert len(hash_refresh_token(a)) == 64  # SHA-256 hex
    assert hash_refresh_token(a) == hash_refresh_token(a)
