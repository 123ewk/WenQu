"""格式解析器(ADR-7):pdf / docx / xlsx / pptx / md / txt → Block 序列。

Block 是「分块的输入」(proto 契约)。约定:
- 表格类内容:text 与 markdown 同为表格的 markdown 表示(embedding 与溯源共用一份);
- xlsx 按 ADR-5 表格感知策略:每个 sheet 先出「概述块(title)」,数据按行组分块,
  每块 markdown 都携带表头 —— chunker 对 table 块整体保留、不再硬切;
- 崩溃隔离由独立进程承担(ADR-7),本模块只抛异常,由 server 归一为 gRPC 状态码。
"""

from __future__ import annotations

import io
import re

from parser_service.pb import parser_pb2


class UnsupportedFormatError(ValueError):
    """未注册的文档格式。"""


_XLSX_ROWS_PER_BLOCK = 20  # 行组大小:20 行/块,块级 token 量对 512 上限安全

# PDF 标题判定:字号超过正文基准的多少倍才算标题。1.15 是与旧行为一致的保守值,
# 可识别 11pt 正文里的 13pt 小标题,又不会被行内字号抖动误触。
_HEADING_RATIO = 1.15
# 标题长度上限:中文标题通常不超过 ~20 字,40 已相当宽松。超过它按正文处理 ——
# 简历里的经历行、法条定义、图注这类"大字号的数据行"往往更长,被判成标题会丢内容。
_MAX_HEADING_CHARS = 40
# 句末标点:标题极少以此结尾,用它区分"标题"与"大字号正文句子"。逗号也算 ——
# 多行段落被折行后,中间行几乎总是以逗号收尾。
_SENTENCE_ENDINGS = (
    "。", ".",
    "!", "!", "?", "?",
    ";", ";",
    ",", ",",
)
# 字号归桶精度:0.25pt 足以吸收字体替换/字距带来的亚像素抖动,又不误并相邻层级。
_SIZE_BUCKET = 0.25
# 标题层级上限,与 proto 注释及其它格式解析器(docx/md 的 1~6)保持一致。
_MAX_HEADING_LEVEL = 6


def parse_document(fmt: str, content: bytes) -> tuple[list[parser_pb2.Block], dict[str, str]]:
    if fmt == "pdf":
        return parse_pdf(content)
    if fmt == "docx":
        return parse_docx(content)
    if fmt == "xlsx":
        return parse_xlsx(content)
    if fmt == "pptx":
        return parse_pptx(content)
    if fmt in ("md", "markdown"):
        return parse_markdown(content)
    if fmt == "txt":
        return parse_plain_text(content)
    raise UnsupportedFormatError(fmt)


# ---------------------------- PDF ----------------------------


def parse_pdf(content: bytes) -> tuple[list[parser_pb2.Block], dict[str, str]]:
    """PDF → Block 序列。

    版式还原按三条纪律(此前踩过的坑都记在这里):

    1. **行是合并的最小单位**。同字号相邻 span 只在**同一行内**合并(靠 pymupdf 的
       line 结构),绝不跨行拼接 —— 否则"教育经历 / 南华大学… / 项目经验"三行会被
       粘成一个块(实测踩过:简历类多头文档的面包屑因此出现同级标题);
    2. **标题层级按本文档实际字号相对定级**。先排出正文基准字号,再把"显著大于基准"
       的字号去重降序编号(最大=1、次大=2、…),而不是一律压成 1/2 —— 后者会让
       所有中间层标题全部同级;
    3. **文字与表格按页面坐标归并**。表格先于文字块解析,但阅读顺序必须按 y 坐标
       还原,不能把表格整体排到页尾。
    """
    import pymupdf

    blocks: list[parser_pb2.Block] = []
    meta: dict[str, str] = {}
    # 每页的候选行:(y, x, size, text);表格另行收集后按下标插入
    page_rows: list[list[tuple[float, float, float, str]]] = []
    page_tables: list[list[tuple[float, str]]] = []
    with pymupdf.open(stream=content, filetype="pdf") as doc:
        meta["pages"] = str(doc.page_count)
        for page in doc:
            rows: list[tuple[float, float, float, str]] = []
            for raw in page.get_text("dict")["blocks"]:
                for line in raw.get("lines", []):
                    # 行内合并:同字号、位置相邻的 span 合成一行文字
                    pieces: list[tuple[float, str]] = []
                    for span in line.get("spans", []):
                        text = span["text"].strip()
                        if not text:
                            continue
                        pieces.append((float(span["size"]), text))
                    if not pieces:
                        continue
                    rows.append(_merge_line_spans(pieces, line.get("bbox", (0, 0, 0, 0))))

            tables: list[tuple[float, str]] = []
            for table in page.find_tables().tables:
                markdown = _markdown_table([_pdf_row(row) for row in table.extract()])
                # 用表格左上角 y 坐标参与排序,保证与上下文的阅读顺序
                bbox = getattr(table, "bbox", None) or (0.0, 0.0, 0.0, 0.0)
                tables.append((float(bbox[1]), markdown))
            page_rows.append(rows)
            page_tables.append(tables)

        body_size = _body_font_size(page_rows)
        # 本文档全部"标题候选"字号的降序编号表:{字号: 层级}
        level_of_size = _heading_levels(page_rows, body_size)

        for pno, rows in enumerate(page_rows, start=1):
            # 合并文本行与表格,统一按 y 坐标排序还原阅读顺序
            merged: list[tuple[float, str, float | None]] = [
                (y, text, size) for y, _x, size, text in rows
            ]
            merged.extend(
                (y, markdown, None) for y, markdown in page_tables[pno - 1]
            )
            merged.sort(key=lambda item: item[0])
            blocks.extend(
                _blocks_for_page(merged, pno, level_of_size, body_size)
            )
    return blocks, meta


