"""业务数据访问接口(集中定义,基准 01)。

服务层只依赖本文件 Protocol,实现位于 application/repository;
测试用内存 fake 实现同一协议(基准 03)。
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from app.domain.models import (
    ApiKey,
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

if TYPE_CHECKING:
    from app.application.repository.knowledge import KbStats  # 仅类型标注用


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
        self,
        space_id: uuid.UUID,
        limit: int,
        offset: int,
        action: str | None = None,
        actor_id: uuid.UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
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
        families: list[
            tuple[int, str, dict, list[tuple[str, list[float] | None, dict]]]
        ],
    ) -> None:
        """幂等重建:先删后插(父子分块,OPT-4)。

        families = [(seq, 父块content, 父块meta, [(子块content, 子块embedding, 子块meta), ...])];
        父块是引用/上下文单元,不进索引(embedding/tsv 落空);子块是检索窗口,
        携带向量与全文索引,meta 含 tokens。
        """


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

    def get_many(self, chunk_ids: list[uuid.UUID]) -> list[Chunk]:
        """按 id 批量取块(父子分块:检索命子块后回取父块,OPT-4)。"""


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


class ApiKeyRepository(Protocol):
    """API Key 持久化:按哈希查找(认证热路径),按空间列出/改名/吊销。"""

    def get_by_hash(self, key_hash: str) -> ApiKey | None: ...
    def get(self, key_id: uuid.UUID) -> ApiKey | None: ...
    def create(self, api_key: ApiKey) -> ApiKey: ...
    def save(self, api_key: ApiKey) -> ApiKey: ...
    def list_for_space(self, space_id: uuid.UUID) -> list[ApiKey]: ...


class ChatGateway(Protocol):
    """对话网关(真实实现走模型层 SSE;测试用假实现)。

    返回 Generator(而非裸 Iterator)是契约的一部分:断线宽限到点时,泵线程靠
    close() 在当前 yield 点中止生成并触发实现内部的 with 清理,释放上游连接。
    """

    def chat_stream(
        self, messages: list[dict[str, str]], model_id: str | None = None
    ) -> Generator[str, None, None]: ...


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


class KbStatsRepository(Protocol):
    """知识库聚合统计与空间级入库进度(KbStats 定义在 application/repository)。"""

    def stats_for_kbs(self, kb_ids: list[uuid.UUID]) -> dict[uuid.UUID, KbStats]: ...
    def list_active_in_space(self, space_id: uuid.UUID, limit: int) -> list[Document]: ...
    def counts_by_status(self, space_id: uuid.UUID) -> dict[str, int]: ...


class ChunkQueryRepository(Protocol):
    """分块读取(前端分块查看页 + 引用抽屉):按文档分页列块 / 取单块全文。"""

    def get(self, chunk_id: uuid.UUID) -> Chunk | None: ...

    def list_for_document(
        self, document_id: uuid.UUID, limit: int, offset: int
    ) -> tuple[list[Chunk], int]: ...
