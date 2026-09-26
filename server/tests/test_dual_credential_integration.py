"""双凭据强制(OPT-41 N1)HTTP 集成测试:真人带 JWT 也吃档位这一套。

覆盖单元测试拿不到的东西:
- **灰度开关**真的改变行为:`APP_REQUIRE_PERMISSION_GROUP_FOR_CORE` 关 = 人与今天完全
  一致(只看角色),开 = 清单内接口还要求"有生效档位";Key 链路不受开关影响(一直强制);
- **§4.2 边界**在真路由上成立:清单外的管理接口(空间/成员/档位/Key/审计)只看角色,
  没配档位的 Admin 不能因此失去管理入口;
- **两道闸分工**:清单内只读接口(库列表/文档列表/分块)只受知识库范围约束,
  会改数据或花额度的六条才 additionally 要能力 —— 否则只有 chat 档位的成员打不开问答页;
- **错误契约**:`PERMISSION_GROUP_REQUIRED` 是 403 不是 401(401 会被前端踢去重新登录),
  能力/范围拒绝沿用 API Key 那一对码,靠 `details.actor="member"` 分文案;
- **移出成员**的挂载行处置(默认清、显式保留)与审计留痕。
"""

from __future__ import annotations

import uuid

import pytest

from app.application.service.permission_enforcement import clear_enforcement_cache
from tests.conftest import create_group
from tests.conftest import register as _register

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _isolated_enforcement_cache():
    """OPT-48:开关读值缓存是进程级的,每个用例前后都清,双向防跨用例泄漏。"""
    clear_enforcement_cache()
    yield
    clear_enforcement_cache()


def _uname(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


def _gate_on(monkeypatch) -> None:
    """把开关置为"打开"(模拟 env 回落路径:库里无行 + env=1)。

    OPT-48 起读点改走"DB 优先 → env 回落"的读值服务,deps 不再直接调 get_settings,
    接缝随之换到服务模块;进程内 TTL 缓存是跨请求的,翻转后必须清一次,
    否则 setup 阶段缓存住的"关"会盖过这里的环境变量。
    """
    from app.application.service import permission_enforcement as pe_module
    from app.core.config import Settings

    monkeypatch.setattr(
        pe_module,
        "get_settings",
        lambda: Settings(require_permission_group_for_core=True),
    )
    pe_module.clear_enforcement_cache()


def _scene(client) -> tuple[dict, dict, str, str, str]:
    """owner + 空间 + 两个库 + 一个 KB 都建好(开关还没开,setup 走今天的口径)。"""
    owner = _register(client, _uname("dualowner"))
    auth = {"Authorization": f"Bearer {owner['access_token']}"}
    space_id = client.post("/api/v1/spaces", json={"name": "双凭据空间"}, headers=auth).json()[
        "id"
    ]
    base = f"/api/v1/spaces/{space_id}/knowledge-bases"
    kb_a = client.post(base, json={"name": "范围库A"}, headers=auth).json()["id"]
    kb_b = client.post(base, json={"name": "范围库B"}, headers=auth).json()["id"]
    return owner, auth, space_id, kb_a, kb_b


def _member(client, auth: dict, space_id: str, prefix: str, role: int) -> dict:
    name = _uname(prefix)
    user = _register(client, name)
    resp = client.post(
        f"/api/v1/spaces/{space_id}/members",
        json={"username": name, "role": role},
        headers=auth,
    )
    assert resp.status_code == 201, resp.text
    return {"auth": {"Authorization": f"Bearer {user['access_token']}"}, **user["user"]}


def _give_group(client, auth: dict, space_id: str, user_id: str, **group_kwargs) -> dict:
    """建档位 + 分配给此人 + 置为当前(生效的完整三步,和生产口径一致)。"""
    group = create_group(client, auth, space_id, **group_kwargs)
    base = f"/api/v1/spaces/{space_id}/permission-groups/{group['id']}"
    assert (
        client.post(f"{base}/members", json={"user_id": user_id}, headers=auth).status_code == 200
    )
    assert client.put(f"{base}/current", json={"user_id": user_id}, headers=auth).status_code == 200
    return group


def _base(space_id: str) -> str:
    return f"/api/v1/spaces/{space_id}/knowledge-bases"


def _error(resp):
    return resp.json()["error"]


# ---------------------------- 灰度开关 ----------------------------


def test_switch_off_human_path_is_role_only(client) -> None:
    """开关默认关:没档位的成员照旧能用核心接口(上线那一刻不会全员掉进禁用态)。"""
    _owner, auth, space_id, _kb_a, _kb_b = _scene(client)
    viewer = _member(client, auth, space_id, "dualviewer", 10)
    assert client.get(_base(space_id), headers=viewer["auth"]).status_code == 200


def test_switch_on_core_route_requires_a_group(client, monkeypatch) -> None:
    """开关打开:同一请求变 403 PERMISSION_GROUP_REQUIRED,且仍是 403(不是 401)。"""
    _owner, auth, space_id, _kb_a, _kb_b = _scene(client)
    viewer = _member(client, auth, space_id, "dualviewer2", 10)
    _gate_on(monkeypatch)
    resp = client.get(_base(space_id), headers=viewer["auth"])
    assert resp.status_code == 403
    assert _error(resp)["code"] == "PERMISSION_GROUP_REQUIRED"
    assert "管理员" in _error(resp)["message"]


def test_switch_on_management_routes_keep_working(client, monkeypatch) -> None:
    """§4.2:Admin 一档都没有,也必须能管成员/建档位/建 Key —— 否则空间失去管理。"""
    _owner, auth, space_id, _kb_a, _kb_b = _scene(client)
    editor = _member(client, auth, space_id, "dualedgeo", 30)
    outsider = _register(client, _uname("dualin"))
    _gate_on(monkeypatch)
    # 拉成员(清单外)→ 201
    assert (
        client.post(
            f"/api/v1/spaces/{space_id}/members",
            json={"username": outsider["user"]["username"], "role": 10},
            headers=editor["auth"],
        ).status_code
        == 201
    )
    # 建档位(清单外)→ 201
    assert (
        client.post(
            f"/api/v1/spaces/{space_id}/permission-groups",
            json={"name": "问答档", "capabilities": ["chat"], "all_kbs": True},
            headers=editor["auth"],
        ).status_code
        == 201
    )
    # Key 列表/审计都在清单外:同为"没档位的 Admin",照旧只看角色
    keys = client.get(f"/api/v1/spaces/{space_id}/api-keys", headers=editor["auth"])
    assert keys.status_code == 200
    assert (
        client.get(f"/api/v1/spaces/{space_id}/audit-logs", headers=editor["auth"]).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/v1/spaces/{space_id}/ask", json={"question": "q"}, headers=editor["auth"]
        ).status_code
        == 403
    )


