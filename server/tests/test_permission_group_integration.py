"""权限档位 HTTP 集成测试(OPT-41,真 Postgres)。

覆盖服务层单测拿不到的三件事:
1. **契约形状**:响应里能力/范围/成员是谁可见的、字段叫什么(前端按这份渲染);
2. **真库约束**:"每人每空间至多一档 current" 由部分唯一索引兜底,换档链路必须走真库
   (这条取代了 OPT-31 的 `api_keys.is_active` 互斥索引测试);
3. **凭据类型边界**:档位管理只认人(JWT)。程序凭据能改自己的权限 = 一把外泄的 Key
   就能自我提权,所以这组路由刻意不进 Key 的路由授权表。
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import create_group, create_key
from tests.conftest import register as _register

pytestmark = pytest.mark.integration


def _uname(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


def _space(client) -> tuple[dict, dict, str]:
    """owner + 两个 KB 的空间;返回 (owner, auth, space_id)。"""
    owner = _register(client, _uname("grpowner"))
    auth = {"Authorization": f"Bearer {owner['access_token']}"}
    space_id = client.post("/api/v1/spaces", json={"name": "档位空间"}, headers=auth).json()[
        "id"
    ]
    return owner, auth, space_id


def _add_member(client, auth: dict, space_id: str, prefix: str, role: int) -> dict:
    name = _uname(prefix)
    user = _register(client, name)
    client.post(
        f"/api/v1/spaces/{space_id}/members",
        json={"username": name, "role": role},
        headers=auth,
    )
    return user


def _group_body(**overrides) -> dict:
    body = {"name": "只读问答", "description": "", "capabilities": ["chat"], "all_kbs": True}
    body.update(overrides)
    return body


def _base(space_id: str) -> str:
    return f"/api/v1/spaces/{space_id}/permission-groups"


# ---------------------------- 建档与契约形状 ----------------------------


def test_admin_creates_group_and_viewer_can_read_it(client) -> None:
    _owner, auth, space_id = _space(client)
    created = client.post(_base(space_id), json=_group_body(), headers=auth)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["capabilities"] == ["chat"]
    assert body["all_kbs"] is True and body["kb_ids"] == []
    assert body["members"] == []

    viewer = _add_member(client, auth, space_id, "grpviewer", 10)
    viewer_auth = {"Authorization": f"Bearer {viewer['access_token']}"}
    listed = client.get(_base(space_id), headers=viewer_auth)
    assert listed.status_code == 200
    assert [g["id"] for g in listed.json()] == [body["id"]]
    # Editor/Viewer 不能建:档位是权限源,能写就能给自己提权
    assert (
        client.post(_base(space_id), json=_group_body(name="x"), headers=viewer_auth).status_code
        == 403
    )


def test_all_kbs_true_with_kb_ids_is_rejected(client) -> None:
    """两个字段说同一件事,矛盾输入必须 422,不能让"漏勾"变成放开全空间。"""
    _owner, auth, space_id = _space(client)
    kb = client.post(
        f"/api/v1/spaces/{space_id}/knowledge-bases", json={"name": "库"}, headers=auth
    ).json()["id"]
    resp = client.post(
        _base(space_id),
        json=_group_body(kb_ids=[kb], all_kbs=True),
        headers=auth,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_kb_scope_must_stay_in_space(client) -> None:
    _owner, auth, space_id = _space(client)
    _other_owner, other_auth, other_space = _space(client)
    foreign = client.post(
        f"/api/v1/spaces/{other_space}/knowledge-bases",
        json={"name": "别人的库"},
        headers=other_auth,
    ).json()["id"]
    resp = client.post(
        _base(space_id), json=_group_body(kb_ids=[foreign], all_kbs=False), headers=auth
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "KB_NOT_FOUND"


def test_patch_group_records_before_and_after_in_audit(client) -> None:
    """"谁把范围从全部放成了 1 个库"必须能从一条审计里读出来。"""
    _owner, auth, space_id = _space(client)
    kb = client.post(
        f"/api/v1/spaces/{space_id}/knowledge-bases", json={"name": "产品库"}, headers=auth
    ).json()["id"]
    group = create_group(client, auth, space_id, name="问答档", capabilities=["chat"])
    resp = client.patch(
        f"{_base(space_id)}/{group['id']}",
        json=_group_body(name="问答档-仅产品库", kb_ids=[kb], all_kbs=False),
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["kb_ids"] == [kb]
    logs = client.get(
        f"/api/v1/spaces/{space_id}/audit-logs", headers=auth
    ).json()["items"]
    entry = next(x for x in logs if x["action"] == "permission_group.updated")
    assert entry["detail"]["kb_scope_before"] == "all"
    assert entry["detail"]["kb_scope_after"] == "1"


# ---------------------------- 分配与换档(取代 OPT-31 的 activate) ----------------------------


def test_assign_then_set_current_switches_exclusively(client) -> None:
    """两档分配给同一人后换档:每人每空间至多一档 current(真库部分唯一索引链路)。"""
    _owner, auth, space_id = _space(client)
    editor = _add_member(client, auth, space_id, "grpeditor", 20)
    editor_id = editor["user"]["id"]
    a = create_group(client, auth, space_id, name="档A")
    b = create_group(client, auth, space_id, name="档B")

    assigned = client.post(
        f"{_base(space_id)}/{a['id']}/members",
        json={"user_id": editor_id},
        headers=auth,
    )
    assert assigned.status_code == 200, assigned.text
    (member,) = assigned.json()["members"]
    assert member["is_current"] is False  # 分配 ≠ 生效
    assert member["username"] == editor["user"]["username"]
    assert member["role"] == 20
    client.post(
        f"{_base(space_id)}/{b['id']}/members",
        json={"user_id": editor_id},
        headers=auth,
    )

    client.put(
        f"{_base(space_id)}/{a['id']}/current", json={"user_id": editor_id}, headers=auth
    )
    out = client.put(
        f"{_base(space_id)}/{b['id']}/current",
        json={"user_id": editor_id},
        headers=auth,
    )
    assert out.status_code == 200, out.text
    assert out.json()["members"][0]["is_current"] is True
    current_flags = {
        g["name"]: g["members"][0]["is_current"]
        for g in client.get(_base(space_id), headers=auth).json()
    }
    assert current_flags == {"档A": False, "档B": True}
    logs = client.get(
        f"/api/v1/spaces/{space_id}/audit-logs", headers=auth
    ).json()["items"]
    assert any(x["action"] == "permission_group.current_set" for x in logs)


def test_set_current_requires_prior_assignment(client) -> None:
    _owner, auth, space_id = _space(client)
    editor = _add_member(client, auth, space_id, "grpeditor2", 20)
    group = create_group(client, auth, space_id)
    resp = client.put(
        f"{_base(space_id)}/{group['id']}/current",
        json={"user_id": editor["user"]["id"]},
        headers=auth,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_assign_non_member_is_404(client) -> None:
    _owner, auth, space_id = _space(client)
    stranger = _register(client, _uname("grpstranger"))
    group = create_group(client, auth, space_id)
    resp = client.post(
        f"{_base(space_id)}/{group['id']}/members",
        json={"user_id": stranger["user"]["id"]},
        headers=auth,
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "MEMBER_NOT_FOUND"


def test_unassign_drops_current_pointer(client) -> None:
    """解除分配 = 他的 current 一并消失(fail-closed:没有档位 = 没有可检索范围)。"""
    _owner, auth, space_id = _space(client)
    editor = _add_member(client, auth, space_id, "grpeditor3", 20)
    group = create_group(client, auth, space_id)
    client.post(
        f"{_base(space_id)}/{group['id']}/members",
        json={"user_id": editor["user"]["id"]},
        headers=auth,
    )
    client.put(
        f"{_base(space_id)}/{group['id']}/current",
        json={"user_id": editor["user"]["id"]},
        headers=auth,
    )
    resp = client.delete(
        f"{_base(space_id)}/{group['id']}/members/{editor['user']['id']}", headers=auth
    )
    assert resp.status_code == 204
    assert client.get(_base(space_id), headers=auth).json()[0]["members"] == []


def test_write_actions_require_admin_over_http(client) -> None:
    _owner, auth, space_id = _space(client)
    editor = _add_member(client, auth, space_id, "grpeditor4", 20)
    editor_auth = {"Authorization": f"Bearer {editor['access_token']}"}
    group = create_group(client, auth, space_id)
    assert (
        client.patch(
            f"{_base(space_id)}/{group['id']}", json=_group_body(name="改"), headers=editor_auth
        ).status_code
        == 403
    )
    assert (
        client.delete(f"{_base(space_id)}/{group['id']}", headers=editor_auth).status_code == 403
    )
    assert (
        client.post(
            f"{_base(space_id)}/{group['id']}/members",
            json={"user_id": editor["user"]["id"]},
            headers=editor_auth,
        ).status_code
        == 403
    )


# ---------------------------- 跨空间与凭据边界 ----------------------------


def test_group_of_another_space_is_404(client) -> None:
    """防枚举:别人的档位既不暴露存在性,也不允许在本空间引用。"""
    _owner, auth, space_id = _space(client)
    _foreign_owner, foreign_auth, foreign_space = _space(client)
    foreign = create_group(client, foreign_auth, foreign_space)
    for method, url in (
        ("get", f"{_base(space_id)}/{foreign['id']}"),
        ("patch", f"{_base(space_id)}/{foreign['id']}"),
        ("delete", f"{_base(space_id)}/{foreign['id']}"),
    ):
        resp = client.request(method.upper(), url, json={"name": "x"}, headers=auth)
        assert resp.status_code == 404, f"{method} {url} -> {resp.status_code}"


def test_api_key_cannot_manage_permission_groups(client) -> None:
    """程序凭据不能改权限:这组路由只认 JWT,带 Key 一律 401(与未登录同构)。"""
    _owner, auth, space_id = _space(client)
    key = create_key(client, auth, space_id, create_group(client, auth, space_id)["id"])
    resp = client.get(
        _base(space_id), headers={"X-API-Key": key["plaintext"]}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTH_REQUIRED"
    resp = client.post(_base(space_id), json=_group_body(), headers={"X-API-Key": key["plaintext"]})
    assert resp.status_code == 401


def test_unauthenticated_returns_unified_error_shell(client) -> None:
    resp = client.get(_base(str(uuid.uuid4())))
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTH_REQUIRED"
