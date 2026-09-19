"""API Key 集成测试(真 Postgres,OPT-6):对应 Go-Live 冒烟第 5 条。

「建 API Key → KB 范围限定生效 → 范围外被拒 → 审计有记录」的完整 HTTP 链路:
签发只回显一次明文、能力级授权、范围收窄、吊销即失效、创建者移除即失效、审计留痕。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from tests.conftest import register as _register

pytestmark = pytest.mark.integration


def _uname(prefix: str) -> str:
    """集成测试共用一个库,用户名必须全局唯一。"""
    return f"{prefix}{uuid.uuid4().hex[:8]}"


def _setup_space_with_two_kbs(client) -> tuple[dict, str, str, str]:
    """owner 建空间 + 两个 KB;返回 (owner, space_id, kb_a_id, kb_b_id)。"""
    owner = _register(client, _uname("keyowner"))
    auth = {"Authorization": f"Bearer {owner['access_token']}"}
    space_id = client.post("/api/v1/spaces", json={"name": "密钥空间"}, headers=auth).json()[
        "id"
    ]
    base = f"/api/v1/spaces/{space_id}/knowledge-bases"
    kb_a = client.post(base, json={"name": "范围库A"}, headers=auth).json()
    kb_b = client.post(base, json={"name": "范围库B"}, headers=auth).json()
    return owner, space_id, kb_a["id"], kb_b["id"]


def _create_key(
    client,
    auth: dict,
    space_id: str,
    capabilities: list[str],
    kb_ids: list[str] | None = None,
    name: str = "ci-key",
) -> dict:
    resp = client.post(
        f"/api/v1/spaces/{space_id}/api-keys",
        json={
            "name": name,
            "description": "",
            "capabilities": capabilities,
            "kb_ids": kb_ids or [],
        },
        headers=auth,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _key_header(plaintext: str) -> dict:
    return {"X-API-Key": plaintext}


def test_key_lifecycle_plaintext_shown_once(client) -> None:
    owner, space_id, kb_a, _kb_b = _setup_space_with_two_kbs(client)
    auth = {"Authorization": f"Bearer {owner['access_token']}"}
    created = _create_key(client, auth, space_id, ["chat"], [kb_a])

    assert created["plaintext"].startswith("sk-")
    assert created["key_hint"] != created["plaintext"]
    assert created["capabilities"] == ["chat"]
    assert created["kb_ids"] == [kb_a]
    assert created["revoked_at"] is None

    listed = client.get(f"/api/v1/spaces/{space_id}/api-keys", headers=auth).json()
    assert len(listed) == 1
    assert "plaintext" not in listed[0]  # 明文绝不二次出现
    assert listed[0]["key_hint"] == created["key_hint"]

    # 未知能力名 → 422
    resp = client.post(
        f"/api/v1/spaces/{space_id}/api-keys",
        json={"name": "bad", "capabilities": ["root"]},
        headers=auth,
    )
    assert resp.status_code == 422


def test_key_requires_editor(client) -> None:
    owner, space_id, _kb_a, _kb_b = _setup_space_with_two_kbs(client)
    auth = {"Authorization": f"Bearer {owner['access_token']}"}
    viewer_name = _uname("keyviewer")
    viewer = _register(client, viewer_name)
    viewer_auth = {"Authorization": f"Bearer {viewer['access_token']}"}
    client.post(
        f"/api/v1/spaces/{space_id}/members",
        json={"username": viewer_name, "role": 10},
        headers=auth,
    )
    resp = client.post(
        f"/api/v1/spaces/{space_id}/api-keys",
        json={"name": "v", "capabilities": ["chat"]},
        headers=viewer_auth,
    )
    assert resp.status_code == 403
    # Viewer 可以列表(Viewer+)
    assert client.get(f"/api/v1/spaces/{space_id}/api-keys", headers=viewer_auth).status_code == 200


def test_unauthenticated_and_invalid_keys(client) -> None:
    _owner, space_id, _kb_a, _kb_b = _setup_space_with_two_kbs(client)
    url = f"/api/v1/spaces/{space_id}/retrieval/search"

    # 无凭据 → 401
    assert client.post(url, json={"query": "q"}).status_code == 401
    # 纯垃圾 Key → 401 API_KEY_INVALID
    resp = client.post(url, json={"query": "q"}, headers=_key_header("sk-live-garbage"))
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "API_KEY_INVALID"
    # JWT 格式错误放在 Key 头里同样 401
    assert client.post(url, json={"query": "q"}, headers=_key_header("")).status_code == 401


def test_key_on_jwt_only_route_denied(client) -> None:
    """仅 JWT 的路由(如用户资料)不识别 Key:带 Key 访问 → 401(与未登录同构)。

    「路由未登记授权表 → 403 route_not_grantable」这一 fail-closed 分支针对的是
    以后新挂到 get_current_actor 却忘了进表的路由(防误放开),当前无此路径。
    """
    owner, space_id, _kb_a, _kb_b = _setup_space_with_two_kbs(client)
    auth = {"Authorization": f"Bearer {owner['access_token']}"}
    created = _create_key(client, auth, space_id, ["chat", "documents"])
    resp = client.get("/api/v1/users/me", headers=_key_header(created["plaintext"]))
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTH_REQUIRED"


def test_search_capability_and_scope(client, no_model_keys) -> None:
    """chat 能力:/search 范围内放行;范围外 403;未授 chat 能力 → 403。"""
    owner, space_id, kb_a, kb_b = _setup_space_with_two_kbs(client)
    auth = {"Authorization": f"Bearer {owner['access_token']}"}
    scoped = _create_key(client, auth, space_id, ["chat"], [kb_a])
    nocap = _create_key(client, auth, space_id, ["documents"], name="doc-only")
    url = f"/api/v1/spaces/{space_id}/retrieval/search"

    # 范围外(请求只含范围外 KB)→ 403 API_KEY_SCOPE_DENIED(发生在检索之前)
    resp = client.post(
        url, json={"query": "q", "kb_ids": [kb_b]}, headers=_key_header(scoped["plaintext"])
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "API_KEY_SCOPE_DENIED"

    # 没有 chat 能力 → 403 API_KEY_CAPABILITY_DENIED
    resp = client.post(url, json={"query": "q"}, headers=_key_header(nocap["plaintext"]))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "API_KEY_CAPABILITY_DENIED"

    # 范围内 → 放行到服务层(此处 embedder 未 stub,模型未配置 → 503 MODEL_NOT_CONFIGURED;
    # 能到达这一步即证明 Key 已通过认证+授权+范围三道闸)
    resp = client.post(
        url,
        json={"query": "q", "kb_ids": [kb_a]},
        headers=_key_header(scoped["plaintext"]),
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "MODEL_NOT_CONFIGURED"

    # 请求 kb_ids 与范围取交集:A+B → 只留 A(不整体失败)
    resp = client.post(
        url,
        json={"query": "q", "kb_ids": [kb_a, kb_b]},
        headers=_key_header(scoped["plaintext"]),
    )
    assert resp.status_code == 503  # 同上,交集非空即放行

    # 全库 Key(空范围)→ 不限制
    allkb = _create_key(client, auth, space_id, ["chat"], [], name="all-key")
    resp = client.post(
        url, json={"query": "q", "kb_ids": [kb_b]}, headers=_key_header(allkb["plaintext"])
    )
    assert resp.status_code == 503


def test_search_in_scope_returns_hits_with_stub_embedder(client) -> None:
    """范围 + 能力 + 认证三道闸全过后,真库检索返回命中(带假 embedder)。"""
    import app.main as main_module
    from app.api.deps import get_embedding_gateway

    class StubEmbedder:
        def embed(self, texts, model_id=None):
            out = []
            for t in texts:
                v = [0.0] * 1024
                v[0 if "检索" in t else 1] = 1.0
                out.append(v)
            return out

    created = main_module.create_app()
    created.dependency_overrides[get_embedding_gateway] = StubEmbedder

    owner, space_id, kb_a, _kb_b = _setup_space_with_two_kbs(client)
    auth = {"Authorization": f"Bearer {owner['access_token']}"}

    # 上传真实文档并手动置为 completed + 写入向量/全文(与检索集成测试同法)
    from sqlalchemy import func

    from app.core.db import get_session_factory
    from app.domain.enums import DocumentStatus
    from app.domain.models import Chunk, Document

    resp = client.post(
        f"/api/v1/spaces/{space_id}/knowledge-bases/{kb_a}/documents",
        files={"file": ("检索说明.md", "混合检索算法说明".encode(), "text/markdown")},
        headers=auth,
    )
    assert resp.status_code == 201, resp.text
    doc_id = resp.json()["id"]
    with get_session_factory()() as db:
        doc = db.get(Document, uuid.UUID(doc_id))
        assert doc is not None
        doc.status = DocumentStatus.COMPLETED
        db.add(
            Chunk(
                document_id=doc.id,
                space_id=doc.space_id,
                seq=0,
                content="混合检索算法通过 RRF 融合两路排名。",
                embedding=[1.0] + [0.0] * 1023,
                tsv=func.to_tsvector("simple", "混合 检索 算法 RRF 融合"),
            )
        )
        db.commit()

    key = _create_key(client, auth, space_id, ["chat"], [kb_a])
    with TestClient(created, raise_server_exceptions=False) as overridden:
        resp = overridden.post(
            f"/api/v1/spaces/{space_id}/retrieval/search",
            json={"query": "检索算法"},
            headers=_key_header(key["plaintext"]),
        )
        assert resp.status_code == 200, resp.text
        hits = resp.json()
        assert hits and "混合检索" in hits[0]["content"]

        # last_used_at 被更新
        listed = client.get(f"/api/v1/spaces/{space_id}/api-keys", headers=auth).json()
        mine = next(k for k in listed if k["id"] == key["id"])
        assert mine["last_used_at"] is not None


def test_documents_capability_flow(client) -> None:
    """documents 能力:范围内上传/查看放行,范围外 403;上传后审计有 document.uploaded。"""
    owner, space_id, kb_a, kb_b = _setup_space_with_two_kbs(client)
    auth = {"Authorization": f"Bearer {owner['access_token']}"}
    key = _create_key(client, auth, space_id, ["documents"], [kb_a])
    headers = {**_key_header(key["plaintext"])}
    base = f"/api/v1/spaces/{space_id}/knowledge-bases"

    # 范围外上传 → 403
    resp = client.post(
        f"{base}/{kb_b}/documents",
        files={"file": ("x.md", b"x")},
        headers=headers,
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "API_KEY_SCOPE_DENIED"

    # 范围内上传 → 201
    resp = client.post(
        f"{base}/{kb_a}/documents",
        files={"file": ("key上传.md", "# 内容".encode(), "text/markdown")},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    doc_id = resp.json()["id"]

    # 范围内文档读 / 范围外 KB 列表过滤
    assert client.get(f"{base}/{kb_a}/documents/{doc_id}", headers=headers).status_code == 200
    kbs = client.get(base, headers=headers).json()
    assert [kb["id"] for kb in kbs] == [kb_a]  # 只看得到范围库A

    # chat 能力的 Key 不能碰文档接口
    chat_key = _create_key(client, auth, space_id, ["chat"], name="chat-only")
    resp = client.get(
        f"{base}/{kb_a}/documents", headers=_key_header(chat_key["plaintext"])
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "API_KEY_CAPABILITY_DENIED"

    # 审计:Key 以创建者身份留下 document.uploaded
    logs = client.get(f"/api/v1/spaces/{space_id}/audit-logs", headers=auth).json()["items"]
    assert any(item["action"] == "document.uploaded" for item in logs)


def test_ask_with_key_scope(client, no_model_keys) -> None:
    """/ask:范围内放行进入 SSE(模型未配置 → 流内 error 事件);范围外 403。"""
    owner, space_id, kb_a, kb_b = _setup_space_with_two_kbs(client)
    auth = {"Authorization": f"Bearer {owner['access_token']}"}
    key = _create_key(client, auth, space_id, ["chat"], [kb_a])
    url = f"/api/v1/spaces/{space_id}/ask"

    resp = client.post(
        url, json={"question": "q", "kb_ids": [kb_b]}, headers=_key_header(key["plaintext"])
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "API_KEY_SCOPE_DENIED"

    # 范围内:SSE 开始(200 + event-stream);模型未配置在流内报 error
    resp = client.post(url, json={"question": "q"}, headers=_key_header(key["plaintext"]))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert '"type": "error"' in resp.text or '"type":"error"' in resp.text


def test_revoke_and_creator_removal_kill_key(client) -> None:
    """吊销即 401;创建者被移出空间即 401;审计有 api_key.revoked 与拒绝记录。"""
    owner, space_id, _kb_a, _kb_b = _setup_space_with_two_kbs(client)
    auth = {"Authorization": f"Bearer {owner['access_token']}"}

    # editor 建 Key,随后被移出空间
    editor_name = _uname("keyeditor")
    editor = _register(client, editor_name)
    editor_auth = {"Authorization": f"Bearer {editor['access_token']}"}
    client.post(
        f"/api/v1/spaces/{space_id}/members",
        json={"username": editor_name, "role": 20},
        headers=auth,
    )
    editor_key = _create_key(client, editor_auth, space_id, ["chat"])
    url = f"/api/v1/spaces/{space_id}/retrieval/search"
    resp = client.post(url, json={"query": "q"}, headers=_key_header(editor_key["plaintext"]))
    assert resp.status_code in (200, 503)  # 通过认证+授权(到模型闸之前都算放行)

    client.delete(
        f"/api/v1/spaces/{space_id}/members/{editor['user']['id']}", headers=auth
    )
    resp = client.post(url, json={"query": "q"}, headers=_key_header(editor_key["plaintext"]))
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "API_KEY_INVALID"

    # 吊销 → 立即 401
    owner_key = _create_key(client, auth, space_id, ["chat"])
    resp = client.delete(
        f"/api/v1/spaces/{space_id}/api-keys/{owner_key['id']}", headers=auth
    )
    assert resp.status_code == 204
    resp = client.post(url, json={"query": "q"}, headers=_key_header(owner_key["plaintext"]))
    assert resp.status_code == 401

    # 审计语义:吊销动作入审计;吊销/失效 Key 的使用是 401(凭据问题,按噪声控制
    # 不入审计);而 403 的授权拒绝(范围外)必须留下 access.denied —— 冒烟第 5 条
    scoped_key = _create_key(client, auth, space_id, ["chat"], [_kb_a])
    client.post(
        url, json={"query": "q", "kb_ids": [_kb_b]}, headers=_key_header(scoped_key["plaintext"])
    )
    logs = client.get(f"/api/v1/spaces/{space_id}/audit-logs", headers=auth).json()["items"]
    actions = {item["action"] for item in logs}
    assert "api_key.revoked" in actions
    assert any(
        item["action"] == "access.denied" and item["result"] == "denied" for item in logs
    )


def test_key_hint_stable_and_cross_space_isolation(client) -> None:
    """别的空间的 Key id 不可见(404),hint 不含完整明文。"""
    owner, space_id, _kb_a, _kb_b = _setup_space_with_two_kbs(client)
    auth = {"Authorization": f"Bearer {owner['access_token']}"}
    key = _create_key(client, auth, space_id, ["chat"])

    other = _register(client, _uname("keyother"))
    other_auth = {"Authorization": f"Bearer {other['access_token']}"}
    other_space = client.post(
        "/api/v1/spaces", json={"name": "别的空间"}, headers=other_auth
    ).json()["id"]

    resp = client.delete(
        f"/api/v1/spaces/{other_space}/api-keys/{key['id']}", headers=other_auth
    )
    assert resp.status_code == 404  # 防枚举:既不暴露存在性也不允许跨空间操作

    assert key["key_hint"].startswith("sk-") and "****" in key["key_hint"]


def test_create_key_validation_missing_name(client) -> None:
    """name 必填;capabilities/kb_ids 有默认(可省略),未知能力名另测。"""
    owner, space_id, _kb_a, _kb_b = _setup_space_with_two_kbs(client)
    auth = {"Authorization": f"Bearer {owner['access_token']}"}
    resp = client.post(
        f"/api/v1/spaces/{space_id}/api-keys",
        json={"capabilities": ["chat"]},
        headers=auth,
    )
    assert resp.status_code == 422
