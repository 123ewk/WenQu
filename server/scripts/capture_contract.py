"""抓取新增端点的真实响应形状,用于编写/复核前端对接文档的响应示例。

⚠️ 会在**目标数据库**中创建样例数据(一个空间、一个知识库、若干文档与分块),
   仅供开发/文档用途;不要指向生产库。

用法:uv run python scripts/capture_contract.py(需先 alembic upgrade head)
仅覆盖两处外部依赖:向量化与对象存储(模型 API 需真实 Key,故用确定性桩)。
"""

from __future__ import annotations

import io
import json
import os
import uuid

os.environ["APP_DATABASE_URL"] = "postgresql+psycopg://wenqu:wenqu_dev_only@localhost:5432/wenqu"

import logging

logging.disable(logging.CRITICAL)

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func

from app.api.deps import get_embedding_gateway, get_storage
from app.core.db import get_session_factory
from app.core.storage import MemoryStorage
from app.domain.enums import DocumentStatus
from app.domain.models import Chunk, Document
from app.main import create_app


class StubEmbedder:
    def embed(self, texts, model_id=None):
        v = [0.0] * 1024
        v[0] = 1.0
        return [v for _ in texts]


def show(title, payload, limit=None):
    print(f"\n=== {title} ===")
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    print(text[:limit] if limit else text)


app = create_app()
shared = MemoryStorage()
app.dependency_overrides[get_storage] = lambda: shared
app.dependency_overrides[get_embedding_gateway] = StubEmbedder

with TestClient(app, raise_server_exceptions=False) as c:
    tag = uuid.uuid4().hex[:6]
    reg = c.post(
        "/api/v1/auth/register",
        json={"username": f"doc{tag}", "nickname": "文档样例", "password": "secret-pass-1"},
    ).json()
    auth = {"Authorization": f"Bearer {reg['access_token']}"}
    space = c.post("/api/v1/spaces", json={"name": "示例空间"}, headers=auth).json()
    sid = space["id"]

    # 模型清单
    show("GET /api/v1/models(节选)", c.get("/api/v1/models", headers=auth).json(), 700)

    # KB + 造统计数据
    kb = c.post(
        f"/api/v1/spaces/{sid}/knowledge-bases",
        json={"name": "产品手册库", "description": "示例"},
        headers=auth,
    ).json()
    with get_session_factory()() as db:
        done = Document(
            kb_id=uuid.UUID(kb["id"]), space_id=uuid.UUID(sid), filename="手册.pdf",
            format="pdf", size_bytes=204800, source="s1", status=DocumentStatus.COMPLETED,
        )
        busy = Document(
            kb_id=uuid.UUID(kb["id"]), space_id=uuid.UUID(sid), filename="价格表.xlsx",
            format="xlsx", size_bytes=51200, source="s2", status=DocumentStatus.EMBEDDING,
        )
        bad = Document(
            kb_id=uuid.UUID(kb["id"]), space_id=uuid.UUID(sid), filename="旧版.doc",
            format="docx", size_bytes=10240, source="s3", status=DocumentStatus.FAILED,
            error_code="INGEST_FAILED",
            error_message="仅支持 pdf/docx/xlsx/pptx/md/txt(旧版 office 格式请另存为新格式)",
        )
        db.add_all([done, busy, bad])
        db.flush()
        chunk = Chunk(
            document_id=done.id, space_id=done.space_id, seq=0,
            content="混合检索使用 RRF 融合向量与全文两路排名。" * 3,
            tsv=func.to_tsvector("simple", "混合 检索 RRF 融合"),
            meta={"kind": "table", "tokens": 137, "breadcrumb": ["产品手册库", "检索设计"], "page": 3},
        )
        db.add(chunk)
        db.commit()
        chunk_id = chunk.id

    show(
        "GET knowledge-bases(带聚合统计)",
        c.get(f"/api/v1/spaces/{sid}/knowledge-bases", headers=auth).json()[0],
    )

    show(
        "GET ingestion-progress",
        c.get(f"/api/v1/spaces/{sid}/ingestion-progress", headers=auth).json(),
    )

    show(
        "GET 文档详情(失败文档,含 error_message)",
        c.get(
            f"/api/v1/spaces/{sid}/knowledge-bases/{kb['id']}/documents/{bad.id}",
            headers=auth,
        ).json(),
    )

    show(
        "GET 分块详情(引用抽屉用全文)",
        c.get(
            f"/api/v1/spaces/{sid}/knowledge-bases/{kb['id']}/documents/{done.id}"
            f"/chunks/{chunk_id}",
            headers=auth,
        ).json(),
    )

    show(
        "POST 检索(请求级覆盖,不改空间配置)",
        c.post(
            f"/api/v1/spaces/{sid}/retrieval/search",
            json={
                "query": "混合检索",
                "vector_weight": 0.5,
                "fulltext_weight": 0.5,
                "min_score": 0.2,
                "rrf_k": 30,
                "top_k": 3,
            },
            headers=auth,
        ).json(),
        600,
    )

    reparse = c.post(
        f"/api/v1/spaces/{sid}/knowledge-bases/{kb['id']}/documents/{bad.id}/reparse",
        headers=auth,
    )
    show("POST reparse(返回重置后的文档)", reparse.json())
    show(
        "POST reparse 处理中(409)",
        c.post(
            f"/api/v1/spaces/{sid}/knowledge-bases/{kb['id']}/documents/{bad.id}/reparse",
            headers=auth,
        ).json(),
    )

    # 头像:验证 avatar_url 形态
    buf = io.BytesIO()
    Image.new("RGB", (48, 48), (79, 110, 242)).save(buf, format="PNG")
    up = c.post(
        "/api/v1/users/me/avatar",
        files={"file": ("me.png", buf.getvalue(), "image/png")},
        headers=auth,
    ).json()
    show("POST avatar 后 UserOut.avatar_url", {"avatar_url": up["avatar_url"]})
    show(
        "GET members(带 avatar_url)",
        c.get(f"/api/v1/spaces/{sid}/members", headers=auth).json(),
        500,
    )

    conv = c.post(
        f"/api/v1/spaces/{sid}/conversations", json={"title": "新会话"}, headers=auth
    ).json()
    show(
        "PATCH 会话重命名",
        c.patch(
            f"/api/v1/spaces/{sid}/conversations/{conv['id']}",
            json={"title": "报销制度咨询"},
            headers=auth,
        ).json(),
    )

    show(
        "审计 result/actor_name(被拒记录)",
        [
            i
            for i in c.get(f"/api/v1/spaces/{sid}/audit-logs", headers=auth).json()["items"]
            if i["result"] == "denied"
        ][:1],
    )
