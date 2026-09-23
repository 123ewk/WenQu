"""问答流水线装配测试(OPT-5):fail-closed 配置校验 + 阶段顺序;OPT-45 P2 记忆召回注入。"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.application.service.qa_pipeline import (
    DEFAULT_PIPELINE,
    GenerationState,
    RecallMemoryStage,
    _build_messages,
    build_stages,
)


def _noop(*_args: Any, **_kwargs: Any) -> Any:
    return None


def test_default_pipeline_builds_all_stages_in_order() -> None:
    stages = build_stages(DEFAULT_PIPELINE, search=_noop, persist=_noop, chat=object())
    assert [stage.name for stage in stages] == list(DEFAULT_PIPELINE)


def test_default_pipeline_includes_recall_memory_before_compose() -> None:
    """记忆召回阶段必须在 compose 之前(否则召回结果进不了提示词)。"""
    order = list(DEFAULT_PIPELINE)
    assert "recall_memory" in order
    assert order.index("recall_memory") < order.index("compose")


def test_unknown_stage_fails_closed() -> None:
    with pytest.raises(ValueError, match="未知的 QA 流水线阶段"):
        build_stages(["retrieve", "magic"], search=_noop, persist=_noop, chat=object())


def test_empty_pipeline_rejected() -> None:
    with pytest.raises(ValueError, match="不能为空"):
        build_stages([], search=_noop, persist=_noop, chat=object())


def test_pipeline_can_be_reordered() -> None:
    """链序可配置:同名阶段按给定顺序出现(默认序之外的重组由运维自担)。"""
    reordered = list(reversed(DEFAULT_PIPELINE))
    stages = build_stages(reordered, search=_noop, persist=_noop, chat=object())
    assert [stage.name for stage in stages] == reordered


# ---------------------------- OPT-45 P2:记忆召回注入 ----------------------------


def _make_state() -> GenerationState:
    from types import SimpleNamespace

    return GenerationState(
        ctx=SimpleNamespace(question="q", space_id=uuid.uuid4(), user_id=uuid.uuid4()),  # type: ignore[arg-type]
        handle=None,  # type: ignore[arg-type]
        stream_id=uuid.uuid4(),
        log=None,  # type: ignore[arg-type]
    )


def test_recall_stage_populates_memories_from_collaborator() -> None:
    state = _make_state()
    stage = RecallMemoryStage(lambda _ctx: ["报销上限两千元"])
    stage.run(state)
    assert state.memories == ["报销上限两千元"]


def test_recall_stage_swallows_errors_and_degrades_to_empty() -> None:
    """召回是增益不是必需:抛异常只置空,绝不冒泡中断问答。"""

    def boom(_ctx: Any) -> list[str]:
        raise RuntimeError("embedding down")

    state = _make_state()
    RecallMemoryStage(boom).run(state)
    assert state.memories == []


def test_compose_injects_memory_block_and_empty_is_identical() -> None:
    citations = [{"index": 1, "filename": "手册.md", "excerpt": "资料内容"}]
    with_mem = _build_messages([], "问题", citations, ["A 结论", "B 决策"])
    without_mem = _build_messages([], "问题", citations, [])
    none_mem = _build_messages([], "问题", citations)

    system = with_mem[0]["content"]
    assert "已确认相关记忆" in system
    assert "- A 结论" in system and "- B 决策" in system
    # 空召回(None / [])与"无记忆"逐字节一致:回退不改变提示词形态
    assert without_mem[0]["content"] == none_mem[0]["content"]
    assert "已确认相关记忆" not in without_mem[0]["content"]

