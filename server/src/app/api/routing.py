"""请求级 UoW 提交时机修正。

FastAPI 的 yield 依赖收尾代码在响应"完整发出之后"才执行,而本项目的
get_db 把 commit 放在收尾里:真实 HTTP 链路上,"建档/建库/建 Key 后客户端
立即发起的下一个请求"会读不到刚写入的行(Go-Live 冒烟实测:注册后
POST /spaces 401、建空间后建库 404、建 Key 后检索 API_KEY_INVALID,
重试一次即成功;进程内 TestClient 永远测不出)。
本路由类在响应发送前(端点函数返回后)提交请求会话,把写可见性收敛到
响应可达之前;异常路径不提交,仍由 get_db 收尾回滚。
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Request, Response
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool

from app.core.db import DB_SESSION_SCOPE_KEY

RouteHandler = Callable[[Request], Coroutine[Any, Any, Response]]


def commit_before_send(handler: RouteHandler) -> RouteHandler:
    async def wrapped(request: Request) -> Response:
        response = await handler(request)
        session = request.scope.get(DB_SESSION_SCOPE_KEY)
        if session is not None:
            # 同步会话放线程池提交,避免阻塞事件循环
            await run_in_threadpool(session.commit)
        return response

    return wrapped


class CommitBeforeSendRoute(APIRoute):
    """挂到所有走数据库的 APIRouter:class_=CommitBeforeSendRoute。"""

    def get_route_handler(self) -> RouteHandler:
        return commit_before_send(super().get_route_handler())
