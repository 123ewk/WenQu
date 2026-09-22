"""权限档位服务单测(OPT-41):建档/改权限/分配/换档的门槛与 fail-closed 口径。

测试意图(权限模型最容易悄悄跑偏的地方,不测实现细节):
- 档位是**权限源**,所以写入门槛与建 Key 一致(Admin+),读入门槛与列表一致(VIEWER+);
- 范围两个字段必须互相约束:`all_kbs=true` 还带 kb_ids 是矛盾输入(422),
  `all_kbs=false` + 空列表是合法输入且含义为"一个库都不给"(fail-closed);
- 改权限要能追溯:审计里必须同时有改前与改后的能力清单与范围;
- 删除只允许删空档:还挂着凭据或成员的档位一律拒绝(级联会一次抹掉一批人的权限,
  且明文 Key 不可回显,事后无法向使用方交代);
- 分配与换档是两个动作:`assign` 不自动置 current;`set_current` 前必须先分配,
  且同一人同一空间原 current 自动让位(每人至多一档);
- 跨空间的档位 id 一律 404,不泄露存在性。
"""

from __future__ import annotations

import types

import pytest

from app.application.service.permission_groups import PermissionGroupService
from app.core.errors import AppError, ErrorCode
from app.domain.enums import ApiCapability, AuditAction, Role
from app.domain.models import KnowledgeBase
from tests.test_api_key_service import _perm, make_api_env

_FORBIDDEN = ErrorCode.FORBIDDEN[1]
_NOT_FOUND = ErrorCode.NOT_FOUND[1]
_VALIDATION = ErrorCode.VALIDATION[1]
_KB_NOT_FOUND = ErrorCode.KB_NOT_FOUND[1]
_MEMBER_NOT_FOUND = ErrorCode.MEMBER_NOT_FOUND[1]


def make_group_env() -> types.SimpleNamespace:
    """档位服务与 Key 服务同仓共存(真实生产形态就是同一套 fake 仓储)。"""
    scene = make_api_env()
    scene.group_svc = PermissionGroupService(
        scene.groups, scene.env.spaces, scene.kbs, scene.env.audit, scene.api_keys, scene.env.users
    )
    return scene


def _create(scene: types.SimpleNamespace, **overrides) -> object:
    params: dict = {
        "name": "只读问答-产品库",
        "description": "",
        "capabilities": [ApiCapability.CHAT],
        "kb_ids": [],
        "all_kbs": True,
    }
    params.update(overrides)
    return scene.group_svc.create(scene.space_id, scene.owner, **params)


def _add_kb(scene: types.SimpleNamespace, name: str):
    kb = KnowledgeBase(space_id=scene.space_id, name=name, created_by=scene.owner.id)
    scene.kbs.create(kb)
    return kb.id


def _logs(scene: types.SimpleNamespace, action: AuditAction) -> list:
    return [x for x in scene.env.audit.logs if x.action == action]


# ---------------------------- 建档 ----------------------------


def test_admin_creates_group_with_explicit_all_kbs() -> None:
    scene = make_group_env()
    group = _create(scene)
    assert group.space_id == scene.space_id
    assert group.all_kbs is True and group.kb_ids == []
    assert group.capabilities == [str(ApiCapability.CHAT)]
    assert group.created_by == scene.owner.id
    assert len(_logs(scene, AuditAction.PERMISSION_GROUP_CREATED)) == 1


def test_empty_scope_without_all_kbs_means_no_kb_at_all() -> None:
    """fail-closed:漏勾"全部库"且没勾具体库 = 一个都不给,而不是"默认全空间"。"""
    scene = make_group_env()
    group = _create(scene, all_kbs=False)
    assert group.all_kbs is False and group.kb_ids == []


def test_all_kbs_true_with_kb_ids_is_a_contradiction() -> None:
    scene = make_group_env()
    kb = _add_kb(scene, "产品手册")
    with pytest.raises(AppError) as exc:
        _create(scene, all_kbs=True, kb_ids=[kb])
    assert exc.value.code_str == _VALIDATION


