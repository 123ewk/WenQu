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


# ---------------------------- PDF 版式:行边界、层级与阅读顺序 ----------------------------


def _pdf(lines: list[tuple[float, float, float, str]]) -> bytes:
    """按 (x, y, size, text) 列表生成单页 PDF。"""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    for x, y, size, text in lines:
        page.insert_text((x, y), text, fontsize=size, fontname="china-s")
    data = doc.tobytes()
    doc.close()
    return data


def test_pdf_same_size_lines_are_not_merged_across_lines() -> None:
    """同一字号的相邻行必须各自成块 —— 跨行合并会把多个小节粘成一个标题。"""
    content = _pdf(
        [
            (72, 60, 14, "教育经历"),
            (72, 80, 14, "南华大学(一本) 数据科学与大数据技术"),
            (72, 100, 14, "项目经验"),
        ]
    )
    blocks, _meta = _blocks(content, "pdf")
    texts = [b.text for b in blocks]
    assert len(blocks) == 3, texts
    assert texts[0] == "教育经历"
    assert "南华大学" in texts[1]
    assert texts[2] == "项目经验"
    # 有意取舍:连续同字号的行按正文保守处理(宁可少认标题,也不能把内容当标题丢掉)
    assert all(b.type == "paragraph" for b in blocks), [b.type for b in blocks]


def test_pdf_heading_levels_follow_distinct_sizes() -> None:
    """标题层级按实际出现的字号相对定级:最大=1,次大=2,再次=3(不再一律压成 1/2)。"""
    lines: list[tuple[float, float, float, str]] = [(72, 50, 24, "大模型应用开发工程师")]
    y = 80
    for i in range(6):  # 正文若干行,让基准字号落在正文上
        lines.append((72, y, 10, f"正文第{i}行内容用于确定正文字号"))
        y += 16
    lines.append((72, y + 10, 16, "教育经历"))
    lines.append((72, y + 30, 13, "南华大学"))

    blocks, _meta = _blocks(_pdf(lines), "pdf")
    by_text = {b.text: b.level for b in blocks if b.type == "title"}
    assert by_text["大模型应用开发工程师"] == 1, by_text
    assert by_text["教育经历"] == 2, by_text
    assert by_text["南华大学"] == 3, by_text


def test_pdf_table_between_paragraphs_keeps_reading_order() -> None:
    """表格块必须按 y 坐标排在上下正文之间,不能被整体挪到页尾。"""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 60), "表格前的说明文字", fontsize=10, fontname="china-s")
    x0, y0, w, h = 72, 90, 200, 60
    for i in range(3):
        page.draw_line((x0, y0 + i * h / 2), (x0 + w, y0 + i * h / 2))
    for j in range(3):
        page.draw_line((x0 + j * w / 2, y0), (x0 + j * w / 2, y0 + h))
    cells = [["名称", "数量"], ["苹果", "3"], ["香蕉", "5"]]
    for r in range(3):
        for c in range(2):
            page.insert_text(
                (x0 + c * w / 2 + 5, y0 + r * h / 2 + 14),
                cells[r][c],
                fontsize=10,
                fontname="china-s",
            )
    page.insert_text((72, 200), "表格后的收尾文字", fontsize=10, fontname="china-s")
    data = doc.tobytes()
    doc.close()

    blocks, _meta = _blocks(data, "pdf")
    seq = [(b.type, b.text) for b in blocks]
    table_index = next(i for i, (kind, _t) in enumerate(seq) if kind == "table")
    intro_index = next(i for i, (_k, text) in enumerate(seq) if "表格前" in text)
    outro_index = next(i for i, (_k, text) in enumerate(seq) if "表格后" in text)
    assert intro_index < table_index < outro_index, seq


def test_pdf_multi_line_large_font_paragraph_is_kept_as_text() -> None:
    """大字号**连续多行**是正文段落(摘要/引文/图注),必须全部保留为文本块。

    chunking 把标题当分节边界且不产出块,所以"被判成标题又没有后续正文"的文字会
    整段消失。逐行判定会犯这个错;按行组判定(标题是孤行)才安全。
    """
    para_lines = [
        "这是一段用较大字号排版的摘要文字,",
        "它连续占用了好几行,属于正文段落,",
        "不应该被当成标题处理。",
    ]
    lines: list[tuple[float, float, float, str]] = []
    y = 60
    for i in range(4):
        lines.append((72, y, 10, f"正文字号的内容第{i}行,用来确定基准字号。"))
        y += 16
    for text in para_lines:
        lines.append((72, y + 10, 14, text))
        y += 20

    blocks, _meta = _blocks(_pdf(lines), "pdf")
    for text in para_lines:
        kept = [b for b in blocks if b.text == text]
        assert kept, [b.text for b in blocks]
        assert kept[0].type == "paragraph", (kept[0].type, text)


