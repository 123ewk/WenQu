"""后台 worker 入口:python -m app.worker(M2-4)。

循环:陈旧 claim 回收(每 30s)→ SKIP LOCKED 认领 → 处理 → 空转 sleep。
网关(解析/对象存储)进程级复用;向量化网关按轮构建(模型目录库内可改,OPT-25);
仓储按会话绑定。
优雅停机:M4 随部署翻转接 SIGTERM;当前 Ctrl+C 退出,任务有回收兜底。
"""

from __future__ import annotations

import logging
import socket
import threading
import time
import uuid

from app.application.repository.knowledge import (
    ChunkRepositoryImpl,
    DocumentRepositoryImpl,
    KnowledgeBaseRepositoryImpl,
)
from app.application.repository.tasks import TaskRepositoryImpl
from app.application.service.ingestion import IngestionService
from app.application.service.maintenance import MaintenanceService
from app.application.service.usage import UsageRecorder
from app.core.config import get_settings
from app.core.db import get_session_factory
from app.core.model_catalog import ModelCatalog
from app.core.model_client import EmbeddingClient
from app.core.parser_client import ParserClient
from app.core.storage import MinioStorage
from app.domain.interfaces import EmbeddingGateway

logger = logging.getLogger("app.worker")

_IDLE_SECONDS = 2.0
_RECOVER_INTERVAL_SECONDS = 30.0
_MAINTENANCE_INTERVAL_SECONDS = 3600.0  # 清理任务每小时一轮


class Worker:
    def __init__(self) -> None:
        settings = get_settings()
        self._parser = ParserClient(settings.parser_grpc_addr, settings.parser_grpc_token)
        # 信号量刻意进程级:它是全 worker 的后台向量化并发上限;
        # 目录则每轮重建 —— 自定义模型(OPT-25)运行期可增删,不能进程级缓存
        self._bg_semaphore = threading.Semaphore(settings.model_bg_concurrency)
        self._storage = MinioStorage(
            settings.minio_endpoint,
            settings.minio_access_key,
            settings.minio_secret_key,
            settings.minio_bucket,
            settings.minio_secure,
        )
        self._audit_retention_days = settings.audit_retention_days
        # OPT-28:入库 token 用量按空间/成员记账(独立会话写,失败只记日志)
        self._usage_recorder = UsageRecorder(get_session_factory())
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

    def _wrap_embedder(
        self,
        client: EmbeddingClient,
        space_id: uuid.UUID | None,
        user_id: uuid.UUID | None,
    ) -> EmbeddingGateway:
        """取不到空间 = 无租户上下文,不记账(与 API 层网关同一口径)。"""
        if space_id is None:
            return client
        return self._usage_recorder.embedding(client, space_id, user_id)

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
                # OPT-27:按任务所属空间现建目录快照(空间模型/空间 embedding Key;信号量仍进程级)
                # OPT-28:空间已知才套计量外壳;user_id 为文档上传者(可空 → 只进空间合计)
                embedder_factory=lambda session, space_id, user_id: self._wrap_embedder(
                    EmbeddingClient(
                        ModelCatalog.load_from_db(session, space_id),
                        background_semaphore=self._bg_semaphore,
                    ),
                    space_id,
                    user_id,
                ),
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