def _blocks_for_page(
    merged: list[tuple[float, str, float | None]],
    pno: int,
    level_of_size: dict[float, int],
    body_size: float,
) -> list[parser_pb2.Block]:
    """把一页的(已按 y 排序的)行/表格转成 Block 序列。

    逐行判定标题,再把连续的正文行合并成段落。两个"为什么不能更简单":

    - 为什么不先按字号归行组再判标题:真实文档里标题与其后紧跟的内容行常常
      **同字号同字重**(实测简历:"教育经历" 与 "南华大学…" 都是 11pt 加粗),
      按行组判定会把标题一起吞进段落;逐行判定 + 长度上限才能把 4 字的标题与
      42 字的内容行区分开;
    - 为什么不是每行一个块:单行不是段落,逐行成块会产生大量碎块并触发下游的
      小块合并(合并又会污染面包屑),所以正文行按连续同字号合并。
    """
    blocks: list[parser_pb2.Block] = []
    pending: list[str] = []
    pending_size: float | None = None

    for _y, text, size in merged:
        if size is None:  # 表格:自成一块
            _flush_paragraph(blocks, pending, pno)
            pending_size = None
            blocks.append(
                parser_pb2.Block(type="table", text=text, markdown=text, page=pno)
            )
            continue
        bucket = _bucket(size)
        if _is_heading(text, bucket, level_of_size, body_size):
            _flush_paragraph(blocks, pending, pno)
            pending_size = None
            blocks.append(
                parser_pb2.Block(
                    type="title", text=text, page=pno, level=level_of_size[bucket]
                )
            )
            continue
        if pending and bucket != pending_size:
            _flush_paragraph(blocks, pending, pno)  # 字号变了 → 新段落
        pending.append(text)
        pending_size = bucket
    _flush_paragraph(blocks, pending, pno)
    return blocks


def _flush_paragraph(
    blocks: list[parser_pb2.Block], pending: list[str], pno: int
) -> None:
    if pending:
        blocks.append(parser_pb2.Block(type="paragraph", text="\n".join(pending), page=pno))
        pending.clear()


def _body_font_size(page_rows: list[list[tuple[float, float, float, str]]]) -> float:
    """正文基准字号 = **字符总量最多**的那个字号(按字符量加权,不看行数)。

    为什么不用行数中位数:标题密集的文档(简历、封面、目录)里标题行数可能与正文
    相当,甚至更多,中位数会落到标题字号上,导致"要么全判成标题、要么一个都认不出"。
    正文的特征是**字数多**,按字符量取众数对行数不敏感。

    全篇只有一种字号时,它就是"正文",不产生任何标题。
    """
    weight: dict[float, int] = {}
    for rows in page_rows:
        for _y, _x, size, text in rows:
            weight[size] = weight.get(size, 0) + len(text)
    if not weight:
        return 0.0
    return max(weight.items(), key=lambda item: item[1])[0]


