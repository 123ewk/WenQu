"""格式解析器测试:程序化生成真实文件字节,验证块结构与表格感知策略。"""

from __future__ import annotations

import io

import pytest

from parser_service.formats import UnsupportedFormatError, parse_document


def _blocks(content: bytes, fmt: str):
    blocks, meta = parse_document(fmt, content)
    return blocks, meta


def test_markdown_headings_lists_paragraphs() -> None:
    md = (
        "# 一级标题\n\n正文第一段。\n多行。\n\n## 二级标题\n\n"
        "- 项目一\n- 项目二\n1. 步骤\n\n结尾段。"
    ).encode()
    blocks, meta = _blocks(md, "md")
    types = [b.type for b in blocks]
    assert types[0] == "title" and blocks[0].level == 1 and blocks[0].text == "一级标题"
    assert "二级标题" in [b.text for b in blocks if b.type == "title"]
    lst = next(b for b in blocks if b.type == "list")
    assert "项目一" in lst.text and "步骤" in lst.text
    assert types[-1] == "paragraph" and blocks[-1].text == "结尾段。"
    assert meta == {}


def test_plain_text_paragraphs() -> None:
    blocks, _ = _blocks("第一段\n多行。\n\n第二段".encode(), "txt")
    assert [b.text for b in blocks] == ["第一段\n多行。", "第二段"]


def test_docx_headings_tables_in_document_order() -> None:
    from docx import Document

    doc = Document()
    doc.add_heading("第一章 概述", level=1)
    doc.add_paragraph("本章介绍背景。")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "名称"
    table.cell(0, 1).text = "数量"
    table.cell(1, 0).text = "苹果"
    table.cell(1, 1).text = "3"
    doc.add_paragraph("表格之后还有一段。")
    buffer = io.BytesIO()
    doc.save(buffer)

    blocks, meta = _blocks(buffer.getvalue(), "docx")
    # 文档顺序:标题 → 段落 → 表格 → 段落
    types = [b.type for b in blocks]
    assert types == ["title", "paragraph", "table", "paragraph"]
    assert blocks[0].level == 1
    assert "名称" in blocks[2].markdown and "苹果" in blocks[2].markdown
    assert meta["tables"] == "1"


def test_xlsx_sheet_overview_and_header_in_every_block() -> None:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "库存"
    ws.append(["品名", "库存量"])
    for i in range(1, 26):  # 25 行数据 → 20+5 两个行组块
        ws.append([f"物品{i}", i])
    buffer = io.BytesIO()
    wb.save(buffer)

    blocks, meta = _blocks(buffer.getvalue(), "xlsx")
    assert meta["sheets"] == "1"
    overview = next(b for b in blocks if b.type == "title")
    assert "工作表 库存" in overview.text and "25 行" in overview.text
    table_blocks = [b for b in blocks if b.type == "table"]
    assert len(table_blocks) == 2  # ADR-5:按行组分块
    for block in table_blocks:  # 每块 markdown 都携带表头
        first_data_line = block.markdown.splitlines()[0]
        assert "品名" in first_data_line and "库存量" in first_data_line


def test_pptx_titles_paragraphs_and_page_numbers() -> None:
    from pptx import Presentation

    prs = Presentation()
    layout = prs.slide_layouts[1]  # 标题和内容
    slide = prs.slides.add_slide(layout)
    slide.shapes.title.text = "项目汇报"
    body = slide.placeholders[1]
    body.text_frame.text = "第一要点"
    buffer = io.BytesIO()
    prs.save(buffer)

    blocks, meta = _blocks(buffer.getvalue(), "pptx")
    title = next(b for b in blocks if b.type == "title")
    assert title.text == "项目汇报" and title.page == 1 and title.level == 1
    assert any(b.type == "paragraph" and "第一要点" in b.text for b in blocks)
    assert meta["slides"] == "1"


def test_pdf_title_detection_and_page_meta() -> None:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    # 默认 helv 字体无 CJK 字形(提取为占位点),内置 china-s 才能还原中文
    page.insert_text((72, 72), "RAG 平台总体设计", fontsize=22, fontname="china-s")
    page.insert_text(
        (72, 120), "这是正文的第一个自然段,字号明显更小。", fontsize=11, fontname="china-s"
    )
    content = doc.tobytes()

    blocks, meta = _blocks(content, "pdf")
    assert meta["pages"] == "1"
    titles = [b for b in blocks if b.type == "title"]
    assert any("总体设计" in t.text for t in titles), [b.text for b in blocks]
    assert any(b.type == "paragraph" and "正文" in b.text for b in blocks)


def test_unknown_format_raises() -> None:
    with pytest.raises(UnsupportedFormatError):
        parse_document("exe", b"MZ")
