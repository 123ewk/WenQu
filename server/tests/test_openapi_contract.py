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
    "/api/v1/spaces",
    "/api/v1/spaces/{space_id}",
    "/api/v1/spaces/{space_id}/members",
    "/api/v1/spaces/{space_id}/members/{member_user_id}",
    "/api/v1/spaces/{space_id}/leave",
    "/api/v1/spaces/{space_id}/audit-logs",
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
