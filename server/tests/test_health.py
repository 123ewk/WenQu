"""M0 冒烟:健康检查、系统信息、统一错误壳。全部零外部依赖(基准 03)。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import AppError, ErrorCode
from app.main import create_app, register_error_handlers


def test_health_ok() -> None:
    client = TestClient(create_app())
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_system_info_has_version() -> None:
    client = TestClient(create_app())
    data = client.get("/system/info").json()
    assert data["version"]
    assert data["env"] == "dev"


def test_app_error_uses_unified_shell() -> None:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise AppError(ErrorCode.NOT_FOUND, "资源不存在", http_status=404)

    client = TestClient(app)
    resp = client.get("/boom")
    assert resp.status_code == 404
    assert resp.json() == {
        "success": False,
        "error": {"code": "NOT_FOUND", "message": "资源不存在", "details": None},
    }


def test_unhandled_error_masked_as_500() -> None:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/crash")
    async def crash() -> None:
        raise RuntimeError("secret-internal-detail")

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/crash")
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "secret" not in resp.text  # 不泄漏内部信息
