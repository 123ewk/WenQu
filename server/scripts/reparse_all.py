"""按当前分块/解析逻辑重建已完成文档的块(运维脚本)。

什么时候需要跑:解析器或分块器的逻辑变更后,已入库文档的分块仍是旧逻辑产物。
本脚本对目标文档重跑一次入库流水线(解析 → 分块 → 向量化 → 写库),使其与新逻辑一致。

两条安全纪律(都是踩过坑补的):
1. **先确认原始文件在对象存储里存在**,不存在就跳过 —— 否则会把文档从 completed
   改成 pending/failed(冒烟测试残留的假 source 就会触发这种误伤);
2. **入队后轮询数据库等终态**,不自己认领任务 —— worker 可能同时在跑,自己认领会
   拿到别人的任务并误报结果。

运行(需 postgres + minio + parser 服务就绪,且 `.env` 里模型 Key 可用的环境):
    uv run python scripts/reparse_all.py                 # 全部 completed 文档
    uv run python scripts/reparse_all.py --dry-run       # 只统计,不改库
    uv run python scripts/reparse_all.py --space <uuid>  # 只处理某个空间
    uv run python scripts/reparse_all.py --document <uuid>

幂等:块先删后插,重复执行不会产生重复块。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid

os.environ.setdefault(
    "APP_DATABASE_URL", "postgresql+psycopg://wenqu:wenqu_dev_only@localhost:5432/wenqu"
)
# 与 parser 服务默认端口一致;若 .env 配了 PARSER_GRPC_ADDR,以 .env 为准
os.environ.setdefault("PARSER_GRPC_ADDR", "127.0.0.1:50051")

from sqlalchemy import func, select  # noqa: E402

from app.application.repository.tasks import TaskRepositoryImpl  # noqa: E402
from app.application.service.ingestion import enqueue_document_ingest  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.db import get_session_factory  # noqa: E402
from app.core.storage import MinioStorage  # noqa: E402
from app.domain.enums import DocumentStatus  # noqa: E402
from app.domain.models import Chunk, Document  # noqa: E402

_TERMINAL = {DocumentStatus.COMPLETED, DocumentStatus.FAILED}
_POLL_SECONDS = 0.5
_TIMEOUT_SECONDS = 900  # 单份文档最长等待(大文件向量化较慢)


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

    storage = MinioStorage(
        settings.minio_endpoint,
        settings.minio_access_key,
        settings.minio_secret_key,
        settings.minio_bucket,
        settings.minio_secure,
    )

    # 预处理:区分"原始文件健在"与"对象已不可用"的文档
    ready: list[tuple[Document, int]] = []
    missing: list[Document] = []
    for doc in documents:
        try:
            storage.get(doc.source)
        except Exception:  # noqa: BLE001 — 对象不可用即跳过,不动它的状态
            missing.append(doc)
            continue
        ready.append((doc, chunk_count(session, doc.id)))

    embedder_note = "入队后由后台 worker 消费(未启动 worker 会等待到超时)"
    print(f"待处理 {len(documents)} 份:可重建 {len(ready)} 份,跳过 {len(missing)} 份"
          + ("(dry-run,不写库)" if args.dry_run else f"  [{embedder_note}]"))
    if missing:
        print("  以下文档的原始文件不在对象存储里(无法重建,状态未改动):")
        for doc in missing:
            print(f"    - {doc.filename}  source={doc.source}")

    if args.dry_run:
        for doc, n in ready:
            print(f"  {doc.filename[:40]:40} {doc.format:5} 当前 {n} 块")
        print(f"合计 {sum(n for _d, n in ready)} 块")
        return 0

    processed = failed = timed_out = 0
    for doc, before in ready:
        doc.status = DocumentStatus.PENDING
        doc.error_code = None
        doc.error_message = None
        session.flush()
        enqueue_document_ingest(TaskRepositoryImpl(session), doc.id)
        session.commit()

        # 只观察结果:任务由 worker(或脚本自身的流水线)消费,谁做都行
        deadline = time.monotonic() + _TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            time.sleep(_POLL_SECONDS)
            session.expire_all()
            current = session.get(Document, doc.id)
            if current is not None and current.status in _TERMINAL:
                break

        session.expire_all()
        current = session.get(Document, doc.id)
        status = current.status if current else "?"
        after = chunk_count(session, doc.id)
        if status == DocumentStatus.COMPLETED:
            processed += 1
            mark = "=" if before == after else "→"
            print(f"  ✓ {doc.filename[:36]:36} {before} {mark} {after} 块")
        elif status == DocumentStatus.PENDING and time.monotonic() >= deadline:
            timed_out += 1
            # 没有 worker 消费:把状态放回 completed,不留"永挂 pending"的文档
            if current is not None:
                current.status = DocumentStatus.COMPLETED
                session.commit()
            print(f"  ⏱ {doc.filename[:36]:36} 超时未处理(是否未启动 worker?)已还原状态")
        else:
            failed += 1
            print(f"  ✗ {doc.filename[:36]:36} 未完成(status={status},块={after})")

    print(f"\n完成:成功 {processed} 份,失败 {failed} 份,超时 {timed_out} 份,跳过 {len(missing)} 份")
    return 0 if failed == 0 and timed_out == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
