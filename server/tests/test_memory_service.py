"""外部事实记忆服务单元测试(OPT-45 P1,假仓储/假网关):去重、召回谓词、CRUD 隔离。"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.application.service.memory import MemoryService
from app.core.config import Settings
from app.core.errors import AppError
from app.domain.enums import Role
from app.domain.models import Membership, Memory, Space
from tests.fakes import FakeSpaceRepository, FakeUserRepository, make_user


class FakeMemoryRepo:
    def __init__(self) -> None:
        self.items: dict[uuid.UUID, Memory] = {}

    def add(self, memory: Memory) -> Memory:
        if memory.id is None:
            memory.id = uuid.uuid4()
        self.items[memory.id] = memory
        return memory

    def save(self, memory: Memory) -> Memory:
        self.items[memory.id] = memory
        return memory

    def get(self, memory_id, space_id, user_id) -> Memory | None:
        m = self.items.get(memory_id)
        if m is None or m.space_id != space_id or m.user_id != user_id:
            return None
        return m

    def list_for_actor(self, space_id, user_id, limit, offset):
        rows = [
            m for m in self.items.values() if m.space_id == space_id and m.user_id == user_id
        ]
        return rows[offset : offset + limit], len(rows)

    def find_duplicate(self, space_id, user_id, content):
        target = content.strip()
        for m in self.items.values():
            if m.space_id == space_id and m.user_id == user_id and m.content.strip() == target:
                return m
        return None

    def search_by_embedding(self, space_id, user_id, embedding, limit, min_similarity=0.0):
        rows = [
            m for m in self.items.values() if m.space_id == space_id and m.user_id == user_id
        ]
        return [(m, 0.9) for m in rows[:limit]]

    def delete(self, memory: Memory) -> None:
        self.items.pop(memory.id, None)


class FakeEmbedder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts, model_id=None):
        self.calls.append(list(texts))
        return [[0.1] * 1024 for _ in texts]


def _build(role=Role.VIEWER, memory_enabled=True):
    users = FakeUserRepository()
    spaces = FakeSpaceRepository(users_ref=users.users)
    user = users.create(make_user("u"))
    other = users.create(make_user("o"))
    space = spaces.create(Space(name="s", description=""))
    spaces.add_member(Membership(space_id=space.id, user_id=user.id, role=role))
    spaces.add_member(Membership(space_id=space.id, user_id=other.id, role=Role.VIEWER))
    repo = FakeMemoryRepo()
    embedder = FakeEmbedder()
    settings = Settings(
        memory_enabled=memory_enabled, memory_recall_top_k=5, memory_recall_min_sim=0.3
    )
    svc = MemoryService(repo, spaces, embedder, settings)
    return SimpleNamespace(
        svc=svc, repo=repo, embedder=embedder, user=user, other=other, space=space
    )


def test_save_confirmed_strips_and_embeds() -> None:
    env = _build()
    m = env.svc.save_confirmed(env.space.id, env.user.id, "  结论甲  ", kind="decision")
    assert m.content == "结论甲"
    assert m.embedding is not None
    assert env.embedder.calls == [["结论甲"]]


def test_save_dedupes_identical_content() -> None:
    env = _build()
    first = env.svc.save_confirmed(env.space.id, env.user.id, "同一结论")
    second = env.svc.save_confirmed(env.space.id, env.user.id, "同一结论")
    assert first.id == second.id
    assert len(env.repo.items) == 1


def test_save_rejects_empty() -> None:
    env = _build()
    with pytest.raises(AppError):
        env.svc.save_confirmed(env.space.id, env.user.id, "   ")


def test_save_requires_membership() -> None:
    env = _build()
    stranger_space = uuid.uuid4()
    with pytest.raises(AppError):
        env.svc.save_confirmed(stranger_space, env.user.id, "内容")


def test_recall_disabled_returns_empty_without_embedding() -> None:
    env = _build(memory_enabled=False)
    env.svc.save_confirmed(env.space.id, env.user.id, "内容")
    env.embedder.calls.clear()
    assert env.svc.recall(env.space.id, env.user.id, "问题") == []
    assert env.embedder.calls == []  # 关闭时不该省算 embedding


def test_recall_returns_memories() -> None:
    env = _build()
    env.svc.save_confirmed(env.space.id, env.user.id, "内容")
    hits = env.svc.recall(env.space.id, env.user.id, "问题")
    assert len(hits) == 1


def test_update_reembeds_only_on_content_change() -> None:
    env = _build()
    m = env.svc.save_confirmed(env.space.id, env.user.id, "旧内容")
    env.embedder.calls.clear()
    updated = env.svc.update(env.space.id, env.user.id, m.id, topic="新主题")
    assert updated.topic == "新主题"
    assert env.embedder.calls == []  # 正文没变不重算向量
    env.embedder.calls.clear()
    env.svc.update(env.space.id, env.user.id, m.id, content="新内容")
    assert env.embedder.calls == [["新内容"]]


def test_cross_user_update_and_delete_are_404() -> None:
    env = _build()
    m = env.svc.save_confirmed(env.space.id, env.user.id, "私有记忆")
    with pytest.raises(AppError) as exc:
        env.svc.update(env.space.id, env.other.id, m.id, content="篡改")
    assert exc.value.code_str == "MEMORY_NOT_FOUND"
    with pytest.raises(AppError) as exc:
        env.svc.delete(env.space.id, env.other.id, m.id)
    assert exc.value.code_str == "MEMORY_NOT_FOUND"
    assert m.id in env.repo.items  # 未被删除
