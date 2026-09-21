"""空间模型配置端点集成测试(OPT-27,真 Postgres)。

覆盖:① 门禁(匿名 401 / 非成员 404 / Viewer 只读 / Admin+ 可写);
② 密钥"写入即加密、只回显掩码"与 embedding Key 的清除;
③ 空间层规则(embedding 不可配、chat 必带 context_tokens、kind 与协议匹配);
④ 跨空间隔离与"空间设默认不动全局默认"(层谓词回归);
⑤ rerank 开关落库与读回;⑥ 【测试连接】的 424 reason 分诊;
⑦ prod 下自配 base_url 的 SSRF 闸门(dev 放行、prod 拒内网、显式豁免可放行)。

上游调用一律打桩(只验分诊与形状,不真连厂商);CI 无 .env,主密钥显式注入。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.crypto import CredentialCipher
from app.core.db import get_session_factory
from app.core.model_protocols import ModelCallError, registered_rerank_protocols
from app.domain.models import (
    AuditLog,
    CustomModel,
    ModelProvider,
    SpaceModelSettings,
    User,
)
from tests.conftest import register as _register

pytestmark = pytest.mark.integration

_CIPHER_KEY = "space-model-test-key"


@pytest.fixture(autouse=True)
def _cipher(monkeypatch: pytest.MonkeyPatch) -> None:
    """加密与解密两端注入同一把测试密钥(CI 无 .env,真实入口会 fail-closed 503)。"""

    def factory() -> CredentialCipher:
        return CredentialCipher(_CIPHER_KEY)

    monkeypatch.setattr(
        "app.application.service.model_admin.get_credential_cipher", factory
    )
    monkeypatch.setattr("app.core.model_client.get_credential_cipher", factory)


@pytest.fixture(autouse=True)
def _reset_tables(client):
    """session 级共用库:前后都清,别把空间私有模型泄漏给其他文件。"""

    def _clean() -> None:
        with get_session_factory()() as db:
            db.execute(delete(SpaceModelSettings))
            db.execute(delete(CustomModel))
            db.execute(delete(ModelProvider))
            db.commit()

    _clean()
    yield
    _clean()


def _auth(session: dict) -> dict:
    return {"Authorization": f"Bearer {session['access_token']}"}


def _owner(client, username: str, space_name: str) -> tuple[str, dict]:
    """注册 + 建空间,返回 (space_id, 属主 Authorization)。"""
    session = _register(client, username)
    resp = client.post(
        "/api/v1/spaces", json={"name": space_name}, headers=_auth(session)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], _auth(session)


def _member(client, space_id: str, username: str, role: int, owner_auth: dict) -> dict:
    """把新人拉进空间并返回其会话(角色由属主指定)。"""
    session = _register(client, username)
    resp = client.post(
        f"/api/v1/spaces/{space_id}/members",
        json={"username": username, "role": role},
        headers=owner_auth,
    )
    assert resp.status_code == 201, resp.text
    return session


def _provider_body(**overrides) -> dict:
    body = {
        "provider_type": "deepseek",
        "name": f"演示厂{uuid.uuid4().hex[:6]}",
        "api_key": "sk-1234567890abcdef",
        # base_url 留空 = 取该类型的默认地址:写死 deepseek 会把别的类型的行污染成 deepseek 地址
        "base_url": "",
    }
    body.update(overrides)
    return body


def _model_body(provider_id: str, **overrides) -> dict:
    body = {
        "provider_id": provider_id,
        "kind": "chat",
        "model_name": "deepseek-chat",
        "context_tokens": 64000,
    }
    body.update(overrides)
    return body


def _create_provider(client, space_id: str, auth: dict, **overrides) -> dict:
    resp = client.post(
        f"/api/v1/spaces/{space_id}/model-config/providers",
        json=_provider_body(**overrides),
        headers=auth,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_model(client, space_id: str, auth: dict, provider_id: str, **overrides) -> dict:
    resp = client.post(
        f"/api/v1/spaces/{space_id}/model-config/models",
        json=_model_body(provider_id, **overrides),
        headers=auth,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------- 门禁 ----------------------------


def test_gate_anon_nonmember_and_viewer_readonly(client) -> None:
    space_id, owner = _owner(client, "mcgate", "门禁空间")
    base = f"/api/v1/spaces/{space_id}/model-config"

    assert client.get(base).status_code == 401
    stranger = _auth(_register(client, "mcstranger"))
    ghost_space = str(uuid.uuid4())
    # 非成员拿到与"空间不存在"同壳:不泄露存在性
    assert client.get(base, headers=stranger).status_code == 404
    assert client.get(base, headers=stranger).json()["error"]["code"] == "SPACE_NOT_FOUND"
    assert (
        client.get(f"/api/v1/spaces/{ghost_space}/model-config", headers=owner).status_code
        == 404
    )

    viewer = _auth(_member(client, space_id, "mcviewer", 10, owner))
    assert client.get(base, headers=viewer).status_code == 200
    assert client.get(f"{base}/types", headers=viewer).status_code == 200
    denied = client.post(f"{base}/providers", json=_provider_body(), headers=viewer)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "FORBIDDEN"
    assert client.put(
        f"{base}/rerank-switch", json={"enabled": True}, headers=viewer
    ).status_code == 403
    # 属主(Owner≥Admin)同一路由可写
    assert client.post(f"{base}/providers", json=_provider_body(), headers=owner).status_code == 201


def test_api_key_cannot_touch_model_config(client) -> None:
    space_id, owner = _owner(client, "mckey", "钥匙空间")
    key = client.post(
        f"/api/v1/spaces/{space_id}/api-keys",
        json={
            "name": "k",
            "capabilities": ["chat", "documents"],
            "member_user_id": _user_id_of(client, owner),
        },
        headers=owner,
    )
    assert key.status_code == 201, key.text
    resp = client.get(
        f"/api/v1/spaces/{space_id}/model-config",
        headers={"X-API-Key": key.json()["plaintext"]},
    )
    assert resp.status_code == 401  # 本组路由只认 JWT
    assert resp.json()["error"]["code"] == "AUTH_REQUIRED"


def _user_id_of(client, auth: dict) -> str:
    return client.get("/api/v1/users/me", headers=auth).json()["id"]


# ---------------------------- embedding Key ----------------------------


def test_embedding_key_mask_clear_and_view(client) -> None:
    space_id, owner = _owner(client, "mcemb", "向量化空间")
    base = f"/api/v1/spaces/{space_id}/model-config"

    view = client.get(base, headers=owner).json()
    assert view["embedding"]["model_name"] == "text-embedding-v3"
    assert view["embedding"]["dims"] == 1024
    assert view["embedding"]["key_configured"] is False
    assert view["embedding"]["fallback_active"] is False  # 全局也没配

    saved = client.put(
        f"{base}/embedding-key", json={"api_key": "sk-a1b2c3d4e5f6g7h8"}, headers=owner
    )
    assert saved.status_code == 200 and saved.json()["ok"] is True
    assert "sk-a1b2c3d4e5f6g7h8" not in saved.text

    view = client.get(base, headers=owner).json()
    assert view["embedding"]["key_configured"] is True
    assert view["embedding"]["key_hint"] == "sk-a****g7h8"
    with get_session_factory()() as db:
        row = db.get(SpaceModelSettings, uuid.UUID(space_id))
        assert row is not None
        assert row.embedding_key_encrypted.startswith("enc:v1:")
        assert "sk-a1b2c3d4e5f6g7h8" not in row.embedding_key_encrypted

    # 空串 = 清除(二次确认由前端弹)
    client.put(f"{base}/embedding-key", json={"api_key": ""}, headers=owner)
    view = client.get(base, headers=owner).json()
    assert view["embedding"]["key_configured"] is False and view["embedding"]["key_hint"] == ""
    with get_session_factory()() as db:
        row = db.get(SpaceModelSettings, uuid.UUID(space_id))
        assert row is not None and row.embedding_key_encrypted == ""
    actions = _audit_actions(space_id)
    assert "model_setting.embedding_key.updated" in actions


def _audit_actions(space_id: str) -> set[str]:
    with get_session_factory()() as db:
        return set(
            db.scalars(select(AuditLog.action).where(AuditLog.space_id == uuid.UUID(space_id)))
        )


# ---------------------------- 供应商 / 模型 ----------------------------


def test_dashscope_rerank_type_uses_native_protocol(client) -> None:
    """F2:百炼重排走原生协议,两种重排协议都只挂 rerank。

    旧写法给百炼预填 `compatible-mode/v1` + cohere 平铺协议,上游按 cohere 形状
    收不到 `input/parameters`,探活一直 424 —— 类型表与真端点对不上就是这条回归。
    """
    space_id, owner = _owner(client, "mcrerank", "重排类型空间")
    base = f"/api/v1/spaces/{space_id}/model-config"

    types = {t["key"]: t for t in client.get(f"{base}/types", headers=owner).json()}
    native = types["dashscope_rerank"]
    assert native["protocol"] == "dashscope_rerank"
    # base_url 只到站点根:原生服务路径由适配器钉死,预填带 /v1 会拼出两段
    assert native["default_base_url"] == "https://dashscope.aliyuncs.com"
    assert native["kinds"] == ["rerank"]
    assert types["jina_rerank"]["protocol"] == "cohere_rerank"
    # 类型表的重排协议清单由注册表派生(新增协议不用再抄)
    assert {t["protocol"] for t in types.values() if "rerank" in t["kinds"]} == set(
        registered_rerank_protocols()
    )

    provider = _create_provider(
        client, space_id, owner, provider_type="dashscope_rerank", name="百炼原生厂", base_url=""
    )
    assert provider["protocol"] == "dashscope_rerank"
    assert provider["base_url"] == "https://dashscope.aliyuncs.com"

    model = _create_model(
        client, space_id, owner, provider["id"],
        kind="rerank", model_name="gte-rerank-v2", context_tokens=None,
    )
    assert model["kind"] == "rerank"  # 新协议接得住 rerank 模型

    # 原生协议同样是"重排协议",不许挂 chat(embedding 一并拒)
    for kind in ("chat", "embedding"):
        bad = client.post(
            f"{base}/models",
            json=_model_body(provider["id"], kind=kind, context_tokens=4096),
            headers=owner,
        )
        assert bad.status_code == 422 and "重排协议" in bad.text


def test_provider_rows_carry_kinds_so_frontend_stops_guessing_protocol(client) -> None:
    """OPT-35:聚合视图的供应商行下发 kinds,前端分块不再按 protocol 硬编码。

    F2 之后重排协议有两个,`protocol === 'cohere_rerank'` 会让百炼重排供应商既不进
    重排块又串进对话块(表现 = "配了但页面看不到")。kinds 由类型表派生,新增协议零改动。
    """
    space_id, owner = _owner(client, "mckinds", "分块空间")
    base = f"/api/v1/spaces/{space_id}/model-config"

    chat = _create_provider(client, space_id, owner, name="对话厂")
    native = _create_provider(
        client, space_id, owner, provider_type="dashscope_rerank", name="百炼重排厂"
    )
    flat = _create_provider(
        client, space_id, owner, provider_type="siliconflow_rerank", name="硅基重排厂"
    )
    rows = {p["id"]: p for p in client.get(base, headers=owner).json()["providers"]}
    assert rows[chat["id"]]["kinds"] == ["chat"]
    assert rows[native["id"]]["kinds"] == ["rerank"]
    assert rows[flat["id"]]["kinds"] == ["rerank"]

    # 分块判据自洽:两类互斥,不会有一行同时落进两个区块
    by_kind = {k: {i for i, p in rows.items() if k in p["kinds"]} for k in ("chat", "rerank")}
    assert by_kind["chat"] == {chat["id"]} and by_kind["rerank"] == {native["id"], flat["id"]}

    # 类型表里同一协议的 kinds 必须唯一,否则"按协议派生"会互相覆盖
    types = client.get(f"{base}/types", headers=owner).json()
    per_protocol: dict[str, set[str]] = {}
    for t in types:
        per_protocol.setdefault(t["protocol"], set()).update(t["kinds"])
    assert per_protocol["openai_compatible"] == {"chat"}
    assert per_protocol["dashscope_rerank"] == {"rerank"}
    assert per_protocol["cohere_rerank"] == {"rerank"}

    # 未知协议(库里被手改的残留行)→ 空清单:宁可两个区块都不列,也不蒙一个
    with get_session_factory()() as db:
        db.add(
            ModelProvider(
                space_id=uuid.UUID(space_id),
                name="历史遗留厂",
                protocol="legacy_rerank_v0",
                base_url="https://legacy.example",
                api_key_encrypted="",
            )
        )
        db.commit()
    named = {p["name"]: p for p in client.get(base, headers=owner).json()["providers"]}
    assert named["历史遗留厂"]["kinds"] == []


def test_provider_types_carry_model_name_presets_without_gating(client) -> None:
    """OPT-36:/types 下发模型名提示清单,后端不校验 —— 自定义值必须照样能存。

    预设只是"人类常见的填法";填错的表现不变(探活/真实调用时上游报 model_name)。
    """
    space_id, owner = _owner(client, "mcpreset", "预设空间")
    base = f"/api/v1/spaces/{space_id}/model-config"

    types = {t["key"]: t for t in client.get(f"{base}/types", headers=owner).json()}
    assert types["deepseek"]["model_names"][:1] == ["deepseek-chat"]
    # 已下线的名字不许进表(gte-rerank 无 -v2 于 2026-05-30 下线)
    assert types["dashscope_rerank"]["model_names"][:1] == ["gte-rerank-v2"]
    assert "gte-rerank" not in types["dashscope_rerank"]["model_names"]
    # 豆包要填逐账号自建的接入点 id、vLLM 完全取决于部署方 → 空预设,前端渲染纯输入框
    assert types["doubao"]["model_names"] == [] and types["vllm"]["model_names"] == []
    assert all(
        t["model_names"] for key, t in types.items() if key not in ("doubao", "vllm")
    )

    provider = _create_provider(client, space_id, owner)
    custom = _create_model(
        client, space_id, owner, provider["id"], model_name="公司自研网关-v3"
    )
    assert custom["model_name"] == "公司自研网关-v3"  # 清单里没有也照样收


def test_provider_type_prefill_and_masked_list(client) -> None:
    space_id, owner = _owner(client, "mcprov", "供应商空间")
    base = f"/api/v1/spaces/{space_id}/model-config"

    types = client.get(f"{base}/types", headers=owner).json()
    deepseek = next(t for t in types if t["key"] == "deepseek")
    assert deepseek["default_base_url"] == "https://api.deepseek.com/v1"
    assert deepseek["kinds"] == ["chat"]
    assert any("rerank" in t["key"] for t in types)  # 重排入口在清单里

    # base_url 留空 = 用该供应商默认地址(预填只是便利,不是白名单)
    created = _create_provider(client, space_id, owner, base_url="")
    assert created["base_url"] == "https://api.deepseek.com/v1"
    assert created["key_hint"] == "sk-1****cdef"
    assert created["protocol"] == "openai_compatible"

    unknown = client.post(
        f"{base}/providers", json=_provider_body(provider_type="magic"), headers=owner
    )
    assert unknown.status_code == 422 and "deepseek" in unknown.text

    dup = client.post(
        f"{base}/providers", json=_provider_body(name=created["name"]), headers=owner
    )
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "MODEL_PROVIDER_NAME_TAKEN"

    patched = client.patch(
        f"{base}/providers/{created['id']}",
        json={"api_key": "sk-newsecret99"},
        headers=owner,
    ).json()
    assert patched["key_hint"] == "sk-n****et99" and "sk-newsecret99" not in str(patched)
    patched = client.patch(
        f"{base}/providers/{created['id']}", json={"api_key": ""}, headers=owner
    ).json()
    assert patched["key_hint"] == ""  # 免密钥本地端点(Ollama/vLLM)

    view = client.get(base, headers=owner).json()
    assert [p["id"] for p in view["providers"]] == [created["id"]]
    assert view["providers"][0]["model_count"] == 0
    assert "api_key_encrypted" not in str(view)


def test_key_and_base_url_charset_gate(client) -> None:
    """Key 进 `Authorization` 头、base_url 进 URL:脏字符必须在写入口 422。

    dev 实测踩到的原形状:Key 存成中文后,第一次检索就在 httpx 编头时抛
    UnicodeEncodeError → 500 INTERNAL_ERROR(本该是配置期就能给出的 422)。
    """
    space_id, owner = _owner(client, "mccharset", "字符闸门空间")
    base = f"/api/v1/spaces/{space_id}/model-config"

    for bad in ("短", "sk-abc def", "sk-ab\t中文"):
        denied = client.put(f"{base}/embedding-key", json={"api_key": bad}, headers=owner)
        assert denied.status_code == 422, f"{bad!r} 竟通过:{denied.text}"
        assert "ASCII" in denied.text

    # 首尾空白是粘贴常态:规范化后入库,不作为错误暴露给成员
    ok = client.put(
        f"{base}/embedding-key", json={"api_key": "  sk-goodkey0123456789\n"}, headers=owner
    )
    assert ok.status_code == 200, ok.text
    assert client.get(base, headers=owner).json()["embedding"]["key_hint"] == "sk-g****6789"

    zh = client.post(
        f"{base}/providers",
        json=_provider_body(name="中文厂", base_url="http://中文.example.com/v1"),
        headers=owner,
    )
    assert zh.status_code == 422 and "ASCII" in zh.text, zh.text
    spaced = client.post(
        f"{base}/providers",
        json=_provider_body(name="空格厂", base_url="http://a b.com/v1"),
        headers=owner,
    )
    assert spaced.status_code == 422, spaced.text
    # 供应商名允许中文(只进 model_id 与界面,不进头),但地址必须干净
    named = client.post(
        f"{base}/providers",
        json=_provider_body(name="中文厂", base_url="https://api.deepseek.com/v1"),
        headers=owner,
    )
    assert named.status_code == 201, named.text
    assert named.json()["model_count"] == 0


def test_space_layer_model_rules(client) -> None:
    space_id, owner = _owner(client, "mcrule", "规则空间")
    base = f"/api/v1/spaces/{space_id}/model-config"
    pid = _create_provider(client, space_id, owner)["id"]

    # embedding 内置锁死:空间层不接受该 kind
    emb = client.post(
        f"{base}/models",
        json=_model_body(pid, kind="embedding", dims=1024),
        headers=owner,
    )
    assert emb.status_code == 422 and "embedding" in emb.text

    # chat 必带 context_tokens(OPT-29 进度条的分母)
    no_ctx = client.post(
        f"{base}/models",
        json=_model_body(pid, context_tokens=None),
        headers=owner,
    )
    assert no_ctx.status_code == 422 and "context_tokens" in no_ctx.text

    # kind 与协议必须匹配:对话供应商挂不上 rerank 模型
    mismatch = client.post(
        f"{base}/models", json=_model_body(pid, kind="rerank"), headers=owner
    )
    assert mismatch.status_code == 422

    created = _create_model(client, space_id, owner, pid)
    assert created["model_id"] == f"{created['provider_name']}/deepseek-chat"  # 自动生成 id
    assert created["context_tokens"] == 64000

    rerank_pid = _create_provider(
        client, space_id, owner, provider_type="cohere_rerank", name="重排厂", base_url=""
    )["id"]
    assert rerank_pid
    rr = _create_model(
        client,
        space_id,
        owner,
        rerank_pid,
        kind="rerank",
        model_name="gte-rerank-v2",
        context_tokens=None,
    )
    assert rr["kind"] == "rerank" and rr["context_tokens"] is None

    # 挂到不存在的供应商 → 404
    ghost = client.post(
        f"{base}/models", json=_model_body(str(uuid.uuid4())), headers=owner
    )
    assert ghost.status_code == 404

    # 删供应商 → 其模型一并消失(级联)
    assert client.delete(f"{base}/providers/{pid}", headers=owner).status_code == 204
    view = client.get(base, headers=owner).json()
    assert {m["id"] for m in view["models"]} == {rr["id"]}


def test_cross_space_isolation(client) -> None:
    space_a, owner_a = _owner(client, "msaowner", "空间A")
    space_b, owner_b = _owner(client, "msbowner", "空间B")
    base_a = f"/api/v1/spaces/{space_a}/model-config"
    base_b = f"/api/v1/spaces/{space_b}/model-config"

    provider = _create_provider(client, space_a, owner_a, name="独占厂")
    model = _create_model(client, space_a, owner_a, provider["id"])

    # B 的属主看不到 A 的任何条目,也改不动
    assert client.get(base_b, headers=owner_b).json()["providers"] == []
    assert (
        client.patch(
            f"{base_b}/providers/{provider['id']}", json={"enabled": False}, headers=owner_b
        ).status_code
        == 404
    )
    assert (
        client.delete(f"{base_b}/models/{model['id']}", headers=owner_b).status_code == 404
    )
    # 同名在两个空间各自合法(partial unique 按层分)
    same = client.post(
        f"{base_b}/providers", json=_provider_body(name="独占厂"), headers=owner_b
    )
    assert same.status_code == 201, same.text

    # A 的行仍在原位
    view_a = client.get(base_a, headers=owner_a).json()
    assert [p["name"] for p in view_a["providers"]] == ["独占厂"]
    assert view_a["models"][0]["model_id"] == model["model_id"]


def test_space_default_does_not_clobber_global_default(client) -> None:
    """层谓词回归:空间上新建默认模型不得清掉全局默认(反之亦然)。"""
    space_id, owner = _owner(client, "mscdef", "默认空间")
    base = f"/api/v1/spaces/{space_id}/model-config"
    with get_session_factory()() as db:
        gp = _global_provider(db, "全局厂")
        _global_model(db, gp, "g/chat", is_default=True)
        db.commit()

    pid = _create_provider(client, space_id, owner)["id"]
    mine = _create_model(client, space_id, owner, pid, is_default=True)
    with get_session_factory()() as db:
        global_row = db.scalar(
            select(CustomModel).where(CustomModel.model_id == "g/chat")
        )
        assert global_row is not None and global_row.is_default is True
        space_row = db.get(CustomModel, uuid.UUID(mine["id"]))
        assert space_row is not None and space_row.is_default is True

    # 第二默认顶掉第一默认(只在本空间内互斥)
    second = _create_model(
        client, space_id, owner, pid, model_name="deepseek-reasoner", is_default=True
    )
    view = client.get(base, headers=owner).json()
    defaults = [m["model_id"] for m in view["models"] if m["is_default"]]
    assert defaults == [second["model_id"]]
    with get_session_factory()() as db:
        assert db.scalar(select(CustomModel).where(CustomModel.model_id == "g/chat")).is_default


def _global_provider(db: Session, name: str) -> ModelProvider:
    p = ModelProvider(
        name=name,
        protocol="openai_compatible",
        base_url="https://global.example/v1",
        api_key_encrypted="",
    )
    db.add(p)
    db.flush()
    return p


def _global_model(
    db: Session, provider: ModelProvider, model_id: str, *, is_default: bool = False
) -> CustomModel:
    m = CustomModel(
        model_id=model_id,
        provider_id=provider.id,
        kind="chat",
        model_name=model_id,
        is_default=is_default,
    )
    db.add(m)
    return m


# ---------------------------- rerank 开关 ----------------------------


def test_rerank_switch_roundtrip_and_audit(client) -> None:
    space_id, owner = _owner(client, "mscrank", "重排空间")
    base = f"/api/v1/spaces/{space_id}/model-config"
    assert client.get(base, headers=owner).json()["rerank"] == {
        "enabled": False,
        "default_model": None,
    }
    assert client.put(
        f"{base}/rerank-switch", json={"enabled": True}, headers=owner
    ).status_code == 200
    assert client.get(base, headers=owner).json()["rerank"]["enabled"] is True
    # 开关开着但没有默认 rerank 模型 = 检索时等同关闭(后端允许,前端给黄色提示)
    assert client.get(base, headers=owner).json()["rerank"]["default_model"] is None
    assert "model_setting.rerank.updated" in _audit_actions(space_id)


# ---------------------------- 【测试连接】 ----------------------------


class _ProbeAdapter:
    """假协议适配器:按预设结果或异常回应探活调用。"""

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.fail = fail
        self.calls: list[tuple] = []

    def embed(self, provider, model, api_key, texts, *, semaphore=None, transport=None, usage=None):
        return self._hit(provider.key, model.id, api_key)

    def chat_stream(
        self, provider, model, api_key, messages, temperature, *, transport=None, usage=None
    ):
        # 必须保持生成器语义:真实适配器返回 Generator,探活靠 next()+close()
        self._hit(provider.key, model.id, api_key)
        yield "ok"

    def chat_stream_tools(self, *a, **kw):  # pragma: no cover - 探活不用
        raise AssertionError("not used")

    def rerank(self, provider, model, api_key, query, documents, top_n, *, transport=None):
        return self._hit(provider.key, model.id, api_key)

    def _hit(self, *args):
        self.calls.append(args)
        if self.fail is not None:
            raise self.fail
        return []


def _patch_protocol(monkeypatch: pytest.MonkeyPatch, adapter: _ProbeAdapter) -> None:
    monkeypatch.setattr("app.application.service.space_models.get_protocol", lambda key: adapter)
    monkeypatch.setattr(
        "app.application.service.space_models.get_rerank_protocol", lambda key: adapter
    )


def test_probes_success_and_upstream_reason_mapping(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    space_id, owner = _owner(client, "msctest", "探活空间")
    base = f"/api/v1/spaces/{space_id}/model-config"
    adapter = _ProbeAdapter()
    _patch_protocol(monkeypatch, adapter)

    # embedding Key 探活:候选明文 Key 直接带上游,不需要先落库
    ok = client.post(
        f"{base}/embedding-key/test", json={"api_key": "sk-candidate"}, headers=owner
    )
    assert ok.status_code == 200 and ok.json()["ok"] is True
    assert adapter.calls[-1][:2] == ("builtin-dashscope", "builtin/text-embedding-v3")
    assert adapter.calls[-1][2] == "sk-candidate"  # 明文只在探活这一跳

    pid = _create_provider(client, space_id, owner)["id"]
    no_model = client.post(f"{base}/providers/{pid}/test", headers=owner)
    assert no_model.status_code == 422 and "先添加模型" in no_model.text

    model = _create_model(client, space_id, owner, pid)
    assert client.post(f"{base}/models/{model['id']}/test", headers=owner).json() == {"ok": True}
    # 已存密钥走解密:适配器拿到的是明文,不是 enc:v1: 密文
    assert not adapter.calls[-1][2].startswith("enc:v1:")

    for status, reason in [
        (401, "auth"),
        (403, "auth"),
        (404, "model_name"),
        (429, "network"),
    ]:
        _patch_protocol(
            monkeypatch,
            _ProbeAdapter(fail=ModelCallError("演示厂", status, "upstream says no")),
        )
        resp = client.post(f"{base}/models/{model['id']}/test", headers=owner)
        assert resp.status_code == 424, resp.text
        err = resp.json()["error"]
        assert err["code"] == "UPSTREAM_MODEL_ERROR"
        assert err["details"]["reason"] == reason

    # F1:百炼对"模型已下线"回 403 AccessDenied,与"Key 无权限"同状态码 —— reason 仍是 auth
    # (契约枚举不动),但措辞必须两条都指到,否则用户会去查一把没问题的密钥。
    def _message_for(status: int) -> str:
        _patch_protocol(
            monkeypatch, _ProbeAdapter(fail=ModelCallError("演示厂", status, "no"))
        )
        body = client.post(f"{base}/models/{model['id']}/test", headers=owner).json()
        return body["error"]["message"]

    assert "鉴权" in _message_for(401)
    assert "不可用" in _message_for(403)
    assert "upstream" not in _message_for(403)  # 厂商响应体不外泄

    _patch_protocol(monkeypatch, _ProbeAdapter(fail=httpx.ConnectError("no route")))
    resp = client.post(f"{base}/providers/{pid}/test", headers=owner)
    assert resp.status_code == 424
    assert resp.json()["error"]["details"]["reason"] == "network"

    # 失败也要留痕:审计行写在独立会话里,才扛得住抛错带来的请求事务 rollback
    details = _model_tested_details(space_id)
    assert {"ok": True} in details
    assert {"ok": False, "reason": "auth"} in details, details
    assert {"ok": False, "reason": "model_name"} in details, details
    assert {"ok": False, "reason": "network"} in details, details


def _model_tested_details(space_id: str) -> list[dict]:
    with get_session_factory()() as db:
        rows = db.scalars(
            select(AuditLog).where(
                AuditLog.space_id == uuid.UUID(space_id),
                AuditLog.action == "model.tested",
            )
        ).all()
        return [dict(row.detail or {}) for row in rows]


def _prod_settings(monkeypatch: pytest.MonkeyPatch, *, allow_private: bool = False) -> None:
    """把环境翻成 prod(闸门只在 prod 生效:dev 要能连本地 Ollama/vLLM)。"""
    monkeypatch.setattr(
        "app.application.service.space_models.get_settings",
        lambda: SimpleNamespace(env="prod", allow_private_model_urls=allow_private),
    )


def test_space_base_url_ssrf_gate(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """空间自配 base_url = 用户可控出站地址:prod 必须挡内网,否则整个平台是 SSRF 探头。"""
    space_id, owner = _owner(client, "mcssrf", "闸门空间")
    base = f"/api/v1/spaces/{space_id}/model-config"

    # dev:本机地址放行(预置的 Ollama/vLLM 类型就是 127.0.0.1)
    dev = client.post(
        f"{base}/providers",
        json=_provider_body(base_url="http://127.0.0.1:11434/v1"),
        headers=owner,
    )
    assert dev.status_code == 201, dev.text

    _prod_settings(monkeypatch)
    for bad in (
        "http://127.0.0.1:11434/v1",
        "http://169.254.169.254/latest/meta-data/",
        "http://localhost:9200/",
        "http://10.0.0.7:11434/v1",
    ):
        denied = client.post(
            f"{base}/providers",
            json=_provider_body(name=f"内网{uuid.uuid4().hex[:6]}", base_url=bad),
            headers=owner,
        )
        assert denied.status_code == 422, f"{bad} 竟通过: {denied.text}"
        assert "不安全" in denied.text
    # PATCH 同样过闸门(不能先建公网再改成内网)
    pid = dev.json()["id"]
    patched = client.patch(
        f"{base}/providers/{pid}", json={"base_url": "http://127.0.0.1:11434/v1"}, headers=owner
    )
    assert patched.status_code == 422

    # 显式豁免(内网自建网关的私有化部署)与公网地址在 prod 都可用
    _prod_settings(monkeypatch, allow_private=True)
    exempt = client.post(
        f"{base}/providers",
        json=_provider_body(name=f"内网网关{uuid.uuid4().hex[:6]}", base_url="http://10.0.0.7:11434/v1"),
        headers=owner,
    )
    assert exempt.status_code == 201, exempt.text
    _prod_settings(monkeypatch)
    public = client.post(
        f"{base}/providers",
        json=_provider_body(name=f"公网厂{uuid.uuid4().hex[:6]}", base_url="https://api.deepseek.com/v1"),
        headers=owner,
    )
    assert public.status_code == 201, public.text

    # 全局层不套本闸门:运维给的地址按运维的判断办(私有化内网网关是合法形态)
    root = _register(client, "mcssrfroot")
    with get_session_factory()() as db:
        root_user = db.get(User, uuid.UUID(root["user"]["id"]))
        assert root_user is not None
        root_user.is_superuser = True
        db.commit()
    global_resp = client.post(
        "/api/v1/admin/model-providers",
        json={
            "name": f"运维内网{uuid.uuid4().hex[:6]}",
            "base_url": "http://10.0.0.7:11434/v1",
            "protocol": "openai_compatible",
            "api_key": "sk-1234567890abcdef",
        },
        headers=_auth(root),
    )
    assert global_resp.status_code == 201, global_resp.text
