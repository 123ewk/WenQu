"""后台 worker 入口:python -m app.worker(M2-4)。

循环:陈旧 claim 回收(每 30s)→ SKIP LOCKED 认领 → 处理 → 空转 sleep。
网关(解析/向量化/对象存储)进程级复用;仓储按会话绑定。
优雅停机:M4 随部署翻转接 SIGTERM;当前 Ctrl+C 退出,任务有回收兜底。
"""

from __future__ import annotations

import logging
import socket
import threading
import time

from app.application.repository.knowledge import (
    ChunkRepositoryImpl,
    DocumentRepositoryImpl,
    KnowledgeBaseRepositoryImpl,
)
from app.application.repository.tasks import TaskRepositoryImpl
from app.application.service.ingestion import IngestionService
from app.application.service.maintenance import MaintenanceService
from app.core.config import get_settings
from app.core.db import get_session_factory
from app.core.model_catalog import ModelCatalog
from app.core.model_client import EmbeddingClient
from app.core.parser_client import ParserClient
from app.core.storage import MinioStorage

logger = logging.getLogger("app.worker")

_IDLE_SECONDS = 2.0
_RECOVER_INTERVAL_SECONDS = 30.0
_MAINTENANCE_INTERVAL_SECONDS = 3600.0  # 清理任务每小时一轮


class Worker:
    def __init__(self) -> None:
        settings = get_settings()
        catalog = ModelCatalog.load()
        self._parser = ParserClient(settings.parser_grpc_addr, settings.parser_grpc_token)
        self._embedder = EmbeddingClient(
            catalog,
            settings,
            background_semaphore=threading.Semaphore(settings.model_bg_concurrency),
        )
        self._storage = MinioStorage(
            settings.minio_endpoint,
            settings.minio_access_key,
            settings.minio_secret_key,
            settings.minio_bucket,
            settings.minio_secure,
        )
        self._audit_retention_days = settings.audit_retention_days
        self._last_recover = 0.0
        self._last_maintenance = 0.0

    def run_maintenance(self) -> tuple[int, int]:
        """审计保留期清扫 + 过期 refresh 清除(失败只记日志,不打断主循环)。"""
        with get_session_factory()() as db:
            service = MaintenanceService(db, self._audit_retention_days)
            try:
                return service.run_all()
            except Exception:  # noqa: BLE001 — 清理失败不影响任务处理
                logger.exception("maintenance run failed")
                return (0, 0)

    def run_once(self, worker_id: str) -> str | None:
        """回收陈旧 claim(限频)→ 认领处理一条;队列空返回 None。"""
        session_factory = get_session_factory()
        with session_factory() as db:
            service = IngestionService(
                documents=DocumentRepositoryImpl(db),
                kbs=KnowledgeBaseRepositoryImpl(db),
                tasks=TaskRepositoryImpl(db),
                chunks=ChunkRepositoryImpl(db),
                storage=self._storage,
                parser=self._parser,
                embedder=self._embedder,
            )
            now = time.monotonic()
            if now - self._last_maintenance > _MAINTENANCE_INTERVAL_SECONDS:
                self._last_maintenance = now
                # 让出当前会话,避免与任务事务互相影响
                db.commit()
                self.run_maintenance()
                db.commit()
            if now - self._last_recover > _RECOVER_INTERVAL_SECONDS:
                recovered = service.recover_stale(db)
                if recovered:
                    logger.warning("recovered %d stale tasks", recovered)
                self._last_recover = now
            return service.handle_next(db, worker_id)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    worker = Worker()
    worker_id = f"{socket.gethostname()}-{id(worker) & 0xFFFF:04x}"
    logger.info("worker %s started", worker_id)
    try:
        while True:
            handled = worker.run_once(worker_id)
            if handled is None:
                time.sleep(_IDLE_SECONDS)
    except KeyboardInterrupt:
        logger.info("worker %s stopped", worker_id)


if __name__ == "__main__":
    main()
