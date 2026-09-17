"""凭据加密:AES-256-GCM,主密钥来自 `APP_MASTER_KEY`(OPT-7)。

用途:凡是"后端需要拿回明文去用"的第三方凭据(当前是 API Key,后续如上游模型
Key 托管)都不能明文落库,统一走本模块。

设计取舍:
- **AEAD 而非裸加密**:GCM 自带认证标签,密文被改动一位就解密失败,不会返回垃圾明文;
- **每次加密独立随机 nonce**(12 字节),同一明文两次密文不同,杜绝密文比对;
- **密文带 `enc:v1:` 版本前缀**(与 `docs/架构设计.md` §8 的统一加密入口一致):
  将来换算法或轮换主密钥时,可以按前缀判别新旧记录,
  不必一次性停机重加密(轮换本身未实现,前缀是它的前置条件);
- **密钥派生用 SHA-256**:主密钥在 `.env` 里是任意长度字符串,统一派生 32 字节;
  这不提升强度,主密钥必须是高熵随机串(生成方式见 `deploy/.env.example`);
- **fail-closed**:主密钥缺失直接抛 503,绝不退回硬编码默认密钥 —— 否则"加密"会
  在无密钥环境里静默变成"可预测密钥加密",比不加密更危险。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
from functools import lru_cache

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode

_VERSION = "enc:v1"
_PREFIX = f"{_VERSION}:"
_NONCE_BYTES = 12
_TAG_BYTES = 16  # GCM 认证标签长度;最短合法载荷 = nonce + tag
_KEY_BYTES = 32  # AES-256


class CredentialCipher:
    """对称加密器:构造时注入主密钥,便于测试替换与将来做密钥版本并存。"""

    def __init__(self, master_key: str) -> None:
        if not master_key:
            raise AppError(
                ErrorCode.CREDENTIAL_KEY_MISSING,
                "服务端未配置凭据加密主密钥(APP_MASTER_KEY),无法处理加密凭据",
                http_status=503,
            )
        self._aead = AESGCM(hashlib.sha256(master_key.encode("utf-8")).digest()[:_KEY_BYTES])

    def encrypt(self, plaintext: str) -> str:
        nonce = os.urandom(_NONCE_BYTES)
        blob = self._aead.encrypt(nonce, plaintext.encode("utf-8"), None)
        return f"{_PREFIX}{base64.b64encode(nonce + blob).decode('ascii')}"

    def decrypt(self, token: str) -> str:
        """解密失败一律归一为 CREDENTIAL_DECRYPT_FAILED,不向调用方泄露原因。"""
        if not token.startswith(_PREFIX):
            raise self._invalid()
        payload = token[len(_PREFIX) :]
        if not payload:
            raise self._invalid()
        try:
            raw = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise self._invalid() from exc
        if len(raw) < _NONCE_BYTES + _TAG_BYTES:
            raise self._invalid()
        nonce, blob = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
        try:
            return self._aead.decrypt(nonce, blob, None).decode("utf-8")
        except (InvalidTag, UnicodeDecodeError) as exc:
            raise self._invalid() from exc

    @staticmethod
    def _invalid() -> AppError:
        return AppError(
            ErrorCode.CREDENTIAL_DECRYPT_FAILED,
            "凭据解密失败(数据损坏或主密钥已更换)",
            http_status=500,
        )


@lru_cache
def get_credential_cipher() -> CredentialCipher:
    """进程级单例:主密钥来自配置,运行期不变。"""
    return CredentialCipher(get_settings().master_key)
