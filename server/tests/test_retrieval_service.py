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
from app.domain.models import Chunk, Document, Membership, Space
from tests.fakes import FakeSpaceRepository, FakeUserRepository, make_user


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

    def embed(self, texts, model_id=None):
        self.inputs.append(texts)
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
    service = RetrievalService(repo, None, None, spaces, embedder)  # type: ignore[arg-type]
    return SimpleNamespace(
        service=service, repo=repo, embedder=embedder, user=user, space=space
    )


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
    # 候选数 = top_k * 3,保证两路各自候选足够融合
    assert ("vector", env.space.id, 24) in env.repo.calls
    assert ("fulltext", env.space.id, 24) in env.repo.calls


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
