"""API Key 服务单测(OPT-6):生成/哈希查找/能力级授权/范围收窄/吊销。

测试意图(安全语义为主,不测实现细节):
- 明文只在创建时出现一次,入库只有哈希 —— 库被读走也不能反推 Key;
- 认证只按哈希等值查找,不比对明文;
- 未声明的能力一律拒绝(fail-closed),空能力清单不等于"放行";
- KB 范围是"二次收窄":范围外的 KB 即使 Key 有 retrieval 能力也拒绝;
- 吊销、创建者退出空间都要立刻失效,不留"孤儿凭据"。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.application.service.api_keys import ApiKeyService
from app.core.errors import AppError, ErrorCode
from app.domain.enums import ApiCapability, AuditAction, Role
from app.domain.models import KnowledgeBase
from tests.fakes import FakeKnowledgeBaseRepository
from tests.test_auth_service import make_services

_FORBIDDEN = ErrorCode.FORBIDDEN[1]
_NOT_FOUND = ErrorCode.NOT_FOUND[1]
_SPACE_NOT_FOUND = ErrorCode.SPACE_NOT_FOUND[1]
_VALIDATION = ErrorCode.VALIDATION[1]
_KB_NOT_FOUND = ErrorCode.KB_NOT_FOUND[1]
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
    """owner 建空间 + editor 加为成员;返回 API Key 服务与底层 fake。"""
    env = make_services()
    owner = env.auth.register("owner", "所有者", "secret-pass-1")
    editor = env.auth.register("editor", "编辑者", "secret-pass-2")
    space, _ = env.space_svc.create(owner.user, "研发空间", "")
    env.space_svc.add_member(space.id, owner.user, "editor", Role.EDITOR)
    api_keys = FakeApiKeyRepository()
    kbs = FakeKnowledgeBaseRepository()
    svc = ApiKeyService(api_keys, env.spaces, kbs, env.audit)
    return SimpleNamespace(
        env=env,
        svc=svc,
        api_keys=api_keys,
        kbs=kbs,
        owner=owner.user,
        editor=editor.user,
        space_id=space.id,
    )


def _add_kb(scene: SimpleNamespace, name: str) -> uuid.UUID:
    kb = KnowledgeBase(space_id=scene.space_id, name=name, created_by=scene.owner.id)
    scene.kbs.create(kb)
    return kb.id


def _create(scene: SimpleNamespace, **overrides):
    params = {
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


def test_create_requires_editor_or_higher() -> None:
    """Viewer 不能建 Key(建 Key 属治理动作)。"""
    scene = make_api_env()
    viewer = scene.env.auth.register("viewer", "只读者", "secret-pass-3")
    scene.env.space_svc.add_member(scene.space_id, scene.owner, "viewer", Role.VIEWER)
    with pytest.raises(AppError) as exc:
        scene.svc.create(
            scene.space_id, viewer.user, "n", "", [ApiCapability.CHAT], []
        )
    assert exc.value.code_str == _FORBIDDEN


def test_create_writes_audit() -> None:
    scene = make_api_env()
    _create(scene, name="audited")
    assert str(AuditAction.API_KEY_CREATED) in [
        str(log.action) for log in scene.env.audit.logs
    ]


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


# ---------------------------- 认证 ----------------------------


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


def test_creator_leaving_space_invalidates_key() -> None:
    """创建者被移出空间后其 Key 立刻失效 —— 不留"孤儿凭据"。"""
    scene = make_api_env()
    created = scene.svc.create(
        scene.space_id, scene.editor, "editor-key", "", [ApiCapability.CHAT], []
    )
    scene.env.space_svc.remove_member(scene.space_id, scene.owner, scene.editor.id)
    assert scene.svc.authenticate(created.plaintext) is None


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
        other_space.id, scene.owner, "n", "", [ApiCapability.CHAT], []
    )
    with pytest.raises(AppError) as exc:
        scene.svc.revoke(scene.space_id, scene.owner, created.api_key.id)
    assert exc.value.code_str == _NOT_FOUND


def test_list_carries_no_plaintext() -> None:
    """服务返回实体(含哈希,供内部使用);响应脱敏是路由 DTO 的职责,见路由测试。"""
    scene = make_api_env()
    created = _create(scene)
    rows = scene.svc.list_for_space(scene.space_id, scene.owner)
    assert rows
    for row in rows:
        for field in ("name", "description", "key_hint", "key_hash"):
            assert created.plaintext != str(getattr(row, field, ""))


def test_list_includes_revoked_for_audit_trail() -> None:
    scene = make_api_env()
    created = _create(scene)
    scene.svc.revoke(scene.space_id, scene.owner, created.api_key.id)
    rows = scene.svc.list_for_space(scene.space_id, scene.owner)
    assert [row.id for row in rows] == [created.api_key.id]
    assert rows[0].revoked_at is not None


def test_non_member_gets_404_on_list() -> None:
    """非成员不能列出(404 而非 403,防枚举)。"""
    scene = make_api_env()
    outsider = scene.env.auth.register("outsider", "路人", "secret-pass-9")
    with pytest.raises(AppError) as exc:
        scene.svc.list_for_space(scene.space_id, outsider.user)
    assert exc.value.code_str == _SPACE_NOT_FOUND
