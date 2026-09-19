"""问答流水线装配测试(OPT-5):fail-closed 配置校验 + 阶段顺序。"""

from __future__ import annotations

from typing import Any

import pytest

from app.application.service.qa_pipeline import DEFAULT_PIPELINE, build_stages


def _noop(*_args: Any, **_kwargs: Any) -> Any:
    return None


def test_default_pipeline_builds_all_stages_in_order() -> None:
    stages = build_stages(DEFAULT_PIPELINE, search=_noop, persist=_noop, chat=object())
    assert [stage.name for stage in stages] == list(DEFAULT_PIPELINE)


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