def test_unknown_capability_rejected_with_allowed_values() -> None:
    """拼错的能力名不能变成"静默少给一个能力"(与 API Key 共用同一份校验)。"""
    scene = make_group_env()
    with pytest.raises(AppError) as exc:
        _create(scene, capabilities=["chatt"])
    assert exc.value.code_str == _VALIDATION
    assert str(ApiCapability.CHAT) in exc.value.message


def test_kb_outside_space_rejected() -> None:
    """范围里的库必须属于本空间,否则就是跨租户指向。"""
    scene = make_group_env()
    other, _ = scene.env.space_svc.create(scene.admin, "别的空间", "")
    other_kb = KnowledgeBase(space_id=other.id, name="别人的库", created_by=scene.admin.id)
    scene.kbs.create(other_kb)
    with pytest.raises(AppError) as exc:
        _create(scene, all_kbs=False, kb_ids=[other_kb.id])
    assert exc.value.code_str == _KB_NOT_FOUND


def test_editor_cannot_create_group() -> None:
    scene = make_group_env()
    with pytest.raises(AppError) as exc:
        scene.group_svc.create(
            scene.space_id,
            scene.editor,
            "x",
            "",
            [ApiCapability.CHAT],
            [],
            True,
        )
    assert exc.value.code_str == _FORBIDDEN


# ---------------------------- 改权限(N8) ----------------------------


def test_update_records_before_and_after_scope() -> None:
    """"谁把范围从全部放成 1 个库"必须能从一条审计里读出来。"""
    scene = make_group_env()
    kb = _add_kb(scene, "产品手册")
    group = _create(scene, capabilities=[ApiCapability.CHAT])
    updated = scene.group_svc.update(
        scene.space_id,
        scene.owner,
        group.id,
        name="只读问答-仅产品库",
        description="",
        capabilities=[ApiCapability.CHAT, ApiCapability.DOCUMENTS],
        kb_ids=[kb],
        all_kbs=False,
    )
    assert updated.all_kbs is False and updated.kb_ids == [str(kb)]
    assert updated.name == "只读问答-仅产品库"
    log = _logs(scene, AuditAction.PERMISSION_GROUP_UPDATED)[-1]
    assert log.detail["kb_scope_before"] == "all"
    assert log.detail["kb_scope_after"] == "1"
    assert log.detail["capabilities_before"] == [str(ApiCapability.CHAT)]
    assert log.detail["capabilities_after"] == [
        str(ApiCapability.CHAT),
        str(ApiCapability.DOCUMENTS),
    ]


def test_update_changes_a_key_that_is_already_in_use() -> None:
    """"修复权限"的落点:改档位即时作用于其下已签发的 Key,不必吊销重签。"""
    scene = make_group_env()
    kb_a = _add_kb(scene, "A库")
    kb_b = _add_kb(scene, "B库")
    group = _create(scene, all_kbs=False, kb_ids=[kb_a])
    created = scene.svc.create(
        scene.space_id, scene.owner, owner_user_id=scene.owner.id, group_id=group.id, name="ci"
    )
    assert _perm(scene, created.api_key).resolve_kb_scope([kb_a, kb_b]) == [kb_a]
    scene.group_svc.update(
        scene.space_id,
        scene.owner,
        group.id,
        name=group.name,
        description="",
        capabilities=[ApiCapability.CHAT],
        kb_ids=[kb_b],
        all_kbs=False,
    )
    assert _perm(scene, created.api_key).resolve_kb_scope([kb_a, kb_b]) == [kb_b]


def test_update_other_space_group_404() -> None:
    scene = make_group_env()
    group = _create(scene)
    other, _ = scene.env.space_svc.create(scene.admin, "别的空间", "")
    with pytest.raises(AppError) as exc:
        scene.group_svc.update(
            other.id,
            scene.admin,
            group.id,
            name="改名",
            description="",
            capabilities=[],
            kb_ids=[],
            all_kbs=False,
        )
    assert exc.value.code_str == _NOT_FOUND


