"""WenQu 主服务入口(薄):装配配置、日志、错误壳与路由;启动副作用统一在 create_app(基准 01)。"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response

from app.api.denied_audit import record_denied
from app.api.deps import get_event_log, get_stream_supervisor
from app.api.routes import api_keys as api_key_routes
from app.api.routes import auth as auth_routes
from app.api.routes import chunking as chunking_routes
from app.api.routes import conversations as conversation_routes
from app.api.routes import health as health_routes
from app.api.routes import knowledge as knowledge_routes
from app.api.routes import models as model_routes
from app.api.routes import retrieval as retrieval_routes
from app.api.routes import spaces as spaces_routes
from app.api.routes import users as users_routes
from app.application.streaming import StreamJanitor
from app.core.config import get_settings
from app.core.errors import (
    AppError,
    app_error_handler,
    set_denied_audit_hook,
    unhandled_error_handler,
    validation_error_handler,
)
from app.core.logging import request_id_var, setup_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def stream_lifespan(_: FastAPI) -> AsyncIterator[None]:
    """OPT-3 流式资源的生命周期(事件日志为 API 进程内单例,治理只能在同进程):
    启动清道夫(TTL 回收已完成流);关停时通知进行中的生成落库部分答案。"""
    janitor = StreamJanitor(get_event_log())
    janitor.start()
    try:
        yield
    finally:
        janitor.stop()
        notified = get_stream_supervisor().shutdown_all(wait_seconds=5.0)
        if notified:
            logger.info("关停:已通知 %d 个进行中的生成落库部分答案", notified)


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.env)
    if settings.env == "dev" and not settings.jwt_secret:
        logger.warning(
            "APP_JWT_SECRET 未配置:dev 模式下将临时随机生成(仅本进程有效,重启即失效);"
            "生产必须显式配置(Go-Live 翻转项 #3)"
        )

    app = FastAPI(
        title="WenQu API",
        version=settings.version,
        lifespan=stream_lifespan,
        # Go-Live 翻转项 #7:Swagger/OpenAPI 仅非生产模式暴露
        docs_url="/docs" if settings.env == "dev" else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.env == "dev" else None,
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        request_id_var.set(rid)
        started = time.perf_counter()
        response: Response = await call_next(request)
        response.headers["x-request-id"] = rid
        logger.info(
            "%s %s -> %s (%.1fms)",
            request.method,
            request.url.path,
            response.status_code,
            (time.perf_counter() - started) * 1000,
        )
        return response

    register_error_handlers(app)
    set_denied_audit_hook(record_denied)
    app.include_router(health_routes.router)
    app.include_router(auth_routes.router)
    app.include_router(users_routes.router)
    app.include_router(spaces_routes.router)
    app.include_router(knowledge_routes.router)
    app.include_router(chunking_routes.router)
    app.include_router(retrieval_routes.router)
    app.include_router(conversation_routes.router)
    app.include_router(model_routes.router)
    app.include_router(api_key_routes.router)
    return app


app = create_app()