def test_non_member_still_gets_404_not_403(client, monkeypatch) -> None:
    """开关打开不能改变"空间不存在"的口径:非成员仍然探不到这个空间。"""
    _owner, _auth, space_id, _kb_a, _kb_b = _scene(client)
    stranger = _register(client, _uname("dualstranger"))
    _gate_on(monkeypatch)
    resp = client.get(
        _base(space_id), headers={"Authorization": f"Bearer {stranger['access_token']}"}
    )
    assert resp.status_code == 404
    assert _error(resp)["code"] == "SPACE_NOT_FOUND"


# ---------------------------- 范围闸:清单内全部路由都受档位知识库约束 ----------------------------


def test_member_kb_list_is_narrowed_by_group(client, monkeypatch) -> None:
    _owner, auth, space_id, kb_a, kb_b = _scene(client)
    editor = _member(client, auth, space_id, "dualedgeo2", 20)
    _give_group(
        client, auth, space_id, editor["id"], name="只A库", capabilities=["chat"], kb_ids=[kb_a]
    )
    _gate_on(monkeypatch)
    listed = client.get(_base(space_id), headers=editor["auth"])
    assert listed.status_code == 200
    assert [kb["id"] for kb in listed.json()] == [kb_a]
    # 直接点名范围外的库 → 403,不静默放行也不降级成"全部"
    resp = client.get(f"{_base(space_id)}/{kb_b}", headers=editor["auth"])
    assert resp.status_code == 403
    assert _error(resp)["code"] == "API_KEY_SCOPE_DENIED"
    assert _error(resp)["details"]["actor"] == "member"


def test_chat_only_member_can_read_docs_list_but_not_upload(client, monkeypatch) -> None:
    """两道闸分工的落点:库列表/文档列表只卡范围,上传才 additionally 卡能力。"""
    _owner, auth, space_id, kb_a, _kb_b = _scene(client)
    editor = _member(client, auth, space_id, "dualedgeo3", 20)
    _give_group(client, auth, space_id, editor["id"], name="纯问答", capabilities=["chat"])
    _gate_on(monkeypatch)
    assert client.get(_base(space_id), headers=editor["auth"]).status_code == 200
    assert (
        client.get(f"{_base(space_id)}/{kb_a}/documents", headers=editor["auth"]).status_code
        == 200
    )
    resp = client.post(
        f"{_base(space_id)}/{kb_a}/documents",
        files={"file": ("人上传.md", "# 内容".encode(), "text/markdown")},
        headers=editor["auth"],
    )
    assert resp.status_code == 403
    assert _error(resp)["code"] == "API_KEY_CAPABILITY_DENIED"
    details = _error(resp)["details"]
    assert details["actor"] == "member"
    assert details["group_name"] == "纯问答"
    assert details["capability"] == "documents"


