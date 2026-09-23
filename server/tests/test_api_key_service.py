"""API Key 服务单测(OPT-6 / OPT-26 / OPT-41):签发绑成员、权限读档位、认证双校验。

测试意图(安全语义为主,不测实现细节):
- 明文只在创建时出现一次,入库只有哈希 —— 库被读走也不能反推 Key;
- OPT-26:签发必须指定空间内的 `owner_user_id`,操作者门槛 Admin+(与"拉成员"同闸);
  认证 = Key 有效 + 归因成员仍是成员 双重校验,不留"孤儿凭据";
- OPT-41:能力与知识库范围**只从档位读** —— 改一次档位,其下已签发的 Key 即时改口径;
  签发不再派生档位,也不顺手把 owner 加进档位的成员列表;
- 档位没勾任何库(`all_kbs=false` + 空列表)= 什么都不允许,不因"空"而放行全空间;
- 吊销、归因成员被移出都要立刻失效;签发人(创建者)被移出不影响 Key。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.application.service.api_keys import ApiKeyService
from app.core.errors import AppError, ErrorCode
from app.domain.enums import ApiCapability, AuditAction, Role
from app.domain.models import KnowledgeBase, PermissionGroup
from tests.fakes import FakeKnowledgeBaseRepository
from tests.test_auth_service import make_services

_FORBIDDEN = ErrorCode.FORBIDDEN[1]
_NOT_FOUND = ErrorCode.NOT_FOUND[1]
_SPACE_NOT_FOUND = ErrorCode.SPACE_NOT_FOUND[1]
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
        if api_key.created_at is None:  # 模拟 server_default=now()
            api_key.created_at = datetime.now(UTC)
        self.keys[api_key.id] = api_key
        return api_key

    def save(self, api_key):
        self.keys[api_key.id] = api_key
        return api_key

    def list_for_space(self, space_id: uuid.UUID):
        return [k for k in self.keys.values() if k.space_id == space_id]

    def list_for_group(self, group_id: uuid.UUID):
        return [k for k in self.keys.values() if k.group_id == group_id]

    def delete(self, api_key) -> None:
        self.keys.pop(api_key.id, None)


def make_api_env() -> SimpleNamespace:
    """owner 建空间;admin 与 editor 入空间;Key 服务与档位服务共用同一套 fake 仓储。"""
    env = make_services()
    owner = env.auth.register("owner", "所有者", "secret-pass-1")
    admin = env.auth.register("admin", "管理员", "secret-pass-2")
    editor = env.auth.register("editor", "编辑者", "secret-pass-3")
    space, _ = env.space_svc.create(owner.user, "研发空间", "")
    env.space_svc.add_member(space.id, owner.user, "admin", Role.ADMIN)
    env.space_svc.add_member(space.id, owner.user, "editor", Role.EDITOR)
    api_keys = FakeApiKeyRepository()
    kbs = FakeKnowledgeBaseRepository()
    # 档位仓储与 SpaceService 用的是同一个 fake 实例:生产形态就是一套仓储,
    # 而且"移出成员要顺带清挂载"这条链路必须跨两个服务看到同一份数据。
    groups = env.groups
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


def _group(
    scene: SimpleNamespace,
    *,
    name: str = "默认档",
    capabilities: list[ApiCapability | str] | None = None,
    kb_ids: list[uuid.UUID] | None = None,
    all_kbs: bool = False,
) -> PermissionGroup:
    """直接往 fake 仓储塞一个档位(绕开档位服务,让本文件只测 Key 侧读口径)。"""
    return scene.groups.create(
        PermissionGroup(
            space_id=scene.space_id,
            name=name,
            description="",
            capabilities=[str(c) for c in (
                [ApiCapability.CHAT] if capabilities is None else capabilities
            )],
            kb_ids=[str(kb) for kb in (kb_ids or [])],
            all_kbs=all_kbs,
            created_by=scene.owner.id,
        )
    )


def _create(scene: SimpleNamespace, **overrides):
    """建一把 Key;未显式给 group_id 时按 caps/kb_ids/all_kbs 先派一个档位。"""
    params: dict = {
        "owner_user_id": scene.owner.id,  # 默认归因到签发人自己
        "name": "ci-pipeline",
        "description": "",
        "group_id": None,
        "capabilities": [ApiCapability.CHAT],
        "kb_ids": None,
        "all_kbs": True,
        "actor": scene.owner,
    }
    params.update(overrides)
    group_id = params["group_id"] or _group(
        scene,
        capabilities=params["capabilities"],
        kb_ids=params["kb_ids"],
        all_kbs=params["all_kbs"],
    ).id
    return scene.svc.create(
        scene.space_id,
        params["actor"],
        owner_user_id=params["owner_user_id"],
        group_id=group_id,
        name=params["name"],
        description=params["description"],
    )


def _perm(scene: SimpleNamespace, api_key):
    """取 Key 的生效权限(OPT-41 N4)—— 每次重新读档位,判定的唯一入口。"""
    return scene.svc.effective_permission(api_key)


# ---------------------------- 创建与存储 ----------------------------


def test_created_key_has_sk_prefix_and_full_plaintext_returned_once() -> None:
    scene = make_api_env()
    created = _create(scene)
    assert created.plaintext.startswith("sk-")
    assert len(created.plaintext) > 20
    assert created.api_key.key_hint.startswith("sk-")
    assert created.api_key.owner_user_id == scene.owner.id


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


def test_two_keys_can_share_one_group() -> None:
    """一个档位多把凭据(OPT-41 存在的理由之一):轮换明文不影响权限口径。"""
    scene = make_api_env()
    group = _group(scene, name="只读问答")
    a = _create(scene, name="a", group_id=group.id)
    b = _create(scene, name="b", group_id=group.id)
    assert a.api_key.group_id == b.api_key.group_id == group.id
    assert a.api_key.key_hash != b.api_key.key_hash


def test_create_requires_admin_or_higher() -> None:
    """OPT-26:建 Key 与"拉成员"同闸 —— Viewer/Editor 都不行,Admin/Owner 可以。"""
    scene = make_api_env()
    viewer = scene.env.auth.register("viewer", "只读者", "secret-pass-4")
    scene.env.space_svc.add_member(scene.space_id, scene.owner, "viewer", Role.VIEWER)
    group = _group(scene)
    for actor in (viewer.user, scene.editor):
        with pytest.raises(AppError) as exc:
            scene.svc.create(
                scene.space_id, actor, owner_user_id=actor.id, group_id=group.id, name="n"
            )
        assert exc.value.code_str == _FORBIDDEN
    # Admin 成员可以替自己签
    created = scene.svc.create(
        scene.space_id, scene.admin, owner_user_id=scene.admin.id, group_id=group.id, name="n"
    )
    assert created.api_key.created_by == scene.admin.id
    # Admin 也可以替别人签(归因目标是成员即可)
    created = scene.svc.create(
        scene.space_id,
        scene.admin,
        owner_user_id=scene.editor.id,
        group_id=group.id,
        name="n2",
    )
    assert created.api_key.owner_user_id == scene.editor.id


def test_create_target_must_be_member() -> None:
    """归因目标不是本空间成员 → 404 MEMBER_NOT_FOUND。"""
    scene = make_api_env()
    outsider = scene.env.auth.register("outsider", "路人", "secret-pass-5")
    with pytest.raises(AppError) as exc:
        _create(scene, owner_user_id=outsider.user.id)
    assert exc.value.code_str == _MEMBER_NOT_FOUND


def test_create_group_must_exist_in_this_space() -> None:
    """档位不存在或属于别的空间 → 404:存在性本身不泄露,更不能跨租户指向。"""
    scene = make_api_env()
    with pytest.raises(AppError) as exc:
        _create(scene, group_id=uuid.uuid4())
    assert exc.value.code_str == _NOT_FOUND
    other_space, _ = scene.env.space_svc.create(scene.owner, "另一空间", "")
    other_group = scene.groups.create(
        PermissionGroup(
            space_id=other_space.id, name="别人的档", capabilities=["chat"], all_kbs=True
        )
    )
    with pytest.raises(AppError) as exc:
        _create(scene, group_id=other_group.id)
    assert exc.value.code_str == _NOT_FOUND


def test_create_does_not_derive_a_group() -> None:
    """OPT-41 之后的口径:签发只是"给现成档位配凭据",不再 1:1 派生档位。"""
    scene = make_api_env()
    group = _group(scene)
    before = len(scene.groups.list_for_space(scene.space_id))
    created = _create(scene, group_id=group.id)
    assert len(scene.groups.list_for_space(scene.space_id)) == before
    assert created.api_key.group_id == group.id
    assert scene.groups.list_members(group.id) == []


def test_create_writes_audit_with_owner_and_group() -> None:
    scene = make_api_env()
    group = _group(scene, name="问答档", all_kbs=True)
    _create(scene, name="audited", owner_user_id=scene.admin.id, group_id=group.id)
    logs = [
        log
        for log in scene.env.audit.logs
        if str(log.action) == str(AuditAction.API_KEY_CREATED)
    ]
    assert logs
    detail = logs[-1].detail
    assert detail["owner_user_id"] == str(scene.admin.id)
    assert detail["group_id"] == str(group.id)
    assert detail["capabilities"] == [str(ApiCapability.CHAT)]
    assert detail["kb_scope"] == "all"


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
    """归因成员被移出 → Key 立刻失效;签发人(创建者)被移出不影响。"""
    scene = make_api_env()
    created = _create(scene, name="admin-key", owner_user_id=scene.admin.id)
    # 移出归因成员(admin)→ Key 立即失效;签发人 owner 的身份无关紧要
    scene.env.space_svc.remove_member(scene.space_id, scene.owner, scene.admin.id)
    assert scene.svc.authenticate(created.plaintext) is None


def test_key_survives_creator_removal() -> None:
    """OPT-26 核心:认证只校验归因成员;签发 Admin 被移出,Key(归因 Editor)仍有效。"""
    scene = make_api_env()
    created = _create(scene, actor=scene.admin, owner_user_id=scene.editor.id)
    scene.env.space_svc.remove_member(scene.space_id, scene.owner, scene.admin.id)
    found = scene.svc.authenticate(created.plaintext)
    assert found is not None and found.id == created.api_key.id


def test_legacy_key_falls_back_to_creator() -> None:
    """存量旧 Key(无 owner_user_id)回退 created_by 判定,迁移双保险。"""
    scene = make_api_env()
    created = _create(scene, actor=scene.admin, owner_user_id=scene.admin.id)
    stored = scene.api_keys.get(created.api_key.id)
    stored.owner_user_id = None  # 模拟 0012 回填前的旧行:身份回到签发人 admin
    assert scene.svc.authenticate(created.plaintext) is not None
    scene.env.space_svc.remove_member(scene.space_id, scene.owner, scene.admin.id)
    assert scene.svc.authenticate(created.plaintext) is None


def test_owner_of_reports_current_role() -> None:
    scene = make_api_env()
    created = _create(scene, owner_user_id=scene.editor.id)
    owner, role = scene.svc.owner_of(created.api_key)
    assert owner is not None and owner.id == scene.editor.id
    assert role == int(Role.EDITOR)
    # 降级后角色实时变化(路由层 403 的依据)
    scene.env.space_svc.change_role(
        scene.space_id, scene.owner, scene.editor.id, int(Role.VIEWER)
    )
    _, role = scene.svc.owner_of(created.api_key)
    assert role == int(Role.VIEWER)


# ---------------------------- 能力级授权(fail-closed,读档位) ----------------------------


def test_capability_check_allows_declared() -> None:
    scene = make_api_env()
    created = _create(scene, capabilities=[ApiCapability.CHAT])
    _perm(scene, created.api_key).require_capability(ApiCapability.CHAT)


def test_capability_check_denies_undeclared() -> None:
    """档位只给了「对话检索」,用「文档管理」能力必须拒绝。"""
    scene = make_api_env()
    created = _create(scene, capabilities=[ApiCapability.CHAT])
    with pytest.raises(AppError) as exc:
        _perm(scene, created.api_key).require_capability(ApiCapability.DOCUMENTS)
    assert exc.value.code_str == _CAPABILITY_DENIED
    assert exc.value.http_status == 403


def test_empty_capabilities_denies_everything() -> None:
    """空能力清单 = 什么都不允许(不因为"空"而放行)。"""
    scene = make_api_env()
    created = _create(scene, capabilities=[])
    for cap in ApiCapability:
        with pytest.raises(AppError):
            _perm(scene, created.api_key).require_capability(cap)


# ---------------------------- KB 范围二次收窄(读档位) ----------------------------


def test_group_with_all_kbs_does_not_restrict() -> None:
    """`all_kbs=true` 是显式勾选的"全部库" → None(不限制)。"""
    scene = make_api_env()
    created = _create(scene, all_kbs=True)
    assert _perm(scene, created.api_key).resolve_kb_scope(None) is None


def test_group_without_any_kb_denies() -> None:
    """fail-closed:未勾"全部库"且没勾具体库 = 一个都不给,而不是"默认全空间"。"""
    scene = make_api_env()
    created = _create(scene, all_kbs=False, kb_ids=[])
    with pytest.raises(AppError) as exc:
        _perm(scene, created.api_key).resolve_kb_scope(None)
    assert exc.value.code_str == _SCOPE_DENIED


def test_kb_scope_narrows_requested_list() -> None:
    """档位限定 KB-A;请求同时要 A、B → 只允许 A,不因越界整体失败。"""
    scene = make_api_env()
    allowed = _add_kb(scene, "允许库")
    other = _add_kb(scene, "禁止库")
    created = _create(scene, all_kbs=False, kb_ids=[allowed])
    assert _perm(scene, created.api_key).resolve_kb_scope([allowed, other]) == [allowed]


def test_kb_scope_outside_only_request_denied() -> None:
    """请求只要范围外的 KB → 拒绝(不能悄悄降级成"全部")。"""
    scene = make_api_env()
    allowed = _add_kb(scene, "允许库")
    other = _add_kb(scene, "禁止库")
    created = _create(scene, all_kbs=False, kb_ids=[allowed])
    with pytest.raises(AppError) as exc:
        _perm(scene, created.api_key).resolve_kb_scope([other])
    assert exc.value.code_str == _SCOPE_DENIED


def test_permission_reads_the_group_live_not_a_snapshot() -> None:
    """一处真相的落点:档位改了范围,已签发的 Key 下一次判定就用新范围(无需重签)。"""
    scene = make_api_env()
    kb_a = _add_kb(scene, "A库")
    kb_b = _add_kb(scene, "B库")
    group = _group(scene, kb_ids=[kb_a], all_kbs=False)
    created = _create(scene, group_id=group.id)
    assert _perm(scene, created.api_key).resolve_kb_scope([kb_a, kb_b]) == [kb_a]
    group.kb_ids = [str(kb_b)]
    assert _perm(scene, created.api_key).resolve_kb_scope([kb_a, kb_b]) == [kb_b]
    group.capabilities = []
    with pytest.raises(AppError) as exc:
        _perm(scene, created.api_key).require_capability(ApiCapability.CHAT)
    assert exc.value.code_str == _CAPABILITY_DENIED


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
    other_group = scene.groups.create(
        PermissionGroup(space_id=other_space.id, name="外档", capabilities=["chat"])
    )
    created = scene.svc.create(
        other_space.id,
        scene.owner,
        owner_user_id=scene.owner.id,
        group_id=other_group.id,
        name="n",
    )
    with pytest.raises(AppError) as exc:
        scene.svc.revoke(scene.space_id, scene.owner, created.api_key.id)
    assert exc.value.code_str == _NOT_FOUND


def test_list_carries_group_and_owner_view_without_plaintext() -> None:
    """服务返回 ApiKeyWithOwner 视图;响应脱敏是路由 DTO 的职责,见集成测试。"""
    scene = make_api_env()
    kb = _add_kb(scene, "产品手册")
    group = _group(scene, name="文档档", capabilities=[ApiCapability.DOCUMENTS], kb_ids=[kb])
    created = _create(scene, owner_user_id=scene.admin.id, group_id=group.id)
    (row,) = scene.svc.list_for_space(scene.space_id, scene.owner)
    assert row.group.id == group.id
    assert row.group.capabilities == [str(ApiCapability.DOCUMENTS)]
    assert row.group.kb_ids == [str(kb)]
    assert row.owner is not None and row.owner.id == scene.admin.id
    assert row.owner_role == int(Role.ADMIN)
    assert row.created_by_name == "owner"
    for field in ("name", "description", "key_hint", "key_hash"):
        assert created.plaintext != str(getattr(row.api_key, field, ""))


def test_list_shares_one_group_between_keys_without_extra_load() -> None:
    """多把 Key 同一档位:列表按 id 批量取档位,而不是每把查一次。"""
    scene = make_api_env()
    group = _group(scene)
    _create(scene, name="a", group_id=group.id)
    _create(scene, name="b", group_id=group.id)
    rows = scene.svc.list_for_space(scene.space_id, scene.owner)
    assert [r.group.id for r in rows] == [group.id, group.id]


def test_list_includes_revoked_for_audit_trail() -> None:
    scene = make_api_env()
    created = _create(scene)
    scene.svc.revoke(scene.space_id, scene.owner, created.api_key.id)
    rows = scene.svc.list_for_space(scene.space_id, scene.owner)
    assert [row.api_key.id for row in rows] == [created.api_key.id]
    assert rows[0].api_key.revoked_at is not None


def test_list_marks_removed_owner_as_none() -> None:
    """归因成员被移出后列表仍可查(Viewer+ 可读),成员置灰展示不炸。"""
    scene = make_api_env()
    _create(scene, owner_user_id=scene.editor.id)
    scene.env.space_svc.remove_member(scene.space_id, scene.owner, scene.editor.id)
    rows = scene.svc.list_for_space(scene.space_id, scene.owner)
    assert rows[0].owner is None and rows[0].owner_role is None


def test_non_member_gets_404_on_list() -> None:
    """非成员不能列出(404 而非 403,防枚举)。"""
    scene = make_api_env()
    outsider = scene.env.auth.register("outsider", "路人", "secret-pass-9")
    with pytest.raises(AppError) as exc:
        scene.svc.list_for_space(scene.space_id, outsider.user)
    assert exc.value.code_str == _SPACE_NOT_FOUND


def test_describe_matches_list_view_for_the_same_key() -> None:
    """单条视图与列表视图同源:前端两处渲染不必分叉。"""
    scene = make_api_env()
    created = _create(scene, owner_user_id=scene.admin.id)
    (listed,) = scene.svc.list_for_space(scene.space_id, scene.owner)
    described = scene.svc.describe(created.api_key)
    assert described.group.id == listed.group.id
    assert described.owner_role == listed.owner_role == int(Role.ADMIN)
