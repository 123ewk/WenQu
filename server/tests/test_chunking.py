"""分块器单元测试:重叠、面包屑、表格感知、不变量(基准 03)。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.application.chunking import (
    DEFAULT_MAX_TOKENS,
    ChunkDraft,
    blocks_from_text,
    chunk_blocks,
    estimate_tokens,
)


def _block(**kwargs: object) -> SimpleNamespace:
    fields = {"type": "paragraph", "text": "", "markdown": "", "page": 0, "level": 0}
    fields.update(kwargs)
    return SimpleNamespace(**fields)


def _sentences(count: int, size: int = 20) -> str:
    # 每句 size 个 CJK 字符 + 句号,便于精确控制 token 预算
    return "".join(f"第{i}句{'内容字' * size}。" for i in range(count))


def test_estimate_tokens_is_deterministic() -> None:
    assert estimate_tokens("知识库检索") == 5
    assert estimate_tokens("hello world") == 2
    assert estimate_tokens("混合 mixed 检索") == 5  # 4 CJK + 1 word


def test_long_text_splits_with_overlap() -> None:
    text = _sentences(60, size=5)  # 每句 19 token,恰好能落入 15% 重叠窗口
    drafts = chunk_blocks([_block(text=text)], max_tokens=128)
    assert len(drafts) > 3
    assert all(d.tokens <= 128 for d in drafts)
    # 相邻块共享尾部句子:第 i 块的最后一个句子必须出现在第 i+1 块里(句子编号唯一)
    tail_sentence = drafts[0].content.split("。")[-2].strip()
    assert tail_sentence in drafts[1].content


def test_breadcrumb_context_prefixes_chunks() -> None:
    blocks = [
        _block(type="title", text="系统设计", level=1),
        _block(type="title", text="检索模块", level=2),
        _block(text=_sentences(40)),
    ]
    drafts = chunk_blocks(blocks, max_tokens=128)
    assert all(d.content.startswith("系统设计 > 检索模块\n") for d in drafts)
    assert all(d.breadcrumb == ["系统设计", "检索模块"] for d in drafts)


def test_small_table_kept_intact() -> None:
    table = "\n".join(["| 名称 | 数量 |", "|---|---|", "| 苹果 | 3 |", "| 香蕉 | 5 |"])
    drafts = chunk_blocks([_block(type="table", markdown=table)])
    assert len(drafts) == 1
    assert drafts[0].kind == "table"
    assert "苹果" in drafts[0].content and "香蕉" in drafts[0].content


def test_big_table_splits_with_header_in_every_chunk() -> None:
    header = "| 品名 | 描述信息列 |"
    rows = [header, "|---|---|"]
    rows += [f"| 物品{i} | {'描述' * 20} |" for i in range(30)]
    drafts = chunk_blocks([_block(type="table", markdown="\n".join(rows))], max_tokens=80)
    assert len(drafts) >= 3
    assert all(d.kind == "table" for d in drafts)
    for draft in drafts:  # 表头进每块(ADR-5)
        assert any(line == header for line in draft.content.splitlines())


def test_tiny_adjacent_chunks_merged() -> None:
    blocks = [_block(text="短句一。"), _block(text="短句二。"), _block(text="短句三。")]
    drafts = chunk_blocks(blocks)
    assert len(drafts) == 1
    assert "短句一" in drafts[0].content and "短句三" in drafts[0].content


def test_single_oversized_sentence_hard_split() -> None:
    huge = "超" * 3000  # 单句 3000 token
    drafts = chunk_blocks([_block(text=huge)], max_tokens=64)
    assert all(d.tokens <= 64 for d in drafts)
    assert "".join(d.content for d in drafts).count("超") == 3000


def test_validator_rejects_over_limit_never_happens() -> None:
    # 任意怪异输入都不应抛 ValueError(不变量),只可能产出合法块
    weird = [_block(text="字" * 10), _block(type="table", markdown="|a|"), _block(text="")]
    drafts = chunk_blocks(weird, max_tokens=64)
    assert all(isinstance(d, ChunkDraft) for d in drafts)


def test_blocks_from_text_markdown_structure() -> None:
    blocks = blocks_from_text("# 标题\n\n段落一。\n\n段落二。", "md")
    assert [b.type for b in blocks] == ["title", "paragraph", "paragraph"]
    assert blocks[0].level == 1
    txt = blocks_from_text("A 段\n\nB 段", "txt")
    assert [b.text for b in txt] == ["A 段", "B 段"]
    # 属性对象与 parser Block 同构,能被 chunk_blocks 直接消费
    drafts = chunk_blocks(blocks_from_text("# 标题\n\n" + _sentences(40), "md"))
    assert drafts and all(d.breadcrumb == ["标题"] for d in drafts)


def test_default_parameters_documented() -> None:
    assert DEFAULT_MAX_TOKENS == 512
    drafts = chunk_blocks([_block(text=_sentences(5))])
    assert all(d.tokens <= DEFAULT_MAX_TOKENS for d in drafts)


def test_chunks_respect_breadcrumb_token_budget() -> None:
    long_title = "很长的标题" * 10
    blocks = [
        _block(type="title", text=long_title, level=1),
        _block(text=_sentences(20)),
    ]
    drafts = chunk_blocks(blocks, max_tokens=128)
    assert all(d.tokens <= 128 for d in drafts)
    with pytest.raises(ValueError):
        chunk_blocks([_block(text="x" * 10)], max_tokens=-1)  # 非法参数直接暴露