def _heading_levels(
    page_rows: list[list[tuple[float, float, float, str]]], body_size: float
) -> dict[float, int]:
    """把"显著大于正文"的字号去重降序编号 → {字号: 层级}。

    显著 = 超过正文基准 _HEADING_RATIO 倍;无正文基准时不做标题识别,
    避免把整篇文档每行都当标题。

    字号先按 _SIZE_BUCKET 归桶再比较:真实 PDF 里同一视觉层级的字号常有
    0.01pt 级抖动(字体替换、字距调整),精确比较会把同一层拆成多个层级。
    层级上限 _MAX_HEADING_LEVEL(与 proto 注释及其它格式保持一致)。
    """
    if body_size <= 0:
        return {}
    candidates = sorted(
        {
            _bucket(size)
            for rows in page_rows
            for _y, _x, size, _text in rows
            if _bucket(size) > body_size * _HEADING_RATIO
        },
        reverse=True,
    )
    return {
        size: min(index + 1, _MAX_HEADING_LEVEL) for index, size in enumerate(candidates)
    }


def _is_heading(
    text: str, size: float, level_of_size: dict[float, int], body_size: float
) -> bool:
    """标题判定 = 字号显著大于正文 + 够短 + 不像完整句子。

    三条里后两条是防"大字号正文"被误判成标题:标题一旦被判成标题,chunking 只把它
    当分节边界、不产出块,所以"后面没有正文的标题"整段内容会**静默消失**。
    摘要、引文、图注、法条定义这类大字号长句正是这种误判的高发区。
    """
    if size not in level_of_size:
        return False
    if len(text) > _MAX_HEADING_CHARS:
        return False
    # 句末标点几乎只出现在正文;标题极少以句号/问号/感叹号结尾
    return not text.rstrip().endswith(_SENTENCE_ENDINGS)


def _bucket(size: float) -> float:
    """把字号归到 _SIZE_BUCKET 精度的桶里,吸收同一层级的亚像素抖动。"""
    return round(size / _SIZE_BUCKET) * _SIZE_BUCKET


def _merge_line_spans(
    pieces: list[tuple[float, str]], bbox: object
) -> tuple[float, float, float, str]:
    """一行内的 span 合并为单行文本,返回 (y, x, 字号, 文本)。

    字号取行内最大者(标题行里混入小字上标不影响识别);y/x 取行 bbox 左上角。
    """
    size = max(item[0] for item in pieces)
    text = " ".join(item[1] for item in pieces).strip()
    x0, y0 = 0.0, 0.0
    if isinstance(bbox, (list, tuple)) and len(bbox) >= 2:
        x0, y0 = float(bbox[0]), float(bbox[1])
    return y0, x0, size, text


def _pdf_row(row: list[str | None]) -> list[str]:
    return [(cell or "").replace("\n", " ").strip() for cell in row]


# ---------------------------- DOCX ----------------------------


def parse_docx(content: bytes) -> tuple[list[parser_pb2.Block], dict[str, str]]:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(io.BytesIO(content))
    blocks: list[parser_pb2.Block] = []
    tables = 0
    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            para = Paragraph(child, doc)
            text = para.text.strip()
            if not text:
                continue
            style = ""
            try:
                style = getattr(para.style, "name", "") or ""
            except Exception:  # noqa: BLE001 — 样式缺失不阻断解析
                style = ""
            heading = re.search(r"heading\s*(\d)", style, re.IGNORECASE)
            if heading:
                blocks.append(
                    parser_pb2.Block(
                        type="title", text=text, level=min(int(heading.group(1)), 6)
                    )
                )
            elif style.lower().startswith("list"):
                blocks.append(parser_pb2.Block(type="list", text=text))
            else:
                blocks.append(parser_pb2.Block(type="paragraph", text=text))
        elif tag == "tbl":
            tables += 1
            table = Table(child, doc)
            rows = [
                [cell.text.replace("\n", " ").strip() for cell in row.cells]
                for row in table.rows
            ]
            markdown = _markdown_table(rows)
            blocks.append(parser_pb2.Block(type="table", text=markdown, markdown=markdown))
    return blocks, {"tables": str(tables)}


