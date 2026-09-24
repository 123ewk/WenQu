"""生效权限合成器单测(OPT-41 N4 + §4.1 三条链路 + §4.2 边界)。

测试意图(这里钉的是"口径",不是实现):
- **一个档位两种指向**:同一档位经 Key 和经"成员挂载"算出来的能力/范围必须一模一样,
  否则"人在网页上做的事和程序拿 Key 做的事不等价"会以最隐蔽的方式回来;
- **fail-closed**:空能力 = 什么都不允许;`all_kbs=false` 且没勾库 = 一个库都不给;
  请求全在范围外 → 403,绝不静默扩权也绝不静默降级成"全部";
- **两条链路同码不同文案**:错误码沿用 API Key 那一对(第三方零回归),用
  `details.actor` 区分是人还是程序,前端据此换提示;
- **非成员先 404 再 403**(防枚举):不能让人凭 403 探出"这个空间存在、只是没给我配档"。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.application.service.effective_permission import (
    SOURCE_API_KEY,
    SOURCE_MEMBER,
    EffectivePermission,
    Source,
    permission_of_group,
    permission_of_member,
)
from app.core.errors import AppError, ErrorCode
from app.domain.enums import ApiCapability, Role
from app.domain.models import (
    Membership,
    PermissionGroup,
    PermissionGroupMember,
    Space,
    User,
)
from tests.fakes import FakePermissionGroupRepository, FakeSpaceRepository, FakeUserRepository

_CAPABILITY_DENIED = ErrorCode.API_KEY_CAPABILITY_DENIED[1]
_SCOPE_DENIED = ErrorCode.API_KEY_SCOPE_DENIED[1]
_GROUP_REQUIRED = ErrorCode.PERMISSION_GROUP_REQUIRED[1]
_SPACE_NOT_FOUND = ErrorCode.SPACE_NOT_FOUND[1]


def _scene() -> SimpleNamespace:
    """一套内存仓储 + 空间里一个成员;各测试自己往上挂档位。"""
    users = FakeUserRepository()
    spaces = FakeSpaceRepository(users_ref=users.users)
    s = SimpleNamespace(users=users, spaces=spaces, groups=FakePermissionGroupRepository())
    s.space_id = _add_space(s)
    return s


def _add_space(scene: SimpleNamespace) -> uuid.UUID:
    space = Space(name="研发空间", description="", created_by=uuid.uuid4())
    scene.spaces.create(space)
    return space.id


def _add_user(scene: SimpleNamespace, username: str) -> User:
    user = User(username=username, nickname=username, password_hash="x")
    scene.users.users[user.id] = user
    return user


def _member(scene: SimpleNamespace, username: str = "editor") -> uuid.UUID:
    user = _add_user(scene, username)
    scene.spaces.add_member(
        Membership(space_id=scene.space_id, user_id=user.id, role=int(Role.EDITOR))
    )
    return user.id


def _group(
    scene: SimpleNamespace,
    *,
    name: str = "问答档",
    capabilities: list[ApiCapability] | None = None,
    kb_ids: list[uuid.UUID] | None = None,
    all_kbs: bool = False,
) -> PermissionGroup:
    return scene.groups.create(
        PermissionGroup(
            space_id=scene.space_id,
            name=name,
            description="",
            capabilities=[str(c) for c in (
                [ApiCapability.CHAT] if capabilities is None else capabilities
            )],
            kb_ids=[str(k) for k in (kb_ids or [])],
            all_kbs=all_kbs,
        )
    )


def _mount(
    scene: SimpleNamespace, group: PermissionGroup, user_id: uuid.UUID, *, current: bool = True
) -> None:
    scene.groups.add_member(
        PermissionGroupMember(
            group_id=group.id,
            user_id=user_id,
            space_id=scene.space_id,
            is_current=current,
        )
    )


def _perm_of(
    group: PermissionGroup,
    *,
    source: Source = SOURCE_API_KEY,
    api_key_id: uuid.UUID | None = None,
) -> EffectivePermission:
    return permission_of_group(group, source=source, api_key_id=api_key_id)


@pytest.fixture
def scene() -> SimpleNamespace:
    return _scene()


# ---------------------------- §4.1 链路一:程序带明文 Key ----------------------------


def test_key_source_carries_group_capabilities(scene: SimpleNamespace) -> None:
    group = _group(scene, capabilities=[ApiCapability.CHAT])
    perm = _perm_of(group)
    perm.require_capability(ApiCapability.CHAT)
    assert perm.source == SOURCE_API_KEY
    assert perm.requires_capability(ApiCapability.CHAT) is True
    assert perm.requires_capability(ApiCapability.DOCUMENTS) is False


def test_key_capability_denial_names_api_key_actor(scene: SimpleNamespace) -> None:
    """文案与 details 都要说清是"Key 的档位"不给,第三方排错时不会误以为登录掉了。"""
    group = _group(scene, name="只读问答", capabilities=[ApiCapability.CHAT])
    perm = _perm_of(group)
    with pytest.raises(AppError) as exc:
        perm.require_capability(ApiCapability.DOCUMENTS)
    assert exc.value.code_str == _CAPABILITY_DENIED
    assert exc.value.http_status == 403
    assert exc.value.details["actor"] == "api_key"
    assert exc.value.details["group_name"] == "只读问答"
    assert "API Key" in exc.value.message


def test_empty_capability_list_denies_everything(scene: SimpleNamespace) -> None:
    group = _group(scene, capabilities=[])
    perm = _perm_of(group)
    for cap in ApiCapability:
        assert perm.requires_capability(cap) is False
        with pytest.raises(AppError):
            perm.require_capability(cap)


# ---------------------------- §4.1 链路二:人带 JWT(服务端解析档位) ----------------------------


def test_member_with_current_group_resolves_its_permission(scene: SimpleNamespace) -> None:
    user_id = _member(scene)
    group = _group(scene, capabilities=[ApiCapability.CHAT, ApiCapability.DOCUMENTS])
    _mount(scene, group, user_id)
    perm = permission_of_member(scene.groups, scene.spaces, scene.space_id, user_id)
    assert perm.source == SOURCE_MEMBER
    assert perm.group_id == group.id
    assert perm.capabilities == {str(ApiCapability.CHAT), str(ApiCapability.DOCUMENTS)}


def test_member_without_current_group_is_told_to_contact_admin(scene: SimpleNamespace) -> None:
    """403 而非 401:人登录着,缺的是管理员给他配档位(401 会被前端踢成重新登录)。"""
    user_id = _member(scene)
    with pytest.raises(AppError) as exc:
        permission_of_member(scene.groups, scene.spaces, scene.space_id, user_id)
    assert exc.value.code_str == _GROUP_REQUIRED
    assert exc.value.http_status == 403
    assert "管理员" in exc.value.message


def test_assigned_but_not_current_group_does_not_grant(scene: SimpleNamespace) -> None:
    """分配 ≠ 生效:没置 current 的档位不能当权限用(否则"待分配"变成误放行)。"""
    user_id = _member(scene)
    _mount(scene, _group(scene, name="备用档"), user_id, current=False)
    with pytest.raises(AppError) as exc:
        permission_of_member(scene.groups, scene.spaces, scene.space_id, user_id)
    assert exc.value.code_str == _GROUP_REQUIRED


def test_mount_pointing_at_deleted_group_denies(scene: SimpleNamespace) -> None:
    user_id = _member(scene)
    group = _group(scene)
    _mount(scene, group, user_id)
    scene.groups.delete(group.id)
    with pytest.raises(AppError) as exc:
        permission_of_member(scene.groups, scene.spaces, scene.space_id, user_id)
    assert exc.value.code_str == _GROUP_REQUIRED
    assert exc.value.details["reason"] == "group_missing"


def test_non_member_hit_404_before_any_403(scene: SimpleNamespace) -> None:
    """非成员问核心接口 → 404(与业务服务同口径),不能靠 403 探出空间存在。"""
    outsider = _add_user(scene, "outsider").id
    with pytest.raises(AppError) as exc:
        permission_of_member(scene.groups, scene.spaces, scene.space_id, outsider)
    assert exc.value.code_str == _SPACE_NOT_FOUND
    assert exc.value.http_status == 404


# ---------------------------- §4.1 链路三:两条链路必须同构 ----------------------------


def test_both_sources_of_same_group_agree(scene: SimpleNamespace) -> None:
    """口径 A 的落点:同一档位经 Key 与经成员算出来必须逐字段相等(除来源与回指 id)。"""
    kb = uuid.uuid4()
    group = _group(scene, capabilities=[ApiCapability.CHAT], kb_ids=[kb])
    user_id = _member(scene)
    _mount(scene, group, user_id)
    key_id = uuid.uuid4()
    by_key = _perm_of(group, source=SOURCE_API_KEY, api_key_id=key_id)
    by_member = permission_of_member(scene.groups, scene.spaces, scene.space_id, user_id)
    assert by_key.capabilities == by_member.capabilities
    assert by_key.kb_ids == by_member.kb_ids
    assert by_key.all_kbs == by_member.all_kbs
    assert by_key.group_id == by_member.group_id
    assert by_key.api_key_id == key_id and by_member.api_key_id is None


def test_denial_wording_differs_but_code_does_not(scene: SimpleNamespace) -> None:
    """同码不同文案:错误码是对外契约(第三方已接入),主语按来源换,前端看 actor 分提示。"""
    group = _group(scene, capabilities=[])
    user_id = _member(scene)
    _mount(scene, group, user_id)
    by_member = permission_of_member(scene.groups, scene.spaces, scene.space_id, user_id)
    msgs: dict[str, str] = {}
    for perm in (_perm_of(group), by_member):
        with pytest.raises(AppError) as exc:
            perm.require_capability(ApiCapability.CHAT)
        msgs[exc.value.details["actor"]] = exc.value.message
    assert set(msgs) == {"api_key", "member"}
    assert msgs["api_key"] != msgs["member"]


# ---------------------------- 知识库范围(两条链路共用) ----------------------------


def test_all_kbs_expands_to_none_at_call_time(scene: SimpleNamespace) -> None:
    """`all_kbs=true` 每次请求实时展开成"不限库",新建的库立刻在范围内(不存快照)。"""
    group = _group(scene, all_kbs=True)
    assert _perm_of(group).resolve_kb_scope(None) is None
    assert _perm_of(group).allows_kb(uuid.uuid4()) is True


def test_group_without_any_kb_denies_instead_of_defaulting_to_all(scene: SimpleNamespace) -> None:
    group = _group(scene, all_kbs=False, kb_ids=[])
    with pytest.raises(AppError) as exc:
        _perm_of(group).resolve_kb_scope(None)
    assert exc.value.code_str == _SCOPE_DENIED
    assert exc.value.details["reason"] == "empty_scope"


def test_request_mixing_allowed_and_denied_kb_is_narrowed(scene: SimpleNamespace) -> None:
    kb_a, kb_b = uuid.uuid4(), uuid.uuid4()
    perm = _perm_of(_group(scene, kb_ids=[kb_a]))
    assert perm.resolve_kb_scope([kb_a, kb_b]) == [kb_a]
    assert perm.resolve_kb_scope(None) == [kb_a]


def test_request_only_out_of_scope_kb_denied(scene: SimpleNamespace) -> None:
    kb_a, kb_b = uuid.uuid4(), uuid.uuid4()
    perm = _perm_of(_group(scene, kb_ids=[kb_a]))
    with pytest.raises(AppError) as exc:
        perm.resolve_kb_scope([kb_b])
    assert exc.value.code_str == _SCOPE_DENIED
    assert exc.value.details["reason"] == "out_of_scope"


def test_garbage_kb_id_is_dropped_not_crashing(scene: SimpleNamespace) -> None:
    """JSONB 里存的是字符串,脏值当作"不在范围内"处理,不能让请求 500。"""
    good = uuid.uuid4()
    group = _group(scene)
    group.kb_ids = [str(good), "not-a-uuid"]
    perm = _perm_of(group)
    assert perm.resolve_kb_scope([good, uuid.uuid4()]) == [good]
