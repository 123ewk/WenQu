"""双错误码体系(基准 02)。

内部数值码按域分段:1000-1999 通用 / 2xxx 认证 / 3xxx 知识库与文档 / 4xxx 会话问答
/ 5xxx Agent / 6xxx 模型调用。
稳定字符串码面向前端做 i18n 映射;契约:加码不破坏,改码需协调前端发版(本注释即契约锚点)。
新增错误必须先在 ErrorCode 注册 —— CI 的错误码注册扫描(M1 接入)以此文件为唯一事实源。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class ErrorCode:
    """(内部数值码, 稳定字符串码)注册表。"""

    # 1000-1999 通用
    INTERNAL = (1000, "INTERNAL_ERROR")
    VALIDATION = (1001, "VALIDATION_ERROR")
    NOT_FOUND = (1002, "NOT_FOUND")
    # 2xxx 认证与账号
    AUTH_REQUIRED = (2000, "AUTH_REQUIRED")
    FORBIDDEN = (2001, "FORBIDDEN")
    INVALID_CREDENTIALS = (2002, "INVALID_CREDENTIALS")
    TOKEN_EXPIRED = (2003, "TOKEN_EXPIRED")
    REFRESH_TOKEN_INVALID = (2004, "REFRESH_TOKEN_INVALID")
    USERNAME_TAKEN = (2005, "USERNAME_TAKEN")
    USER_NOT_FOUND = (2006, "USER_NOT_FOUND")
    MEMBER_ALREADY = (2007, "MEMBER_ALREADY")
    # 3xxx 知识库与空间
    SPACE_NOT_FOUND = (3000, "SPACE_NOT_FOUND")
    MEMBER_NOT_FOUND = (3001, "MEMBER_NOT_FOUND")
    KB_NOT_FOUND = (3010, "KB_NOT_FOUND")
    KB_NAME_TAKEN = (3011, "KB_NAME_TAKEN")
    DOCUMENT_NOT_FOUND = (3020, "DOCUMENT_NOT_FOUND")
    UNSUPPORTED_FORMAT = (3021, "UNSUPPORTED_FORMAT")
    FILE_TOO_LARGE = (3022, "FILE_TOO_LARGE")
    # 6xxx 模型调用
    MODEL_NOT_CONFIGURED = (6001, "MODEL_NOT_CONFIGURED")
    MODEL_CALL_FAILED = (6000, "MODEL_CALL_FAILED")


class AppError(Exception):
    """领域错误:handler/service 抛出,由统一壳渲染为 {success, error:{code,message,details}}。"""

    def __init__(
        self,
        code: tuple[int, str],
        message: str,
        http_status: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code_num, self.code_str = code
        self.message = message
        self.http_status = http_status
        self.details = details


async def app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Starlette 的 handler 类型签名为 Exception;实际只对 AppError 注册本 handler
    assert isinstance(exc, AppError)
    return JSONResponse(
        status_code=exc.http_status,
        content={
            "success": False,
            "error": {"code": exc.code_str, "message": exc.message, "details": exc.details},
        },
    )


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """请求参数校验失败 → 422 统一壳(错误契约对所有响应一致,基准 02)。"""
    assert isinstance(exc, RequestValidationError)
    return JSONResponse(
        status_code=422,
        content={
            "success": False,
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "请求参数不合法",
                "details": list(exc.errors()),
            },
        },
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """非领域错误一律 500 兜底且不泄漏内部信息(基准 02)。"""
    logger.error("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": {"code": "INTERNAL_ERROR", "message": "内部错误,请稍后重试", "details": None},
        },
    )
