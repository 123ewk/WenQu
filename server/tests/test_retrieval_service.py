"""RRF 融合与检索服务单元测试(基准 03):排名融合语义、权重、权限、单路命中。"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.application.service.retrieval import (
    DEFAULT_FULLTEXT_WEIGHT,
    DEFAULT_MIN_VECTOR_SCORE,
    DEFAULT_RRF_K,
    DEFAULT_VECTOR_WEIGHT,
    RetrievalService,
    rrf_fuse,
)
from app.core.errors import AppError
from app.domain.enums import Role
from app.domain.models import Chunk, Document, KnowledgeBase, Membership, Space
from tests.fakes import (
    FakeKnowledgeBaseRepository,
    FakeSpaceRepository,
    FakeUserRepository,
    make_user,
)


def test_rrf_fuse_ranks_top_of_both_paths_first() -> None:
    # 两路都把 c1 排第一、c2 排第二 → 融合后仍 c1 > c2
    fused = rrf_fuse([("c1", 0.9), ("c2", 0.8)], [("c1", 5.0), ("c3", 3.0)])
    ids = [item[0] for item in fused]
    assert ids[0] == "c1"
    assert set(ids) == {"c1", "c2", "c3"}
    # 只有一路命中的 c2/c3 分数低于两路都命中的 c1
    scores = dict((i, s) for i, s, _v, _f in fused)
    assert scores["c1"] > scores["c2"] and scores["c1"] > scores["c3"]


def test_rrf_fuse_weights_shift_ranking() -> None:
    # 全文权重抬高后,仅全文命中的 c2 可能反超仅向量命中的 c1
    vector = [("c1", 0.9)]
    fulltext = [("c2", 9.0)]
    default_fused = rrf_fuse(vector, fulltext)
    assert default_fused[0][0] == "c1"  # 默认向量权重 0.7 > 0.3
    flipped = rrf_fuse(vector, fulltext, vector_weight=0.1, fulltext_weight=0.9)
    assert flipped[0][0] == "c2"


def test_rrf_fuse_scores_use_documented_formula() -> None:
    fused = rrf_fuse([("c1", 1.0)], [])
    score = fused[0][1]
    assert score == pytest.approx(DEFAULT_VECTOR_WEIGHT / (DEFAULT_RRF_K + 1))
    assert DEFAULT_VECTOR_WEIGHT == 0.7 and DEFAULT_FULLTEXT_WEIGHT == 0.3


def test_rrf_fuse_tracks_ranks_per_path() -> None:
    fused = rrf_fuse([("c1", 0.9)], [("c1", 4.0)])
    _cid, _score, v_rank, f_rank = fused[0]
    assert (v_rank, f_rank) == (1, 1)


class FakeRetrievalRepo:
    def __init__(self, vector_hits, fulltext_hits) -> None:
        self.vector_hits = vector_hits
        self.fulltext_hits = fulltext_hits
        self.calls: list[tuple] = []
        self.received_tokens: str | None = None

    def vector_search(self, space_id, embedding, kb_ids, limit, min_similarity=0.0):
        self.calls.append(("vector", space_id, limit))
        self.received_min_similarity = min_similarity
        return [hit for hit in self.vector_hits if hit[2] >= min_similarity]

    def fulltext_search(self, space_id, jieba_tokens, kb_ids, limit):
        self.calls.append(("fulltext", space_id, limit))
        self.received_tokens = jieba_tokens
        return self.fulltext_hits


class FakeEmbedder:
    def __init__(self) -> None:
        self.inputs: list[list[str]] = []
        self.model_ids: list[str | None] = []

    def embed(self, texts, model_id=None):
        self.inputs.append(texts)
        self.model_ids.append(model_id)
        return [[0.1, 0.2, 0.3]]


def _chunk_pair(space_id: uuid.UUID, kb_id: uuid.UUID, content: str):
    document = Document(
        id=uuid.uuid4(), kb_id=kb_id, space_id=space_id, filename="f.txt", format="txt",
        source="s", status="completed",
    )
    chunk = Chunk(
        id=uuid.uuid4(), document_id=document.id, space_id=space_id, seq=0, content=content
    )
    chunk.meta = {"breadcrumb": ["第一章"], "page": 1}
    return chunk, document


def build_env(
    role: Role = Role.VIEWER,
    vector_specs: list[str] | None = None,
    fulltext_specs: list[str] | None = None,
):
    """vector_specs/fulltext_specs:按内容文本构造命中(命中行的 space 与成员空间一致)。"""
    users = FakeUserRepository()
    spaces = FakeSpaceRepository(users_ref=users.users)
    user = users.create(make_user("u"))
    space = spaces.create(Space(name="s", description=""))
    spaces.add_member(Membership(space_id=space.id, user_id=user.id, role=role))
    kb_id = uuid.uuid4()

    def hits(specs: list[str] | None, default: str | None):
        texts = specs if specs is not None else ([default] if default else [])
        return [
            (*_chunk_pair(space.id, kb_id, text), 1.0 - i * 0.1)
            for i, text in enumerate(texts)
        ]

    repo = FakeRetrievalRepo(hits(vector_specs, "向量命中内容"), hits(fulltext_specs, None))
    embedder = FakeEmbedder()
    kbs = FakeKnowledgeBaseRepository()
    kb = KnowledgeBase(
        space_id=space.id, name="默认库", embedding_model="test-embedding-model"
    )
    kbs.create(kb)
    service = RetrievalService(repo, kbs, None, spaces, embedder)  # type: ignore[arg-type]
    return SimpleNamespace(
        service=service, repo=repo, embedder=embedder, user=user, space=space,
        kbs=kbs, kb=kb, kb_id=kb.id,
    )


def test_search_embeds_query_with_kb_embedding_model() -> None:
    """/ask 传来的对话模型 id 不参与检索:查询向量取 KB 的 embedding_model(§11.2-1)。"""
    env = build_env()
    env.service.search(
        env.user.id, env.space.id, "检索测试",
        kb_ids=[env.kb.id], model_id="deepseek/deepseek-chat",
    )
    assert env.embedder.model_ids == ["test-embedding-model"]


def test_search_without_kbs_falls_back_to_catalog_default() -> None:
    """空间还没有 KB → None(目录默认 embedding 模型),保持未配密钥 503 语义。"""
    env = build_env()
    env.kbs.delete(env.kb)
    env.service.search(env.user.id, env.space.id, "检索测试")
    assert env.embedder.model_ids == [None]


def test_search_returns_fused_results_with_metadata() -> None:
    env = build_env()
    results = env.service.search(env.user.id, env.space.id, "检索测试")
    assert len(results) == 1
    hit = results[0]
    assert hit.content == "向量命中内容"
    assert hit.filename == "f.txt"
    assert hit.vector_rank == 1 and hit.fulltext_rank is None
    assert hit.meta["breadcrumb"] == ["第一章"]
    assert env.embedder.inputs == [["检索测试"]]  # 查询向量化只发一次
    # 候选数 = 生效 top_k * 3(未传 top_k 时取空间默认 6),保证两路候选足够融合
    assert ("vector", env.space.id, 18) in env.repo.calls
    assert ("fulltext", env.space.id, 18) in env.repo.calls


def test_search_passes_jieba_tokens_to_fulltext_path() -> None:
    env = build_env()
    env.service.search(env.user.id, env.space.id, "混合检索算法")
    assert env.repo.received_tokens
    assert "混合" in env.repo.received_tokens  # 中文被切分,而非整串


def test_search_requires_membership() -> None:
    env = build_env()
    with pytest.raises(AppError) as exc_info:
        env.service.search(uuid.uuid4(), env.space.id, "查询")
    assert exc_info.value.code_str == "SPACE_NOT_FOUND"


def test_search_rejects_empty_query() -> None:
    env = build_env()
    with pytest.raises(AppError) as exc_info:
        env.service.search(env.user.id, env.space.id, "   ")
    assert exc_info.value.code_str == "VALIDATION_ERROR"


def test_search_fulltext_only_hit_included() -> None:
    env = build_env(vector_specs=[], fulltext_specs=["仅全文命中内容"])
    results = env.service.search(env.user.id, env.space.id, "关键词")
    assert len(results) == 1
    assert results[0].content == "仅全文命中内容"
    assert results[0].fulltext_rank == 1 and results[0].vector_rank is None


def test_cross_space_search_rejected_in_m2() -> None:
    env = build_env()
    with pytest.raises(AppError) as exc_info:
        env.service.search(env.user.id, env.space.id, "查询", space_ids=[uuid.uuid4()])
    assert exc_info.value.code_str == "VALIDATION_ERROR"


def test_vector_threshold_filters_irrelevant_hits() -> None:
    """低于阈值的向量候选被丢弃:避免"知识库无相关内容"时仍拿噪声块作答。"""
    env = build_env(vector_specs=["高相关", "低相关"])
    # build_env 给每个命中依次 1.0, 0.9 的相似度;把阈值抬到 0.95 后只剩第一个
    env.service._min_vector_score = 0.95  # noqa: SLF001 — 单测直接调阈值
    results = env.service.search(env.user.id, env.space.id, "查询")
    assert [r.content for r in results] == ["高相关"]


def test_default_threshold_is_conservative_and_documented() -> None:
    env = build_env()
    assert DEFAULT_MIN_VECTOR_SCORE == 0.3
    env.service.search(env.user.id, env.space.id, "查询")
    assert env.repo.received_min_similarity == DEFAULT_MIN_VECTOR_SCORE


def test_no_hits_returns_empty_for_no_result_branch() -> None:
    env = build_env(vector_specs=[], fulltext_specs=[])
    assert env.service.search(env.user.id, env.space.id, "完全不相关") == []


def test_space_retrieval_params_drive_the_search() -> None:
    """空间级参数必须真正生效,否则存了也只是装饰(缺口 #5)。

    用可观察行为验证:把 min_score 提到 0.95 后,低相似候选被过滤;把 default_top_k
    设为 1 后,即便不传 top_k 也只返回一条(空间默认值被采用)。
    """
    env = build_env(vector_specs=["高相关", "低相关"])
    # build_env 的相似度依次 1.0、0.9;阈值 0.95 应只留下第一条
    env.space.retrieval_min_score = 0.95
    env.space.retrieval_default_top_k = 1
    env.space.retrieval_rrf_k = 30
    env.space.retrieval_vector_weight = 0.5
    env.space.retrieval_fulltext_weight = 0.5

    results = env.service.search(env.user.id, env.space.id, "查询")

    assert [r.content for r in results] == ["高相关"]  # 空间默认 top_k=1 与阈值同时生效
    assert env.repo.received_min_similarity == 0.95

    # 显式传 top_k 覆盖空间默认值(放回默认阈值,避免被 0.95 一并过滤掉第二条)
    env.space.retrieval_min_score = 0.3
    assert len(env.service.search(env.user.id, env.space.id, "查询", top_k=2)) == 2
    assert len(env.service.search(env.user.id, env.space.id, "查询")) == 1  # 空间默认仍是 1


def test_missing_space_falls_back_to_service_defaults() -> None:
    """空间取不到时回退到服务级默认,不抛异常(检索不应因配置缺失而 500)。"""
    env = build_env(vector_specs=["命中"])
    env.service._spaces = _EmptySpaces()  # noqa: SLF001 — 模拟空间已删
    results = env.service.search(env.user.id, env.space.id, "查询")
    assert results == [] or results[0].content == "命中"


class _EmptySpaces:
    def get(self, space_id):
        return None

    def get_membership(self, space_id, user_id):
        from app.domain.models import Membership

        return Membership(space_id=space_id, user_id=user_id, role=Role.VIEWER)


def test_request_level_overrides_do_not_touch_space_config() -> None:
    """检索测试的请求级调参:只影响本次调用,不写回空间配置(原型 Tab B 的语义)。"""
    env = build_env(vector_specs=["高相关", "低相关"])
    env.space.retrieval_min_score = 0.3  # 空间默认宽松
    env.space.retrieval_default_top_k = 2

    strict = env.service.search(
        env.user.id, env.space.id, "查询", overrides={"min_score": 0.95, "top_k": 1}
    )

    assert [r.content for r in strict] == ["高相关"]  # 请求级阈值生效
    # 空间配置未被修改
    assert env.space.retrieval_min_score == 0.3
    assert env.space.retrieval_default_top_k == 2
    # 不带覆盖时仍用空间默认(两条都返回)
    assert len(env.service.search(env.user.id, env.space.id, "查询")) == 2


def test_request_level_rrf_weights_change_fusion() -> None:
    """请求级权重参与融合:仅全文命中的候选在权重倾斜后反超。"""
    env = build_env(vector_specs=["向量命中"], fulltext_specs=["全文命中"])
    default = env.service.search(env.user.id, env.space.id, "查询")
    assert default[0].content == "向量命中"  # 默认向量权重高

    tilted = env.service.search(
        env.user.id,
        env.space.id,
        "查询",
        overrides={"vector_weight": 0.1, "fulltext_weight": 0.9},
    )
    assert tilted[0].content == "全文命中"
