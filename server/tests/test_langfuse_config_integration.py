"""Langfuse 可观测配置(OPT-49)HTTP 集成测试:守卫/密钥三态/掩码/审计/nil-safe 运行时读取。

钉住的需求口径:
- **superuser 专属**(与模型配置一致):普通用户 403;X-API-Key 不接 /admin/* → 401;
- **密钥三态语义**:不传 = 不改动、空串 = 清除、非空 = 替换;enabled=true 前置私钥已配置;
- **掩码**:响应与审计都不出现明文 Secret,只有 key_hint;
- **nil-safe**:表无行/未启用时运行时读取返回 None;保存后写时失效、下一次读取立即生效;
- **审计**:langfuse_config.updated,detail 只含 host/enabled/key_hint。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

import app.application.service.langfuse_config as lf_module
from app.application.service.langfuse_config import clear_langfuse_config_cache
from app.core.crypto import CredentialCipher
from app.core.db import get_session_factory
from app.domain.models import AuditLog, LangfuseConfig, User
from tests.conftest import register as _register

pytestmark = pytest.mark.integration

LF_URL = "/api/v1/admin/langfuse-config"


@pytest.fixture(autouse=True)
def _cipher_and_isolated(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """固定测试主密钥(CI 无 .env 时真实入口会 503)+ 每用例清理配置行与缓存。"""
    monkeypatch.setattr(
        lf_module,
        "get_credential_cipher",
        lambda: CredentialCipher("langfuse-config-test-key"),
    )
    clear_langfuse_config_cache()
    yield
    with get_session_factory()() as db:
        row = db.get(LangfuseConfig, 1)
        if row is not None:
            db.delete(row)
            db.commit()
    clear_langfuse_config_cache()


def _uname(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


def _auth(session: dict) -> dict:
    return {"Authorization": f"Bearer {session['access_token']}"}


def _superuser(client, prefix: str = "lfroot") -> dict:
    """注册后直接改库置 superuser(与 test_admin_models_integration 同口径)。"""
    session = _register(client, _uname(prefix))
    with get_session_factory()() as db:
        user = db.get(User, uuid.UUID(session["user"]["id"]))
        assert user is not None
        user.is_superuser = True
        db.commit()
    return session


def _put(client, headers, **overrides) -> dict:
    body = {
        "host": "https://langfuse.internal.example",
        "public_key": "pk-lf-1234567890",
        "secret_key": "sk-lf-abcdef9012345678",
        "enabled": True,
    }
    body.update(overrides)
    return client.put(LF_URL, json=body, headers=headers)


# ---------------------------- 权限 ----------------------------


def test_plain_user_forbidden_and_key_failclosed(client) -> None:
    plain = _register(client, _uname("lfplain"))
    assert client.get(LF_URL, headers=_auth(plain)).status_code == 403
    assert client.put(LF_URL, json={"host": "https://x", "public_key": "pk", "enabled": False},
                      headers=_auth(plain)).status_code == 403
    # Key 链路不接 /admin/*:带 Key 不带 Bearer → 401(既有 fail-closed)
    anon_with_key = client.get(LF_URL, headers={"X-API-Key": "whatever"})
    assert anon_with_key.status_code == 401


# ---------------------------- 保存/读取与掩码 ----------------------------


def test_put_creates_and_get_masks_secret(client) -> None:
    root = _superuser(client)
    resp = _put(client, _auth(root))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is True
    assert body["host"] == "https://langfuse.internal.example"  # 尾斜杠归一
    assert body["public_key"] == "pk-lf-1234567890"
    assert body["key_hint"] == "sk-l****5678"
    assert body["updated_by_hint"] == root["user"]["username"]
    # 明文 Secret 与密文都不出现在响应里
    assert "sk-lf-abcdef" not in resp.text
    assert "enc:v1:" not in resp.text


def test_secret_three_state_semantics(client) -> None:
    root = _superuser(client)
    # 首存 secret=A
    assert _put(client, _auth(root)).json()["key_hint"] == "sk-l****5678"
    # 不传 secret_key = 不改动
    same = _put(client, _auth(root), enabled=True, secret_key=None)
    assert same.json()["key_hint"] == "sk-l****5678"
    # 空串 = 清除;清除后再启用 → 400(启用前置私钥已配置)
    cleared = _put(client, _auth(root), enabled=False, secret_key="")
    assert cleared.status_code == 200 and cleared.json()["key_hint"] == ""
    blocked = _put(client, _auth(root), enabled=True, secret_key=None)
    assert blocked.status_code == 400
    assert "Secret Key" in blocked.json()["error"]["message"]
    # 非空 = 替换
    replaced = _put(client, _auth(root), secret_key="sk-lf-ffffffffffff", enabled=True)
    assert replaced.json()["key_hint"] == "sk-l****ffff"


def test_host_and_key_required(client) -> None:
    root = _superuser(client)
    # 空串在契约模型层(min_length=1)即被 422 拦下
    assert (
        client.put(LF_URL, json={"host": "", "public_key": "pk", "enabled": False},
                   headers=_auth(root)).status_code
        == 422
    )
    assert (
        client.put(LF_URL, json={"host": "https://x", "public_key": "", "enabled": False},
                   headers=_auth(root)).status_code
        == 422
    )
    # 纯空白穿透模型校验,由服务层归一为 400
    assert (
        client.put(LF_URL, json={"host": "   ", "public_key": "pk", "enabled": False},
                   headers=_auth(root)).status_code
        == 400
    )


# ---------------------------- nil-safe 运行时读取与缓存 ----------------------------


def test_runtime_read_nil_safe_then_live(client) -> None:
    root = _superuser(client)
    with get_session_factory()() as db:
        # 表无行 → None(nil-safe,问答链路行为与未接入时一致)
        assert lf_module.langfuse_settings(db) is None
    # 经 API 保存并启用 → 运行时读取立即生效(私钥解密回明文)
    assert _put(client, _auth(root)).status_code == 200
    with get_session_factory()() as db:
        settings = lf_module.langfuse_settings(db)
        assert settings is not None
        assert settings.host == "https://langfuse.internal.example"
        assert settings.secret_key == "sk-lf-abcdef9012345678"
    # 关闭 → None(写侧失效,本进程立即)
    assert client.put(LF_URL, json={"host": "https://langfuse.internal.example",
                                    "public_key": "pk-lf-1234567890", "secret_key": None,
                                    "enabled": False},
                      headers=_auth(root)).status_code == 200
    with get_session_factory()() as db:
        assert lf_module.langfuse_settings(db) is None


def test_runtime_cache_within_ttl(client, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    real_repo = lf_module.LangfuseConfigRepositoryImpl

    class CountingRepo(real_repo):
        def get_row(self):
            calls["n"] += 1
            return super().get_row()

    monkeypatch.setattr(lf_module, "LangfuseConfigRepositoryImpl", CountingRepo)
    clear_langfuse_config_cache()
    with get_session_factory()() as db:
        assert lf_module.langfuse_settings(db) is None  # 首次:打库
        assert calls["n"] == 1
        assert lf_module.langfuse_settings(db) is None  # TTL 内:不再打库(含"未接入"态)
        assert calls["n"] == 1


# ---------------------------- 审计 ----------------------------


def test_toggle_writes_audit_without_secret_material(client) -> None:
    root = _superuser(client)
    assert _put(client, _auth(root)).status_code == 200
    assert (
        client.put(LF_URL, json={"host": "https://langfuse.internal.example",
                                 "public_key": "pk-lf-1234567890", "secret_key": None,
                                 "enabled": False},
                   headers=_auth(root)).status_code
        == 200
    )
    with get_session_factory()() as db:
        rows = (
            db.execute(
                select(AuditLog).where(AuditLog.action == "langfuse_config.updated")
            )
            .scalars()
            .all()
        )
    mine = [row for row in rows if str(row.actor_id) == root["user"]["id"]]
    assert len(mine) == 2
    for row in mine:
        assert "sk-lf-" not in str(row.detail)  # 密钥本体不落审计
        assert "enc:v1:" not in str(row.detail)
    assert {row.detail["enabled"] for row in mine} == {True, False}
    assert all(row.space_id is None for row in mine)  # 全局配置不属于任何空间

# ---------------------------- OPT-49 N2:问答观测句柄 nil-safe ----------------------------


def test_start_qa_trace_nil_safe_and_no_langfuse_import(
    client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    from app.application.service.observability import start_qa_trace

    clear_langfuse_config_cache()
    monkeypatch.delenv("APP_LANGFUSE_CONFIG_CACHE_TTL_SECONDS", raising=False)
    sid = uuid.uuid4()
    with get_session_factory()() as db:
        obs = start_qa_trace(
            db, user_id=sid, space_id=sid, question="q", kb_ids=None, model_id=None, agent=False
        )
    assert type(obs).__name__ == "_NullQaTrace"  # 未接入 → 零操作句柄
    obs.finish(answer="a")  # 不抛
    # 懒加载:未配置路径零 import(连 langfuse 包都不进 sys.modules)
    assert "langfuse" not in sys.modules
