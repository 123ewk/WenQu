"""业务数据访问接口(集中定义,基准 01)。

服务层只依赖本文件 Protocol,实现位于 application/repository;
测试用内存 fake 实现同一协议(基准 03)。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Protocol

from app.domain.models import (
    AuditLog,
    Chunk,
    Conversation,
    Document,
    KnowledgeBase,
    Membership,
    Message,
    RefreshToken,
    Space,
    Task,
    User,
)


class UserRepository(Protocol):
    def get(self, user_id: uuid.UUID) -> User | None: ...
    def get_by_username(self, username: str) -> User | None: ...
    def create(self, user: User) -> User: ...
    def save(self, user: User) -> User: ...


class RefreshTokenRepository(Protocol):
    def get_by_hash(self, token_hash: str) -> RefreshToken | None: ...
    def create(self, token: RefreshToken) -> RefreshToken: ...
    def delete(self, token: RefreshToken) -> None: ...
    def delete_by_user(self, user_id: uuid.UUID) -> None: ...


class SpaceRepository(Protocol):
    def get(self, space_id: uuid.UUID) -> Space | None: ...
    def create(self, space: Space) -> Space: ...
    def save(self, space: Space) -> Space: ...
    def delete(self, space: Space) -> None: ...
    def list_for_user(self, user_id: uuid.UUID) -> list[tuple[Space, int]]: ...
    def get_membership(self, space_id: uuid.UUID, user_id: uuid.UUID) -> Membership | None: ...
    def get_member(self, space_id: uuid.UUID, member_user_id: uuid.UUID) -> Membership | None: ...
    def list_members(self, space_id: uuid.UUID) -> list[tuple[Membership, User]]: ...
    def add_member(self, membership: Membership) -> Membership: ...
    def update_member(self, membership: Membership) -> Membership: ...
    def remove_member(self, membership: Membership) -> None: ...


class AuditRepository(Protocol):
    def add(self, log: AuditLog) -> AuditLog: ...
    def list_for_space(
        self, space_id: uuid.UUID, limit: int, offset: int
    ) -> tuple[list[AuditLog], int]: ...


class KnowledgeBaseRepository(Protocol):
    def get(self, kb_id: uuid.UUID) -> KnowledgeBase | None: ...
    def get_by_name(self, space_id: uuid.UUID, name: str) -> KnowledgeBase | None: ...
    def create(self, kb: KnowledgeBase) -> KnowledgeBase: ...
    def save(self, kb: KnowledgeBase) -> KnowledgeBase: ...
    def delete(self, kb: KnowledgeBase) -> None: ...
    def list_for_space(self, space_id: uuid.UUID) -> list[KnowledgeBase]: ...


class DocumentRepository(Protocol):
    def get(self, document_id: uuid.UUID) -> Document | None: ...
    def create(self, document: Document) -> Document: ...
    def save(self, document: Document) -> Document: ...
    def delete(self, document: Document) -> None: ...
    def list_for_kb(
        self, kb_id: uuid.UUID, limit: int, offset: int
    ) -> tuple[list[Document], int]: ...


class ChunkRepository(Protocol):
    def replace_for_document(
        self,
        document: Document,
        drafts: list[tuple[int, str, list[float] | None, dict]],
    ) -> None:
        """幂等重建:先删后插。drafts = [(seq, content, embedding, meta)]。"""


class TaskRepository(Protocol):
    def enqueue(self, task: Task) -> Task: ...
    def get(self, task_id: uuid.UUID) -> Task | None: ...
    def claim(self, worker_id: str) -> Task | None:
        """SKIP LOCKED 认领一条到期待处理任务(置 running 并记认领人)。"""
    def mark_succeeded(self, task: Task) -> None: ...
    def mark_failed(self, task: Task, error: str) -> bool:
        """失败闭环:重试(退避)或落死信;返回是否已死信。"""
    def recover_stale(self) -> int:
        """陈旧 claim 回收:超 timeout_s 仍 running 的任务重新入队;返回回收数。"""


class RetrievalRepository(Protocol):
    """混合检索候选召回(两路独立,Q可合并);租户谓词在实现层强制。"""

    def vector_search(
        self,
        space_id: uuid.UUID,
        embedding: list[float],
        kb_ids: list[uuid.UUID] | None,
        limit: int,
        min_similarity: float = 0.0,
    ) -> list[tuple[Chunk, Document, float]]: ...

    def fulltext_search(
        self,
        space_id: uuid.UUID,
        jieba_tokens: str,
        kb_ids: list[uuid.UUID] | None,
        limit: int,
    ) -> list[tuple[Chunk, Document, float]]: ...


class ConversationRepository(Protocol):
    def create(self, conversation: Conversation) -> Conversation: ...
    def get(self, conversation_id: uuid.UUID) -> Conversation | None: ...
    def save(self, conversation: Conversation) -> Conversation: ...
    def delete(self, conversation: Conversation) -> None: ...
    def list_for_user(
        self, space_id: uuid.UUID, user_id: uuid.UUID
    ) -> list[Conversation]: ...


class MessageRepository(Protocol):
    def add(self, message: Message) -> Message: ...
    def list_for_conversation(self, conversation_id: uuid.UUID) -> list[Message]: ...
    def next_seq(self, conversation_id: uuid.UUID) -> int: ...


class ChatGateway(Protocol):
    """对话网关(真实实现走模型层 SSE;测试用假实现)。"""

    def chat_stream(
        self, messages: list[dict[str, str]], model_id: str | None = None
    ) -> Iterator[str]: ...


class ParserGateway(Protocol):
    """解析服务网关(真实实现走 gRPC,测试用假实现)。"""

    def parse(
        self, document_id: str, fmt: str, content: bytes
    ) -> tuple[list[object], dict[str, str]]: ...


class EmbeddingGateway(Protocol):
    """向量化网关(真实实现走模型层,测试用假实现)。model_id 缺省用全局默认。"""

    def embed(
        self, texts: list[str], model_id: str | None = None
    ) -> list[list[float]]: ...
