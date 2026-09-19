"""分块器(ADR-5):recursive 切分 + 标题画像 + 表格感知 + validator。

设计约定:
- token 估算是确定性近似:CJK 字符按 1 token、其它按空白分词(不引入分词器依赖,
  换精确 tokenizer 只动 estimate_tokens 一处);
- 标题层级 → 面包屑上下文头(检索与引用展示都受益),头部 token 计入块预算;
- 表格块整体保留不硬切;超预算的 markdown 表格按数据行分组,表头进每一块;
- validator 兜底:空块/超限直接抛错(内部不变量),过碎的小块自动并入相邻块。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from types import SimpleNamespace

DEFAULT_MAX_TOKENS = 512
_OVERLAP_RATIO = 0.15
_MIN_CHUNK_TOKENS = 32  # 低于该值视为碎块,向后合并
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
# 递归切分的分隔符阶梯(按语义强度从强到弱):段落 → 行 → 句 → 子句。
# 优先在靠前的分隔符断开,尽量不把句子拦腰截断;全都没有时才字符级硬切。
_SEPARATORS = ("\n\n", "\n", "。", "；", ";", "！", "!", "？", "?", ",", ",", "、", " ")

# BlockLike:parser gRPC Block 的结构子集(type/text/markdown/page/level),便于测试构造


@dataclass
class ChunkDraft:
    content: str
    breadcrumb: list[str]
    page: int | None
    kind: str  # text | table
    tokens: int
    # 正文(不含面包屑头)。单独保留是为了合并碎块时能重算内容,避免把头重复拼进去。
    body: str = ""

    def __post_init__(self) -> None:
        if not self.body:
            self.body = self.rendered_body()

    def rendered_body(self) -> str:
        """去掉面包屑头后的正文(头 = 第一行,且与 breadcrumb 渲染结果一致时才算)。"""
        header = " > ".join(self.breadcrumb)
        if header and self.content.startswith(f"{header}\n"):
            return self.content[len(header) + 1 :]
        return self.content

    def render(self) -> str:
        header = " > ".join(self.breadcrumb)
        return f"{header}\n{self.body}" if header else self.body


def estimate_tokens(text: str) -> int:
    cjk = len(_CJK_RE.findall(text))
    words = len(_CJK_RE.sub(" ", text).split())
    return cjk + words


def chunk_blocks(
    blocks: Iterable[object],
    max_tokens: int = DEFAULT_MAX_TOKENS,
    overlap_tokens: int | None = None,
) -> list[ChunkDraft]:
    """按小节装块:**装得下就一块,装不下才递归切**。

    一个小节的正文被视为一个整体预算:小节总量不超过上限时产出**一个**块。
    此前是逐段落调用切分函数,一个小节里有几段就产出几块,即使总量远小于上限 ——
    这正是"块太散"的根因(检索时上下文被切碎,召回质量下降)。

    表格仍单独成块(整体保留,表头进每块),并打断正文、保持在原位置。
    """
    if overlap_tokens is None:
        overlap_tokens = int(max_tokens * _OVERLAP_RATIO)
    drafts: list[ChunkDraft] = []
    for breadcrumb, units in _group_sections(blocks):
        header = " > ".join(breadcrumb)
        header_cost = estimate_tokens(header) + 1 if header else 0
        budget = max_tokens - header_cost
        run: list[tuple[str, int | None]] = []
        for kind, text, page in units:
            if kind == "table":
                drafts.extend(_pack_run(run, breadcrumb, header, budget, overlap_tokens))
                run = []
                drafts.extend(
                    _chunk_table(text, breadcrumb, page, max_tokens, header_cost)
                )
            else:
                run.append((text, page))
        drafts.extend(_pack_run(run, breadcrumb, header, budget, overlap_tokens))
    drafts = _merge_tiny(drafts, max_tokens)
    _validate(drafts, max_tokens)
    return drafts


DEFAULT_CHILD_MAX_TOKENS = 256  # 父子分块:子块(检索窗口)上限;父块仍 ≤ max_tokens


def parent_child_chunks(
    blocks: Iterable[object],
    max_tokens: int = DEFAULT_MAX_TOKENS,
    child_max_tokens: int = DEFAULT_CHILD_MAX_TOKENS,
) -> list[tuple[ChunkDraft, list[ChunkDraft]]]:
    """父子分块(ADR-5,OPT-4):父块 = 上下文/引用单元,子块 = 检索窗口。

    父块由 chunk_blocks 产出(行为与 M2 完全一致);每个父块至少派生一个子块:
    - 短父块(≤ child_max_tokens)与表格父块整块作为子块 —— 表格行本身已是
      细粒度匹配单元,再拆 markdown 行会破坏表结构;
    - 长父块按 recursive 阶梯以 child_max_tokens 重切,相邻子块保留尾部重叠。
    子块正文是父块正文的**连续片段**(重叠部分与后续片段在原文中相接),因此
    子块 tokens ≤ 父块 tokens,不会突破 max_tokens 不变量。入库时只有子块进
    向量/全文索引,检索命中子块后回取父块组装引用与生成上下文。
    """
    result: list[tuple[ChunkDraft, list[ChunkDraft]]] = []
    for parent in chunk_blocks(blocks, max_tokens):
        result.append((parent, _child_drafts(parent, child_max_tokens)))
    return result


def _child_drafts(parent: ChunkDraft, child_max_tokens: int) -> list[ChunkDraft]:
    header = " > ".join(parent.breadcrumb)
    if parent.kind == "table" or parent.tokens <= child_max_tokens:
        return [
            ChunkDraft(
                content=parent.content,
                breadcrumb=list(parent.breadcrumb),
                page=parent.page,
                kind=parent.kind,
                tokens=parent.tokens,
                body=parent.body,
            )
        ]
    header_cost = estimate_tokens(header) + 1 if header else 0
    # 面包屑头比子块上限还长时保底 32 保证有进展;此时子块最坏等于父块本身,
    # 仍是连续片段,不突破 max_tokens
    budget = max(child_max_tokens - header_cost, 32)
    atoms, natural = _split_recursive(parent.body, budget)
    overlap = int(child_max_tokens * _OVERLAP_RATIO)
    return _pack_atoms(
        atoms, parent.breadcrumb, parent.page, header, budget, overlap if natural else 0
    )


def blocks_from_text(text: str, fmt: str = "md") -> list[SimpleNamespace]:
    """预览入口的轻量 md/txt 分块前置解析(流水线正式入口以 parser 服务为准)。

    返回与 parser Block 同构的属性对象,供 _group_sections 统一消费。
    """
    blocks: list[SimpleNamespace] = []
    if fmt == "txt":
        return [
            SimpleNamespace(type="paragraph", text=seg.strip(), markdown="", page=0, level=0)
            for seg in text.split("\n\n")
            if seg.strip()
        ]
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            blocks.append(
                SimpleNamespace(
                    type="paragraph", text="\n".join(paragraph), markdown="", page=0, level=0
                )
            )
            paragraph.clear()

    for raw in text.splitlines():
        line = raw.strip()
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            flush()
            blocks.append(
                SimpleNamespace(
                    type="title",
                    text=heading.group(2),
                    markdown="",
                    page=0,
                    level=len(heading.group(1)),
                )
            )
        elif not line:
            flush()
        else:
            paragraph.append(line)
    flush()
    return blocks


# ---------------------------- 内部 ----------------------------


def _group_sections(
    blocks: Iterable[object],
) -> Iterator[tuple[list[str], list[tuple[str, str, int | None]]]]:
    breadcrumb: list[str] = []
    units: list[tuple[str, str, int | None]] = []
    for block in blocks:
        btype = getattr(block, "type", "paragraph")
        text = (getattr(block, "markdown", "") or "").strip() or getattr(block, "text", "").strip()
        page = getattr(block, "page", 0) or None
        if not text:
            continue
        if btype == "title":
            if units:
                yield list(breadcrumb), units
                units = []
            level = int(getattr(block, "level", 0) or 1)
            del breadcrumb[max(level - 1, 0):]
            breadcrumb.append(text)
        elif btype == "table":
            units.append(("table", text, page))
        else:
            units.append(("text", text, page))
    if units:
        yield list(breadcrumb), units


def _pack_run(
    run: list[tuple[str, int | None]],
    breadcrumb: list[str],
    header: str,
    budget: int,
    overlap_tokens: int,
) -> list[ChunkDraft]:
    """把一个小节的连续正文段落装成块:整体装得下就一块,否则递归切。

    "按小节决策"而不是"按段落决策"是这里的关键:段落只是排版单位,不是语义单位,
    按段落切会把一个小节的上下文打散成多块(见 chunk_blocks 的说明)。
    """
    if not run:
        return []
    page = run[0][1]
    text = "\n".join(part for part, _page in run)
    if estimate_tokens(text) <= budget:
        return [_emit([text], breadcrumb, page, header, "text")]

    atoms, natural = _split_recursive(text, budget)
    # 无自然边界的硬切不做重叠:重叠只会把无意义的长串(如 base64)重复一遍
    effective_overlap = overlap_tokens if natural else 0
    return _pack_atoms(atoms, breadcrumb, page, header, budget, effective_overlap)


def _split_recursive(text: str, budget: int) -> tuple[list[str], bool]:
    """递归切分:段落 → 行 → 句 → 子句 → 字符,返回 (片段列表, 是否用了自然边界)。

    每个片段自身保证 <= budget;优先在语义边界断开,避免把句子拦腰截断。
    只有当所有自然分隔符都不存在时才退化为按字符硬切(此时 natural=False)。
    """
    if estimate_tokens(text) <= budget:
        return [text], True
    for separator in _SEPARATORS:
        pieces = _split_keep(text, separator)
        if len(pieces) > 1:
            atoms: list[str] = []
            for piece in pieces:
                sub, _natural = _split_recursive(piece, budget)
                atoms.extend(sub)
            return atoms, True
    return _hard_split_text(text, budget), False


def _split_keep(text: str, separator: str) -> list[str]:
    """按分隔符切分,并把分隔符保留在**前一片末尾**(切分不丢字符)。"""
    parts = text.split(separator)
    pieces: list[str] = []
    for index, part in enumerate(parts):
        if index < len(parts) - 1:
            pieces.append(part + separator)
        elif part:
            pieces.append(part)
    return [piece for piece in pieces if piece]


def _hard_split_text(text: str, budget: int) -> list[str]:
    """无自然边界时的字符级硬切;每片 <= budget(单字符即超限时也保证有进展)。"""
    pieces: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for char in text:
        cost = estimate_tokens(char)
        if current and current_tokens + cost > budget:
            pieces.append("".join(current))
            current, current_tokens = [], 0
        current.append(char)
        current_tokens += cost
    if current:
        pieces.append("".join(current))
    return pieces


def _pack_atoms(
    atoms: list[str],
    breadcrumb: list[str],
    page: int | None,
    header: str,
    budget: int,
    overlap_tokens: int,
) -> list[ChunkDraft]:
    """贪心装块 + 相邻块尾部重叠:重叠片段计入下一块预算,保证不超上限。"""
    drafts: list[ChunkDraft] = []
    window: list[str] = []
    window_tokens = 0
    fresh = 0  # window 里自上次产出后**新**加入的片段数(重叠带过来的不算)
    for atom in atoms:
        cost = estimate_tokens(atom)
        if fresh and window_tokens + cost > budget:
            drafts.append(_emit(window, breadcrumb, page, header, "text"))
            window, window_tokens = _overlap_tail(window, overlap_tokens)
            fresh = 0
        window.append(atom)
        window_tokens += cost
        fresh += 1
    if fresh:  # 只有重叠、没有新内容的尾巴不再单独成块
        drafts.append(_emit(window, breadcrumb, page, header, "text"))
    return drafts


def _overlap_tail(window: list[str], overlap_tokens: int) -> tuple[list[str], int]:
    """取窗口尾部不超过 overlap_tokens 的片段,作为下一块开头的上下文重叠。"""
    if overlap_tokens <= 0:
        return [], 0
    tail: list[str] = []
    tail_tokens = 0
    for atom in reversed(window):
        cost = estimate_tokens(atom)
        if tail_tokens + cost > overlap_tokens:
            break
        tail.insert(0, atom)
        tail_tokens += cost
    return tail, tail_tokens


def _hard_split(
    sentence: str,
    breadcrumb: list[str],
    page: int | None,
    budget: int,
    header: str,
) -> Iterator[ChunkDraft]:
    piece: list[str] = []
    piece_tokens = 0
    # 按字符累积;中文 1 字 1 token,非中文按词,逐段试探保证不超预算
    for char in sentence:
        if piece_tokens + estimate_tokens(char) > budget and piece:
            yield _emit(piece, breadcrumb, page, header, "text")
            piece, piece_tokens = [], 0
        piece.append(char)
        piece_tokens += estimate_tokens(char)
    if piece:
        yield _emit(piece, breadcrumb, page, header, "text")


def _chunk_table(
    markdown: str,
    breadcrumb: list[str],
    page: int | None,
    max_tokens: int,
    header_cost: int,
) -> list[ChunkDraft]:
    header = " > ".join(breadcrumb)
    if estimate_tokens(markdown) + header_cost <= max_tokens:
        return [_emit([markdown], breadcrumb, page, header, "table")]
    lines = markdown.splitlines()
    if len(lines) < 2:  # 无表体可分,退化为整块(validator 会拦)
        return [_emit([markdown], breadcrumb, page, header, "table")]
    head, body = lines[0], lines[1:]
    budget = max_tokens - header_cost - estimate_tokens(head) - 1
    drafts: list[ChunkDraft] = []
    group: list[str] = []
    group_tokens = 0
    for line in body:
        cost = estimate_tokens(line)
        if cost > budget:  # 单行超预算:硬切该行,保住 max_tokens 不变量
            if group:
                drafts.append(_emit([head, *group], breadcrumb, page, header, "table"))
                group, group_tokens = [], 0
            drafts.extend(_hard_split(line, breadcrumb, page, budget, header))
            continue
        if group_tokens + cost > budget and group:
            drafts.append(_emit([head, *group], breadcrumb, page, header, "table"))
            group, group_tokens = [], 0
        group.append(line)
        group_tokens += cost
    if group:
        drafts.append(_emit([head, *group], breadcrumb, page, header, "table"))
    return drafts


def _emit(
    parts: list[str],
    breadcrumb: list[str],
    page: int | None,
    header: str,
    kind: str,
) -> ChunkDraft:
    body = "\n".join(part for part in parts if part.strip())
    content = f"{header}\n{body}" if header else body
    return ChunkDraft(
        content=content,
        breadcrumb=list(breadcrumb),
        page=page,
        kind=kind,
        tokens=estimate_tokens(content),
        body=body,
    )


def _merge_tiny(drafts: list[ChunkDraft], max_tokens: int) -> list[ChunkDraft]:
    merged: list[ChunkDraft] = []
    for draft in drafts:
        prev = merged[-1] if merged else None
        if (
            prev is not None
            and prev.kind == draft.kind == "text"
            and prev.breadcrumb == draft.breadcrumb
            and prev.tokens < _MIN_CHUNK_TOKENS
            and prev.tokens + draft.tokens <= max_tokens
        ):
            # 合并正文后重算内容:直接拼 content 会把面包屑头重复 N 次
            prev.body = f"{prev.body}\n{draft.body}"
            prev.content = prev.render()
            prev.tokens = estimate_tokens(prev.content)
            continue
        merged.append(draft)
    return merged


def _validate(drafts: list[ChunkDraft], max_tokens: int) -> None:
    for draft in drafts:
        if not draft.content.strip():
            raise ValueError("chunker 产生空块(内部不变量被破坏)")
        if draft.tokens > max_tokens:
            raise ValueError(
                f"chunk 超限 {draft.tokens}>{max_tokens}(内部不变量被破坏)"
            )
