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


def test_hybrid_retrieval_real_pg(client) -> None:
    """混合检索真库验证:向量路与全文路独立命中,RRF 融合,租户隔离,权限 404/403。"""
    import uuid as uuid_mod

    import jieba
    from sqlalchemy import func

    from app.api.deps import get_embedding_gateway
    from app.application.repository.retrieval import RetrievalRepositoryImpl
    from app.core.db import get_session_factory
    from app.domain.enums import DocumentStatus
    from app.domain.models import Chunk, Document, KnowledgeBase

    # 假 embedder:1024 维空间里,相关文本落在 e0 附近、无关文本落在 e1 附近
    def _vec(primary: bool) -> list[float]:
        v = [0.0] * 1024
        v[0 if primary else 1] = 1.0
        return v

    class StubEmbedder:
        def embed(self, texts, model_id=None):
            return [
                _vec("检索" in t or "算法" in t or "rrf" in t.lower()) for t in texts
            ]

    import app.main as main_module

    created = main_module.create_app()
    created.dependency_overrides[get_embedding_gateway] = StubEmbedder
    from fastapi.testclient import TestClient

    owner = _register(client, "retrievalowner")
    auth = {"Authorization": f"Bearer {owner['access_token']}"}
    space_id = client.post("/api/v1/spaces", json={"name": "检索空间"}, headers=auth).json()["id"]

    # 造两个知识库:一个含目标内容(嵌入向量贴近查询),一个含干扰内容
    with get_session_factory()() as db:
        kb_ok = KnowledgeBase(space_id=uuid_mod.UUID(space_id), name="目标库")
        kb_other = KnowledgeBase(space_id=uuid_mod.UUID(space_id), name="干扰库")
        db.add(kb_ok)
        db.add(kb_other)
        db.flush()
        doc_ok = Document(
            kb_id=kb_ok.id, space_id=kb_ok.space_id, filename="算法说明.md", format="md",
            source=f"{space_id}/{kb_ok.id}/d1", status=DocumentStatus.COMPLETED,
        )
        doc_other = Document(
            kb_id=kb_other.id, space_id=kb_other.space_id, filename="无关.md", format="md",
            source=f"{space_id}/{kb_other.id}/d2", status=DocumentStatus.COMPLETED,
        )
        db.add(doc_ok)
        db.add(doc_other)
        db.flush()
        db.add(
            Chunk(
                document_id=doc_ok.id, space_id=doc_ok.space_id, seq=0,
                content="混合检索算法通过 RRF 融合向量与全文两路排名。",
                embedding=_vec(True),
                tsv=func.to_tsvector(
                    "simple",
                    " ".join(
                        jieba.cut_for_search("混合检索算法通过 RRF 融合向量与全文两路排名。")
                    ),
                ),
            )
        )
        db.add(
            Chunk(
                document_id=doc_other.id, space_id=doc_other.space_id, seq=0,
                content="完全无关的报销制度说明。",
                embedding=_vec(False),
                tsv=func.to_tsvector(
                    "simple", " ".join(jieba.cut_for_search("完全无关的报销制度说明。"))
                ),
            )
        )
        db.commit()

    # 仓储层直测:两路各自都能命中目标块
    with get_session_factory()() as db:
        repo = RetrievalRepositoryImpl(db)
        vector_hits = repo.vector_search(uuid_mod.UUID(space_id), _vec(True), None, 5)
        assert vector_hits and "混合检索算法" in vector_hits[0][0].content
        fulltext_hits = repo.fulltext_search(
            uuid_mod.UUID(space_id), " ".join(jieba.cut_for_search("检索算法")), None, 5
        )
        assert fulltext_hits and "混合检索算法" in fulltext_hits[0][0].content
        # 租户谓词:换一个空间查同一内容 → 空
        assert repo.vector_search(uuid_mod.uuid4(), _vec(True), None, 5) == []
        assert repo.fulltext_search(uuid_mod.uuid4(), "检索", None, 5) == []
        # kb_ids 收窄
        narrowed = repo.vector_search(
            uuid_mod.UUID(space_id), _vec(True), [kb_other.id], 5
        )
        assert all(doc.kb_id == kb_other.id for _c, doc, _s in narrowed)

    # API 层:走完整路由(依赖覆盖假 embedder)
    other = _register(client, "retrievalother")
    other_auth = {"Authorization": f"Bearer {other['access_token']}"}
    resp = client.post(
        f"/api/v1/spaces/{space_id}/retrieval/search",
        json={"query": "混合检索算法", "top_k": 3},
        headers=other_auth,
    )
    assert resp.status_code == 404  # 非成员 → 防枚举 404

    with TestClient(created, raise_server_exceptions=False) as overridden:
        resp = overridden.post(
            f"/api/v1/spaces/{space_id}/retrieval/search",
            json={"query": "混合检索算法", "top_k": 3},
            headers=auth,
        )
        assert resp.status_code == 200, resp.text
        hits = resp.json()
        assert hits and "混合检索算法" in hits[0]["content"]
        assert hits[0]["score"] > 0
        assert hits[0]["vector_rank"] is not None or hits[0]["fulltext_rank"] is not None
        assert hits[0]["meta"].get("kind") in (None, "text")


