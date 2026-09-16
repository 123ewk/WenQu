"""DB 任务队列实现(ADR-2):SKIP LOCKED 认领 + 退避重试 + 死信 + 陈旧回收。

Redis 的替代位(基准偏离已登记,触发条件:日任务 >1000)。claim 用
FOR UPDATE SKIP LOCKED 保证多 worker 并发认领互不阻塞;认领/失败闭环在同
一 Session 事务内完成,由调用方提交。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.enums import TaskStatus
from app.domain.models import Task

_BACKOFF_BASE_SECONDS = 30  # 第 n 次重试退避 30 * 2^(n-1) 秒


def _utcnow() -> datetime:
    return datetime.now(UTC)


class TaskRepositoryImpl:
    def __init__(self, db: Session) -> None:
        self._db = db

    def enqueue(self, task: Task) -> Task:
        self._db.add(task)
        self._db.flush()
        return task

    def get(self, task_id: uuid.UUID) -> Task | None:
        return self._db.get(Task, task_id)

    def claim(self, worker_id: str) -> Task | None:
        task = self._db.scalar(
            select(Task)
            .where(Task.status == TaskStatus.PENDING, Task.run_after <= func.now())
            .order_by(Task.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if task is None:
            return None
        task.status = TaskStatus.RUNNING
        task.claimed_by = worker_id
        task.claimed_at = _utcnow()
        self._db.flush()
        return task

    def mark_succeeded(self, task: Task) -> None:
        task.status = TaskStatus.SUCCEEDED
        task.last_error = None
        self._db.flush()

    def mark_failed(self, task: Task, error: str) -> bool:
        task.retry_count += 1
        task.last_error = error[:2000]
        if task.retry_count >= task.max_retry:
            task.status = TaskStatus.DEAD
            self._db.flush()
            return True
        task.status = TaskStatus.PENDING
        backoff = _BACKOFF_BASE_SECONDS * 2 ** (task.retry_count - 1)
        task.run_after = _utcnow() + timedelta(seconds=backoff)
        task.claimed_by = None
        task.claimed_at = None
        self._db.flush()
        return False

    def recover_stale(self) -> int:
        recovered = 0
        stale_tasks = list(
            self._db.scalars(
                select(Task)
                .where(
                    Task.status == TaskStatus.RUNNING,
                    Task.claimed_at.is_not(None),
                    # 超过任务自身 timeout_s 仍未完成 → 认领者已死
                    Task.claimed_at
                    < func.now() - func.make_interval(0, 0, 0, 0, 0, 0, Task.timeout_s),
                )
                .with_for_update(skip_locked=True)
            )
        )
        for task in stale_tasks:
            task.retry_count += 1
            if task.retry_count >= task.max_retry:
                task.status = TaskStatus.DEAD
                task.last_error = "stale claim recovered but retries exhausted"
            else:
                task.status = TaskStatus.PENDING
                task.last_error = "stale claim recovered"
            task.claimed_by = None
            task.claimed_at = None
            recovered += 1
        if stale_tasks:
            self._db.flush()
        return recovered
