"""OpenAPI 契约(基准 03 契约测试 1/2 类):

1. 关键端点存在性;2. 提交入库的 docs/api/openapi.json 与代码生成结果 drift 检查
(改动路由后必须 `make server-openapi` 重新导出,否则 CI 红)。
"""

from __future__ import annotations

import json
from pathlib import Path

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