def test_documents_group_member_uploads_within_scope(client, monkeypatch) -> None:
    """同一档位给了 documents 能力后,范围库上传即放行(改档位 = 即时生效,无需重登)。"""
    _owner, auth, space_id, kb_a, kb_b = _scene(client)
    editor = _member(client, auth, space_id, "dualedgeo4", 20)
    _give_group(
        client,
        auth,
        space_id,
        editor["id"],
        name="文档档",
        capabilities=["chat", "documents"],
        kb_ids=[kb_a],
    )
    _gate_on(monkeypatch)
    resp = client.post(
        f"{_base(space_id)}/{kb_a}/documents",
        files={"file": ("范围内.md", "# 内容".encode(), "text/markdown")},
        headers=editor["auth"],
    )
    assert resp.status_code == 201, resp.text
    assert client.post(
        f"{_base(space_id)}/{kb_b}/documents",
        files={"file": ("范围外.md", "# 内容".encode(), "text/markdown")},
        headers=editor["auth"],
    ).status_code == 403


def test_ask_needs_chat_capability_and_scope(client, monkeypatch) -> None:
    """/ask:有档位没能力 → 能力 403;有能力但请求范围外库 → 范围 403;通过后进 SSE。"""
    _owner, auth, space_id, kb_a, _kb_b = _scene(client)
    editor = _member(client, auth, space_id, "dualedgeo5", 20)
    _give_group(client, auth, space_id, editor["id"], name="文档档", capabilities=["documents"])
    _gate_on(monkeypatch)
    url = f"/api/v1/spaces/{space_id}/ask"
    resp = client.post(url, json={"question": "q"}, headers=editor["auth"])
    assert resp.status_code == 403
    details = _error(resp)["details"]
    assert details["actor"] == "member"
    assert details["group_name"] == "文档档"
    assert details["capability"] == "chat"
    # 换成 chat 档(全部库)后进入流;模型未配置是在流里报 error,不是 HTTP 403
    _give_group(
        client, auth, space_id, editor["id"], name="问答档", capabilities=["chat"], all_kbs=True
    )
    resp = client.post(url, json={"question": "q", "kb_ids": [kb_a]}, headers=editor["auth"])
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")


# ---------------------------- 移出成员时挂载行的处置(弹窗的两个选项) ----------------------------


def test_removal_clears_mount_by_default(client, monkeypatch) -> None:
    _owner, auth, space_id, _kb_a, _kb_b = _scene(client)
    editor = _member(client, auth, space_id, "dualedgeo6", 20)
    _give_group(client, auth, space_id, editor["id"], name="问答档", capabilities=["chat"])
    _gate_on(monkeypatch)
    assert client.get(_base(space_id), headers=editor["auth"]).status_code == 200

    resp = client.delete(f"/api/v1/spaces/{space_id}/members/{editor['id']}", headers=auth)
    assert resp.status_code == 204
    logs = client.get(f"/api/v1/spaces/{space_id}/audit-logs", headers=auth).json()["items"]
    entry = next(x for x in logs if x["action"] == "space.member_removed")
    assert entry["detail"] == {
        "keep_permission_mounts": False,
        "permission_mounts_cleared": 1,
    }
    _readd(client, auth, editor["username"], space_id)
    # 重新入空间不会悄悄带回原档位:他又回到"没配档位"的状态
    resp = client.get(_base(space_id), headers=editor["auth"])
    assert resp.status_code == 403
    assert _error(resp)["code"] == "PERMISSION_GROUP_REQUIRED"


def test_removal_can_keep_mount_on_request(client, monkeypatch) -> None:
    """弹窗里选"保留权限"= `keep_mounts=true`:重新拉回来即恢复,不必再配一次档位。"""
    _owner, auth, space_id, _kb_a, _kb_b = _scene(client)
    editor = _member(client, auth, space_id, "dualedgeo7", 20)
    _give_group(client, auth, space_id, editor["id"], name="问答档", capabilities=["chat"])
    _gate_on(monkeypatch)
    resp = client.delete(
        f"/api/v1/spaces/{space_id}/members/{editor['id']}",
        params={"keep_mounts": "true"},
        headers=auth,
    )
    assert resp.status_code == 204
    _readd(client, auth, editor["username"], space_id)
    assert client.get(_base(space_id), headers=editor["auth"]).status_code == 200
    logs = client.get(f"/api/v1/spaces/{space_id}/audit-logs", headers=auth).json()["items"]
    entry = next(x for x in logs if x["action"] == "space.member_removed")
    assert entry["detail"]["keep_permission_mounts"] is True
    assert entry["detail"]["permission_mounts_kept"] == 1


def _readd(client, auth: dict, username: str, space_id: str) -> None:
    """把人重新拉回空间(集成夹具共用一个库,移出后必须能还原再断言)。"""
    resp = client.post(
        f"/api/v1/spaces/{space_id}/members",
        json={"username": username, "role": 20},
        headers=auth,
    )
    assert resp.status_code == 201, resp.text
