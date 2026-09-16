"""PG 集成测试(testcontainers,基准 03):真实 Postgres + 真迁移 + 全链路 API。

外部依赖(Docker)不可用时 skip 而非失败;注册/登录/空间/成员/RBAC/审计一条龙。
"""

from __future__ import annotations

import os
import shutil

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client():
    if shutil.which("docker") is None:
        pytest.skip("docker 不可用,跳过 PG 集成测试")
    try:
        try:  # testcontainers 4.9+ 将社区模块迁移到 community 命名空间
            from testcontainers.community.postgres import PostgresContainer
        except ImportError:
            from testcontainers.postgres import PostgresContainer

        container = PostgresContainer("pgvector/pgvector:pg16", driver="psycopg")
        container.start()
    except Exception as exc:  # noqa: BLE001 — 环境不可用即跳过
        pytest.skip(f"无法启动 postgres 测试容器: {exc}")

    try:
        os.environ["APP_DATABASE_URL"] = container.get_connection_url()
        from alembic import command
        from alembic.config import Config

        from app.core.config import get_settings
        from app.core.db import get_engine, get_session_factory

        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()

        alembic_cfg = Config("alembic.ini")
        command.upgrade(alembic_cfg, "head")

        from fastapi.testclient import TestClient

        from app.main import create_app

        app = create_app()
        # CI/本地集成环境无 MinIO:存储依赖换内存替身,语义已由 test_storage 钉住
        from app.api.deps import get_memory_storage, get_storage

        app.dependency_overrides[get_storage] = get_memory_storage

        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client
    finally:
        from app.core.db import get_engine, get_session_factory

        get_engine.cache_clear()
        get_session_factory.cache_clear()
        container.stop()


