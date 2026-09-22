"""API Key 服务单测(OPT-6 / OPT-26):签发绑成员/认证双校验/能力授权/范围收窄/吊销。

测试意图(安全语义为主,不测实现细节):
- 明文只在创建时出现一次,入库只有哈希 —— 库被读走也不能反推 Key;
- OPT-26:签发必须绑定空间成员,操作者门槛 Admin+(与"拉成员"同闸);
  认证 = Key 有效 + 绑定成员仍是成员 双重校验,不留"孤儿凭据";
- 未声明的能力一律拒绝(fail-closed),空能力清单不等于"放行";
- KB 范围是"二次收窄":范围外的 KB 即使 Key 有 retrieval 能力也拒绝;
- 吊销、绑定成员被移出都要立刻失效;创建者(签发人)被移出不影响 Key。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.application.service.api_keys import ApiKeyService
from app.core.errors import AppError, ErrorCode
from app.domain.enums import ApiCapability, AuditAction, Role
from app.domain.models import KnowledgeBase
from tests.fakes import FakeKnowledgeBaseRepository, FakePermissionGroupRepository
from tests.test_auth_service import make_services

_FORBIDDEN = ErrorCode.FORBIDDEN[1]
_NOT_FOUND = ErrorCode.NOT_FOUND[1]
_SPACE_NOT_FOUND = ErrorCode.SPACE_NOT_FOUND[1]
_VALIDATION = ErrorCode.VALIDATION[1]
_KB_NOT_FOUND = ErrorCode.KB_NOT_FOUND[1]
_MEMBER_NOT_FOUND = ErrorCode.MEMBER_NOT_FOUND[1]
_CAPABILITY_DENIED = ErrorCode.API_KEY_CAPABILITY_DENIED[1]
_SCOPE_DENIED = ErrorCode.API_KEY_SCOPE_DENIED[1]


class FakeApiKeyRepository:
    """内存版 API Key 仓储:与 domain/interfaces.ApiKeyRepository 同形。"""

    def __init__(self) -> None:
        self.keys: dict[uuid.UUID, object] = {}

    def get_by_hash(self, key_hash: str):
        return next((k for k in self.keys.values() if k.key_hash == key_hash), None)

    def get(self, key_id: uuid.UUID):
        return self.keys.get(key_id)

    def create(self, api_key):
        if api_key.id is None:  # 模拟 DB flush 时主键 default 生效
            api_key.id = uuid.uuid4()
        self.keys[api_key.id] = api_key
        return api_key

    def save(self, api_key):
        self.keys[api_key.id] = api_key
        return api_key

    def list_for_space(self, space_id: uuid.UUID):
        return [k for k in self.keys.values() if k.space_id == space_id]


def make_api_env() -> SimpleNamespace:
    """owner 建空间;admin(Editor 之上)与 editor 与 viewer 入空间;返回服务与底层 fake。"""
    env = make_services()
    owner = env.auth.register("owner", "所有者", "secret-pass-1")
    admin = env.auth.register("admin", "管理员", "secret-pass-2")
    editor = env.auth.register("editor", "编辑者", "secret-pass-3")
    space, _ = env.space_svc.create(owner.user, "研发空间", "")
    env.space_svc.add_member(space.id, owner.user, "admin", Role.ADMIN)
    env.space_svc.add_member(space.id, owner.user, "editor", Role.EDITOR)
    api_keys = FakeApiKeyRepository()
    kbs = FakeKnowledgeBaseRepository()
    groups = FakePermissionGroupRepository()
    svc = ApiKeyService(api_keys, env.spaces, kbs, env.audit, env.users, groups=groups)
    return SimpleNamespace(
        env=env,
        svc=svc,
        api_keys=api_keys,
        kbs=kbs,
        groups=groups,
        owner=owner.user,
        admin=admin.user,
        editor=editor.user,
        space_id=space.id,
    )


def _add_kb(scene: SimpleNamespace, name: str) -> uuid.UUID:
    kb = KnowledgeBase(space_id=scene.space_id, name=name, created_by=scene.owner.id)
    scene.kbs.create(kb)
    return kb.id


def _create(scene: SimpleNamespace, **overrides):
    params = {
        "member_user_id": scene.owner.id,  # 默认绑到签发人自己
        "name": "ci-pipeline",
        "description": "",
        "capabilities": [ApiCapability.CHAT],
        "kb_ids": [],
    }
    params.update(overrides)
    return scene.svc.create(scene.space_id, scene.owner, **params)


# ---------------------------- 创建与存储 ----------------------------


def test_created_key_has_sk_prefix_and_full_plaintext_returned_once() -> None:
    scene = make_api_env()
    created = _create(scene)
    assert created.plaintext.startswith("sk-")
    assert len(created.plaintext) > 20
    assert created.api_key.key_hint.startswith("sk-")
    assert created.api_key.member_user_id == scene.owner.id


def test_hint_is_not_the_full_key() -> None:
    """提示串必须是截断值,否则等于没脱敏。"""
    scene = make_api_env()
    created = _create(scene)
    assert created.plaintext not in created.api_key.key_hint
    assert len(created.api_key.key_hint) < len(created.plaintext)


def test_plaintext_never_stored_only_hash() -> None:
    scene = make_api_env()
    created = _create(scene)
    stored = scene.api_keys.get(created.api_key.id)
    assert stored is not None
    assert not hasattr(stored, "key_plaintext")
    for field in ("name", "description", "key_hash", "key_hint"):
        assert created.plaintext not in str(getattr(stored, field, ""))


def test_hash_is_sha256_hex() -> None:
    scene = make_api_env()
    created = _create(scene)
    assert len(created.api_key.key_hash) == 64
    assert all(c in "0123456789abcdef" for c in created.api_key.key_hash)


def test_two_keys_are_unique() -> None:
    scene = make_api_env()
    first = _create(scene, name="a")
    second = _create(scene, name="b")
    assert first.plaintext != second.plaintext
    assert first.api_key.key_hash != second.api_key.key_hash


def test_create_requires_admin_or_higher() -> None:
    """OPT-26:建 Key 与"拉成员"同闸 —— Viewer/Editor 都不行,Admin/Owner 可以。"""
    scene = make_api_env()
    viewer = scene.env.auth.register("viewer", "只读者", "secret-pass-4")
    scene.env.space_svc.add_member(scene.space_id, scene.owner, "viewer", Role.VIEWER)
    for actor in (viewer.user, scene.editor):
        with pytest.raises(AppError) as exc:
            scene.svc.create(
                scene.space_id, actor, actor.id, "n", "", [ApiCapability.CHAT], []
            )
        assert exc.value.code_str == _FORBIDDEN
    # Admin 成员可以替自己签
    created = scene.svc.create(
        scene.space_id, scene.admin, scene.admin.id, "n", "", [ApiCapability.CHAT], []
    )
    assert created.api_key.created_by == scene.admin.id
    # Admin 也可以替别人签(绑定目标是成员即可)
    created = scene.svc.create(
        scene.space_id, scene.admin, scene.editor.id, "n2", "", [ApiCapability.CHAT], []
    )
    assert created.api_key.member_user_id == scene.editor.id


def test_create_target_must_be_member() -> None:
    """绑定目标不是本空间成员 → 404 MEMBER_NOT_FOUND。"""
    scene = make_api_env()
    outsider = scene.env.auth.register("outsider", "路人", "secret-pass-5")
    with pytest.raises(AppError) as exc:
        scene.svc.create(
            scene.space_id, scene.owner, outsider.user.id, "n", "", [ApiCapability.CHAT], []
        )
    assert exc.value.code_str == _MEMBER_NOT_FOUND


def test_create_writes_audit_with_member() -> None:
    scene = make_api_env()
    _create(scene, name="audited", member_user_id=scene.admin.id)
    logs = [
        log
        for log in scene.env.audit.logs
        if str(log.action) == str(AuditAction.API_KEY_CREATED)
    ]
    assert logs
    assert logs[-1].detail["member_user_id"] == str(scene.admin.id)


def test_unknown_capability_rejected() -> None:
    scene = make_api_env()
    with pytest.raises(AppError) as exc:
        _create(scene, capabilities=["root_everything"])
    assert exc.value.code_str == _VALIDATION


def test_kb_ids_must_belong_to_space() -> None:
    """不能把别的空间的 KB 写进范围(租户边界)。"""
    scene = make_api_env()
    with pytest.raises(AppError) as exc:
        _create(scene, kb_ids=[uuid.uuid4()])
    assert exc.value.code_str == _KB_NOT_FOUND


# ---------------------------- 认证(双重校验) ----------------------------


def test_authenticate_returns_key_for_valid_plaintext() -> None:
    scene = make_api_env()
    created = _create(scene)
    found = scene.svc.authenticate(created.plaintext)
    assert found is not None and found.id == created.api_key.id


@pytest.mark.parametrize("bad", ["", "not-a-key", "sk-live-does-not-exist"])
def test_authenticate_rejects_unknown_and_malformed(bad: str) -> None:
    scene = make_api_env()
    _create(scene)
    assert scene.svc.authenticate(bad) is None


def test_authenticate_rejects_revoked() -> None:
    scene = make_api_env()
    created = _create(scene)
    scene.svc.revoke(scene.space_id, scene.owner, created.api_key.id)
    assert scene.svc.authenticate(created.plaintext) is None


def test_authenticate_updates_last_used() -> None:
    scene = make_api_env()
    created = _create(scene)
    assert created.api_key.last_used_at is None
    scene.svc.authenticate(created.plaintext)
    assert scene.api_keys.get(created.api_key.id).last_used_at is not None


def test_bound_member_removal_invalidates_key() -> None:
    """绑定成员被移出 → Key 立刻失效;签发人(创建者)被移出不影响。"""
    scene = make_api_env()
    created = scene.svc.create(
        scene.space_id, scene.owner, scene.admin.id, "admin-key", "", [ApiCapability.CHAT], []
    )
    # 移出绑定成员(admin)→ Key 立即失效;签发人 owner 的身份无关紧要
    scene.env.space_svc.remove_member(scene.space_id, scene.owner, scene.admin.id)
    assert scene.svc.authenticate(created.plaintext) is None


def test_key_survives_creator_removal() -> None:
    """OPT-26 核心:认证只校验绑定成员;签发 Admin 被移出,Key(绑 Editor)仍有效。"""
    scene = make_api_env()
    created = scene.svc.create(
        scene.space_id,
        scene.admin,
        scene.editor.id,
        "k",
        "",
        [ApiCapability.CHAT],
        [],
    )
    scene.env.space_svc.remove_member(scene.space_id, scene.owner, scene.admin.id)
    found = scene.svc.authenticate(created.plaintext)
    assert found is not None and found.id == created.api_key.id


def test_legacy_key_falls_back_to_creator() -> None:
    """存量旧 Key(无 member_user_id)回退 created_by 判定,迁移双保险。"""
    scene = make_api_env()
    created = scene.svc.create(
        scene.space_id, scene.admin, scene.admin.id, "k", "", [ApiCapability.CHAT], []
    )
    stored = scene.api_keys.get(created.api_key.id)
    stored.member_user_id = None  # 模拟 0012 回填前的旧行:身份回到签发人 admin
    assert scene.svc.authenticate(created.plaintext) is not None
    scene.env.space_svc.remove_member(scene.space_id, scene.owner, scene.admin.id)
    assert scene.svc.authenticate(created.plaintext) is None


def test_member_of_reports_current_role() -> None:
    scene = make_api_env()
    created = scene.svc.create(
        scene.space_id,
        scene.owner,
        scene.editor.id,
        "k",
        "",
        [ApiCapability.CHAT],
        [],
    )
    member, role = scene.svc.member_of(created.api_key)
    assert member is not None and member.id == scene.editor.id
    assert role == int(Role.EDITOR)
    # 降级后角色实时变化(路由层 403 的依据)
    scene.env.space_svc.change_role(
        scene.space_id, scene.owner, scene.editor.id, int(Role.VIEWER)
    )
    _, role = scene.svc.member_of(created.api_key)
    assert role == int(Role.VIEWER)


# ---------------------------- 能力级授权(fail-closed) ----------------------------


def test_capability_check_allows_declared() -> None:
    scene = make_api_env()
    created = _create(scene, capabilities=[ApiCapability.CHAT])
    scene.svc.require_capability(created.api_key, ApiCapability.CHAT)


def test_capability_check_denies_undeclared() -> None:
    """只给了「对话检索」,用「文档管理」必须拒绝。"""
    scene = make_api_env()
    created = _create(scene, capabilities=[ApiCapability.CHAT])
    with pytest.raises(AppError) as exc:
        scene.svc.require_capability(created.api_key, ApiCapability.DOCUMENTS)
    assert exc.value.code_str == _CAPABILITY_DENIED
    assert exc.value.http_status == 403


def test_empty_capabilities_denies_everything() -> None:
    """空能力清单 = 什么都不允许(不因为"空"而放行)。"""
    scene = make_api_env()
    created = _create(scene, capabilities=[])
    for cap in ApiCapability:
        with pytest.raises(AppError):
            scene.svc.require_capability(created.api_key, cap)


# ---------------------------- KB 范围二次收窄 ----------------------------


def test_kb_scope_empty_means_all() -> None:
    scene = make_api_env()
    created = _create(scene, kb_ids=[])
    assert scene.svc.resolve_kb_scope(created.api_key, None) is None  # None = 不限制


def test_kb_scope_narrows_requested_list() -> None:
    """Key 限定 KB-A;请求同时要 A、B → 只允许 A,不因越界整体失败。"""
    scene = make_api_env()
    allowed = _add_kb(scene, "允许库")
    other = _add_kb(scene, "禁止库")
    created = _create(scene, kb_ids=[allowed])
    assert scene.svc.resolve_kb_scope(created.api_key, [allowed, other]) == [allowed]


def test_kb_scope_outside_only_request_denied() -> None:
    """请求只要范围外的 KB → 拒绝(不能悄悄降级成"全部")。"""
    scene = make_api_env()
    allowed = _add_kb(scene, "允许库")
    other = _add_kb(scene, "禁止库")
    created = _create(scene, kb_ids=[allowed])
    with pytest.raises(AppError) as exc:
        scene.svc.resolve_kb_scope(created.api_key, [other])
    assert exc.value.code_str == _SCOPE_DENIED


# ---------------------------- 吊销与列表 ----------------------------


def test_revoke_sets_revoked_at_and_writes_audit() -> None:
    scene = make_api_env()
    created = _create(scene)
    scene.svc.revoke(scene.space_id, scene.owner, created.api_key.id)
    assert scene.api_keys.get(created.api_key.id).revoked_at is not None
    assert str(AuditAction.API_KEY_REVOKED) in [
        str(log.action) for log in scene.env.audit.logs
    ]


def test_revoke_requires_admin() -> None:
    """OPT-26:吊销与签发同闸,Editor 403。"""
    scene = make_api_env()
    created = _create(scene)
    with pytest.raises(AppError) as exc:
        scene.svc.revoke(scene.space_id, scene.editor, created.api_key.id)
    assert exc.value.code_str == _FORBIDDEN
    assert created.api_key.revoked_at is None


def test_revoke_unknown_key_is_404() -> None:
    scene = make_api_env()
    with pytest.raises(AppError) as exc:
        scene.svc.revoke(scene.space_id, scene.owner, uuid.uuid4())
    assert exc.value.code_str == _NOT_FOUND


def test_revoke_cross_space_key_not_found() -> None:
    """别的空间的 Key id 不能被本空间吊销(查询按 space_id 收窄)。"""
    scene = make_api_env()
    other_space, _ = scene.env.space_svc.create(scene.owner, "另一空间", "")
    created = scene.svc.create(
        other_space.id, scene.owner, scene.owner.id, "n", "", [ApiCapability.CHAT], []
    )
    with pytest.raises(AppError) as exc:
        scene.svc.revoke(scene.space_id, scene.owner, created.api_key.id)
    assert exc.value.code_str == _NOT_FOUND


def test_list_carries_no_plaintext_and_member_view() -> None:
    """服务返回 ApiKeyWithMember 视图;响应脱敏是路由 DTO 的职责,见路由测试。"""
    scene = make_api_env()
    created = _create(scene, member_user_id=scene.admin.id)
    rows = scene.svc.list_for_space(scene.space_id, scene.owner)
    assert rows
    for row in rows:
        assert row.member is not None and row.member.id == scene.admin.id
        assert row.member_role == int(Role.ADMIN)
        assert row.created_by_name == "owner"
        for field in ("name", "description", "key_hint", "key_hash"):
            assert created.plaintext != str(getattr(row.api_key, field, ""))


def test_list_includes_revoked_for_audit_trail() -> None:
    scene = make_api_env()
    created = _create(scene)
    scene.svc.revoke(scene.space_id, scene.owner, created.api_key.id)
    rows = scene.svc.list_for_space(scene.space_id, scene.owner)
    assert [row.api_key.id for row in rows] == [created.api_key.id]
    assert rows[0].api_key.revoked_at is not None


def test_list_marks_removed_member_as_none() -> None:
    """绑定成员被移出后列表仍可查(Viewer+ 可读),成员置灰展示不炸。"""
    scene = make_api_env()
    created = scene.svc.create(
        scene.space_id,
        scene.owner,
        scene.editor.id,
        "k",
        "",
        [ApiCapability.CHAT],
        [],
    )
    scene.env.space_svc.remove_member(scene.space_id, scene.owner, scene.editor.id)
    rows = scene.svc.list_for_space(scene.space_id, scene.owner)
    view = next(r for r in rows if r.api_key.id == created.api_key.id)
    assert view.member is None and view.member_role is None


def test_non_member_gets_404_on_list() -> None:
    """非成员不能列出(404 而非 403,防枚举)。"""
    scene = make_api_env()
    outsider = scene.env.auth.register("outsider", "路人", "secret-pass-9")
    with pytest.raises(AppError) as exc:
        scene.svc.list_for_space(scene.space_id, outsider.user)
    assert exc.value.code_str == _SPACE_NOT_FOUND


# ---------------------------- 生效标记 activate(OPT-31) ----------------------------


def _activated(scene, key_id, actor=None):
    return scene.svc.activate(scene.space_id, actor or scene.owner, key_id)


def test_activate_sets_flag_and_writes_audit() -> None:
    scene = make_api_env()
    created = _create(scene)
    assert created.api_key.is_active is not True  # 创建不自动标记,由 Admin 分配
    activated = _activated(scene, created.api_key.id)
    assert activated.is_active is True
    assert scene.api_keys.get(created.api_key.id).is_active is True
    log = [x for x in scene.env.audit.logs if x.action == AuditAction.API_KEY_ACTIVATED]
    assert len(log) == 1 and log[0].target == created.api_key.name


def test_activate_is_mutually_exclusive_per_member() -> None:
    """同一成员换着分配:新激活一把,该成员原生效钥匙自动置 false;别的成员不受影响。"""
    scene = make_api_env()
    mine_a = _create(scene, name="mine-a")
    mine_b = _create(scene, name="mine-b")
    theirs = scene.svc.create(
        scene.space_id, scene.owner, scene.admin.id, "theirs", "", [ApiCapability.CHAT], []
    )
    _activated(scene, theirs.api_key.id, actor=scene.owner)
    _activated(scene, mine_a.api_key.id)
    _activated(scene, mine_b.api_key.id)
    assert scene.api_keys.get(mine_a.api_key.id).is_active is False
    assert scene.api_keys.get(mine_b.api_key.id).is_active is True
    assert scene.api_keys.get(theirs.api_key.id).is_active is True


def test_activate_requires_admin() -> None:
    scene = make_api_env()
    created = scene.svc.create(
        scene.space_id, scene.owner, scene.editor.id, "k", "", [ApiCapability.CHAT], []
    )
    with pytest.raises(AppError) as exc:
        scene.svc.activate(scene.space_id, scene.editor, created.api_key.id)
    assert exc.value.code_str == _FORBIDDEN


def test_activate_revoked_key_rejected() -> None:
    scene = make_api_env()
    created = _create(scene)
    scene.svc.revoke(scene.space_id, scene.owner, created.api_key.id)
    with pytest.raises(AppError) as exc:
        _activated(scene, created.api_key.id)
    assert exc.value.code_str == _VALIDATION


def test_activate_orphan_key_rejected() -> None:
    """绑定成员与签发人都没了(member_user_id/created_by 均 NULL)的 Key 无"成员"可分配。"""
    scene = make_api_env()
    created = _create(scene)
    stored = scene.api_keys.get(created.api_key.id)
    stored.member_user_id = None
    stored.created_by = None
    with pytest.raises(AppError) as exc:
        _activated(scene, created.api_key.id)
    assert exc.value.code_str == _VALIDATION


def test_activate_is_idempotent() -> None:
    scene = make_api_env()
    created = _create(scene)
    _activated(scene, created.api_key.id)
    _activated(scene, created.api_key.id)
    assert scene.api_keys.get(created.api_key.id).is_active is True
    assert (
        len([x for x in scene.env.audit.logs if x.action == AuditAction.API_KEY_ACTIVATED])
        == 1
    )


def test_activate_other_space_key_404() -> None:
    scene = make_api_env()
    created = _create(scene)
    other, _ = scene.env.space_svc.create(scene.admin, "别的空间", "")
    with pytest.raises(AppError) as exc:
        scene.svc.activate(other.id, scene.admin, created.api_key.id)
    assert exc.value.code_str == _NOT_FOUND


def test_inactive_key_still_authenticates() -> None:
    """核心语义:activate 只是管理标记,不参与认证热路径 —— 未标记的 Key 照用。"""
    scene = make_api_env()
    a = _create(scene, name="a")
    b = _create(scene, name="b")
    _activated(scene, b.api_key.id)  # a 被挤下"正在使用"
    assert scene.svc.authenticate(a.plaintext) is not None
    assert scene.svc.authenticate(b.plaintext) is not None


# ---------------------------- 权限档位双写(OPT-41 迁移期) ----------------------------


def test_create_derives_group_with_identical_permissions() -> None:
    """签发即派生一档:能力/范围与旧列逐字一致,空范围翻译成显式 all_kbs。"""
    scene = make_api_env()
    kb = _add_kb(scene, "产品手册")
    created = _create(
        scene,
        capabilities=[ApiCapability.CHAT, ApiCapability.DOCUMENTS],
        kb_ids=[kb],
    )
    key = created.api_key
    group = scene.groups.get(key.group_id)
    assert group is not None
    assert group.space_id == scene.space_id
    assert group.name == key.name and group.description == key.description
    assert group.capabilities == key.capabilities
    assert group.kb_ids == key.kb_ids
    assert group.all_kbs is False
    assert group.created_by == scene.owner.id


def test_create_with_empty_scope_translates_to_all_kbs() -> None:
    """旧语义"空范围 = 不限制"必须变成档位上的显式全部,而不是"一个都不给"。"""
    scene = make_api_env()
    created = _create(scene)
    group = scene.groups.get(created.api_key.group_id)
    assert group.all_kbs is True and group.kb_ids == []


def test_create_mounts_member_as_not_current() -> None:
    """挂载行建了但不是 current —— 分配动作才决定成员当前用哪一档。"""
    scene = make_api_env()
    created = _create(scene)
    members = scene.groups.list_members(created.api_key.group_id)
    assert [(m.user_id, m.is_current) for m in members] == [(scene.owner.id, False)]


def test_activate_moves_current_group_pointer() -> None:
    """分配 = 切 current 指针,与 is_active 同批生效;同人旧档位自动落位。"""
    scene = make_api_env()
    a = _create(scene, name="a")
    b = _create(scene, name="b")
    _activated(scene, a.api_key.id)
    _activated(scene, b.api_key.id)
    current = scene.groups.get_current(scene.space_id, scene.owner.id)
    assert current is not None and current.group_id == b.api_key.group_id
    assert scene.groups.get_member(a.api_key.group_id, scene.owner.id).is_current is False


def test_activate_does_not_duplicate_mount() -> None:
    """重复分配同一把 Key 不产生第二行挂载(唯一约束 group+user)。"""
    scene = make_api_env()
    created = _create(scene)
    _activated(scene, created.api_key.id)
    _activated(scene, created.api_key.id)
    assert len(scene.groups.list_members(created.api_key.group_id)) == 1


def test_activate_recreates_missing_mount() -> None:
    """挂载曾被清理(成员移出过)时补回一行,不留"生效但无指针"的 Key。"""
    scene = make_api_env()
    created = _create(scene)
    group_id = created.api_key.group_id
    mount = scene.groups.get_member(group_id, scene.owner.id)
    scene.groups.delete_member(mount.id)
    _activated(scene, created.api_key.id)
    current = scene.groups.get_current(scene.space_id, scene.owner.id)
    assert current is not None and current.group_id == group_id
