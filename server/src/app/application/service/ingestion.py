"""入库流水线编排(ADR-2):解析 → 分块 → 向量化 → 索引,状态机顺位推进。

失败语义(设计文档):任何一步异常 → 任务重试/死信;文档仅在任务**死信**时置
failed(重试期间保持最后推进到的中间态,用户可见"处理中")。
幂等:已完成的文档重复认领直接成功;分块重建先删后插,重跑不产生重复块。
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from app.application.chunking import chunk_blocks
from app.core.storage import ObjectStorage
from app.domain.enums import DocumentStatus, TaskType
from app.domain.interfaces import (
    ChunkRepository,
    DocumentRepository,
    EmbeddingGateway,
    KnowledgeBaseRepository,
    ParserGateway,
    TaskRepository,
)
from app.domain.models import Task

logger = logging.getLogger("app.ingestion")


class IngestionService:
    def __init__(
        self,
        documents: DocumentRepository,
        kbs: KnowledgeBaseRepository,
        tasks: TaskRepository,
        chunks: ChunkRepository,
        storage: ObjectStorage,
        parser: ParserGateway,
        embedder: EmbeddingGateway,
    ) -> None:
        self._documents = documents
        self._kbs = kbs
        self._tasks = tasks
        self._chunks = chunks
        self._storage = storage
        self._parser = parser
        self._embedder = embedder

    def handle_next(self, db: Session, worker_id: str) -> str | None:
        """认领并处理一条任务;返回任务 id,队列空返回 None。失败按退避/死信闭环。"""
        task = self._tasks.claim(worker_id)
        if task is None:
            return None
        try:
            self._process(task)
        except Exception as exc:  # noqa: BLE001 — 任务级失败闭环,不允许打断 worker 循环
            logger.warning("task %s failed: %s", task.id, exc)
            db.rollback()  # 丢弃半程状态;task 行随后在新事务里闭环
            dead = self._tasks.mark_failed(task, str(exc))
            if dead:
                self._fail_document(task)
            db.commit()
            return str(task.id)
        self._tasks.mark_succeeded(task)
        db.commit()
        return str(task.id)

    def recover_stale(self, db: Session) -> int:
        count = self._tasks.recover_stale()
        db.commit()
        return count

    # ---------------------------- 内部 ----------------------------

    def _process(self, task: Task) -> None:
        document = self._load_document(task)
        if document is None:
            return  # 文档已被删除:任务自然结束
        if document.status == DocumentStatus.COMPLETED:
            return  # 幂等:重复认领直接成功

        document.status = DocumentStatus.PARSING
        self._documents.save(document)
        content = self._storage.get(document.source)
        blocks, _meta = self._parser.parse(str(document.id), document.format, content)

        document.status = DocumentStatus.CHUNKING
        self._documents.save(document)
        drafts = chunk_blocks(blocks)

        document.status = DocumentStatus.EMBEDDING
        self._documents.save(document)
        kb = self._kbs.get(document.kb_id)
        # KB 级 embedding 模型(换模型 = 该 KB 重建索引);KB 意外缺失退回默认模型
        model_id = kb.embedding_model if kb is not None else None
        texts = [d.content for d in drafts]
        vectors = self._embedder.embed(texts, model_id) if texts else []

        self._chunks.replace_for_document(
            document,
            [
                (
                    seq,
                    draft.content,
                    vectors[seq] if seq < len(vectors) else None,
                    {
                        "breadcrumb": draft.breadcrumb,
                        "page": draft.page,
                        "kind": draft.kind,
                        "tokens": draft.tokens,  # ChunkOut.tokens 展示用(前端缺口台账 §11.2-4)
                    },
                )
                for seq, draft in enumerate(drafts)
            ],
        )
        document.status = DocumentStatus.COMPLETED
        document.error_code = None
        self._documents.save(document)

    def _load_document(self, task: Task):
        payload = task.payload or {}
        raw = payload.get("document_id")
        if not raw:
            raise ValueError("task payload 缺少 document_id")
        return self._documents.get(uuid.UUID(str(raw)))

    def _fail_document(self, task: Task) -> None:
        """死信同步:文档翻 failed,机器码 + 任务侧真实原因都给到用户。"""
        try:
            document = self._load_document(task)
            if document is None:
                return
            document.status = DocumentStatus.FAILED
            document.error_code = "INGEST_FAILED"
            document.error_message = (task.last_error or "")[:2000] or None
            self._documents.save(document)
        except Exception:  # noqa: BLE001 — 死信同步失败只记日志,不影响任务闭环
            logger.exception("mark document failed for task %s", task.id)


def enqueue_document_ingest(tasks: TaskRepository, document_id: uuid.UUID) -> Task:
    """上传后入队;type 注册于 TaskType,trace_id 预留链路追踪。"""
    task = Task(
        type=TaskType.INGEST_DOCUMENT,
        payload={"document_id": str(document_id)},
    )
    return tasks.enqueue(task)
