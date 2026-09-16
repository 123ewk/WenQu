"""被拒操作审计(缺口 #3「结果」列的来源)。

为什么单独一个模块、单独一个会话:
- 被拒请求(403)在 `get_db` 里会走 rollback —— 用同一会话写的审计记录会跟着被丢弃;
- 所以这里开一个独立会话并自行 commit,与请求事务解耦;
- 钩子由 main(组合根)注入 errors 的 on-denied 钩子,core 不反向依赖数据层(基准 01)。

记录范围:403 越权(有明确操作意图,是安全审计的核心)与 401 认证失败(仅登录接口,
避免把普通"token 过期"刷成噪声)。space_id 从路径参数取,取不到则记为空(如登录失败)。
"""

from __future__ import annotations

import logging
import uuid

from fastapi import Request

from app.core.errors import AppError
from app.core.security import decode_access_token
from app.domain.enums import AuditAction, AuditResult
from app.domain.models import AuditLog

logger = logging.getLogger("app.audit.denied")

# 只对这些路径记 401:其余 401 是 token 过期/缺失的常规轮换,记了是噪声
_AUTH_FAILURE_PATHS = ("/auth/login", "/auth/register")


def _resolve_actor_id(request: Request) -> uuid.UUID | None:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    try:
        payload = decode_access_token(header[7:].strip())
        return uuid.UUID(str(payload["sub"]))
    except Exception:  # noqa: BLE001 — token 无效时仍要留下被拒记录,只是无主体
        return None


def _resolve_space_id(request: Request) -> uuid.UUID | None:
    raw = request.path_params.get("space_id")
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return None


def record_denied(request: Request, exc: AppError) -> None:
    """写入被拒审计;自身异常绝不影响原错误响应(调用方已兜底)。"""
    if exc.http_status == 401 and not request.url.path.endswith(_AUTH_FAILURE_PATHS):
        return

    from app.core.db import get_session_factory

    actor_id = _resolve_actor_id(request)
    space_id = _resolve_space_id(request)
    detail = {
        "method": request.method,
        "path": request.url.path,
        "code": exc.code_str,
        "message": exc.message,
    }

    with get_session_factory()() as db:
        db.add(
            AuditLog(
                actor_id=actor_id,
                space_id=space_id,
                action=AuditAction.ACCESS_DENIED,
                target=f"{request.method} {request.url.path}"[:128],
                result=AuditResult.DENIED,
                detail=detail,
                ip=request.client.host if request.client else "",
            )
        )
        db.commit()
    logger.info(
        "denied recorded actor=%s space=%s code=%s path=%s",
        actor_id,
        space_id,
        exc.code_str,
        request.url.path,
    )
