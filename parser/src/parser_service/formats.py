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
import statistics

from parser_service.pb import parser_pb2


class UnsupportedFormatError(ValueError):
    """未注册的文档格式。"""


_XLSX_ROWS_PER_BLOCK = 20  # 行组大小:20 行/块,块级 token 量对 512 上限安全


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
    import pymupdf

    blocks: list[parser_pb2.Block] = []
    meta: dict[str, str] = {}
    page_spans: list[list[tuple[float, str]]] = []
    with pymupdf.open(stream=content, filetype="pdf") as doc:
        meta["pages"] = str(doc.page_count)
        for page in doc:
            spans: list[tuple[float, str]] = []
            for raw in page.get_text("dict")["blocks"]:
                for line in raw.get("lines", []):
                    for span in line.get("spans", []):
                        text = span["text"].strip()
                        if text:
                            spans.append((float(span["size"]), text))
            page_spans.append(spans)
            tables = page.find_tables()
            for table in tables.tables:
                markdown = _markdown_table([_pdf_row(row) for row in table.extract()])
                blocks.append(
                    parser_pb2.Block(
                        type="table", text=markdown, markdown=markdown, page=page.number + 1
                    )
                )

        sizes = [size for spans in page_spans for size, _ in spans]
        body_size = statistics.median(sizes) if sizes else 0.0
        for pno, spans in enumerate(page_spans, start=1):
            if not spans:
                continue
            # 启发式标题:块内最大字号显著大于正文中位数且足够短
            merged = _merge_adjacent_spans(spans)
            for size, text in merged:
                is_title = (
                    body_size > 0 and size >= body_size * 1.15 and 0 < len(text) <= 80
                )
                if is_title:
                    level = 1 if size >= body_size * 1.5 else 2
                    blocks.append(parser_pb2.Block(type="title", text=text, page=pno, level=level))
                else:
                    blocks.append(parser_pb2.Block(type="paragraph", text=text, page=pno))
    return blocks, meta


def _pdf_row(row: list[str | None]) -> list[str]:
    return [(cell or "").replace("\n", " ").strip() for cell in row]


def _merge_adjacent_spans(
    spans: list[tuple[float, str]], max_gap: float = 1.2
) -> list[tuple[float, str]]:
    """同字号相邻 span 合并成行,避免一句话被拆成多个块。"""
    merged: list[tuple[float, str]] = []
    for size, text in spans:
        if merged and abs(merged[-1][0] - size) <= max_gap:
            prev_size, prev_text = merged[-1]
            merged[-1] = (max(prev_size, size), f"{prev_text} {text}".strip())
        else:
            merged.append((size, text))
    return merged


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
