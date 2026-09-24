"""OPT-46 演示模式状态接口(未登录可读)。

四条纪律各一条测试:开关关掉时什么都不说、开着但没播种时 ready=false、
文档计数按空间统计且非全 completed 就不 ready、密码在任何情况下都不出接口。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.core.config import Settings
from tests.conftest import register

STATUS_PATH = "/api/v1/demo/status"
PASSWORD_SENTINEL = "demo-secret-never-return-me"


def _demo_settings(monkeypatch, **overrides: Any) -> Settings:
    """只替换 demo 路由读的 get_settings:其余链路照旧用环境配置。"""
    from app.api.routes import demo as demo_routes

    settings = Settings(demo_mode=True, demo_password=PASSWORD_SENTINEL, **overrides)
    monkeypatch.setattr(demo_routes, "get_settings", lambda: settings)
    return settings


def _username(prefix: str = "demo") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _make_space(client, token: str, name: str) -> str:
    auth = {"Authorization": f"Bearer {token}"}
    resp = client.post("/api/v1/spaces", json={"name": name, "description": ""}, headers=auth)
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _add_documents(space_id: str, statuses: list[str]) -> None:
    """直接写 documents 表:入库由 worker 异步完成,测试里不需要真跑流水线。"""
    from app.core.db import get_session_factory
    from app.domain.models import Document, KnowledgeBase

    with get_session_factory()() as db:
        # (space, kb 名) 有唯一约束,每批开一个新库,同一测试里可反复追加
        kb = KnowledgeBase(
            id=uuid.uuid4(),
            space_id=uuid.UUID(space_id),
            name=f"演示知识库-{uuid.uuid4().hex[:6]}",
        )
        db.add(kb)
        db.flush()
        for status in statuses:
            db.add(
                Document(
                    id=uuid.uuid4(),
                    kb_id=kb.id,
                    space_id=uuid.UUID(space_id),
                    filename=f"doc-{uuid.uuid4().hex[:6]}.md",
                    format="md",
                    size_bytes=10,
                    source=f"{space_id}/{kb.id}/x.md",
                    status=status,
                )
            )
        db.commit()


def test_status_endpoint_is_public_and_registered(client):
    """不带凭据也要 200:登录页要用它决定是否显示"一键体验"。"""
    resp = client.get(STATUS_PATH)
    assert resp.status_code == 200, resp.text


def test_disabled_reveals_nothing(client, monkeypatch):
    from app.api.routes import demo as demo_routes

    monkeypatch.setattr(demo_routes, "get_settings", lambda: Settings(demo_mode=False))
    body = client.get(STATUS_PATH).json()
    assert body["enabled"] is False
    assert body["username"] == ""
    assert body["ready"] is False


def test_enabled_but_not_seeded(client, monkeypatch):
    name = _username()
    settings = _demo_settings(monkeypatch, demo_username=name)
    body = client.get(STATUS_PATH).json()
    assert body == {
        "enabled": True,
        "username": name,
        "space_name": settings.demo_space_name,
        "kb_name": settings.demo_kb_name,
        "document_total": 0,
        "document_ready": 0,
        "ready": False,
    }


def test_ready_only_when_every_document_completed(client, monkeypatch):
    name = _username()
    space_name = _username("demo-space")
    _demo_settings(monkeypatch, demo_username=name, demo_space_name=space_name)

    user = register(client, name)
    space_id = _make_space(client, user["access_token"], space_name)
    body = client.get(STATUS_PATH).json()
    assert (body["document_total"], body["ready"]) == (0, False), "空空间不算就绪"

    _add_documents(space_id, ["completed", "completed"])
    body = client.get(STATUS_PATH).json()
    assert (body["document_total"], body["document_ready"], body["ready"]) == (2, 2, True)

    _add_documents(space_id, ["embedding"])  # 还在入库 → 演示中途会问到半截数据
    body = client.get(STATUS_PATH).json()
    assert (body["document_total"], body["document_ready"], body["ready"]) == (3, 2, False)

    _add_documents(space_id, ["failed"])  # 有失败篇目同样不 ready
    body = client.get(STATUS_PATH).json()
    assert body["ready"] is False


def test_other_spaces_do_not_count(client, monkeypatch):
    """演示状态只看配置里那个空间,别人(或同人另建的库)的文档不能混进来。"""
    name = _username()
    _demo_settings(monkeypatch, demo_username=name, demo_space_name="只此一个演示空间")
    user = register(client, name)
    _make_space(client, user["access_token"], "只此一个演示空间")
    _add_documents(_make_space(client, user["access_token"], "另一个空间"), ["completed"])
    body = client.get(STATUS_PATH).json()
    assert (body["document_total"], body["ready"]) == (0, False)


@pytest.mark.parametrize(
    "seed_docs", [False, True], ids=["未播种", "已播种"]
)
def test_password_never_in_response(client, monkeypatch, seed_docs: bool):
    name = _username()
    settings = _demo_settings(monkeypatch, demo_username=name)
    if seed_docs:
        user = register(client, name)
        _add_documents(_make_space(client, user["access_token"], settings.demo_space_name),
                       ["completed"])
    resp = client.get(STATUS_PATH)
    assert PASSWORD_SENTINEL not in resp.text
    assert "password" not in resp.json()
