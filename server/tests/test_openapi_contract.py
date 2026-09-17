"""OpenAPI 契约(基准 03 契约测试 1/2 类):

1. 关键端点存在性;2. 提交入库的 docs/api/openapi.json 与代码生成结果 drift 检查
(改动路由后必须 `make server-openapi` 重新导出,否则 CI 红)。
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
COMMITTED_CONTRACT = REPO_ROOT / "docs" / "api" / "openapi.json"

REQUIRED_PATHS = {
    "/api/v1/auth/register",
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",
    "/api/v1/auth/logout",
    "/api/v1/auth/switch-space",
    "/api/v1/users/me",
    "/api/v1/users/me/password",
    "/api/v1/users/me/avatar",
    "/api/v1/models",
    "/api/v1/spaces/{space_id}/ingestion-progress",
    "/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{document_id}/reparse",
    "/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{document_id}/chunks/{chunk_id}",
    "/api/v1/spaces",
    "/api/v1/spaces/{space_id}",
    "/api/v1/spaces/{space_id}/members",
    "/api/v1/spaces/{space_id}/members/{member_user_id}",
    "/api/v1/spaces/{space_id}/leave",
    "/api/v1/spaces/{space_id}/audit-logs",
    "/api/v1/spaces/{space_id}/knowledge-bases",
    "/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}",
    "/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents",
    "/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{document_id}",
    "/api/v1/chunks/preview",
    "/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/{document_id}/chunks",
    "/api/v1/spaces/{space_id}/retrieval/search",
    "/api/v1/spaces/{space_id}/ask",
    "/api/v1/spaces/{space_id}/conversations",
    "/api/v1/spaces/{space_id}/conversations/{conversation_id}",
    "/api/v1/spaces/{space_id}/conversations/{conversation_id}/messages",
    "/api/v1/spaces/{space_id}/api-keys",
    "/api/v1/spaces/{space_id}/api-keys/{key_id}",
    "/health",
    "/system/info",
}

REQUIRED_ERROR_CODES = {
    "AUTH_REQUIRED",
    "FORBIDDEN",
    "INVALID_CREDENTIALS",
    "TOKEN_EXPIRED",
    "REFRESH_TOKEN_INVALID",
    "USERNAME_TAKEN",
    "USER_NOT_FOUND",
    "MEMBER_ALREADY",
    "SPACE_NOT_FOUND",
    "MEMBER_NOT_FOUND",
    "KB_NOT_FOUND",
    "KB_NAME_TAKEN",
    "DOCUMENT_NOT_FOUND",
    "UNSUPPORTED_FORMAT",
    "FILE_TOO_LARGE",
    "CONVERSATION_NOT_FOUND",
    "MESSAGE_NOT_FOUND",
    "MODEL_CALL_FAILED",
    "MODEL_NOT_CONFIGURED",
    "DOCUMENT_BUSY",
    "CHUNK_NOT_FOUND",
    "CREDENTIAL_KEY_MISSING",
    "CREDENTIAL_DECRYPT_FAILED",
    "API_KEY_INVALID",
    "API_KEY_CAPABILITY_DENIED",
    "API_KEY_SCOPE_DENIED",
    "VALIDATION_ERROR",
    "INTERNAL_ERROR",
    "NOT_FOUND",
}


def generated_paths() -> set[str]:
    return set(create_app().openapi()["paths"])


def test_required_endpoints_exist() -> None:
    paths = generated_paths()
    missing = REQUIRED_PATHS - paths
    assert not missing, f"契约要求的端点缺失: {missing}"


def test_error_codes_registered() -> None:
    """所有稳定字符串码必须在 ErrorCode 注册(错误码注册扫描,基准 02)。"""
    from app.core.errors import ErrorCode

    registered = {
        value[1]
        for name, value in vars(ErrorCode).items()
        if not name.startswith("_") and isinstance(value, tuple)
    }
    missing = REQUIRED_ERROR_CODES - registered
    assert not missing, f"错误码未注册: {missing}"


def test_committed_openapi_in_sync() -> None:
    assert COMMITTED_CONTRACT.exists(), (
        "docs/api/openapi.json 不存在,请运行 make server-openapi 导出"
    )
    committed = json.loads(COMMITTED_CONTRACT.read_text(encoding="utf-8"))
    assert set(committed["paths"]) == generated_paths(), (
        "openapi.json 与代码不一致(drift),请运行 make server-openapi 重新导出"
    )


def test_avatar_get_declares_image_content_type() -> None:
    """读头像的响应必须声明图片类型:声明成 application/json 会让前端类型生成失效。"""
    spec = create_app().openapi()
    responses = spec["paths"]["/api/v1/users/me/avatar"]["get"]["responses"]
    content = responses["200"].get("content", {})
    assert "application/json" not in content, f"读头像不该声明 JSON: {list(content)}"
    assert any(ct.startswith("image/") for ct in content), f"应声明图片类型: {list(content)}"


def test_audit_result_is_enum_in_contract() -> None:
    """result 必须是机器可读枚举,前端才能生成类型而不手写字面量。"""
    spec = create_app().openapi()
    result = spec["components"]["schemas"]["AuditLogOut"]["properties"]["result"]
    enum_values = result.get("enum") or []
    assert set(enum_values) == {"success", "denied"}, f"result 枚举缺失: {result}"


# ---------------------------- OPT-8:CI 门禁(双向镜像 + 错误壳扫描) ----------------------------


def test_required_paths_mirror_generated() -> None:
    """REQUIRED_PATHS 必须与实际契约**双向一致**。

    新增/删除路由而不改本清单 → CI 红(强制感知契约变化,不许悄悄上线新端点);
    清单里的路径从契约中消失 → 同样红。
    """
    missing = REQUIRED_PATHS - generated_paths()
    assert not missing, f"清单声明的路径不存在: {missing}"
    undeclared = generated_paths() - REQUIRED_PATHS
    assert not undeclared, f"契约新增路径未登记进 REQUIRED_PATHS: {undeclared}"


def test_error_codes_mirror_registered() -> None:
    """REQUIRED_ERROR_CODES 与 ErrorCode 注册表**双向一致**。

    新增 ErrorCode 成员而不登记 → CI 红(新错误码必须过契约测试这一关)。
    """
    from app.core.errors import ErrorCode

    registered = {
        value[1]
        for name, value in vars(ErrorCode).items()
        if not name.startswith("_") and isinstance(value, tuple)
    }
    assert registered == REQUIRED_ERROR_CODES, (
        f"未登记: {registered - REQUIRED_ERROR_CODES}; 已失效: {REQUIRED_ERROR_CODES - registered}"
    )


def test_unified_error_shell_on_protected_endpoints() -> None:
    """所有需认证端点(抽样)未登录时必须返回统一错误壳 + AUTH_REQUIRED。

    这是错误壳的漂移门禁:谁改了错误响应结构/兜底码,这里全量爆出来。
    """
    import uuid as _uuid

    space = str(_uuid.uuid4())
    protected = [
        ("GET", "/api/v1/spaces"),
        ("GET", "/api/v1/users/me"),
        ("GET", f"/api/v1/spaces/{space}/knowledge-bases"),
        ("GET", f"/api/v1/spaces/{space}/api-keys"),
        ("GET", f"/api/v1/spaces/{space}/audit-logs"),
        ("GET", f"/api/v1/spaces/{space}/ingestion-progress"),
        ("POST", f"/api/v1/spaces/{space}/retrieval/search"),
        ("POST", f"/api/v1/spaces/{space}/ask"),
        ("POST", f"/api/v1/spaces/{space}/knowledge-bases"),
        ("DELETE", f"/api/v1/spaces/{space}/api-keys/{_uuid.uuid4()}"),
    ]
    client = TestClient(create_app(), raise_server_exceptions=False)
    for method, path in protected:
        resp = client.request(method, path)
        assert resp.status_code == 401, f"{method} {path} -> {resp.status_code}"
        body = resp.json()
        assert body["success"] is False, f"{method} {path} 缺 success:false"
        err = body["error"]
        assert err["code"] == "AUTH_REQUIRED", f"{method} {path} 兜底码异常: {err}"
        assert isinstance(err["message"], str) and err["message"]


def test_public_endpoints_stay_public() -> None:
    """健康检查与系统信息不要求认证(容器探针/前端启动探测依赖它)。"""
    client = TestClient(create_app(), raise_server_exceptions=False)
    assert client.get("/health").status_code == 200
    assert client.get("/system/info").status_code == 200
