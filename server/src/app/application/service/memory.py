"""外部事实记忆服务(OPT-45 P1):确认落库 + 向量召回 + CRUD,一切按 (space_id,user_id) 归属。

职责边界:
- 只在能解析到具体 user 时启用(API Key 无归属成员的调用不进入本服务,见路由/§7);
- save_confirmed 落库前算 embedding 并按正文去重(用户连点/模型重复提议只存一条);
- recall 关闭开关或空命中即返回 [](与"无记忆"逐事件等价,绝不阻断问答)。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from app.core.errors import AppError, ErrorCode
from app.domain.enums import Role
from app.domain.interfaces import (
    EmbeddingGateway,
    MemoryProposalRepository,
    MemoryRepository,
    SpaceRepository,
)
from app.domain.models import Memory, MemoryProposal

if TYPE_CHECKING:
    from app.core.config import Settings

_KINDS = {"fact", "decision"}


def normalize_memory_content(content: str) -> str:
    """归一化:折叠所有连续空白为单空格,去首尾空白。提议去重与 dismissed 比对都用它。"""
    return " ".join((content or "").split())


def memory_content_hash(content: str) -> str:
    """记忆正文的去重键(sha256 of 归一化内容),供忽略留痕跨提议匹配。"""
    return hashlib.sha256(normalize_memory_content(content).encode("utf-8")).hexdigest()


class MemoryService:
    def __init__(
        self,
        memories: MemoryRepository,
        spaces: SpaceRepository,
        embedder: EmbeddingGateway,
        settings: Settings,
        proposals: MemoryProposalRepository | None = None,
    ) -> None:
        self._memories = memories
        self._spaces = spaces
        self._embedder = embedder
        self._settings = settings
        self._proposals = proposals

    def save_confirmed(
        self,
        space_id: uuid.UUID,
        user_id: uuid.UUID,
        content: str,
        kind: str = "fact",
        topic: str = "",
        source_conversation_id: uuid.UUID | None = None,
        source_message_id: uuid.UUID | None = None,
    ) -> Memory:
        self._require_member(space_id, user_id)
        text = content.strip()
        if not text:
            raise AppError(ErrorCode.VALIDATION, "记忆内容不能为空", http_status=400)
        if kind not in _KINDS:
            kind = "fact"
        existing = self._memories.find_duplicate(space_id, user_id, text)
        if existing is not None:
            return existing  # 去重:内容完全相同不再重复落库
        embedding = self._embed_one(text)
        return self._memories.add(
            Memory(
                space_id=space_id,
                user_id=user_id,
                kind=kind,
                content=text,
                topic=topic.strip()[:255],
                source_conversation_id=source_conversation_id,
                source_message_id=source_message_id,
                embedding=embedding,
            )
        )

    def recall(
        self, space_id: uuid.UUID, user_id: uuid.UUID, question: str
    ) -> list[Memory]:
        """按相关性召回本人本空间的记忆;关闭、空问题或该用户零记忆时返回 []。

        零记忆先判空再决定要不要打 embedding —— 绝大多数用户没存过记忆,
        省掉这趟白跑的向量化既降延迟,也保证"无记忆"问答与现状逐事件、逐调用等价。
        """
        if not self._settings.memory_enabled or not question.strip():
            return []
        if self._memories.count_for_actor(space_id, user_id) == 0:
            return []
        embedding = self._embed_one(question)
        hits = self._memories.search_by_embedding(
            space_id,
            user_id,
            embedding,
            limit=self._settings.memory_recall_top_k,
            min_similarity=self._settings.memory_recall_min_sim,
        )
        return [memory for memory, _score in hits]

    def list_memories(
        self, space_id: uuid.UUID, user_id: uuid.UUID, limit: int, offset: int
    ) -> tuple[list[Memory], int]:
        self._require_member(space_id, user_id)
        return self._memories.list_for_actor(space_id, user_id, limit, offset)

    def update(
        self,
        space_id: uuid.UUID,
        user_id: uuid.UUID,
        memory_id: uuid.UUID,
        content: str | None = None,
        kind: str | None = None,
        topic: str | None = None,
    ) -> Memory:
        self._require_member(space_id, user_id)
        memory = self._memories.get(memory_id, space_id, user_id)
        if memory is None:
            raise AppError(ErrorCode.MEMORY_NOT_FOUND, "记忆不存在", http_status=404)
        if content is not None and content.strip():
            memory.content = content.strip()
            memory.embedding = self._embed_one(memory.content)  # 正文变了才重算向量
        if kind is not None and kind in _KINDS:
            memory.kind = kind
        if topic is not None:
            memory.topic = topic.strip()[:255]
        return self._memories.save(memory)

    def delete(self, space_id: uuid.UUID, user_id: uuid.UUID, memory_id: uuid.UUID) -> None:
        self._require_member(space_id, user_id)
        memory = self._memories.get(memory_id, space_id, user_id)
        if memory is None:
            raise AppError(ErrorCode.MEMORY_NOT_FOUND, "记忆不存在", http_status=404)
        self._memories.delete(memory)

    def dismiss_proposal(
        self,
        space_id: uuid.UUID,
        user_id: uuid.UUID,
        content: str,
        kind: str = "fact",
        conversation_id: uuid.UUID | None = None,
    ) -> None:
        """忽略某条记忆提议:写一行 dismissed 留痕(带 TTL),供后续不再弹同一结论。

        以归一化正文的哈希为去重键(与工具提议时同算法),重复忽略幂等只留一条。
        """
        self._require_member(space_id, user_id)
        if self._proposals is None:
            return  # 未接底座(单测/裁剪部署):静默无操作,不影响忽略交互
        text = content.strip()
        if not text:
            raise AppError(ErrorCode.VALIDATION, "忽略内容不能为空", http_status=400)
        now = datetime.now(UTC)
        content_hash = memory_content_hash(text)
        if self._proposals.exists(space_id, user_id, content_hash, now):
            return
        ttl_days = self._settings.memory_proposal_dismiss_ttl_days
        if kind not in _KINDS:
            kind = "fact"
        self._proposals.add(
            MemoryProposal(
                space_id=space_id,
                user_id=user_id,
                conversation_id=conversation_id,
                content_hash=content_hash,
                kind=kind,
                expires_at=now + timedelta(days=ttl_days),
            )
        )

    def dismissed_hashes(self, space_id: uuid.UUID, user_id: uuid.UUID) -> frozenset[str]:
        """本人在本空间未过期的被忽略内容哈希,供 Agent 弹卡前跳过。"""
        if self._proposals is None or not self._settings.memory_enabled:
            return frozenset()
        return frozenset(
            self._proposals.active_hashes(space_id, user_id, datetime.now(UTC))
        )

    def _embed_one(self, text: str) -> list[float]:
        return self._embedder.embed([text])[0]

    def _require_member(self, space_id: uuid.UUID, user_id: uuid.UUID) -> None:
        membership = self._spaces.get_membership(space_id, user_id)
        if membership is None:
            raise AppError(ErrorCode.SPACE_NOT_FOUND, "空间不存在", http_status=404)
        if membership.role < Role.VIEWER:
            raise AppError(ErrorCode.FORBIDDEN, "角色权限不足", http_status=403)
