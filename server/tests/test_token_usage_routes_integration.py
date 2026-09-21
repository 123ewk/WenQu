"""token 用量端点集成测试(OPT-28 读侧,真 Postgres)。

覆盖:① 门槛(匿名 401 / 非成员 404 / Editor 与 Viewer 读不到别人但能读自己 / Admin+ 可汇总);
② 聚合正确性(按成员、按类别计数、estimated 单列、空间合计含无归属人行);
③ 时间边界(7d/30d/all 的 since 下界,未知 period 422);
④ 空间隔离与"退空间成员的历史用量只进合计";⑤ API Key 一律 401(未登记能力表)。

用量事件直接灌库(写入点由 `test_usage_metering_integration.py` 覆盖)。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from app.core.db import get_session_factory
from app.domain.enums import UsageKind, UsageSource
from app.domain.models import TokenUsageEvent
from tests.conftest import register as _register

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _reset_tables(client):
    def _clean() -> None:
        with get_session_factory()() as db:
            db.execute(delete(TokenUsageEvent))
            db.commit()

    _clean()
    yield
    _clean()


def _auth(session: dict) -> dict:
    return {"Authorization": f"Bearer {session['access_token']}"}


def _owner(client, username: str, space_name: str) -> tuple[str, dict, uuid.UUID]:
    session = _register(client, username)
    auth = _auth(session)
    resp = client.post("/api/v1/spaces", json={"name": space_name}, headers=auth)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], auth, uuid.UUID(session["user"]["id"])


def _member(
    client, space_id: str, auth: dict, username: str, role: int
) -> tuple[dict, uuid.UUID]:
    session = _register(client, username)
    resp = client.post(
        f"/api/v1/spaces/{space_id}/members",
        json={"username": username, "role": role},
        headers=auth,
    )
    assert resp.status_code == 201, resp.text
    return _auth(session), uuid.UUID(session["user"]["id"])


def _seed(
    space_id: str,
    user_id: uuid.UUID | None,
    *,
    kind: str = UsageKind.CHAT,
    source: str = UsageSource.UPSTREAM,
    prompt: int = 0,
    completion: int = 0,
    total: int | None = None,
    days_ago: int = 0,
) -> None:
    with get_session_factory()() as db:
        db.add(
            TokenUsageEvent(
                space_id=uuid.UUID(space_id),
                user_id=user_id,
                kind=kind,
                source=source,
                model_id="deepseek/deepseek-chat",
                provider="演示厂",
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=total if total is not None else prompt + completion,
                created_at=datetime.now(UTC) - timedelta(days=days_ago, minutes=1),
            )
        )
        db.commit()


def _summary(client, space_id: str, auth: dict, period: str | None = None) -> dict:
    params = {"period": period} if period else {}
    resp = client.get(
        f"/api/v1/spaces/{space_id}/token-usage", params=params, headers=auth
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _by_user(summary: dict, user_id: uuid.UUID) -> dict:
    return next(m for m in summary["members"] if m["user_id"] == str(user_id))


# ---------------------------- 门槛 ----------------------------


def test_gate_anon_nonmember_and_roles(client) -> None:
    space_id, owner_auth, _ = _owner(client, "usageowner", "用量空间")
    viewer_auth, viewer_id = _member(client, space_id, owner_auth, "usageviewer", 10)
    url = f"/api/v1/spaces/{space_id}/token-usage"

    assert client.get(url).status_code == 401

    stranger = _register(client, "usagestranger")
    resp = client.get(url, headers=_auth(stranger))
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "SPACE_NOT_FOUND"

    # Viewer:别人的账看不到(403),自己的照样能看
    denied = client.get(url, headers=viewer_auth)
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "FORBIDDEN"
    mine = client.get(f"{url}/me", headers=viewer_auth)
    assert mine.status_code == 200, mine.text
    assert mine.json()["usage"]["total_tokens"] == 0

    # 空间属主(Owner 40)可汇总
    assert client.get(url, headers=owner_auth).status_code == 200
    # 不存在的空间与跨空间同壳
    missing = client.get(
        f"/api/v1/spaces/{uuid.uuid4()}/token-usage", headers=owner_auth
    )
    assert missing.status_code == 404


def test_editor_cannot_see_members(client) -> None:
    space_id, owner_auth, _ = _owner(client, "usageedowner", "编辑用量空间")
    editor_auth, _ = _member(client, space_id, owner_auth, "usageeditor", 20)
    resp = client.get(f"/api/v1/spaces/{space_id}/token-usage", headers=editor_auth)
    assert resp.status_code == 403
    mine = client.get(f"/api/v1/spaces/{space_id}/token-usage/me", headers=editor_auth)
    assert mine.status_code == 200


def test_api_key_denied(client) -> None:
    """未登记进能力表 → 用 Key(无 Bearer)访问是 401,不是 403:配置与账务只给人的凭据。"""
    space_id, owner_auth, _ = _owner(client, "usagekeyowner", "密钥用量空间")
    resp = client.get(
        f"/api/v1/spaces/{space_id}/token-usage", headers={"X-API-Key": "whatever"}
    )
    assert resp.status_code == 401 and resp.json()["error"]["code"] == "AUTH_REQUIRED"


# ---------------------------- 聚合 ----------------------------


def test_member_breakdown_and_space_total(client) -> None:
    space_id, owner_auth, owner_id = _owner(client, "usagemix", "混合用量空间")
    member_auth, member_id = _member(client, space_id, owner_auth, "usagemixb", 20)

    _seed(space_id, owner_id, prompt=100, completion=40, total=140)
    _seed(space_id, owner_id, kind=UsageKind.EMBEDDING, prompt=30, total=30)
    # 估算口径单独累加,不许混进精确数
    _seed(
        space_id,
        member_id,
        prompt=10,
        completion=5,
        total=15,
        source=UsageSource.ESTIMATED,
    )
    # 无归属人(上传者已注销)的行:只进空间合计
    _seed(space_id, None, kind=UsageKind.EMBEDDING, prompt=7, total=7)

    summary = _summary(client, space_id, owner_auth, "all")
    names = [m["username"] for m in summary["members"]]
    assert names == ["usagemix", "usagemixb"], summary["members"]

    owner = _by_user(summary, owner_id)
    assert (owner["prompt_tokens"], owner["completion_tokens"]) == (130, 40)
    assert owner["total_tokens"] == 170
    assert (owner["chat_calls"], owner["embedding_calls"]) == (1, 1)
    assert owner["estimated_tokens"] == 0
    assert owner["role"] == 40 and owner["nickname"]

    member = _by_user(summary, member_id)
    assert member["estimated_tokens"] == 15 and member["total_tokens"] == 15

    total = summary["space_total"]
    assert total["total_tokens"] == 170 + 15 + 7
    assert total["estimated_tokens"] == 15
    assert (total["chat_calls"], total["embedding_calls"]) == (2, 2)
    # 合计里有归属人空的行,成员列表里就没有它
    assert sum(m["total_tokens"] for m in summary["members"]) == 185


def test_me_endpoint_sees_only_self(client) -> None:
    space_id, owner_auth, owner_id = _owner(client, "usageme", "我的用量空间")
    _, member_id = _member(client, space_id, owner_auth, "usagemb", 20)
    _seed(space_id, owner_id, prompt=500, completion=100)
    _seed(space_id, member_id, prompt=1, completion=1)

    resp = client.get(
        f"/api/v1/spaces/{space_id}/token-usage/me", params={"period": "all"}, headers=owner_auth
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["usage"]["total_tokens"] == 600 and body["usage"]["chat_calls"] == 1
    assert body["period"] == "all" and body["since"] is None
    assert "members" not in body


# ---------------------------- 时间边界与隔离 ----------------------------


def test_period_bounds(client) -> None:
    space_id, owner_auth, owner_id = _owner(client, "usageperiod", "时间边界空间")
    _seed(space_id, owner_id, prompt=1, total=1, days_ago=8)
    _seed(space_id, owner_id, prompt=2, total=2, days_ago=0)

    assert _summary(client, space_id, owner_auth, "7d")["space_total"]["total_tokens"] == 2
    assert _summary(client, space_id, owner_auth)["space_total"]["total_tokens"] == 3  # 默认 30d
    assert _summary(client, space_id, owner_auth, "all")["space_total"]["total_tokens"] == 3

    resp = client.get(
        f"/api/v1/spaces/{space_id}/token-usage",
        params={"period": "1y"},
        headers=owner_auth,
    )
    assert resp.status_code == 422  # 未知档位不静默回落,直接拒


def test_cross_space_isolation(client) -> None:
    a_id, a_auth, a_user = _owner(client, "usagea", "空间A")
    b_id, b_auth, _ = _owner(client, "usageb", "空间B")
    _seed(a_id, a_user, prompt=9, total=9)

    # B 的属主看 B:一分没有 —— 聚合谓词必须带 space_id
    b_summary = _summary(client, b_id, b_auth, "all")
    assert b_summary["space_total"]["total_tokens"] == 0
    assert all(m["total_tokens"] == 0 for m in b_summary["members"])
    assert _summary(client, a_id, a_auth, "all")["space_total"]["total_tokens"] == 9


def test_ex_member_usage_stays_in_total(client) -> None:
    space_id, owner_auth, _ = _owner(client, "usageleave", "退成员空间")
    _, left_id = _member(client, space_id, owner_auth, "usageleft", 20)
    _seed(space_id, left_id, prompt=42, total=42)

    resp = client.delete(
        f"/api/v1/spaces/{space_id}/members/{left_id}", headers=owner_auth
    )
    assert resp.status_code == 204, resp.text

    summary = _summary(client, space_id, owner_auth, "all")
    assert [m["username"] for m in summary["members"]] == ["usageleave"]
    assert summary["space_total"]["total_tokens"] == 42  # 既成事实不因退空间而消失
