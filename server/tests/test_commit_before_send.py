"""CommitBeforeSendRoute:请求会话的提交必须发生在响应发出之前。

背景:FastAPI yield 依赖的收尾 commit 在响应完整发出之后才执行,真实 HTTP 链路上
"创建后客户端立即发起的下一个请求"会读不到刚写入的行(Go-Live 冒烟实测:注册后
401、建空间后建库 404、建 Key 后 API_KEY_INVALID;TestClient 进程内测不出)。
本测试用 ASGI send 包装记录时序,断言 commit 先于任何 send。
"""

from collections.abc import Iterator
from typing import Annotated, Any

import pytest
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.routing import CommitBeforeSendRoute
from app.core.db import DB_SESSION_SCOPE_KEY


class FakeSession:
    def __init__(self, log: list[str]) -> None:
        self._log = log

    def commit(self) -> None:
        self._log.append("commit")

    def rollback(self) -> None:
        self._log.append("rollback")

    def close(self) -> None:
        self._log.append("close")


class RecordingASGI:
    """记录首次 send(响应开始发出)与 commit 的先后。"""

    def __init__(self, app: Any, log: list[str]) -> None:
        self._app, self._log = app, log

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def send_wrapper(message: Any) -> None:
            self._log.append("send")
            await send(message)

        await self._app(scope, receive, send_wrapper)


def build_client(log: list[str], *, fail: bool = False) -> TestClient:
    def fake_get_db(request: Request) -> Iterator[Any]:
        session = FakeSession(log)
        request.scope[DB_SESSION_SCOPE_KEY] = session
        try:
            yield session
            session.commit()  # 模拟 get_db 收尾提交(晚于发送,兜底)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    router = APIRouter(route_class=CommitBeforeSendRoute)

    @router.get("/thing")
    def read_thing(db: Annotated[Session, Depends(fake_get_db)]) -> dict[str, bool]:
        if fail:
            raise RuntimeError("boom")
        return {"ok": True}

    app = FastAPI()
    app.include_router(router)
    return TestClient(RecordingASGI(app, log))


def test_commit_before_response_sent() -> None:
    log: list[str] = []
    resp = build_client(log).get("/thing")
    assert resp.status_code == 200
    assert log[0] == "commit", f"提交必须最先发生:{log}"
    assert "send" in log
    # 收尾兜底 commit 幂等再跑一次,均在发送之后,不影响发送前可见性
    assert log.count("commit") == 2


def test_error_path_never_commits() -> None:
    log: list[str] = []
    with pytest.raises(RuntimeError):
        build_client(log, fail=True).get("/thing")
    assert "commit" not in log
    assert "rollback" in log