def test_update_requires_admin() -> None:
    scene = make_group_env()
    group = _create(scene)
    with pytest.raises(AppError) as exc:
        scene.group_svc.update(
            scene.space_id,
            scene.editor,
            group.id,
            name="x",
            description="",
            capabilities=[],
            kb_ids=[],
            all_kbs=False,
        )
    assert exc.value.code_str == _FORBIDDEN


# ---------------------------- 删除 ----------------------------


def test_delete_clean_group_writes_audit() -> None:
    scene = make_group_env()
    group = _create(scene)
    scene.group_svc.delete(scene.space_id, scene.owner, group.id)
    assert scene.groups.get(group.id) is None
    assert len(_logs(scene, AuditAction.PERMISSION_GROUP_DELETED)) == 1


def test_delete_refused_while_keys_attached() -> None:
    """档位下还有凭据就不许删:FK 是 RESTRICT,服务层要先给出可读错误而不是 500。"""
    scene = make_group_env()
    group = _create(scene)
    scene.svc.create(
        scene.space_id, scene.owner, owner_user_id=scene.owner.id, group_id=group.id, name="ci"
    )
    with pytest.raises(AppError) as exc:
        scene.group_svc.delete(scene.space_id, scene.owner, group.id)
    assert exc.value.code_str == _VALIDATION
    assert exc.value.details["key_count"] == 1


def test_delete_refused_while_members_attached() -> None:
    scene = make_group_env()
    group = _create(scene)
    scene.group_svc.assign(scene.space_id, scene.owner, group.id, scene.editor.id)
    with pytest.raises(AppError) as exc:
        scene.group_svc.delete(scene.space_id, scene.owner, group.id)
    assert exc.value.code_str == _VALIDATION
    assert exc.value.details["member_count"] == 1


# ---------------------------- 分配与换档 ----------------------------


def test_assign_requires_space_membership() -> None:
    scene = make_group_env()
    stranger = scene.env.auth.register("stranger", "路人", "secret-pass-9")
    group = _create(scene)
    with pytest.raises(AppError) as exc:
        scene.group_svc.assign(scene.space_id, scene.owner, group.id, stranger.user.id)
    assert exc.value.code_str == _MEMBER_NOT_FOUND


def test_assign_is_idempotent_and_does_not_set_current() -> None:
    """分配两次只有一行挂载;分配不等于"他从此用这一档"(那是 set_current 的动作)。"""
    scene = make_group_env()
    group = _create(scene)
    scene.group_svc.assign(scene.space_id, scene.owner, group.id, scene.editor.id)
    scene.group_svc.assign(scene.space_id, scene.owner, group.id, scene.editor.id)
    assert len(scene.groups.list_members(group.id)) == 1
    assert len(_logs(scene, AuditAction.PERMISSION_GROUP_MEMBER_ASSIGNED)) == 1
    assert scene.groups.get_current(scene.space_id, scene.editor.id) is None


def test_assign_requires_admin() -> None:
    scene = make_group_env()
    group = _create(scene)
    with pytest.raises(AppError) as exc:
        scene.group_svc.assign(scene.space_id, scene.editor, group.id, scene.editor.id)
    assert exc.value.code_str == _FORBIDDEN


def test_set_current_moves_pointer_and_clears_previous() -> None:
    """换档 = 每人每空间至多一档:新档置位,原档自动落位。"""
    scene = make_group_env()
    a = _create(scene, name="档A")
    b = _create(scene, name="档B")
    scene.group_svc.assign(scene.space_id, scene.owner, a.id, scene.editor.id)
    scene.group_svc.assign(scene.space_id, scene.owner, b.id, scene.editor.id)
    scene.group_svc.set_current(scene.space_id, scene.owner, a.id, scene.editor.id)
    scene.group_svc.set_current(scene.space_id, scene.owner, b.id, scene.editor.id)
    current = scene.groups.get_current(scene.space_id, scene.editor.id)
    assert current is not None and current.group_id == b.id
    assert scene.groups.get_member(a.id, scene.editor.id).is_current is False