def test_pdf_heading_levels_survive_heading_dense_document() -> None:
    """标题行数接近甚至多于正文行时仍要认出标题(基准字号按字符量加权,不看行数)。

    简历/封面/目录这类文档,标题行数可与正文相当 —— 用行数中位数做基准会失手。
    这里构造 5 个标题行 vs 3 个正文行(标题更多),且标题与正文交替(标题是孤行)。
    """
    lines: list[tuple[float, float, float, str]] = []
    y = 50
    for i in range(5):
        lines.append((72, y, 13, f"小节标题{i}"))
        y += 18
        lines.append((72, y, 10, f"这是正文内容,字数明显更多,用来在字符量上占优。第{i}段"))
        y += 16

    blocks, _meta = _blocks(_pdf(lines), "pdf")
    titles = [b for b in blocks if b.type == "title"]
    assert len(titles) == 5, [(b.type, b.text) for b in blocks]
    assert all(t.level == 1 for t in titles), [(t.text, t.level) for t in titles]


def test_pdf_near_identical_sizes_share_one_level() -> None:
    """字号只有极微差异(字体替换/字距伪影,如 13.0/13.008)时必须视作同一层级。"""
    lines: list[tuple[float, float, float, str]] = [
        (72, 40, 10, "正文基准行一,字数多一些以便确定基准字号。"),
        (72, 56, 10, "正文基准行二,字数多一些以便确定基准字号。"),
        (72, 72, 10, "正文基准行三,字数多一些以便确定基准字号。"),
        (72, 92, 13.0, "标题甲"),
        (72, 108, 10, "正文夹在标题之间,保证每个标题都是孤行。"),
        (72, 128, 13.008, "标题乙"),
        (72, 144, 10, "正文夹在标题之间,保证每个标题都是孤行。"),
        (72, 164, 13.016, "标题丙"),
    ]
    blocks, _meta = _blocks(_pdf(lines), "pdf")
    levels = {b.text: b.level for b in blocks if b.type == "title"}
    assert levels, [(b.type, b.text, b.level) for b in blocks]
    assert set(levels.values()) == {1}, levels


def test_pdf_heading_level_capped_at_six() -> None:
    """层级上限 6(与 proto 注释及其它格式一致),字号再多也不越界。"""
    lines: list[tuple[float, float, float, str]] = []
    y = 40
    for size in (40, 36, 32, 28, 24, 20, 16, 14):
        lines.append((72, y, size, f"层级{size}"))
        y += size + 6
    lines.append((72, y + 10, 10, "正文行一"))
    lines.append((72, y + 26, 10, "正文行二"))
    lines.append((72, y + 42, 10, "正文行三"))

    blocks, _meta = _blocks(_pdf(lines), "pdf")
    levels = [b.level for b in blocks if b.type == "title"]
    assert levels, [(b.type, b.text) for b in blocks]
    assert max(levels) <= 6, levels


def test_pdf_resume_layout_nests_breadcrumbs_and_keeps_content() -> None:
    """简历式版式回归:面包屑逐级嵌套,且"经历行"作为正文保留(不被当标题丢掉)。

    对应线上问题:同字号多行被粘连成一个块 → 面包屑出现同级标题、正文错位。
    """
    import sys

    sys.path.insert(0, "../server/src")
    from app.application.chunking import chunk_blocks

    # 真实简历的正文(10pt)篇幅远多于标题,这里按同样比例构造
    lines: list[tuple[float, float, float, str]] = [
        (150, 55, 20, "大模型应用开发工程师"),
        (80, 83, 10, "易为康 | 男 | 手机:13537186148 | 邮箱:ewk@example.com"),
        (80, 107, 14, "教育经历"),
        (80, 133, 12, "南华大学(一本) | 数据科学与大数据技术 | 2025.09 - 2029.06"),
        (80, 155, 10, "主修课程:数据结构、数据库原理、机器学习、分布式系统。"),
        (80, 173, 10, "在校期间参与多项课程项目,具备扎实的工程实践基础。"),
        (80, 195, 14, "项目经验"),
        (80, 221, 12, "AI Job 控制台"),
        (80, 243, 10, "项目描述:一个 AI 求职助手全栈软件,可自动搜索岗位。"),
        (80, 261, 10, "技术栈:FastAPI、Vue、LangGraph、Playwright。"),
        (80, 279, 10, "项目亮点:自建单线程执行器解决异步与同步库的冲突。"),
    ]
    blocks, _meta = _blocks(_pdf(lines), "pdf")
    drafts = chunk_blocks(blocks)

    breadcrumbs = [d.breadcrumb for d in drafts]
    assert ["大模型应用开发工程师"] in breadcrumbs, breadcrumbs
    assert ["大模型应用开发工程师", "教育经历"] in breadcrumbs, breadcrumbs
    assert ["大模型应用开发工程师", "项目经验", "AI Job 控制台"] in breadcrumbs

    # 关键:经历行必须出现在正文里(它是内容,不是标题)
    joined = "\n".join(d.content for d in drafts)
    assert "南华大学" in joined, joined
    assert "项目描述" in joined, joined
    # 任何一条面包屑里都不允许出现"多个标题挤在一起"的粘连痕迹
    assert all("教育经历 南华" not in " ".join(b) for b in breadcrumbs), breadcrumbs
