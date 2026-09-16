"""WenQu 主服务入口(薄):装配配置、日志、错误壳与路由;启动副作用统一在 create_app(基准 01)。"""

from __future__ import annotations

import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import Response

from app.api.routes.health import router as health_router
from app.core.config import get_settings
from app.core.errors import AppError, app_error_handler, unhandled_error_handler
from app.core.logging import request_id_var, setup_logging

logger = logging.getLogger(__name__)


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)


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
    app.include_router(health_router)
    return app


app = create_app()
