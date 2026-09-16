"""内存版假仓储:实现 domain/interfaces.py 协议,服务单元测试零外部依赖(基准 03)。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.core.security import hash_password
from app.domain.models import AuditLog, Membership, RefreshToken, Space, User


class FakeUserRepository:
    def __init__(self) -> None:
        self.users: dict[uuid.UUID, User] = {}

    def get(self, user_id: uuid.UUID) -> User | None:
        return self.users.get(user_id)

    def get_by_username(self, username: str) -> User | None:
        return next((u for u in self.users.values() if u.username == username), None)

    def create(self, user: User) -> User:
        if user.id is None:  # 模拟 DB flush 时主键 default 生效
            user.id = uuid.uuid4()
        self.users[user.id] = user
        return user

    def save(self, user: User) -> User:
        self.users[user.id] = user
        return user


class FakeRefreshTokenRepository:
    def __init__(self) -> None:
        self.tokens: dict[uuid.UUID, RefreshToken] = {}

    def get_by_hash(self, token_hash: str) -> RefreshToken | None:
        return next((t for t in self.tokens.values() if t.token_hash == token_hash), None)

    def create(self, token: RefreshToken) -> RefreshToken:
        if token.id is None:  # 模拟 DB flush 时主键 default 生效
            token.id = uuid.uuid4()
        self.tokens[token.id] = token
        return token

    def delete(self, token: RefreshToken) -> None:
        self.tokens.pop(token.id, None)

    def delete_by_user(self, user_id: uuid.UUID) -> None:
        victims = [tid for tid, t in self.tokens.items() if t.user_id == user_id]
        for tid in victims:
            del self.tokens[tid]


class FakeSpaceRepository:
    def __init__(self, users_ref: dict[uuid.UUID, User] | None = None) -> None:
        self.spaces: dict[uuid.UUID, Space] = {}
        self.memberships: dict[uuid.UUID, Membership] = {}
        self.users_ref: dict[uuid.UUID, User] = users_ref if users_ref is not None else {}
        self._seq = 0

    def _now(self) -> datetime:
        self._seq += 1
        return datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC).replace(microsecond=self._seq)

    def get(self, space_id: uuid.UUID) -> Space | None:
        return self.spaces.get(space_id)

    def create(self, space: Space) -> Space:
        if space.id is None:  # 模拟 DB flush 时主键 default 生效
            space.id = uuid.uuid4()
        if space.created_at is None:
            space.created_at = self._now()
        self.spaces[space.id] = space
        return space

    def save(self, space: Space) -> Space:
        self.spaces[space.id] = space
        return space

    def delete(self, space: Space) -> None:
        self.spaces.pop(space.id, None)
        self.memberships = {mid: m for mid, m in self.memberships.items() if m.space_id != space.id}

    def list_for_user(self, user_id: uuid.UUID) -> list[tuple[Space, int]]:
        pairs = [
            (self.spaces[m.space_id], m.role)
            for m in self.memberships.values()
            if m.user_id == user_id and m.space_id in self.spaces
        ]
        pairs.sort(key=lambda pair: pair[0].created_at or datetime.min.replace(tzinfo=UTC))
        return pairs

    def get_membership(self, space_id: uuid.UUID, user_id: uuid.UUID) -> Membership | None:
        return next(
            (
                m
                for m in self.memberships.values()
                if m.space_id == space_id and m.user_id == user_id
            ),
            None,
        )

    def get_member(self, space_id: uuid.UUID, member_user_id: uuid.UUID) -> Membership | None:
        return self.get_membership(space_id, member_user_id)

    def list_members(self, space_id: uuid.UUID) -> list[tuple[Membership, User]]:
        result = []
        for m in self.memberships.values():
            if m.space_id == space_id:
                result.append((m, self.users_ref[m.user_id]))
        return result

    def add_member(self, membership: Membership) -> Membership:
        if membership.id is None:  # 模拟 DB flush 时主键 default 生效
            membership.id = uuid.uuid4()
        if membership.created_at is None:
            membership.created_at = self._now()
        self.memberships[membership.id] = membership
        return membership

    def update_member(self, membership: Membership) -> Membership:
        self.memberships[membership.id] = membership
        return membership

    def remove_member(self, membership: Membership) -> None:
        self.memberships.pop(membership.id, None)


class FakeAuditRepository:
    def __init__(self) -> None:
        self.logs: list[AuditLog] = []
        self._seq = 0

    def add(self, log: AuditLog) -> AuditLog:
        self._seq += 1
        if log.created_at is None:  # 模拟 DB flush 时 server_default 生效
            log.created_at = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC).replace(
                microsecond=self._seq
            )
        self.logs.append(log)
        return log

    def list_for_space(
        self, space_id: uuid.UUID, limit: int, offset: int
    ) -> tuple[list[AuditLog], int]:
        scoped = [log for log in self.logs if log.space_id == space_id]
        return list(reversed(scoped))[offset : offset + limit], len(scoped)


def make_user(username: str, password: str = "secret-pass-1") -> User:
    return User(username=username, nickname=username, password_hash=hash_password(password))
