"""按当前分块/解析逻辑重建全部已完成文档的块(运维脚本)。

什么时候需要跑:解析器或分块器的逻辑变更后,已入库文档的分块仍是旧逻辑产物。
本脚本对目标文档重跑一次入库流水线(解析 → 分块 → 向量化 → 写库),使其与新逻辑一致。

**幂等**:`replace_for_document` 先删后插,重复执行不会产生重复块。

运行(需 postgres + minio + parser 服务就绪):
    uv run python scripts/reparse_all.py                 # 全部 completed 文档
    uv run python scripts/reparse_all.py --dry-run       # 只统计当前块数,不改库
    uv run python scripts/reparse_all.py --space <uuid>  # 只处理某个空间
    uv run python scripts/reparse_all.py --document <uuid>

注意:向量化走真实模型网关,未配置模型 Key 时会失败并跳过该文档(不动其现有块)。
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid

os.environ.setdefault(
    "APP_DATABASE_URL", "postgresql+psycopg://wenqu:wenqu_dev_only@localhost:5432/wenqu"
)
os.environ.setdefault("PARSER_GRPC_ADDR", "127.0.0.1:50071")
os.environ.setdefault("PARSER_GRPC_TOKEN", "wenqu_dev_parser_token")

from sqlalchemy import func, select  # noqa: E402

from app.application.repository.knowledge import (  # noqa: E402
    ChunkRepositoryImpl,
    DocumentRepositoryImpl,
    KnowledgeBaseRepositoryImpl,
)
from app.application.repository.tasks import TaskRepositoryImpl  # noqa: E402
from app.application.service.ingestion import (  # noqa: E402
    IngestionService,
    enqueue_document_ingest,
)
from app.core.config import get_settings  # noqa: E402
from app.core.db import get_session_factory  # noqa: E402
from app.core.model_catalog import ModelCatalog  # noqa: E402
from app.core.model_client import EmbeddingClient  # noqa: E402
from app.core.parser_client import ParserClient  # noqa: E402
from app.core.storage import MinioStorage  # noqa: E402
from app.domain.enums import DocumentStatus  # noqa: E402
from app.domain.models import Chunk, Document  # noqa: E402


def chunk_count(session, document_id: uuid.UUID) -> int:
    total = session.scalar(
        select(func.count()).select_from(Chunk).where(Chunk.document_id == document_id)
    )
    return int(total or 0)


def main() -> int:
    parser_args = argparse.ArgumentParser(description="重建已完成文档的分块")
    parser_args.add_argument("--dry-run", action="store_true", help="只统计,不改库")
    parser_args.add_argument("--space", help="只处理该空间的文档")
    parser_args.add_argument("--document", help="只处理该文档")
    args = parser_args.parse_args()

    settings = get_settings()
    session = get_session_factory()()

    stmt = select(Document).where(Document.status == DocumentStatus.COMPLETED)
    if args.space:
        stmt = stmt.where(Document.space_id == uuid.UUID(args.space))
    if args.document:
        stmt = stmt.where(Document.id == uuid.UUID(args.document))
    documents = list(session.scalars(stmt.order_by(Document.created_at)))

    if not documents:
        print("没有状态为 completed 的文档,无需处理")
        return 0

    print(f"待处理 {len(documents)} 份文档" + ("(dry-run,不写库)" if args.dry_run else ""))
    if args.dry_run:
        total = 0
        for doc in documents:
            n = chunk_count(session, doc.id)
            total += n
            print(f"  {doc.filename[:40]:40} {doc.format:5} chunks={n}")
        print(f"合计 {total} 块")
        return 0

    storage = MinioStorage(
        settings.minio_endpoint,
        settings.minio_access_key,
        settings.minio_secret_key,
        settings.minio_bucket,
        settings.minio_secure,
    )
    parser = ParserClient(settings.parser_grpc_addr, settings.parser_grpc_token)
    embedder = EmbeddingClient(ModelCatalog.load(), settings)

    processed = skipped = 0
    for doc in documents:
        before = chunk_count(session, doc.id)
        # 复用真实 reparse 语义:置 pending → 入队 → 认领执行
        doc.status = DocumentStatus.PENDING
        doc.error_code = None
        doc.error_message = None
        session.flush()
        enqueue_document_ingest(TaskRepositoryImpl(session), doc.id)
        session.commit()

        service = IngestionService(
            DocumentRepositoryImpl(session),
            KnowledgeBaseRepositoryImpl(session),
            TaskRepositoryImpl(session),
            ChunkRepositoryImpl(session),
            storage,
            parser,
            embedder,
        )
        task_id = service.handle_next(session, "reparse-all")
        session.refresh(doc)
        after = chunk_count(session, doc.id)
        if doc.status == DocumentStatus.COMPLETED and task_id:
            processed += 1
            print(f"  ✓ {doc.filename[:38]:38} {before} → {after} 块")
        else:
            skipped += 1
            # 失败会把状态留在中间态/失败;恢复为 completed 以免文档"看起来坏了"
            doc.status = DocumentStatus.COMPLETED
            session.commit()
            print(f"  ✗ {doc.filename[:38]:38} 跳过(原因见日志;块保持 {after} 个)")

    print(f"\n完成:成功 {processed} 份,跳过 {skipped} 份")
    return 0 if skipped == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