def test_set_current_is_idempotent() -> None:
    scene = make_group_env()
    group = _create(scene)
    scene.group_svc.assign(scene.space_id, scene.owner, group.id, scene.editor.id)
    scene.group_svc.set_current(scene.space_id, scene.owner, group.id, scene.editor.id)
    scene.group_svc.set_current(scene.space_id, scene.owner, group.id, scene.editor.id)
    assert len(_logs(scene, AuditAction.PERMISSION_GROUP_CURRENT_SET)) == 1


def test_set_current_requires_prior_assignment() -> None:
    """没分配就能换档 = 绕过 assign 的审计与成员校验。"""
    scene = make_group_env()
    group = _create(scene)
    with pytest.raises(AppError) as exc:
        scene.group_svc.set_current(scene.space_id, scene.owner, group.id, scene.editor.id)
    assert exc.value.code_str == _VALIDATION


def test_unassign_reports_whether_it_was_current() -> None:
    scene = make_group_env()
    group = _create(scene)
    scene.group_svc.assign(scene.space_id, scene.owner, group.id, scene.editor.id)
    scene.group_svc.set_current(scene.space_id, scene.owner, group.id, scene.editor.id)
    scene.group_svc.unassign(scene.space_id, scene.owner, group.id, scene.editor.id)
    assert scene.groups.get_current(scene.space_id, scene.editor.id) is None
    log = _logs(scene, AuditAction.PERMISSION_GROUP_MEMBER_UNASSIGNED)[-1]
    assert log.detail["was_current"] is True


def test_unassign_unknown_mount_404() -> None:
    scene = make_group_env()
    group = _create(scene)
    with pytest.raises(AppError) as exc:
        scene.group_svc.unassign(scene.space_id, scene.owner, group.id, scene.editor.id)
    assert exc.value.code_str == _NOT_FOUND


# ---------------------------- 列表视图 ----------------------------


def test_list_view_shows_members_with_live_role_and_current_flag() -> None:
    scene = make_group_env()
    group = _create(scene)
    scene.group_svc.assign(scene.space_id, scene.owner, group.id, scene.editor.id)
    scene.group_svc.set_current(scene.space_id, scene.owner, group.id, scene.editor.id)
    view = next(v for v in scene.group_svc.list_for_space(scene.space_id, scene.owner)
                if v.group.id == group.id)
    (member,) = view.members
    assert member.user_id == scene.editor.id
    assert member.username == "editor"
    assert member.role == Role.EDITOR
    assert member.is_current is True


def test_removal_clears_mounts_by_default() -> None:
    """OPT-41:移出成员默认连带删掉档位挂载(留着的挂载是"没人能看见的权限")。

    想保留(比如只是临时移出、之后拉回来即恢复)必须显式 `keep_mounts=True`,
    这条选择由前端弹窗问操作者,服务端默认走安全的一侧。
    """
    scene = make_group_env()
    group = _create(scene)
    scene.group_svc.assign(scene.space_id, scene.owner, group.id, scene.editor.id)
    scene.env.space_svc.remove_member(group.space_id, scene.owner, scene.editor.id)
    assert scene.groups.members == {}


def test_removal_can_keep_mounts_on_request() -> None:
    """保留时挂载行还在,但人已不是成员 → 列表 role 取不到,前端据此置灰。"""
    scene = make_group_env()
    group = _create(scene)
    scene.group_svc.assign(scene.space_id, scene.owner, group.id, scene.editor.id)
    scene.env.space_svc.remove_member(
        group.space_id, scene.owner, scene.editor.id, keep_mounts=True
    )
    view = scene.group_svc.get(scene.space_id, scene.owner, group.id)
    (member,) = view.members
    assert member.role is None


def test_member_can_list_groups() -> None:
    """读门槛与 Key 列表一致(VIEWER+):成员要能看到自己挂在哪个档位上。"""
    scene = make_group_env()
    _create(scene)
    assert len(scene.group_svc.list_for_space(scene.space_id, scene.editor)) == 1


def test_get_other_space_group_404() -> None:
    scene = make_group_env()
    group = _create(scene)
    other, _ = scene.env.space_svc.create(scene.admin, "别的空间", "")
    with pytest.raises(AppError) as exc:
        scene.group_svc.get(other.id, scene.admin, group.id)
    assert exc.value.code_str == _NOT_FOUND