# ---------------------------- XLSX ----------------------------


def parse_xlsx(content: bytes) -> tuple[list[parser_pb2.Block], dict[str, str]]:
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    blocks: list[parser_pb2.Block] = []
    for sheet in workbook.worksheets:
        rows = [
            [_cell_str(cell) for cell in row]
            for row in sheet.iter_rows(values_only=True)
        ]
        rows = [row for row in rows if any(cell for cell in row)]
        if not rows:
            continue
        header, data = rows[0], rows[1:]
        blocks.append(
            parser_pb2.Block(
                type="title",
                text=f"工作表 {sheet.title}:共 {len(data)} 行 {len(header)} 列",
                level=2,
            )
        )
        header_md = _md_row(header)
        separator = "|" + "---|" * len(header)
        for start in range(0, len(data), _XLSX_ROWS_PER_BLOCK):
            group = data[start : start + _XLSX_ROWS_PER_BLOCK]
            markdown = "\n".join(
                [header_md, separator, *(_md_row(row) for row in group)]
            )
            blocks.append(
                parser_pb2.Block(type="table", text=markdown, markdown=markdown)
            )
    return blocks, {"sheets": str(len(workbook.sheetnames))}


def _cell_str(value: object) -> str:
    if value is None:
        return ""
    return str(value).replace("\n", " ").replace("|", "\\|").strip()


def _md_row(row: list[str]) -> str:
    return "| " + " | ".join(row) + " |"


def _markdown_table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    lines = [_md_row(normalized[0]), "|" + "---|" * width]
    lines.extend(_md_row(row) for row in normalized[1:])
    return "\n".join(lines)


# ---------------------------- PPTX ----------------------------


def parse_pptx(content: bytes) -> tuple[list[parser_pb2.Block], dict[str, str]]:
    from pptx import Presentation

    presentation = Presentation(io.BytesIO(content))
    blocks: list[parser_pb2.Block] = []
    for number, slide in enumerate(presentation.slides, start=1):
        for shape in slide.shapes:
            if shape.has_table:
                rows = [
                    [_cell_str(cell.text) for cell in row.cells]
                    for row in shape.table.rows
                ]
                markdown = _markdown_table(rows)
                blocks.append(
                    parser_pb2.Block(type="table", text=markdown, markdown=markdown, page=number)
                )
            elif shape == getattr(slide.shapes, "title", None) and shape.has_text_frame:
                text = shape.text_frame.text.strip()
                if text:
                    blocks.append(parser_pb2.Block(type="title", text=text, page=number, level=1))
            elif shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    text = para.text.strip()
                    if text:
                        blocks.append(parser_pb2.Block(type="paragraph", text=text, page=number))
    return blocks, {"slides": str(len(presentation.slides))}


# ---------------------------- Markdown / 纯文本 ----------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_LIST_RE = re.compile(r"^(?:[-*+]|\d+[.)])\s+")


def parse_markdown(content: bytes) -> tuple[list[parser_pb2.Block], dict[str, str]]:
    text = content.decode("utf-8", errors="replace")
    blocks: list[parser_pb2.Block] = []
    paragraph: list[str] = []
    list_items: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(parser_pb2.Block(type="paragraph", text="\n".join(paragraph)))
            paragraph.clear()

    def flush_list() -> None:
        if list_items:
            blocks.append(parser_pb2.Block(type="list", text="\n".join(list_items)))
            list_items.clear()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        heading = _HEADING_RE.match(line)
        if heading:
            flush_paragraph()
            flush_list()
            blocks.append(
                parser_pb2.Block(
                    type="title", text=heading.group(2).strip(), level=len(heading.group(1))
                )
            )
        elif not line:
            flush_paragraph()
            flush_list()
        elif _LIST_RE.match(line):
            flush_paragraph()
            list_items.append(line)
        else:
            flush_list()
            paragraph.append(line)
    flush_paragraph()
    flush_list()
    return blocks, {}


def parse_plain_text(content: bytes) -> tuple[list[parser_pb2.Block], dict[str, str]]:
    text = content.decode("utf-8", errors="replace")
    blocks = [
        parser_pb2.Block(type="paragraph", text=segment.strip())
        for segment in text.split("\n\n")
        if segment.strip()
    ]
    return blocks, {}
