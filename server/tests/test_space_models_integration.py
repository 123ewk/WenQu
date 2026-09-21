"""OPT-27b 空间分层模型目录集成测试(真 Postgres)。

覆盖 ModelCatalog.load_from_db(session, space_id) 的四条核心语义:
① 全局 ∪ 空间合并与同名/同 id 遮蔽、默认"空间 → 全局"解析;
② 空间配了向量化 Key 时合成内置 embedding 并顶为默认;
③ 环境门控:prod 快照排除全局 embedding 行(503),dev 保留回落(WARNING);
④ rerank_enabled 随快照带出。
只读目录,不解密(解密发生在真正调用模型时),因此不需要主密钥。
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from app.core.db import get_session_factory
from app.core.errors import AppError
from app.core.model_catalog import (
    BUILTIN_EMBEDDING_MODEL_ID,
    BUILTIN_EMBEDDING_PROVIDER_KEY,
    ModelCatalog,
)
from app.domain.models import CustomModel, ModelProvider, SpaceModelSettings
from tests.conftest import register as _register

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _reset_model_tables(client):
    """session 级共用库:用例前清残留(含其他文件的全局默认),用例后清自己的。

    默认唯一是 partial unique(全局/空间各一套),残留全局默认会挡住本文件的断言与写入。
    """
    with get_session_factory()() as db:
        db.execute(delete(SpaceModelSettings))
        db.execute(delete(CustomModel))
        db.execute(delete(ModelProvider))
        db.commit()
    yield
    with get_session_factory()() as db:
        db.execute(delete(SpaceModelSettings))
        db.execute(delete(CustomModel))
        db.execute(delete(ModelProvider))
        db.commit()


def _mk_provider(
    db,
    *,
    name: str,
    space_id: uuid.UUID | None = None,
    api_key_encrypted: str = "enc:v1:placeholder",
) -> ModelProvider:
    p = ModelProvider(
        name=name,
        protocol="openai_compatible",
        base_url=f"https://{name}.example/v1",
        api_key_encrypted=api_key_encrypted,
        space_id=space_id,
    )
    db.add(p)
    db.flush()  # 生成 id,供 _mk_model 引用
    return p


def _mk_model(
    db,
    *,
    provider: ModelProvider,
    model_id: str,
    kind: str = "chat",
    space_id: uuid.UUID | None = None,
    is_default: bool = False,
    dims: int | None = None,
) -> CustomModel:
    m = CustomModel(
        model_id=model_id,
        provider_id=provider.id,
        kind=kind,
        model_name=model_id.split("/")[-1],
        space_id=space_id,
        is_default=is_default,
        dims=dims,
    )
    db.add(m)
    return m


def _space(client, username: str, name: str) -> str:
    """注册一个用户并建空间(POST /spaces 需 JWT);返回 space_id 字符串。"""
    auth = {"Authorization": f"Bearer {_register(client, username)['access_token']}"}
    resp = client.post("/api/v1/spaces", json={"name": name}, headers=auth)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_space_layer_merges_and_shadows_global(client) -> None:
    space_id = uuid.UUID(_space(client, "layertest", "分层空间"))
    with get_session_factory()() as db:
        gp = _mk_provider(db, name="共用厂")
        _mk_model(db, provider=gp, model_id="g/chat", is_default=True)
        _mk_model(db, provider=gp, model_id="g/emb", kind="embedding", dims=1024, is_default=True)
        sp = _mk_provider(db, name="共用厂", space_id=space_id)  # 同名遮蔽
        # 同 id 改属性:空间层的 g/chat 顶掉全局那条
        _mk_model(
            db, provider=sp, model_id="g/chat", space_id=space_id, is_default=True
        )
        db.commit()
        gp_url, sp_url = gp.base_url, sp.base_url

    with get_session_factory()() as db:
        global_only = ModelCatalog.load_from_db(db)
        assert global_only.providers["共用厂"].base_url == gp_url
        assert global_only.defaults() == {"chat": "g/chat", "embedding": "g/emb"}
        assert global_only.space_id is None and global_only.rerank_enabled is False

        merged = ModelCatalog.load_from_db(db, space_id)
        assert merged.providers["共用厂"].base_url == sp_url  # 空间行遮蔽
        assert merged.get_model("g/chat").provider_key == "共用厂"
        assert merged.get_model("g/chat").context_tokens is None
        assert merged.defaults()["chat"] == "g/chat"  # 空间默认顶掉全局
        assert merged.defaults()["embedding"] == "g/emb"  # 全局 embedding 在 dev 可见


def test_builtin_embedding_synthesized_when_space_has_key(client) -> None:
    space_id = uuid.UUID(_space(client, "builtintest", "内置KEY空间"))
    with get_session_factory()() as db:
        gp = _mk_provider(db, name="全局厂")
        _mk_model(db, provider=gp, model_id="g/emb", kind="embedding", dims=1024, is_default=True)
        db.add(
            SpaceModelSettings(
                space_id=space_id,
                embedding_key_encrypted="enc:v1:spacekey",
                embedding_key_hint="sk-s****test",
            )
        )
        db.commit()

    with get_session_factory()() as db:
        # 全局视图不受影响:仍只有运维的 g/emb
        assert ModelCatalog.load_from_db(db).defaults() == {"embedding": "g/emb"}

        merged = ModelCatalog.load_from_db(db, space_id)
        assert merged.defaults()["embedding"] == BUILTIN_EMBEDDING_MODEL_ID  # 顶掉全局默认
        entry = merged.default_model("embedding")
        assert (entry.dims, entry.provider_key) == (1024, BUILTIN_EMBEDDING_PROVIDER_KEY)
        provider = merged.get_provider(BUILTIN_EMBEDDING_PROVIDER_KEY)
        assert provider.api_key_encrypted == "enc:v1:spacekey"
        # 内置 provider 不进全局层
        assert BUILTIN_EMBEDDING_PROVIDER_KEY not in ModelCatalog.load_from_db(db).providers


def _prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.core.model_catalog.get_settings", lambda: SimpleNamespace(env="prod")
    )


def test_prod_excludes_global_embedding_and_message_is_tailored(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    space_id = uuid.UUID(_space(client, "prodgate", "prod门控空间"))
    with get_session_factory()() as db:
        gp = _mk_provider(db, name="prod厂")
        _mk_model(db, provider=gp, model_id="g/emb", kind="embedding", dims=1024, is_default=True)
        db.commit()

    _prod(monkeypatch)
    with get_session_factory()() as db:
        merged = ModelCatalog.load_from_db(db, space_id)
        assert "g/emb" not in merged.models  # 全局 embedding 行不进 prod 空间快照
        with pytest.raises(AppError) as exc_info:
            merged.default_model("embedding")
        assert exc_info.value.http_status == 503
        assert "该空间未配置向量化 Key" in exc_info.value.message
        # 全局视图(运维 /admin)仍可用全局 embedding
        assert ModelCatalog.load_from_db(db).default_model("embedding").id == "g/emb"

    # 配了 Key 后 prod 立即可用(内置合成与 env 无关)
    with get_session_factory()() as db:
        db.add(SpaceModelSettings(space_id=space_id, embedding_key_encrypted="enc:v1:k"))
        db.commit()
    with get_session_factory()() as db:
        assert (
            ModelCatalog.load_from_db(db, space_id).default_model("embedding").id
            == BUILTIN_EMBEDDING_MODEL_ID
        )


def test_dev_fallback_keeps_global_embedding_with_warning(
    client, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    space_id = uuid.UUID(_space(client, "devfallback", "dev回落空间"))
    with get_session_factory()() as db:
        gp = _mk_provider(db, name="dev厂")
        _mk_model(db, provider=gp, model_id="g/emb", kind="embedding", dims=1024, is_default=True)
        db.commit()

    monkeypatch.setattr(
        "app.core.model_catalog.get_settings", lambda: SimpleNamespace(env="dev")
    )
    with caplog.at_level(logging.WARNING, logger="app.model_catalog"):
        with get_session_factory()() as db:
            merged = ModelCatalog.load_from_db(db, space_id)
    assert merged.default_model("embedding").id == "g/emb"  # dev 回落全局
    assert any("dev 专属路径" in r.getMessage() for r in caplog.records)


def test_rerank_enabled_loaded_from_settings(client) -> None:
    space_on = uuid.UUID(_space(client, "rankon", "重排开"))
    space_off = uuid.UUID(_space(client, "rankoff", "重排关"))
    with get_session_factory()() as db:
        gp = _mk_provider(db, name="重排厂")
        _mk_model(db, provider=gp, model_id="s/rerank", space_id=space_on, kind="rerank")
        db.add(SpaceModelSettings(space_id=space_on, rerank_enabled=True))
        db.add(SpaceModelSettings(space_id=space_off, rerank_enabled=False))
        db.commit()

    with get_session_factory()() as db:
        assert ModelCatalog.load_from_db(db, space_on).rerank_enabled is True
        assert ModelCatalog.load_from_db(db, space_off).rerank_enabled is False
        # 无设置行 = 未启用
        assert ModelCatalog.load_from_db(db, uuid.uuid4()).rerank_enabled is False
        on = ModelCatalog.load_from_db(db, space_on)
        assert on.get_model("s/rerank", "rerank").provider_key == "重排厂"  # 空间 rerank 模型可选
