"""认证服务单元测试:全流程走内存 fake,验证轮换/撤销/审计语义(基准 03)。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.application.service.auth import AuthService
from app.application.service.spaces import SpaceService
from app.core.config import Settings
from app.core.errors import AppError
from app.core.security import decode_access_token, hash_refresh_token
from app.domain.enums import AuditAction
from app.domain.models import RefreshToken
from tests.fakes import (
    FakeAuditRepository,
    FakeRefreshTokenRepository,
    FakeSpaceRepository,
    FakeUserRepository,
)

SETTINGS = Settings(jwt_secret="unit-test-secret-0123456789abcdef0123456789abcdef")


def make_services() -> SimpleNamespace:
    """共享同一批 fake 实例,模拟同一事务视图。"""
    users = FakeUserRepository()
    tokens = FakeRefreshTokenRepository()
    spaces = FakeSpaceRepository(users_ref=users.users)
    audit = FakeAuditRepository()
    return SimpleNamespace(
        auth=AuthService(users, tokens, spaces, audit, SETTINGS),
        space_svc=SpaceService(spaces, users, audit),
        users=users,
        tokens=tokens,
        spaces=spaces,
        audit=audit,
    )


def test_register_creates_user_and_token_pair() -> None:
    env = make_services()
    session = env.auth.register("alice", "爱丽丝", "secret-pass-1")
    assert env.users.get_by_username("alice") is not None
    assert session.tokens.access_token and session.tokens.refresh_token
    assert session.tokens.expires_in == SETTINGS.access_token_minutes * 60
    actions = [log.action for log in env.audit.logs]
    assert str(AuditAction.AUTH_REGISTER) in actions


def test_register_duplicate_username_rejected() -> None:
    env = make_services()
    env.auth.register("alice", "爱丽丝", "secret-pass-1")
    with pytest.raises(AppError) as exc_info:
        env.auth.register("alice", "另一个", "secret-pass-2")
    assert exc_info.value.code_str == "USERNAME_TAKEN"


def test_login_success_and_failure_audited() -> None:
    env = make_services()
    env.auth.register("alice", "爱丽丝", "secret-pass-1")

    session = env.auth.login("alice", "secret-pass-1")
    assert session.user.username == "alice"
    assert session.spaces == []

    with pytest.raises(AppError) as exc_info:
        env.auth.login("alice", "wrong-password")
    assert exc_info.value.code_str == "INVALID_CREDENTIALS"

    with pytest.raises(AppError):
        env.auth.login("ghost", "whatever")

    actions = [log.action for log in env.audit.logs]
    assert str(AuditAction.AUTH_LOGIN_SUCCESS) in actions
    assert actions.count(str(AuditAction.AUTH_LOGIN_FAILED)) == 2  # 密码错 + 用户不存在


def test_refresh_rotates_and_old_token_dies() -> None:
    env = make_services()
    first = env.auth.register("alice", "爱丽丝", "secret-pass-1")

    second = env.auth.refresh(first.tokens.refresh_token)
    assert second.tokens.refresh_token != first.tokens.refresh_token
    assert second.user.id == first.user.id

    with pytest.raises(AppError) as exc_info:
        env.auth.refresh(first.tokens.refresh_token)  # 旧令牌一次性作废
    assert exc_info.value.code_str == "REFRESH_TOKEN_INVALID"

    assert env.auth.refresh(second.tokens.refresh_token).tokens.access_token


def test_refresh_invalid_inputs() -> None:
    env = make_services()
    with pytest.raises(AppError) as unknown:
        env.auth.refresh("garbage-token")
    assert unknown.value.code_str == "REFRESH_TOKEN_INVALID"

    # 过期令牌:视为失效并清理
    env.tokens.create(
        RefreshToken(
            user_id=uuid.uuid4(),
            token_hash=hash_refresh_token("expired-token"),
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
    )
    with pytest.raises(AppError):
        env.auth.refresh("expired-token")
    assert env.tokens.get_by_hash(hash_refresh_token("expired-token")) is None


def test_logout_revokes_own_token_only() -> None:
    env = make_services()
    alice = env.auth.register("alice", "爱丽丝", "secret-pass-1")
    bob = env.auth.register("bob", "鲍勃", "secret-pass-2")

    env.auth.logout(alice.user.id, bob.tokens.refresh_token)  # 拿别人的 token → 不生效
    env.auth.refresh(bob.tokens.refresh_token)  # bob 的令牌仍可用(轮换出新一轮)

    alice2 = env.auth.login("alice", "secret-pass-1")
    env.auth.logout(alice.user.id, alice2.tokens.refresh_token)
    with pytest.raises(AppError):
        env.auth.refresh(alice2.tokens.refresh_token)


def test_switch_space_requires_membership() -> None:
    env = make_services()
    alice = env.auth.register("alice", "爱丽丝", "secret-pass-1")
    bob = env.auth.register("bob", "鲍勃", "secret-pass-2")
    space, _role = env.space_svc.create(bob.user, "bob的空间", "")

    with pytest.raises(AppError) as exc_info:
        env.auth.switch_space(alice.user, space.id, alice.tokens.refresh_token)
    assert exc_info.value.code_str == "SPACE_NOT_FOUND"  # 非成员与不存在同语义,防枚举


def test_switch_space_rotates_and_binds_sid() -> None:
    env = make_services()
    session = env.auth.register("alice", "爱丽丝", "secret-pass-1")
    space, _role = env.space_svc.create(session.user, "我的空间", "")

    pair = env.auth.switch_space(session.user, space.id, session.tokens.refresh_token)
    payload = decode_access_token(pair.access_token, SETTINGS)
    assert payload["sid"] == str(space.id)

    with pytest.raises(AppError):
        env.auth.refresh(session.tokens.refresh_token)  # 旧 refresh 已轮换作废


def test_change_password_revokes_all_sessions() -> None:
    env = make_services()
    session = env.auth.register("alice", "爱丽丝", "secret-pass-1")
    other_session = env.auth.login("alice", "secret-pass-1")  # 第二个会话

    with pytest.raises(AppError) as wrong_old:
        env.auth.change_password(session.user, "wrong-old", "brand-new-pass")
    assert wrong_old.value.code_str == "INVALID_CREDENTIALS"

    with pytest.raises(AppError) as same_new:
        env.auth.change_password(session.user, "secret-pass-1", "secret-pass-1")
    assert same_new.value.code_str == "VALIDATION_ERROR"

    env.auth.change_password(session.user, "secret-pass-1", "brand-new-pass")
    with pytest.raises(AppError):
        env.auth.refresh(session.tokens.refresh_token)  # 全部会话被踢下线
    with pytest.raises(AppError):
        env.auth.refresh(other_session.tokens.refresh_token)
    env.auth.login("alice", "brand-new-pass")  # 新密码可登录
