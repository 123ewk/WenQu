"""凭据加密单测:AES-256-GCM 往返、随机 nonce、防篡改、fail-closed(OPT-7)。

测试意图(不是复述实现):
- 往返正确性含中文与空串(API Key 是 ASCII,但同一把锁以后还要锁别的凭据);
- 同一明文两次加密必须不同 —— 否则密文可被比对,泄露"两个空间用了同一把 Key";
- 任何一位被改动都必须解密失败(GCM 认证标签),不能返回垃圾明文;
- 换密钥必须解不开(不能有"解错但能用"的静默降级);
- 没配主密钥必须大声失败,不能退回硬编码默认密钥。
"""

from __future__ import annotations

import base64

import pytest

from app.core.crypto import CredentialCipher
from app.core.errors import AppError, ErrorCode

_MASTER_KEY = "dev-master-key-for-test-0123456789"
_OTHER_KEY = "another-master-key-0123456789abcdef"

# ErrorCode 成员是 (数字码, 稳定字符串码) 元组;断言按两个维度都核一遍
_DECRYPT_FAILED = ErrorCode.CREDENTIAL_DECRYPT_FAILED
_KEY_MISSING = ErrorCode.CREDENTIAL_KEY_MISSING


def _cipher(key: str = _MASTER_KEY) -> CredentialCipher:
    return CredentialCipher(key)


# ---------------------------- 往返 ----------------------------


@pytest.mark.parametrize(
    "plaintext",
    [
        "sk-live-abcdefghijklmnopqrstuvwxyz0123456789",
        "",
        "含中文的凭据值",
        "x" * 4096,
    ],
)
def test_roundtrip_returns_original(plaintext: str) -> None:
    cipher = _cipher()
    assert cipher.decrypt(cipher.encrypt(plaintext)) == plaintext


def test_same_plaintext_encrypts_differently_each_time() -> None:
    """随机 nonce:同一明文两次密文不同,避免密文比对。"""
    cipher = _cipher()
    assert cipher.encrypt("same-value") != cipher.encrypt("same-value")


def test_ciphertext_carries_version_prefix() -> None:
    """带版本前缀(与架构设计 §8 的统一加密入口 `enc:v1:` 一致),为将来换算法/轮换留判别路径。"""
    assert _cipher().encrypt("v").startswith("enc:v1:")


def test_plaintext_does_not_leak_into_ciphertext() -> None:
    token = _cipher().encrypt("sk-live-secret-value")
    assert "secret-value" not in token


# ---------------------------- 防篡改 ----------------------------


def test_tampered_ciphertext_fails_to_decrypt() -> None:
    cipher = _cipher()
    token = cipher.encrypt("sk-live-tamper-me")
    prefix, _, payload = token.rpartition(":")
    raw = bytearray(base64.b64decode(payload))
    raw[-1] ^= 0x01  # 翻转密文最后一位
    tampered = f"{prefix}:{base64.b64encode(bytes(raw)).decode('ascii')}"

    with pytest.raises(AppError) as exc:
        cipher.decrypt(tampered)
    assert exc.value.code_str == _DECRYPT_FAILED[1]
    assert exc.value.code_num == _DECRYPT_FAILED[0]


def test_wrong_key_cannot_decrypt() -> None:
    token = _cipher().encrypt("sk-live-scoped-to-one-key")
    with pytest.raises(AppError):
        _cipher(_OTHER_KEY).decrypt(token)


# ---------------------------- 输入防御(fail-closed) ----------------------------


def test_empty_master_key_fails_closed() -> None:
    """没配主密钥必须抛错,而不是静默用默认密钥加密。"""
    with pytest.raises(AppError) as exc:
        CredentialCipher("")
    assert exc.value.code_str == _KEY_MISSING[1]
    assert exc.value.code_num == _KEY_MISSING[0]
    assert exc.value.http_status == 503


@pytest.mark.parametrize(
    "bad_token",
    [
        "not-a-versioned-token",
        "enc:v2:AAAA",  # 未知版本,不能当作 v1 硬解
        "enc:v1:",  # 空载荷
        "enc:v1:!!!!",  # 非法 base64
        "enc:v1:" + base64.b64encode(b"short").decode("ascii"),  # 长度不足 nonce+tag
    ],
)
def test_malformed_token_rejected(bad_token: str) -> None:
    with pytest.raises(AppError) as exc:
        _cipher().decrypt(bad_token)
    assert exc.value.code_str == _DECRYPT_FAILED[1]


def test_corrupted_utf8_payload_rejected() -> None:
    """密文认证通过但明文不是合法 UTF-8 时也必须报错,不能抛 UnicodeDecodeError。"""
    import hashlib
    import os

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    aead = AESGCM(hashlib.sha256(_MASTER_KEY.encode()).digest())
    nonce = os.urandom(12)
    blob = aead.encrypt(nonce, b"\xff\xfe\xfd", None)
    forged = "enc:v1:" + base64.b64encode(nonce + blob).decode("ascii")

    with pytest.raises(AppError) as exc:
        _cipher().decrypt(forged)
    assert exc.value.code_str == _DECRYPT_FAILED[1]
