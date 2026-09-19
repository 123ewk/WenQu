"""分块器单元测试:重叠、面包屑、表格感知、不变量(基准 03)。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.application.chunking import (
    DEFAULT_CHILD_MAX_TOKENS,
    DEFAULT_MAX_TOKENS,
    ChunkDraft,
    blocks_from_text,
    chunk_blocks,
    estimate_tokens,
    parent_child_chunks,
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


# ---------------------------- 修复回归:段落合并与面包屑不重复 ----------------------------


def test_merge_tiny_does_not_repeat_breadcrumb_header() -> None:
    """碎块合并后,面包屑头只能出现一次。

    _merge_tiny 用字符串拼接两个块的内容,而每个块的 content 都已带面包屑头
    (如 "标题路径\n正文"),直接拼接会把头重复 N 次污染正文。
    """
    from app.application.chunking import ChunkDraft, _merge_tiny

    drafts = [
        ChunkDraft(content="A > B\n第一小句。", breadcrumb=["A", "B"], page=1,
                   kind="text", tokens=5),
        ChunkDraft(content="A > B\n第二小句。", breadcrumb=["A", "B"], page=1,
                   kind="text", tokens=5),
    ]
    merged = _merge_tiny(drafts, max_tokens=512)
    assert len(merged) == 1, [d.content for d in merged]
    content = merged[0].content
    assert content.count("A > B") == 1, content
    assert "第一小句。" in content and "第二小句。" in content, content


def test_merge_tiny_keeps_header_without_breadcrumb() -> None:
    """无面包屑时合并不得凭空造出头,内容原样相接。"""
    from app.application.chunking import ChunkDraft, _merge_tiny

    drafts = [
        ChunkDraft(content="第一小句。", breadcrumb=[], page=None, kind="text", tokens=5),
        ChunkDraft(content="第二小句。", breadcrumb=[], page=None, kind="text", tokens=5),
    ]
    merged = _merge_tiny(drafts, max_tokens=512)
    assert len(merged) == 1
    assert merged[0].content == "第一小句。\n第二小句。", merged[0].content


# ---------------------------- 小节级聚合:不超上限就整段一块 ----------------------------


def test_section_under_budget_becomes_single_chunk() -> None:
    """同一小节的多个段落,总量未超上限时必须合成**一个**块(不再按段落切散)。"""
    blocks = [
        _block(type="title", text="项目经验", level=1),
        _block(text="第一段标记。" + _sentences(3, size=5)),
        _block(text="第二段标记。" + _sentences(3, size=5)),
        _block(text="第三段标记。" + _sentences(3, size=5)),
    ]
    drafts = chunk_blocks(blocks)  # 默认 512,三段合计远未超限
    assert len(drafts) == 1, [d.body[:26] for d in drafts]
    assert drafts[0].breadcrumb == ["项目经验"]
    body = drafts[0].rendered_body()
    for marker in ("第一段标记", "第二段标记", "第三段标记"):
        assert marker in body, body
    order = [body.index(m) for m in ("第一段标记", "第二段标记", "第三段标记")]
    assert order == sorted(order), body


def test_section_over_budget_splits_and_keeps_all_content_in_order() -> None:
    """超上限时切开:每块 <= 上限,小节内容不丢、顺序不乱。"""
    paragraphs = [f"第{i}段标记。" + _sentences(3, size=5) for i in range(6)]
    blocks = [_block(type="title", text="长小节", level=1)]
    blocks += [_block(text=p) for p in paragraphs]

    drafts = chunk_blocks(blocks, max_tokens=128)
    assert len(drafts) > 1, [d.body[:20] for d in drafts]
    assert all(d.tokens <= 128 for d in drafts)

    joined = "\n".join(d.rendered_body() for d in drafts)
    for i in range(6):
        assert f"第{i}段标记" in joined, i
    order = [joined.index(f"第{i}段标记") for i in range(6)]
    assert order == sorted(order), order


def test_recursive_split_prefers_paragraph_boundary() -> None:
    """递归切分优先落在段落边界:每块正文以完整段落开头(不从句中截断)。"""
    import re as _re

    paragraphs = [f"第{i}段标记。" + _sentences(3, size=5) for i in range(6)]
    blocks = [_block(type="title", text="长小节", level=1)]
    blocks += [_block(text=p) for p in paragraphs]

    drafts = chunk_blocks(blocks, max_tokens=128, overlap_tokens=0)
    assert len(drafts) > 1
    for d in drafts:
        assert _re.match(r"^第\d+段标记。", d.rendered_body()), d.body[:30]


def test_table_breaks_run_and_keeps_position_in_section() -> None:
    """小节内的表格仍是独立块,且位置保持在上下正文之间。"""
    table = "\n".join(["| 名称 | 数量 |", "|---|---|", "| 苹果 | 3 |"])
    blocks = [
        _block(type="title", text="小节", level=1),
        _block(text="表格前的说明文字。"),
        _block(type="table", markdown=table),
        _block(text="表格后的说明文字。"),
    ]
    drafts = chunk_blocks(blocks)
    assert [d.kind for d in drafts] == ["text", "table", "text"], [d.kind for d in drafts]
    assert "表格前" in drafts[0].content
    assert "苹果" in drafts[1].content
    assert "表格后" in drafts[2].content


# ---------------------------- 父子分块(OPT-4):父块=上下文,子块=检索窗口 ---------------------------


def test_parent_layer_identical_to_chunk_blocks() -> None:
    """父子分块的父层必须与 chunk_blocks 输出完全一致(M2 行为不变)。"""
    blocks = [
        _block(type="title", text="长小节", level=1),
        *[_block(text=f"第{i}段。" + _sentences(3, size=5)) for i in range(8)],
        _block(type="table", markdown="|名称|数量|\n|---|---|\n|苹果|3|"),
    ]
    parents = [p for p, _cs in parent_child_chunks(blocks)]
    assert parents == chunk_blocks(blocks)


def test_short_parent_yields_single_identical_child() -> None:
    """不超过子块上限的父块:唯一子块与父块同内容(1:1,检索行为等价)。"""
    (parent, children), = parent_child_chunks([_block(text=_sentences(10, size=5))])
    assert len(children) == 1
    child = children[0]
    assert child.content == parent.content
    assert child.kind == parent.kind
    assert child.tokens == parent.tokens


def test_long_parent_splits_children_within_child_budget() -> None:
    """超上限的父块:子块各自 ≤ 子块预算,相邻子块保留尾部重叠,面包屑头进每个子块。"""
    blocks = [
        _block(type="title", text="系统设计", level=1),
        _block(type="title", text="检索模块", level=2),
        _block(text=_sentences(60, size=5)),  # ~1140 token,父块切多块,子块切更多
    ]
    pairs = parent_child_chunks(blocks)
    assert all(len(children) >= 2 for _p, children in pairs)
    for parent, children in pairs:
        assert all(c.tokens <= DEFAULT_CHILD_MAX_TOKENS for c in children), [
            c.tokens for c in children
        ]
        assert all(c.breadcrumb == parent.breadcrumb for c in children)
        assert all(c.content.startswith("系统设计 > 检索模块\n") for c in children)
    # 重叠:第 i 个子块的尾部句子必须出现在第 i+1 个子块里(句子编号唯一)
    for children in [children for _p, children in pairs]:
        for prev, nxt in zip(children, children[1:], strict=False):
            tail_sentence = prev.content.split("。")[-2].strip()
            assert tail_sentence in nxt.content


def test_table_parent_never_split_into_children() -> None:
    """表格父块整块作为子块(不再二次拆 markdown 行,保住表结构)。"""
    rows = ["| 品名 | 描述信息列 |", "|---|---|"]
    rows += [f"| 物品{i} | {'描述' * 20} |" for i in range(8)]  # ~370 token:>子块预算,≤父块上限
    (parent, children), = parent_child_chunks([_block(type="table", markdown="\n".join(rows))])
    assert parent.tokens > DEFAULT_CHILD_MAX_TOKENS  # 前置:确实超了子块预算
    assert len(children) == 1
    assert children[0].content == parent.content
    assert children[0].kind == "table"


def test_children_never_exceed_parent_token_limit() -> None:
    """子块是父块正文的连续片段:tokens 永不超过父块上限(不变量)。"""
    weird = [
        _block(type="title", text="很长标题" * 15, level=1),  # 面包屑头超子块预算的极端情况
        _block(text="字" * 700),
        _block(type="table", markdown="|a|"),
        _block(text=_sentences(30)),
    ]
    for parent, children in parent_child_chunks(weird, max_tokens=256, child_max_tokens=64):
        assert all(c.tokens <= max(DEFAULT_MAX_TOKENS, parent.tokens) for c in children), (
            parent.tokens,
            [c.tokens for c in children],
        )
        assert all(c.content.strip() for c in children)
