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
_SENTENCE_RE = re.compile(r"[^。！？!?\n]+[。！？!?]?")

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
    if overlap_tokens is None:
        overlap_tokens = int(max_tokens * _OVERLAP_RATIO)
    drafts: list[ChunkDraft] = []
    for breadcrumb, units in _group_sections(blocks):
        header = " > ".join(breadcrumb)
        header_cost = estimate_tokens(header) + 1 if header else 0
        budget = max_tokens - header_cost
        for kind, text, page in units:
            if kind == "table":
                drafts.extend(
                    _chunk_table(text, breadcrumb, page, max_tokens, header_cost)
                )
            else:
                drafts.extend(
                    _pack_sentences(
                        text, breadcrumb, page, budget, overlap_tokens, header_cost
                    )
                )
    drafts = _merge_tiny(drafts, max_tokens)
    _validate(drafts, max_tokens)
    return drafts


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


def _pack_sentences(
    text: str,
    breadcrumb: list[str],
    page: int | None,
    budget: int,
    overlap_tokens: int,
    header_cost: int,
) -> Iterator[ChunkDraft]:
    header = " > ".join(breadcrumb)
    sentences = [s for s in (m.group(0) for m in _SENTENCE_RE.finditer(text)) if s.strip()]
    if not sentences:
        return
    window: list[str] = []
    window_tokens = 0
    for sentence in sentences:
        cost = estimate_tokens(sentence)
        if cost > budget:  # 单句超预算:硬切成预算大小的片
            if window:
                yield _emit(window, breadcrumb, page, header, "text")
                window, window_tokens = [], 0
            yield from _hard_split(sentence, breadcrumb, page, budget, header)
            continue
        if window_tokens + cost > budget and window:
            yield _emit(window, breadcrumb, page, header, "text")
            tail: list[str] = []
            tail_tokens = 0
            for prev in reversed(window):
                t = estimate_tokens(prev)
                if tail_tokens + t > overlap_tokens:
                    break
                tail.insert(0, prev)
                tail_tokens += t
            window, window_tokens = tail, tail_tokens
        window.append(sentence)
        window_tokens += cost
    if window:
        yield _emit(window, breadcrumb, page, header, "text")


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