def test_qa_sse_flow_with_citations(client) -> None:
    """问答闭环:SSE 事件时序、引用落库可回溯、会话隔离、无资料不调模型。"""
    import uuid as uuid_mod

    import jieba
    from fastapi.testclient import TestClient
    from sqlalchemy import func

    import app.main as main_module
    from app.api.deps import get_chat_gateway, get_embedding_gateway
    from app.core.db import get_session_factory
    from app.domain.enums import DocumentStatus
    from app.domain.models import Chunk, Document, KnowledgeBase

    def _vec(primary: bool) -> list[float]:
        v = [0.0] * 1024
        v[0 if primary else 1] = 1.0
        return v

    class StubEmbedder:
        def embed(self, texts, model_id=None):
            return [_vec("检索" in t or "算法" in t) for t in texts]

    class StubChat:
        def __init__(self) -> None:
            self.prompts: list[list[dict]] = []

        def chat_stream(self, messages, model_id=None):
            self.prompts.append(messages)
            yield "根据资料 [1],"
            yield "混合检索采用 RRF 融合。"

    stub_chat = StubChat()
    overridden_app = main_module.create_app()
    overridden_app.dependency_overrides[get_embedding_gateway] = StubEmbedder
    overridden_app.dependency_overrides[get_chat_gateway] = lambda: stub_chat

    owner = _register(client, "qaowner")
    auth = {"Authorization": f"Bearer {owner['access_token']}"}
    space_id = client.post("/api/v1/spaces", json={"name": "问答空间"}, headers=auth).json()["id"]

    with get_session_factory()() as db:
        kb = KnowledgeBase(space_id=uuid_mod.UUID(space_id), name="问答库")
        db.add(kb)
        db.flush()
        doc = Document(
            kb_id=kb.id, space_id=kb.space_id, filename="检索手册.md", format="md",
            source=f"{space_id}/{kb.id}/d", status=DocumentStatus.COMPLETED,
        )
        db.add(doc)
        db.flush()
        db.add(
            Chunk(
                document_id=doc.id, space_id=doc.space_id, seq=0,
                content="混合检索算法使用 RRF 融合向量与全文排名。",
                embedding=_vec(True),
                tsv=func.to_tsvector(
                    "simple",
                    " ".join(jieba.cut_for_search("混合检索算法使用 RRF 融合向量与全文排名。")),
                ),
            )
        )
        db.commit()

    with TestClient(overridden_app, raise_server_exceptions=False) as qa_client:
        # 权限:非成员 404(校验前置在流开始前,所以能拿到 404 而非 200 流)
        other = _register(client, "qaother")
        other_auth = {"Authorization": f"Bearer {other['access_token']}"}
        resp = qa_client.post(
            f"/api/v1/spaces/{space_id}/ask",
            json={"question": "混合检索算法是什么"},
            headers=other_auth,
        )
        assert resp.status_code == 404

        # 空提问 400(同样是前置校验)
        resp = qa_client.post(
            f"/api/v1/spaces/{space_id}/ask", json={"question": ""}, headers=auth
        )
        assert resp.status_code == 422  # pydantic min_length

        resp = qa_client.post(
            f"/api/v1/spaces/{space_id}/ask",
            json={"question": "混合检索算法是什么", "top_k": 3},
            headers=auth,
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")

        events = []
        for line in resp.text.splitlines():
            if line.startswith("data: "):
                import json as json_mod

                events.append(json_mod.loads(line[len("data: ") :]))

        types = [e["type"] for e in events]
        assert types == ["meta", "citations", "delta", "delta", "done"], types
        conversation_id = events[0]["conversation_id"]

        # 引用可回溯:携带 chunk_id/文件名/摘录
        citation = events[1]["citations"][0]
        assert citation["filename"] == "检索手册.md"
        assert citation["chunk_id"] and citation["excerpt"]
        assert events[-1]["cited_indexes"] == [1]

        # 提示词确实带上编号资料
        system_prompt = stub_chat.prompts[0][0]["content"]
        assert "[1] 来源:检索手册.md" in system_prompt

        # 落库消息带引用(刷新页面后仍可回链)
        resp = qa_client.get(
            f"/api/v1/spaces/{space_id}/conversations/{conversation_id}/messages",
            headers=auth,
        )
        assert resp.status_code == 200
        messages = resp.json()
        assert [m["role"] for m in messages] == ["user", "assistant"]
        assert messages[1]["citations"][0]["chunk_id"] == citation["chunk_id"]
        assert messages[1]["content"].startswith("根据资料 [1]")

        # 会话列表;他人拿不到我的会话(404 防枚举)
        listed = qa_client.get(f"/api/v1/spaces/{space_id}/conversations", headers=auth)
        assert [c["id"] for c in listed.json()] == [conversation_id]
        client.post(
            f"/api/v1/spaces/{space_id}/members",
            json={"username": "qaother", "role": 20},
            headers=auth,
        )
        resp = qa_client.get(
            f"/api/v1/spaces/{space_id}/conversations/{conversation_id}/messages",
            headers=other_auth,
        )
        assert resp.status_code == 404

        # 无检索命中 → 不调模型,给固定提示
        before = len(stub_chat.prompts)
        resp = qa_client.post(
            f"/api/v1/spaces/{space_id}/ask",
            json={"question": "完全不相关的报销问题", "top_k": 3},
            headers=auth,
        )
        import json as json_mod

        cold = [
            json_mod.loads(line[len("data: ") :])
            for line in resp.text.splitlines()
            if line.startswith("data: ")
        ]
        assert "没有检索到" in cold[2]["text"]
        assert len(stub_chat.prompts) == before  # 未调模型


def test_maintenance_purges_expired_tokens_and_old_audit(client) -> None:
    """维护任务真库验证:过期 refresh 清除、审计保留期边界(不误删保留期内)。"""
    import uuid as uuid_mod
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text as sql_text

    from app.application.service.maintenance import MaintenanceService
    from app.core.db import get_session_factory
    from app.domain.enums import AuditAction
    from app.domain.models import AuditLog, RefreshToken

    user = _register(client, "maintuser")
    auth = {"Authorization": f"Bearer {user['access_token']}"}
    space_id = client.post(
        "/api/v1/spaces", json={"name": "维护空间"}, headers=auth
    ).json()["id"]
    user_id = uuid_mod.UUID(
        client.get("/api/v1/users/me", headers=auth).json()["id"]
    )

    now = datetime.now(UTC)
    with get_session_factory()() as db:
        # 一个已过期、一个仍有效
        db.add(
            RefreshToken(
                user_id=user_id,
                token_hash="a" * 64,
                expires_at=now - timedelta(hours=1),
            )
        )
        db.add(
            RefreshToken(
                user_id=user_id,
                token_hash="b" * 64,
                expires_at=now + timedelta(days=1),
            )
        )
        # 一条过期审计、一条保留期内审计
        old_log = AuditLog(
            actor_id=user_id,
            space_id=uuid_mod.UUID(space_id),
            action=AuditAction.SPACE_UPDATED,
            target="old",
        )
        db.add(old_log)
        db.flush()
        db.execute(
            sql_text(
                "UPDATE audit_logs SET created_at = now() - interval '200 days' "
                "WHERE id = :i"
            ),
            {"i": str(old_log.id)},
        )
        db.add(
            AuditLog(
                actor_id=user_id,
                space_id=uuid_mod.UUID(space_id),
                action=AuditAction.SPACE_UPDATED,
                target="recent",
            )
        )
        db.commit()

    # 保留期 180 天(默认):200 天前那条应被删,今天的保留
    with get_session_factory()() as db:
        service = MaintenanceService(db, audit_retention_days=180)
        removed_tokens, removed_logs = service.run_all()
        assert removed_tokens == 1  # 只删过期那条
        assert removed_logs == 1  # 只删 200 天前那条

    with get_session_factory()() as db:
        lookup = sql_text(
            "SELECT token_hash FROM refresh_tokens WHERE token_hash IN (:a, :b)"
        )
        remaining_tokens = db.scalars(lookup, {"a": "a" * 64, "b": "b" * 64}).all()
        assert remaining_tokens == ["b" * 64]
        remaining_audit = db.execute(
            sql_text("SELECT target FROM audit_logs WHERE space_id = :s"),
            {"s": space_id},
        ).all()
        targets = {row[0] for row in remaining_audit}
        assert "recent" in targets and "old" not in targets



def test_audit_filters_and_enriched_fields(client) -> None:
    """审计增强契约(缺口 #1/#3/#4):筛选参数、结果字段、操作人昵称冗余。"""
    owner = _register(client, "auditowner")
    auth = {"Authorization": f"Bearer {owner['access_token']}"}
    space_id = client.post(
        "/api/v1/spaces", json={"name": "审计空间"}, headers=auth
    ).json()["id"]

    # 制造多种动作 + 一次被拒操作(Editor 越权改名 → 403)
    client.post(
        f"/api/v1/spaces/{space_id}/members",
        json={"username": "auditowner", "role": 10},
        headers=auth,
    )
    editor = _register(client, "auditeditor")
    editor_auth = {"Authorization": f"Bearer {editor['access_token']}"}
    client.post(
        f"/api/v1/spaces/{space_id}/members",
        json={"username": "auditeditor", "role": 20},
        headers=auth,
    )
    forbidden = client.patch(
        f"/api/v1/spaces/{space_id}", json={"name": "越权改名"}, headers=editor_auth
    )
    assert forbidden.status_code == 403

    base = f"/api/v1/spaces/{space_id}/audit-logs"
    all_logs = client.get(base, headers=auth).json()

    # 每行都有结果字段与操作人昵称(前端不再用 members 映射兜底)
    for item in all_logs["items"]:
        assert item["result"] in ("success", "denied")
        assert item["actor_id"] is None or item["actor_name"]
    assert any(item["result"] == "denied" for item in all_logs["items"])

    # 按动作筛选
    filtered = client.get(base, params={"action": "space.updated"}, headers=auth).json()
    assert filtered["total"] == 0  # 那次改名被拒,没有成功记录

    # 按操作人筛选:只返回该操作人的记录,且被拒记录归属发起者 editor
    editor_id = next(
        item["actor_id"]
        for item in all_logs["items"]
        if item["result"] == "denied"
    )
    by_actor = client.get(base, params={"actor_id": editor_id}, headers=auth).json()
    assert by_actor["total"] >= 1
    assert {item["actor_id"] for item in by_actor["items"]} == {editor_id}
    assert any(item["result"] == "denied" for item in by_actor["items"])
    assert all(item["actor_name"] == "auditeditor" for item in by_actor["items"])

    # 时间范围(闭区间):用现有最新一条的时间戳,必须能取到
    newest = all_logs["items"][0]["created_at"]
    in_range = client.get(base, params={"since": newest}, headers=auth).json()
    assert in_range["total"] >= 1
    assert all(item["created_at"] >= newest for item in in_range["items"])



def test_denied_audit_noise_control(client) -> None:
    """401 噪声控制:常规 token 缺失(如 /users/me)不落被拒审计;

    否则 token 过期会把审计表刷满(缺口 #3 的结果列价值被稀释)。
    """
    owner = _register(client, "noiseowner")
    auth = {"Authorization": f"Bearer {owner['access_token']}"}
    space_id = client.post(
        "/api/v1/spaces", json={"name": "噪声空间"}, headers=auth
    ).json()["id"]

    before = client.get(f"/api/v1/spaces/{space_id}/audit-logs", headers=auth).json()["total"]

    # 无 token 访问受保护接口 → 401,但不应产生审计记录
    assert client.get("/api/v1/users/me").status_code == 401
    assert client.get(f"/api/v1/spaces/{space_id}/audit-logs").status_code == 401

    after = client.get(f"/api/v1/spaces/{space_id}/audit-logs", headers=auth).json()["total"]
    assert after == before

    # 登录失败(401)属于安全事件:应留下记录,但不属于任何空间
    resp = client.post(
        "/api/v1/auth/login", json={"username": "noiseowner", "password": "wrong-pass"}
    )
    assert resp.status_code == 401
    # 登录失败审计已由 auth 服务写入(action=auth.login_failed),此处验证其不污染空间审计
    assert (
        client.get(f"/api/v1/spaces/{space_id}/audit-logs", headers=auth).json()["total"]
        == before
    )
