"""空间服务单元测试:RBAC 数值阶梯、404/403 守卫语义、审计写入(基准 04)。"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.core.errors import AppError
from app.domain.enums import AuditAction, Role
from tests.test_auth_service import make_services


def setup_two_users() -> SimpleNamespace:
    """owner 建空间;editor 被拉入为 Editor;返回场景对象。"""
    env = make_services()
    owner = env.auth.register("owner", "所有者", "secret-pass-1")
    editor = env.auth.register("editor", "编辑者", "secret-pass-2")
    outsider = env.auth.register("outsider", "路人", "secret-pass-3")
    space, role = env.space_svc.create(owner.user, "研发空间", "描述")
    assert role == Role.OWNER
    env.space_svc.add_member(space.id, owner.user, "editor", Role.EDITOR)
    return SimpleNamespace(
        env=env, owner=owner.user, editor=editor.user, outsider=outsider.user, space_id=space.id
    )


def test_non_member_gets_404_not_403() -> None:
    scene = setup_two_users()
    with pytest.raises(AppError) as exc_info:
        scene.env.space_svc.get_with_role(scene.space_id, scene.outsider.id)
    assert exc_info.value.code_str == "SPACE_NOT_FOUND"


def test_update_requires_admin() -> None:
    scene = setup_two_users()
    # Editor(20) < Admin(30) → 403
    with pytest.raises(AppError) as low_role:
        scene.env.space_svc.update(scene.space_id, scene.editor, "新名", "")
    assert low_role.value.code_str == "FORBIDDEN"
    # Owner 更新成功且写审计
    space = scene.env.space_svc.update(scene.space_id, scene.owner, "新名", "新描述")
    assert space.name == "新名"
    actions = [log.action for log in scene.env.audit.logs]
    assert str(AuditAction.SPACE_UPDATED) in actions


def test_update_description_none_keeps_value() -> None:
    """PATCH 语义(前端缺口台账 §9.6):description 省略 = 不改动;显式 "" 才清空。

    此前省略会被无条件清成空串,前端只能把 name/description 一并提交防清空。
    """
    scene = setup_two_users()
    space = scene.env.space_svc.update(scene.space_id, scene.owner, "改名", "原始描述")
    assert space.description == "原始描述"

    kept = scene.env.space_svc.update(scene.space_id, scene.owner, "再改名")
    assert kept.name == "再改名"
    assert kept.description == "原始描述"  # 省略不动

    cleared = scene.env.space_svc.update(scene.space_id, scene.owner, "再改名", "")
    assert cleared.description == ""  # 显式清空仍可用


def test_delete_owner_only() -> None:
    scene = setup_two_users()
    with pytest.raises(AppError) as forbidden:
        scene.env.space_svc.delete(scene.space_id, scene.editor)
    assert forbidden.value.code_str == "FORBIDDEN"
    scene.env.space_svc.delete(scene.space_id, scene.owner)
    assert scene.env.spaces.get(scene.space_id) is None
    # 空间删除后成员关系级联清理
    assert scene.env.spaces.get_membership(scene.space_id, scene.owner.id) is None


def test_add_member_role_rules() -> None:
    scene = setup_two_users()
    env = scene.env
    # 非成员拉人 → 404(须在外人被拉入前断言)
    with pytest.raises(AppError) as not_member:
        env.space_svc.add_member(scene.space_id, scene.outsider, "editor", Role.VIEWER)
    assert not_member.value.code_str == "SPACE_NOT_FOUND"
    # Owner 可拉 Admin
    m, target = env.space_svc.add_member(scene.space_id, scene.owner, "outsider", Role.ADMIN)
    assert m.role == Role.ADMIN and target.username == "outsider"
    # Editor 拉人 → 角色不足(先过 ADMIN 门槛)
    with pytest.raises(AppError) as low:
        env.space_svc.add_member(scene.space_id, scene.editor, "x", Role.VIEWER)
    assert low.value.code_str == "FORBIDDEN"
    # 未注册用户 → 404
    with pytest.raises(AppError) as unknown_user:
        env.space_svc.add_member(scene.space_id, scene.owner, "ghost", Role.EDITOR)
    assert unknown_user.value.code_str == "USER_NOT_FOUND"
    # 重复加入 → 409
    with pytest.raises(AppError) as dup:
        env.space_svc.add_member(scene.space_id, scene.owner, "editor", Role.VIEWER)
    assert dup.value.code_str == "MEMBER_ALREADY"


def test_add_member_cannot_grant_own_role_or_higher() -> None:
    scene = setup_two_users()
    env = scene.env
    with pytest.raises(AppError) as too_high:
        env.space_svc.add_member(scene.space_id, scene.owner, "outsider", Role.OWNER)
    assert too_high.value.code_str == "FORBIDDEN"


def test_change_role_transfer_ownership() -> None:
    scene = setup_two_users()
    env = scene.env
    editor_member = env.spaces.get_membership(scene.space_id, scene.editor.id)
    assert editor_member is not None

    membership, target_user = env.space_svc.change_role(
        scene.space_id, scene.owner, scene.editor.id, Role.OWNER
    )
    assert membership.role == Role.OWNER
    assert target_user.id == scene.editor.id
    # 原 Owner 自动降级 Admin
    owner_member = env.spaces.get_membership(scene.space_id, scene.owner.id)
    assert owner_member is not None and owner_member.role == Role.ADMIN


def test_change_role_guard_rules() -> None:
    scene = setup_two_users()
    env = scene.env
    editor_id = scene.editor.id
    # 自己改自己 → 403
    with pytest.raises(AppError) as self_change:
        env.space_svc.change_role(scene.space_id, scene.editor, editor_id, Role.ADMIN)
    assert self_change.value.code_str == "FORBIDDEN"
    # Owner 改自己 → 403(只能转让)
    with pytest.raises(AppError) as owner_self:
        env.space_svc.change_role(scene.space_id, scene.owner, scene.owner.id, Role.ADMIN)
    assert owner_self.value.code_str == "FORBIDDEN"
    # 改不存在的成员 → 404
    with pytest.raises(AppError) as ghost:
        env.space_svc.change_role(scene.space_id, scene.owner, uuid.uuid4(), Role.EDITOR)
    assert ghost.value.code_str == "MEMBER_NOT_FOUND"


def test_remove_member_rules() -> None:
    scene = setup_two_users()
    env = scene.env
    # Admin(编辑者不行,这里是 owner 操作)移除 editor → 成功
    env.space_svc.remove_member(scene.space_id, scene.owner, scene.editor.id)
    assert env.spaces.get_membership(scene.space_id, scene.editor.id) is None
    # 重新拉入后测边界
    env.space_svc.add_member(scene.space_id, scene.owner, "editor", Role.EDITOR)
    # 移除自己 → 引导走退出
    with pytest.raises(AppError) as self_remove:
        env.space_svc.remove_member(scene.space_id, scene.owner, scene.owner.id)
    assert self_remove.value.code_str == "FORBIDDEN"
    # 移除不存在成员 → 404
    with pytest.raises(AppError) as ghost:
        env.space_svc.remove_member(scene.space_id, scene.owner, uuid.uuid4())
    assert ghost.value.code_str == "MEMBER_NOT_FOUND"


def test_owner_cannot_leave() -> None:
    scene = setup_two_users()
    with pytest.raises(AppError) as forbidden:
        scene.env.space_svc.leave(scene.space_id, scene.owner)
    assert forbidden.value.code_str == "FORBIDDEN"


def test_editor_can_leave() -> None:
    scene = setup_two_users()
    scene.env.space_svc.leave(scene.space_id, scene.editor)
    assert scene.env.spaces.get_membership(scene.space_id, scene.editor.id) is None
    actions = [log.action for log in scene.env.audit.logs]
    assert actions.count(str(AuditAction.MEMBER_REMOVED)) >= 1


def test_audit_requires_admin() -> None:
    scene = setup_two_users()
    logs, total = scene.env.space_svc.list_audit(scene.space_id, scene.owner, 50, 0)
    assert total >= 2  # 建空间 + 拉成员
    assert logs[0].created_at is not None
    with pytest.raises(AppError) as forbidden:
        scene.env.space_svc.list_audit(scene.space_id, scene.editor, 50, 0)
    assert forbidden.value.code_str == "FORBIDDEN"


def test_list_audit_filters_by_action() -> None:
    """按动作类型筛选:只返回该动作,且 total 是筛选后的数量(前端筛选条,缺口 #1)。"""
    scene = setup_two_users()
    env = scene.env
    env.space_svc.update(scene.space_id, scene.owner, "改名", "")

    logs, total = env.space_svc.list_audit(
        scene.space_id, scene.owner, 50, 0, action=AuditAction.MEMBER_ADDED
    )

    assert total == 1
    assert [log.action for log in logs] == [AuditAction.MEMBER_ADDED]


def test_list_audit_filters_by_time_range_inclusive() -> None:
    """时间范围筛选两端都是闭区间:传某条日志自己的时间戳,该条必须被包含。

    边界是筛选类需求最易错处(>= 与 > 之差会让"当天"漏记录),故用真实存储的
    时间戳作为期望值来源,而不是照着重算一遍。
    """
    scene = setup_two_users()
    env = scene.env
    env.space_svc.update(scene.space_id, scene.owner, "改名", "")

    all_logs, total = env.space_svc.list_audit(scene.space_id, scene.owner, 50, 0)
    assert total == 3
    ordered = sorted(all_logs, key=lambda log: log.created_at)

    since_only, since_total = env.space_svc.list_audit(
        scene.space_id, scene.owner, 50, 0, since=ordered[1].created_at
    )
    assert since_total == 2  # 起点那条也包含
    assert ordered[1] in since_only

    until_only, until_total = env.space_svc.list_audit(
        scene.space_id, scene.owner, 50, 0, until=ordered[1].created_at
    )
    assert until_total == 2  # 终点那条也包含
    assert ordered[1] in until_only

    both, both_total = env.space_svc.list_audit(
        scene.space_id,
        scene.owner,
        50,
        0,
        since=ordered[0].created_at,
        until=ordered[1].created_at,
    )
    assert both_total == 2


def test_list_audit_filters_by_actor() -> None:
    """按操作人筛选:editor 被拉入后自己改不了空间,故用 owner 拉人+editor 退出来区分。"""
    scene = setup_two_users()
    env = scene.env
    env.space_svc.update(scene.space_id, scene.owner, "改名", "")  # actor=owner
    env.space_svc.leave(scene.space_id, scene.editor)  # actor=editor

    logs, total = env.space_svc.list_audit(
        scene.space_id, scene.owner, 50, 0, actor_id=scene.editor.id
    )

    assert total == 1
    assert logs[0].actor_id == scene.editor.id
    assert logs[0].action == AuditAction.MEMBER_REMOVED


