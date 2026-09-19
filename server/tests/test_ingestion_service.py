"""入库流水线单元测试(假网关+假队列,基准 03):状态机、幂等、失败闭环。"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.application.service.ingestion import IngestionService, enqueue_document_ingest
from app.core.parser_client import ParserRejectedError
from app.core.storage import MemoryStorage
from app.domain.enums import DocumentStatus, TaskStatus, TaskType
from app.domain.models import Document, KnowledgeBase, Task
from tests.fakes import (
    FakeChunkRepository,
    FakeDocumentRepository,
    FakeKnowledgeBaseRepository,
    FakeTaskRepository,
)


class NullSession:
    """无事务副作用的会话替身:单测直接更新 fake 仓储,不需要真 commit/rollback。"""

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


class FakeParser:
    """可编程假解析网关:返回预置块或抛预置异常。"""

    def __init__(self, blocks: list | None = None, exc: Exception | None = None) -> None:
        self.blocks = blocks or []
        self.exc = exc
        self.calls = 0

    def parse(self, document_id: str, fmt: str, content: bytes):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.blocks, {}


class FakeEmbedder:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []

    def embed(self, texts: list[str], model_id: str | None = None) -> list[list[float]]:
        self.calls.append((texts, model_id))
        return [[0.5, 0.5] for _ in texts]


def build_env(parser: FakeParser | None = None, embedder: FakeEmbedder | None = None):
    documents = FakeDocumentRepository()
    kbs = FakeKnowledgeBaseRepository()
    tasks = FakeTaskRepository()
    chunks = FakeChunkRepository()
    storage = MemoryStorage()
    kb = kbs.create(KnowledgeBase(space_id=uuid.uuid4(), name="库"))
    doc = documents.create(
        Document(
            kb_id=kb.id,
            space_id=kb.space_id,
            filename="a.txt",
            format="txt",
            source=f"{kb.space_id}/{kb.id}/{uuid.uuid4()}/a.txt",
            status=DocumentStatus.PENDING,
        )
    )
    task = tasks.enqueue(Task(type=TaskType.INGEST_DOCUMENT, payload={"document_id": str(doc.id)}))
    storage.put(doc.source, "第一段。\n\n第二段。".encode())
    service = IngestionService(
        documents=documents,
        kbs=kbs,
        tasks=tasks,
        chunks=chunks,
        storage=storage,
        parser=parser
        or FakeParser(blocks=[SimpleNamespace(type="paragraph", text="第一段。第二段。")]),
        embedder=embedder or FakeEmbedder(),
    )
    return SimpleNamespace(
        service=service,
        doc=doc,
        task=task,
        tasks=tasks,
        chunks=chunks,
        documents=documents,
        kbs=kbs,
    )


def test_happy_path_status_flow_and_chunks() -> None:
    env = build_env()
    result = env.service.handle_next(db=NullSession(), worker_id="w1")
    assert result == str(env.task.id)
    assert env.task.status == TaskStatus.SUCCEEDED
    assert env.doc.status == DocumentStatus.COMPLETED
    stored = env.chunks.chunks[env.doc.id]
    assert len(stored) == 1  # 单段小文档:1 个父块
    seq, content, meta, children = stored[0]
    assert seq == 0
    assert "breadcrumb" in meta and "kind" in meta
    assert meta["tokens"] > 0  # 分块 token 数随 meta 落库(前端缺口台账 §11.2-4)
    # 短父块整块作为子块;向量只打在子块上(OPT-4)
    assert len(children) == 1
    child_content, child_embedding, child_meta = children[0]
    assert child_content == content
    assert child_embedding == [0.5, 0.5]
    assert child_meta["tokens"] > 0


def test_long_document_parents_unindexed_children_embedded() -> None:
    """父子分块落库形态:每个父块至少一个子块,向量只给子块;只向量化子块文本。"""
    embedder = FakeEmbedder()
    env = build_env(
        parser=FakeParser(
            blocks=[
                SimpleNamespace(
                    type="paragraph",
                    text="第一段。" + "内容句子。" * 120,
                    markdown="",
                    page=0,
                    level=0,
                )
            ]
        ),
        embedder=embedder,
    )
    env.service.handle_next(db=NullSession(), worker_id="w1")
    stored = env.chunks.chunks[env.doc.id]
    assert len(stored) >= 1
    child_total = sum(len(children) for _s, _c, _m, children in stored)
    assert child_total > len(stored)  # 长文:子块比父块多(窗口更细)
    for _seq, _content, _meta, children in stored:
        assert all(emb is not None for _c, emb, _m in children)
        assert all(cm["tokens"] > 0 for _c, _e, cm in children)
    # 向量化调用只包含子块文本(按序),不含父块全文
    texts, _model = embedder.calls[0]
    assert texts == [c for _s, _ct, _m, ch in stored for c, _e, _cm in ch]
    assert len(texts) == child_total


def test_completed_document_is_idempotent() -> None:
    env = build_env()
    env.doc.status = DocumentStatus.COMPLETED
    env.service.handle_next(db=NullSession(), worker_id="w1")
    assert env.task.status == TaskStatus.SUCCEEDED
    assert env.doc.id not in env.chunks.chunks  # 未重写分块


def test_missing_document_task_succeeds() -> None:
    env = build_env()
    env.task.payload = {"document_id": str(uuid.uuid4())}
    env.service.handle_next(db=NullSession(), worker_id="w1")
    assert env.task.status == TaskStatus.SUCCEEDED


def test_parser_failure_retries_then_dead_and_fails_document() -> None:
    env = build_env(parser=FakeParser(exc=ParserRejectedError("DATA_LOSS: bad pdf")))
    # 前两次失败 → 退避重试;第三次(retry_count==max_retry)死信 + 文档 failed
    env.service.handle_next(db=NullSession(), worker_id="w1")
    assert env.task.status == TaskStatus.PENDING and env.task.retry_count == 1
    assert env.doc.status == DocumentStatus.PARSING  # 重试期间保持中间态,不翻 failed
    env.service.handle_next(db=NullSession(), worker_id="w1")
    assert env.task.retry_count == 2
    env.service.handle_next(db=NullSession(), worker_id="w1")
    assert env.task.status == TaskStatus.DEAD
    assert env.doc.status == DocumentStatus.FAILED
    assert env.doc.error_code == "INGEST_FAILED"


def test_embedder_receives_kb_model() -> None:
    embedder = FakeEmbedder()
    env = build_env(embedder=embedder)
    env.service.handle_next(db=NullSession(), worker_id="w1")
    texts, model_id = embedder.calls[0]
    assert texts and model_id == env.kbs.kbs[env.doc.kb_id].embedding_model


def test_enqueue_uses_registered_task_type() -> None:
    tasks = FakeTaskRepository()
    document_id = uuid.uuid4()
    task = enqueue_document_ingest(tasks, document_id)
    assert task.type == TaskType.INGEST_DOCUMENT
    assert task.status == TaskStatus.PENDING
    assert task.payload == {"document_id": str(document_id)}
