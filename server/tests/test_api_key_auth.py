"""API Key 路由授权表单测(OPT-6):fail-closed 语义。

测试意图:
- 表内路由 → 返回对应能力;
- 表外路由(用户/空间/成员/审计/分块预览等)→ None = 一律拒绝;
- 方法不匹配(用 Key GET 只读的 /ask)→ None;
- 路径段数不同的相似路径 → None(模板匹配不能被追加段绕过);
- 授权表与实际注册路由保持一致:表里登记的每条 (方法,路径) 都必须真实存在,
  防止"表里写了个不存在的路由"这种假覆盖(用 OpenAPI 契约核对)。
"""

from __future__ import annotations

import uuid

from app.api.api_key_auth import resolve_route_capability
from app.domain.enums import ApiCapability

_SPACE = str(uuid.uuid4())
_KB = str(uuid.uuid4())
_DOC = str(uuid.uuid4())


def test_ask_maps_to_chat() -> None:
    cap = resolve_route_capability(f"/api/v1/spaces/{_SPACE}/ask", "POST")
    assert cap == ApiCapability.CHAT


def test_search_maps_to_chat() -> None:
    cap = resolve_route_capability(f"/api/v1/spaces/{_SPACE}/retrieval/search", "POST")
    assert cap == ApiCapability.CHAT


def test_document_delete_maps_to_documents() -> None:
    cap = resolve_route_capability(
        f"/api/v1/spaces/{_SPACE}/knowledge-bases/{_KB}/documents/{_DOC}", "DELETE"
    )
    assert cap == ApiCapability.DOCUMENTS


def test_document_upload_maps_to_documents() -> None:
    cap = resolve_route_capability(
        f"/api/v1/spaces/{_SPACE}/knowledge-bases/{_KB}/documents", "POST"
    )
    assert cap == ApiCapability.DOCUMENTS


def test_reparse_maps_to_documents() -> None:
    cap = resolve_route_capability(
        f"/api/v1/spaces/{_SPACE}/knowledge-bases/{_KB}/documents/{_DOC}/reparse", "POST"
    )
    assert cap == ApiCapability.DOCUMENTS


def test_unregistered_routes_denied() -> None:
    """表外路由:用户资料、成员管理、审计、会话管理、空间删除……一律 None。"""
    denied = [
        ("/api/v1/users/me", "GET"),
        ("/api/v1/users/me/password", "POST"),
        (f"/api/v1/spaces/{_SPACE}", "DELETE"),
        (f"/api/v1/spaces/{_SPACE}/members", "POST"),
        (f"/api/v1/spaces/{_SPACE}/audit-logs", "GET"),
        (f"/api/v1/spaces/{_SPACE}/conversations", "GET"),
        ("/api/v1/chunks/preview", "POST"),
        ("/api/v1/models", "GET"),
        ("/api/v1/auth/login", "POST"),
    ]
    for path, method in denied:
        assert resolve_route_capability(path, method) is None, f"{method} {path}"


def test_method_mismatch_denied() -> None:
    """路径在表内但方法不对:一律 None(不能只看路径)。"""
    space = f"/api/v1/spaces/{_SPACE}"
    assert resolve_route_capability(f"{space}/ask", "GET") is None
    assert resolve_route_capability(f"{space}/retrieval/search", "GET") is None
    kb_docs = f"{space}/knowledge-bases/{_KB}/documents"
    assert resolve_route_capability(kb_docs, "DELETE") is None
    assert resolve_route_capability(f"{kb_docs}/{_DOC}", "POST") is None


def test_extra_segment_not_matched() -> None:
    """往表内路径后面追加段,不能借道放行。"""
    space = f"/api/v1/spaces/{_SPACE}"
    assert resolve_route_capability(f"{space}/ask/extra", "POST") is None
    assert (
        resolve_route_capability(f"{space}/knowledge-bases/{_KB}/documents/extra", "POST")
        is None
    )


def test_missing_segment_not_matched() -> None:
    assert (
        resolve_route_capability(
            f"/api/v1/spaces/{_SPACE}/knowledge-bases/{_KB}/documents/{_DOC}/chunks/x/y",
            "GET",
        )
        is None
    )


def test_granted_routes_all_exist_in_contract() -> None:
    """授权表自检:表里每条路由都必须真实注册在 OpenAPI 契约里。

    这是防"假覆盖"的门禁 —— 表写了、路由没实现(或路径拼错),Key 用户会拿到
    404 之外的困惑行为;这条测试让这种漂移在 CI 就爆出来。
    """
    from app.api.api_key_auth import _ROUTE_CAPABILITIES
    from app.main import create_app

    registered = {
        (method.upper(), path)
        for path, methods in create_app().openapi()["paths"].items()
        for method in methods
        if method in ("get", "post", "patch", "put", "delete")
    }
    for allowed in _ROUTE_CAPABILITIES.values():
        for template, verb in allowed:
            # 把模板还原成"段数相同的任意 UUID"去和注册表比对:
            # 模板 {x} 段在注册表里也是 {x} 段,按段结构比较
            match = any(
                verb == reg_verb and _same_shape(template, reg_path)
                for reg_verb, reg_path in registered
            )
            assert match, f"授权表里的路由未注册: {verb} {template}"


def _same_shape(template: str, actual: str) -> bool:
    t = template.strip("/").split("/")
    a = actual.strip("/").split("/")
    if len(t) != len(a):
        return False
    return all(
        (t_seg.startswith("{") and a_seg.startswith("{"))
        or (t_seg == a_seg and not t_seg.startswith("{"))
        for t_seg, a_seg in zip(t, a, strict=True)
    )
