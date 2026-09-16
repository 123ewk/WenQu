"""内存版假仓储:实现 domain/interfaces.py 协议,服务单元测试零外部依赖(基准 03)。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.core.security import hash_password
from app.domain.enums import TaskStatus
from app.domain.models import (
    AuditLog,
    Chunk,
    Document,
    KnowledgeBase,
    Membership,
    RefreshToken,
    Space,
    Task,
    User,
)


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
        self,
        space_id: uuid.UUID,
        limit: int,
        offset: int,
        action: str | None = None,
        actor_id: uuid.UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> tuple[list[AuditLog], int]:
        scoped = [log for log in self.logs if log.space_id == space_id]
        if action is not None:
            scoped = [log for log in scoped if log.action == action]
        if actor_id is not None:
            scoped = [log for log in scoped if log.actor_id == actor_id]
        if since is not None:
            scoped = [log for log in scoped if log.created_at >= since]
        if until is not None:
            scoped = [log for log in scoped if log.created_at <= until]
        return list(reversed(scoped))[offset : offset + limit], len(scoped)


class FakeKnowledgeBaseRepository:
    def __init__(self) -> None:
        self.kbs: dict[uuid.UUID, KnowledgeBase] = {}

    def get(self, kb_id: uuid.UUID) -> KnowledgeBase | None:
        return self.kbs.get(kb_id)

    def get_by_name(self, space_id: uuid.UUID, name: str) -> KnowledgeBase | None:
        return next(
            (kb for kb in self.kbs.values() if kb.space_id == space_id and kb.name == name),
            None,
        )

    def create(self, kb: KnowledgeBase) -> KnowledgeBase:
        if kb.id is None:  # 模拟 DB flush 时主键 default 生效
            kb.id = uuid.uuid4()
        self.kbs[kb.id] = kb
        return kb

    def save(self, kb: KnowledgeBase) -> KnowledgeBase:
        self.kbs[kb.id] = kb
        return kb

    def delete(self, kb: KnowledgeBase) -> None:
        self.kbs.pop(kb.id, None)

    def list_for_space(self, space_id: uuid.UUID) -> list[KnowledgeBase]:
        return [kb for kb in self.kbs.values() if kb.space_id == space_id]


class FakeDocumentRepository:
    def __init__(self, kbs_ref: dict[uuid.UUID, KnowledgeBase] | None = None) -> None:
        self.documents: dict[uuid.UUID, Document] = {}
        self.kbs_ref = kbs_ref if kbs_ref is not None else {}

    def get(self, document_id: uuid.UUID) -> Document | None:
        return self.documents.get(document_id)

    def create(self, document: Document) -> Document:
        if document.id is None:  # 模拟 DB flush 时主键 default 生效
            document.id = uuid.uuid4()
        self.documents[document.id] = document
        return document

    def save(self, document: Document) -> Document:
        self.documents[document.id] = document
        return document

    def delete(self, document: Document) -> None:
        self.documents.pop(document.id, None)
        # 级联删 chunks 的语义在 DB 靠 FK CASCADE;M2-4 分块写入后在此同步模拟

    def list_for_kb(
        self, kb_id: uuid.UUID, limit: int, offset: int
    ) -> tuple[list[Document], int]:
        scoped = [d for d in self.documents.values() if d.kb_id == kb_id]
        scoped.sort(key=lambda d: d.created_at or datetime.min.replace(tzinfo=UTC))
        return list(reversed(scoped))[offset : offset + limit], len(scoped)


def make_user(username: str, password: str = "secret-pass-1") -> User:
    return User(username=username, nickname=username, password_hash=hash_password(password))


class FakeTaskRepository:
    """内存任务队列:语义与 DB 队列对齐(退避即时完成,无真实等待)。"""

    def __init__(self) -> None:
        self.tasks: dict[uuid.UUID, Task] = {}
        self._seq = 0

    def _now(self) -> datetime:
        self._seq += 1
        return datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC).replace(microsecond=self._seq)

    def enqueue(self, task: Task) -> Task:
        if task.id is None:
            task.id = uuid.uuid4()
        if task.status is None:  # 模拟 ORM/DB default 生效
            task.status = TaskStatus.PENDING
        if task.max_retry is None:
            task.max_retry = 3
        if task.timeout_s is None:
            task.timeout_s = 600
        if task.retry_count is None:
            task.retry_count = 0
        if task.created_at is None:
            task.created_at = self._now()
        self.tasks[task.id] = task
        return task

    def get(self, task_id: uuid.UUID) -> Task | None:
        return self.tasks.get(task_id)

    def claim(self, worker_id: str) -> Task | None:
        for task in self.tasks.values():
            if task.status == TaskStatus.PENDING:
                task.status = TaskStatus.RUNNING
                task.claimed_by = worker_id
                task.claimed_at = self._now()
                return task
        return None

    def mark_succeeded(self, task: Task) -> None:
        task.status = TaskStatus.SUCCEEDED
        task.last_error = None

    def mark_failed(self, task: Task, error: str) -> bool:
        task.retry_count += 1
        task.last_error = error[:2000]
        if task.retry_count >= task.max_retry:
            task.status = TaskStatus.DEAD
            return True
        task.status = TaskStatus.PENDING
        task.claimed_by = None
        task.claimed_at = None
        return False

    def recover_stale(self) -> int:
        recovered = 0
        for task in self.tasks.values():
            if task.status == TaskStatus.RUNNING:
                task.retry_count += 1
                exhausted = task.retry_count >= task.max_retry
                task.status = TaskStatus.DEAD if exhausted else TaskStatus.PENDING
                task.claimed_by = None
                task.claimed_at = None
                recovered += 1
        return recovered


class FakeChunkRepository:
    def __init__(self) -> None:
        self.chunks: dict[uuid.UUID, list[tuple]] = {}

    def replace_for_document(
        self,
        document: Document,
        drafts: list[tuple[int, str, list[float] | None, dict]],
    ) -> None:
        self.chunks[document.id] = list(drafts)

    def list_for_document(self, document_id, limit: int, offset: int):
        """查询语义与 DB 实现一致:按 seq 升序分页,返回 (items, total)。"""
        rows = self.chunks.get(document_id, [])
        items = [
            Chunk(
                document_id=document_id,
                space_id=uuid.uuid4(),
                seq=seq,
                content=content,
                meta=meta,
            )
            for seq, content, _emb, meta in rows
        ]
        items.sort(key=lambda c: c.seq)
        return items[offset : offset + limit], len(rows)