def _register(client, username: str) -> dict:
    resp = client.post(
        "/api/v1/auth/register",
        json={"username": username, "nickname": username, "password": "secret-pass-1"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_full_auth_and_space_flow(client) -> None:
    # ---- 认证门禁 ----
    resp = client.get("/api/v1/users/me")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTH_REQUIRED"

    alice = _register(client, "alice")
    alice_auth = {"Authorization": f"Bearer {alice['access_token']}"}

    resp = client.get("/api/v1/users/me", headers=alice_auth)
    assert resp.status_code == 200 and resp.json()["username"] == "alice"

    # 参数校验走统一错误壳
    resp = client.post(
        "/api/v1/auth/register",
        json={"username": "x", "nickname": "", "password": "short"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    # 重复注册
    resp = client.post(
        "/api/v1/auth/register",
        json={"username": "alice", "nickname": "重复", "password": "secret-pass-1"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "USERNAME_TAKEN"

    # ---- 空间与成员 ----
    resp = client.post(
        "/api/v1/spaces",
        json={"name": "研发空间", "description": "M1 集成测试"},
        headers=alice_auth,
    )
    assert resp.status_code == 201, resp.text
    space_id = resp.json()["id"]

    bob = _register(client, "bob")
    bob_auth = {"Authorization": f"Bearer {bob['access_token']}"}

    # 非成员 404(与不存在同语义)
    resp = client.get(f"/api/v1/spaces/{space_id}", headers=bob_auth)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "SPACE_NOT_FOUND"

    # Owner 拉 Editor
    resp = client.post(
        f"/api/v1/spaces/{space_id}/members",
        json={"username": "bob", "role": 20},
        headers=alice_auth,
    )
    assert resp.status_code == 201, resp.text

    resp = client.get(f"/api/v1/spaces/{space_id}", headers=bob_auth)
    assert resp.status_code == 200 and resp.json()["role"] == 20

    # Editor 访问审计 → 403
    resp = client.get(f"/api/v1/spaces/{space_id}/audit-logs", headers=bob_auth)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"

    # bob 自我提权 → 403(先取成员列表拿 bob 的 user_id)
    resp = client.get(f"/api/v1/spaces/{space_id}/members", headers=alice_auth)
    member = next(m for m in resp.json() if m["username"] == "bob")
    bob_id = member["user_id"]
    resp = client.patch(
        f"/api/v1/spaces/{space_id}/members/{bob_id}", json={"role": 30}, headers=bob_auth
    )
    assert resp.status_code == 403

    # Owner 查审计:建空间/拉成员/拒绝事件都在
    resp = client.get(f"/api/v1/spaces/{space_id}/audit-logs", headers=alice_auth)
    assert resp.status_code == 200
    actions = [item["action"] for item in resp.json()["items"]]
    assert "space.created" in actions and "space.member_added" in actions

    # ---- 切空间轮换 ----
    resp = client.post(
        "/api/v1/auth/switch-space",
        json={"space_id": space_id, "refresh_token": alice["refresh_token"]},
        headers=alice_auth,
    )
    assert resp.status_code == 200, resp.text
    rotated = resp.json()

    resp = client.post("/api/v1/auth/refresh", json={"refresh_token": alice["refresh_token"]})
    assert resp.status_code == 401  # 旧 refresh 已作废
    resp = client.post("/api/v1/auth/refresh", json={"refresh_token": rotated["refresh_token"]})
    assert resp.status_code == 200
    latest = resp.json()

    # ---- 登出与改密踢下线 ----
    resp = client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": latest["refresh_token"]},
        headers={"Authorization": f"Bearer {latest['access_token']}"},
    )
    assert resp.status_code == 204
    resp = client.post("/api/v1/auth/refresh", json={"refresh_token": latest["refresh_token"]})
    assert resp.status_code == 401

    resp = client.post(
        "/api/v1/users/me/password",
        json={"old_password": "secret-pass-1", "new_password": "brand-new-pass"},
        headers=alice_auth,
    )
    assert resp.status_code == 204
    resp = client.post(
        "/api/v1/auth/login", json={"username": "alice", "password": "brand-new-pass"}
    )
    assert resp.status_code == 200
    resp = client.post(
        "/api/v1/auth/login", json={"username": "alice", "password": "secret-pass-1"}
    )
    assert resp.status_code == 401


def test_rag_data_layer_roundtrip(client) -> None:
    """M2 数据层:KB/文档/分块/任务四表在真实 PG 落库读回(含 vector/tsv/JSONB 列)。"""
    import uuid as uuid_mod

    from sqlalchemy import func, select, text

    from app.core.db import get_session_factory
    from app.domain.enums import DocumentStatus, TaskStatus, TaskType
    from app.domain.models import Chunk, Document, KnowledgeBase, Task

    owner = _register(client, "ragdataowner")
    auth = {"Authorization": f"Bearer {owner['access_token']}"}
    space_id = client.post(
        "/api/v1/spaces", json={"name": "数据层空间"}, headers=auth
    ).json()["id"]

    session_factory = get_session_factory()
    with session_factory() as db:
        kb = KnowledgeBase(
            id=uuid_mod.uuid4(), space_id=uuid_mod.UUID(space_id), name="数据层验证库"
        )
        doc = Document(
            id=uuid_mod.uuid4(),
            kb_id=kb.id,
            space_id=kb.space_id,
            filename="sample.pdf",
            format="pdf",
            source=f"{kb.space_id}/{kb.id}/{uuid_mod.uuid4()}/sample.pdf",
            status=DocumentStatus.PENDING,
        )
        db.add(kb)
        db.flush()  # 无 relationship 声明,先落 KB 再插 document 以满足 FK
        db.add(doc)
        db.flush()

        chunk = Chunk(
            id=uuid_mod.uuid4(),
            document_id=doc.id,
            space_id=doc.space_id,
            seq=0,
            content="知识库全文检索测试段落",
            embedding=[0.125] * 1024,
            tsv=func.to_tsvector("simple", "知识库全文检索测试段落"),
            meta={"page": 1, "breadcrumb": ["第一章"]},
        )
        task = Task(
            id=uuid_mod.uuid4(),
            type=TaskType.INGEST_DOCUMENT,
            payload={"document_id": str(doc.id), "trace_id": "t-123"},
            status=TaskStatus.PENDING,
        )
        db.add(chunk)
        db.add(task)
        db.commit()

        row = db.scalar(select(Chunk).where(Chunk.id == chunk.id))
        assert row is not None and len(row.embedding) == 1024
        assert row.embedding[0] == 0.125
        assert row.tsv is not None
        assert row.meta["breadcrumb"] == ["第一章"]

        hit = db.scalar(
            text(
                "SELECT id FROM chunks WHERE tsv @@ plainto_tsquery('simple', :q) "
                "AND space_id = :sid"
            ),
            {"q": "知识库全文检索测试段落", "sid": str(doc.space_id)},
        )
        assert hit is not None

        indexes = {
            r[0]
            for r in db.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'chunks'")
            )
        }
        assert "ix_chunks_embedding_hnsw" in indexes
        assert "ix_chunks_tsv_gin" in indexes

        loaded_task = db.scalar(select(Task).where(Task.id == task.id))
        assert loaded_task is not None
        assert loaded_task.payload["trace_id"] == "t-123"
        assert loaded_task.status == TaskStatus.PENDING


def test_kb_and_document_api_flow(client) -> None:
    """M2 知识域 API:KB CRUD + 上传/列表/详情/删除 + RBAC 门槛 + 审计动作。"""
    owner = _register(client, "kbowner")
    auth = {"Authorization": f"Bearer {owner['access_token']}"}
    space_id = client.post(
        "/api/v1/spaces", json={"name": "知识域空间"}, headers=auth
    ).json()["id"]
    base = f"/api/v1/spaces/{space_id}/knowledge-bases"

    # 未登录 401
    assert client.get(base).status_code == 401

    # 创建 KB(默认 embedding 模型来自 models.yaml 默认值)
    resp = client.post(
        base, json={"name": "产品文档库", "description": "d"}, headers=auth
    )
    assert resp.status_code == 201, resp.text
    kb = resp.json()
    assert kb["embedding_model"] == "dashscope/text-embedding-v3"
    assert kb["embedding_dim"] == 1024

    # 同名 409;列表可见
    assert client.post(base, json={"name": "产品文档库"}, headers=auth).status_code == 409
    assert len(client.get(base, headers=auth).json()) == 1

    # 上传文档(multipart)→ status=pending
    resp = client.post(
        f"{base}/{kb['id']}/documents",
        files={"file": ("入门.md", "# 标题\n正文".encode(), "text/markdown")},
        headers=auth,
    )
    assert resp.status_code == 201, resp.text
    doc = resp.json()
    assert doc["status"] == "pending" and doc["format"] == "md" and doc["size_bytes"] > 0

    # 不支持格式 415
    resp = client.post(
        f"{base}/{kb['id']}/documents",
        files={"file": ("a.exe", b"MZ", "application/x-msdownload")},
        headers=auth,
    )
    assert resp.status_code == 415
    assert resp.json()["error"]["code"] == "UNSUPPORTED_FORMAT"

    # 列表与详情
    assert client.get(f"{base}/{kb['id']}/documents", headers=auth).json()["total"] == 1
    assert (
        client.get(f"{base}/{kb['id']}/documents/{doc['id']}", headers=auth).status_code
        == 200
    )

    # 非成员访问空间资源 → 404
    other = _register(client, "kbother")
    other_auth = {"Authorization": f"Bearer {other['access_token']}"}
    resp = client.get(base, headers=other_auth)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "SPACE_NOT_FOUND"

    # Viewer 门槛:拉进空间给 Viewer 角色后,上传/删除 → 403
    client.post(
        f"/api/v1/spaces/{space_id}/members",
        json={"username": "kbother", "role": 10},
        headers=auth,
    )
    resp = client.post(
        f"{base}/{kb['id']}/documents", files={"file": ("b.md", b"x")}, headers=other_auth
    )
    assert resp.status_code == 403

    # 删除文档 → total 归零;删除 KB → 详情 404
    assert (
        client.delete(f"{base}/{kb['id']}/documents/{doc['id']}", headers=auth).status_code
        == 204
    )
    assert client.get(f"{base}/{kb['id']}/documents", headers=auth).json()["total"] == 0
    assert client.delete(f"{base}/{kb['id']}", headers=auth).status_code == 204
    assert client.get(f"{base}/{kb['id']}", headers=auth).status_code == 404

    # 审计动作齐全
    resp = client.get(f"/api/v1/spaces/{space_id}/audit-logs", headers=auth)
    actions = {item["action"] for item in resp.json()["items"]}
    assert {"kb.created", "document.uploaded", "document.deleted", "kb.deleted"} <= actions


def test_chunk_preview_api(client) -> None:
    """分块预览:登录可用,标题生成面包屑,超长文本按预算切分。"""
    user = _register(client, "chunkuser")
    auth = {"Authorization": f"Bearer {user['access_token']}"}

    assert client.post(
        "/api/v1/chunks/preview", json={"text": "内容"}
    ).status_code == 401

    text = "# 接入指南\n\n" + "。".join(f"配置步骤{i}说明文字内容" for i in range(80)) + "。"
    resp = client.post(
        "/api/v1/chunks/preview", json={"text": text, "format": "md"}, headers=auth
    )
    assert resp.status_code == 200, resp.text
    chunks = resp.json()
    assert len(chunks) > 1
    assert all(c["tokens"] <= 512 for c in chunks)
    assert all(c["breadcrumb"] == ["接入指南"] for c in chunks)
    assert all(c["kind"] == "text" for c in chunks)


def test_task_queue_claim_retry_dead_and_recover(client) -> None:
    """DB 队列(ADR-2)真实语义:SKIP LOCKED 认领、退避重试、死信、陈旧 claim 回收。"""
    import uuid as uuid_mod

    from app.application.repository.tasks import TaskRepositoryImpl
    from app.core.db import get_session_factory
    from app.domain.enums import DocumentStatus, TaskStatus, TaskType
    from app.domain.models import Document, KnowledgeBase, Task

    user = _register(client, "queueuser")
    auth = {"Authorization": f"Bearer {user['access_token']}"}
    space_id = client.post("/api/v1/spaces", json={"name": "队列空间"}, headers=auth).json()["id"]

    session_factory = get_session_factory()
    with session_factory() as db:
        kb = KnowledgeBase(space_id=uuid_mod.UUID(space_id), name="队列库")
        db.add(kb)
        db.flush()
        doc = Document(
            kb_id=kb.id,
            space_id=kb.space_id,
            filename="q.txt",
            format="txt",
            source=f"{kb.space_id}/{kb.id}/{uuid_mod.uuid4()}/q.txt",
            status=DocumentStatus.PENDING,
        )
        db.add(doc)
        db.flush()
        repo = TaskRepositoryImpl(db)
        task = repo.enqueue(
            Task(
                type=TaskType.INGEST_DOCUMENT,
                payload={"document_id": str(doc.id)},
                status=TaskStatus.PENDING,
                max_retry=2,
                timeout_s=1,
            )
        )
        db.commit()
        task_id = task.id

    # 认领:库中可能有其它用例遗留的 pending 任务,循环认领直到拿到本任务
    # (SKIP LOCKED 语义:已锁行被跳过而非等待,由下面 db2 断言验证)
    def claim_until(repo, worker: str, wanted: int):
        for _ in range(50):
            task_row = repo.claim(worker)
            if task_row is None:
                return None
            if task_row.id == wanted:
                return task_row
        raise AssertionError("未能在 50 次内认领到目标任务")

    with session_factory() as db1, session_factory() as db2:
        repo1, repo2 = TaskRepositoryImpl(db1), TaskRepositoryImpl(db2)
        first = claim_until(repo1, "worker-1", task_id)
        assert first is not None and first.id == task_id
        # 目标任务已被 db1 的行锁占用:db2 认领不会拿到同一行(不阻塞)
        second = repo2.claim("worker-2")
        assert second is None or second.id != task_id
        db1.commit()
        db2.rollback()

    # 退避重试:第 1 次失败 → pending 且 run_after 推后(退避期内不可被认领)
    with session_factory() as db:
        repo = TaskRepositoryImpl(db)
        task = repo.get(task_id)
        assert task is not None
        assert repo.mark_failed(task, "boom") is False
        assert task.status == TaskStatus.PENDING and task.retry_count == 1
        db.commit()

    with session_factory() as db:
        repo = TaskRepositoryImpl(db)
        for _ in range(50):  # 退避期内,任何 worker 都认领不到本任务
            claimed_row = repo.claim("worker-3")
            assert claimed_row is None or claimed_row.id != task_id
            if claimed_row is None:
                break
        db.rollback()

    # 第 2 次失败 → 死信(达到 max_retry)
    with session_factory() as db:
        repo = TaskRepositoryImpl(db)
        task = repo.get(task_id)
        assert task is not None
        assert repo.mark_failed(task, "boom again") is True
        assert task.status == TaskStatus.DEAD and task.retry_count == 2
        assert "boom again" in (task.last_error or "")
        db.commit()

    # 陈旧 claim 回收:running 且 claimed_at 早于 timeout_s → 重新入队
    with session_factory() as db:
        repo = TaskRepositoryImpl(db)
        stale = repo.enqueue(
            Task(
                type=TaskType.INGEST_DOCUMENT,
                payload={"document_id": str(doc.id)},
                status=TaskStatus.PENDING,
                max_retry=3,
                timeout_s=1,
            )
        )
        db.commit()
        stale_id = stale.id

    with session_factory() as db:
        repo = TaskRepositoryImpl(db)
        claimed = claim_until(repo, "worker-4", stale_id)
        assert claimed is not None and claimed.id == stale_id
        # 伪造成"很久以前认领":直接改 claimed_at 到过去
        from sqlalchemy import text as sql_text

        db.execute(
            sql_text("UPDATE tasks SET claimed_at = now() - interval '1 hour' WHERE id = :tid"),
            {"tid": str(stale_id)},
        )
        db.commit()

    with session_factory() as db:
        repo = TaskRepositoryImpl(db)
        assert repo.recover_stale() == 1
        db.commit()

    with session_factory() as db:
        repo = TaskRepositoryImpl(db)
        recovered = repo.get(stale_id)
        assert recovered is not None
        assert recovered.status == TaskStatus.PENDING
        assert recovered.retry_count == 1
        assert recovered.claimed_by is None
