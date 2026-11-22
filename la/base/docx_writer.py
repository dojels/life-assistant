"""Markdown → Word(.docx):**正式文档观感** —— M12 / P1-4。

用户 2026-10-13 拍板:**公文风**(标题黑体 + 正文仿宋)+ 表格加合计行与斑马纹。

两套字体方案(frontmatter 里 `style: 报告` 可切回):
    公文(默认)  正文仿宋四号 / 固定行距 26pt / 页边距按 GB/T 9704 / 二级标题楷体
    报告        正文宋体小四 / 1.5 倍行距 / 封面微软雅黑

成品长这样:
    封面     标题(居中·大字) + 副标题 + 作者·日期 + 分页
    页眉     文档标题 + 一条下边框线
    页脚     第 N 页(页码是**域**,不是死数字)
    正文     首行缩进 2 字符 / 段后 6pt / 两端对齐
    表格     Table Grid + 表头灰底加粗 + **斑马纹** + **合计行** + 列宽按内容估算

合计行规则(可预测、可关):
  * 至少 2 行数据,且至少一列是数字才加;
  * 表头含"人均/单价/比例/占比/率/日期/时间/编号/序号"的列**不求和**(求了是错的);
  * frontmatter 写 `total: off` 直接关掉。

三条纪律:
  1. **写完回读自检**(verify_docx):段落/表格/关键文字对不上就**不算成功**;
  2. **中文字体显式设 w:eastAsia**:不设的话 Word 会拿默认字体渲染中文,排版会散;
  3. **样式写死在这里**,不引模板文件、不引样式表 —— 一份文件、一个真源。
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date as date_type

from . import markdown_lite

# ⚠️ **延迟导入**(照 D82 引 pycryptodome 的纪律):没装 python-docx 时,
#    只有"写 Word"这一步会失败并给出安装指引,整个应用照常跑。
try:
    from docx import Document as DocxDocument
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor

    DOCX_AVAILABLE = True
except ImportError:  # pragma: no cover - 只在没装依赖的环境里走到
    DOCX_AVAILABLE = False
    DocxDocument = None  # type: ignore[assignment]
    WD_TABLE_ALIGNMENT = WD_ALIGN_PARAGRAPH = WD_BREAK = None  # type: ignore[assignment]
    OxmlElement = qn = Cm = Pt = RGBColor = None  # type: ignore[assignment]

FONT_LATIN = "Times New Roman"
SIZE_TITLE = 26
SIZE_SMALL = 9
GRAY = RGBColor(0x59, 0x59, 0x59)
HEADER_FILL = "D9D9D9"          # 表头:中灰
ZEBRA_FILL = "F5F5F5"           # 斑马纹:极浅灰(再深就抢正文的注意力)

#: 正文每段上限(超了说明这不是"文档"而是数据倾倒)
MAX_PARAGRAPH_CHARS = 4000

#: 「这一列不该求和」的表头关键词 —— 人均/单价求出来是错的,日期求和更没意义
NO_TOTAL_HEADERS = ("人均", "单价", "均价", "比例", "占比", "率", "百分比",
                    "日期", "时间", "编号", "序号", "排名")


@dataclass(frozen=True)
class Scheme:
    """一套字体与版式。文案里的两套方案都在这里,新增一套 = 加一个条目。"""

    name: str
    heading: str          # 一级标题(黑体)
    heading2: str         # 二级标题(公文体例用楷体)
    body: str             # 正文
    ui: str               # 封面与页眉页脚
    size_body: float
    line_spacing: float   # 数值 = 固定磅值;<2 当作倍数
    margins: tuple[float, float, float, float]   # 上 下 左 右(cm)


SCHEMES: dict[str, Scheme] = {
    "公文": Scheme(name="公文", heading="黑体", heading2="楷体", body="仿宋", ui="黑体",
                   size_body=14, line_spacing=26, margins=(3.7, 3.5, 2.8, 2.6)),
    "报告": Scheme(name="报告", heading="黑体", heading2="黑体", body="宋体", ui="微软雅黑",
                   size_body=12, line_spacing=1.5, margins=(2.54, 2.54, 3.0, 3.0)),
}
DEFAULT_SCHEME = "公文"


class OfficeWriteError(RuntimeError):
    """生成 Office 文件时的错误。消息是给模型/用户看的。"""


@dataclass
class DocxFacts:
    """自检用的"应该长什么样"。verify 拿它对比回读结果。"""

    title: str = ""
    headings: list[str] = field(default_factory=list)
    tables: int = 0
    table_headers: list[list[str]] = field(default_factory=list)


def _require_docx() -> None:
    if not DOCX_AVAILABLE:
        raise OfficeWriteError(
            "写 Word 需要 python-docx,当前环境里没装。请在项目虚拟环境里执行 "
            "pip install python-docx(依赖清单见 pyproject.toml)"
        )


def build_docx(
    markdown: str,
    *,
    fallback_title: str = "",
    today: str = "",
    style: str = "",
) -> tuple[bytes, DocxFacts]:
    """Markdown → docx 字节。返回 (文件字节, 自检事实)。"""
    _require_docx()
    document = markdown_lite.parse(markdown)
    title = document.title or fallback_title or "文档"
    if not document.blocks:
        raise OfficeWriteError("内容里没有任何可排版的段落")
    for block in document.blocks:
        if len(block.text) > MAX_PARAGRAPH_CHARS:
            raise OfficeWriteError(
                "有一段超过 " + str(MAX_PARAGRAPH_CHARS) + " 字,不像文档正文;请拆成多段或分文件"
            )
    meta = document.meta
    scheme = SCHEMES.get((style or meta.get("style") or DEFAULT_SCHEME).strip(), SCHEMES[DEFAULT_SCHEME])
    want_total = str(meta.get("total", "auto")).strip().lower() not in ("off", "0", "false", "no", "关")

    doc = DocxDocument()
    _setup_page(doc, scheme)
    _setup_base_style(doc, scheme)
    _add_cover(doc, title=title, subtitle=meta.get("subtitle", ""),
               author=meta.get("author", ""), when=meta.get("date", "") or today, scheme=scheme)
    _setup_header(doc, meta.get("header", "") or title, scheme=scheme)
    _setup_footer(doc, scheme=scheme)

    facts = DocxFacts(title=title, tables=0)
    first_heading_skipped = False
    for block in document.blocks:
        if block.kind == "heading":
            # frontmatter 没给标题时,一级标题已经上了封面 —— 正文里不再重复一遍
            if block.level == 1 and not first_heading_skipped and block.text == title:
                first_heading_skipped = True
                continue
            _add_heading(doc, block.text, block.level, scheme=scheme)
            facts.headings.append(block.text)
        elif block.kind == "bullet":
            _add_list_item(doc, block.text, numbered=False, scheme=scheme)
        elif block.kind == "number":
            _add_list_item(doc, block.text, numbered=True, scheme=scheme)
        elif block.kind == "table":
            _add_table(doc, block.rows, scheme=scheme, want_total=want_total)
            facts.tables += 1
            facts.table_headers.append(list(block.rows[0]) if block.rows else [])
        elif block.kind == "pagebreak":
            doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
        else:
            _add_paragraph(doc, block.text, scheme=scheme)

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue(), facts


def verify_docx(data: bytes, facts: DocxFacts) -> list[str]:
    """回读自检:返回问题列表(**空列表 = 通过**)。"""
    _require_docx()
    problems: list[str] = []
    try:
        doc = DocxDocument(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - 打不开就是最严重的问题
        return ["生成的 docx 打不开:" + type(exc).__name__ + " " + str(exc)[:80]]
    texts = [paragraph.text.strip() for paragraph in doc.paragraphs]
    joined = "\n".join(texts)
    if facts.title and facts.title not in joined:
        problems.append("标题没写进去:" + facts.title[:24])
    for heading in facts.headings[:20]:
        if heading and heading not in joined:
            problems.append("小标题丢了:" + heading[:24])
    if len(doc.tables) != facts.tables:
        problems.append("表格数量不对:期望 " + str(facts.tables) + " 实际 " + str(len(doc.tables)))
    for index, header in enumerate(facts.table_headers[:10]):
        if index >= len(doc.tables):
            break
        cells = [cell.text.strip() for cell in doc.tables[index].rows[0].cells]
        if [item for item in header if item] and cells[:len(header)] != header[:len(cells)]:
            problems.append("第 " + str(index + 1) + " 张表的表头对不上")
    return problems


# -- 页面与样式 -------------------------------------------------------------
def _setup_page(doc, scheme: Scheme) -> None:  # noqa: ANN001
    top, bottom, left, right = scheme.margins
    section = doc.sections[0]
    section.page_width = Cm(21.0)      # A4
    section.page_height = Cm(29.7)
    section.top_margin = Cm(top)
    section.bottom_margin = Cm(bottom)
    section.left_margin = Cm(left)
    section.right_margin = Cm(right)


def _setup_base_style(doc, scheme: Scheme) -> None:  # noqa: ANN001
    style = doc.styles["Normal"]
    style.font.name = FONT_LATIN
    style.font.size = Pt(scheme.size_body)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), scheme.body)
    paragraph = style.paragraph_format
    if scheme.line_spacing >= 2:
        # 公文体例的行距是**固定磅值**(28pt 那种);直接给倍数会显得松
        from docx.enum.text import WD_LINE_SPACING

        paragraph.line_spacing_rule = WD_LINE_SPACING.EXACTLY
        paragraph.line_spacing = Pt(scheme.line_spacing)
    else:
        paragraph.line_spacing = scheme.line_spacing
    paragraph.space_after = Pt(6)


def _style_run(run, *, font: str, size: float, bold: bool = False, color=None) -> None:  # noqa: ANN001
    """⚠️ 三件事都要设:w:ascii / w:hAnsi(西文)+ w:eastAsia(中文),少一个中文就会掉字体。"""
    run.font.name = FONT_LATIN
    run.font.size = Pt(size)
    run.font.bold = bold
    if color is not None:
        run.font.color.rgb = color
    rpr = run._element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.append(fonts)
    fonts.set(qn("w:ascii"), FONT_LATIN)
    fonts.set(qn("w:hAnsi"), FONT_LATIN)
    fonts.set(qn("w:eastAsia"), font)


def _indent_first_line(paragraph, *, size: float, chars: int = 200) -> None:  # noqa: ANN001
    """首行缩进 2 字符(东亚版式用 firstLineChars,磅值只是不给东亚引擎时的兜底)。"""
    ppr = paragraph._p.get_or_add_pPr()
    ind = ppr.find(qn("w:ind"))
    if ind is None:
        ind = OxmlElement("w:ind")
        ppr.append(ind)
    ind.set(qn("w:firstLineChars"), str(chars))
    ind.set(qn("w:firstLine"), str(int(size * 20 * chars / 100)))


def _write_runs(paragraph, text: str, *, font: str, size: float) -> None:  # noqa: ANN001
    for piece, bold in markdown_lite.inline_bold(text):
        if not piece:
            continue
        run = paragraph.add_run(piece)
        _style_run(run, font=font, size=size, bold=bold)


def _add_paragraph(doc, text: str, *, scheme: Scheme) -> None:  # noqa: ANN001
    paragraph = doc.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    _indent_first_line(paragraph, size=scheme.size_body)
    _write_runs(paragraph, text, font=scheme.body, size=scheme.size_body)


def _add_heading(doc, text: str, level: int, *, scheme: Scheme) -> None:  # noqa: ANN001
    sizes = {1: scheme.size_body + 2, 2: scheme.size_body + 1,
             3: scheme.size_body, 4: scheme.size_body}
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(14 if level <= 2 else 10)
    paragraph.paragraph_format.space_after = Pt(8 if level <= 2 else 6)
    # 公文体例:一级黑体、二级楷体、三级及以下仿宋加粗 —— 靠**字体**分级,不靠字号堆叠
    font = scheme.heading if level <= 1 else (scheme.heading2 if level == 2 else scheme.body)
    _write_runs(paragraph, text, font=font, size=sizes.get(level, scheme.size_body))
    for run in paragraph.runs:
        run.font.bold = True


def _add_list_item(doc, text: str, *, numbered: bool, scheme: Scheme) -> None:  # noqa: ANN001
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.left_indent = Cm(0.75)
    paragraph.paragraph_format.space_after = Pt(3)
    # ⚠️ 不挂 Word 的 List Bullet 样式:那套编号跨文档不稳定(要定义 numbering.xml),
    #    这里用字面符号,打开到哪台机器上都长一样。
    if not numbered:
        run = paragraph.add_run("· ")
        _style_run(run, font=scheme.body, size=scheme.size_body)
    _write_runs(paragraph, text, font=scheme.body, size=scheme.size_body)


# -- 表格 -------------------------------------------------------------------
def plan_total(rows: list[list[str]]) -> list[int]:
    """算出"哪些列该求和"。返回**列下标**列表(空 = 这张表不加合计行)。

    规则(可预测 > 聪明):
      * 至少 2 行数据;
      * 该列至少 2 个单元格是数字,且没有一个非空单元格不是数字;
      * 表头不含"人均/单价/比例/日期"这类**求了就是错**的词。
    """
    if len(rows) < 3:            # 表头 + 至少 2 行数据
        return []
    header = rows[0]
    body = rows[1:]
    columns: list[int] = []
    for index in range(len(header)):
        title = str(header[index] or "")
        if any(word in title for word in NO_TOTAL_HEADERS):
            continue
        values = [str(row[index]) if index < len(row) else "" for row in body]
        present = [value for value in values if value.strip()]
        if len(present) < 2:
            continue
        numbers = [_as_number(value) for value in present]
        if all(item is not None for item in numbers):
            columns.append(index)
    return columns


def _total_row(rows: list[list[str]], columns: list[int]) -> list[str]:
    width = len(rows[0])
    row = [""] * width
    for index in columns:
        total = sum(_as_number(str(item[index]) if index < len(item) else "") or 0.0
                    for item in rows[1:])
        row[index] = _format_number(total)
    row[0] = "合计"
    return row


def _as_number(text: str) -> float | None:
    cleaned = str(text or "").strip().replace(",", "").replace("，", "")
    for symbol in ("¥", "￥", "$", "元", "人", "笔", "次", "个"):
        cleaned = cleaned.replace(symbol, "")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _format_number(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return str(round(value, 2))


def _add_table(doc, rows: list[list[str]], *, scheme: Scheme, want_total: bool = True) -> None:  # noqa: ANN001
    if not rows:
        return
    columns = plan_total(rows) if want_total else []
    body = [list(row) for row in rows]
    if columns:
        body = body + [_total_row(rows, columns)]
    width = max(len(row) for row in body)
    normalized = [list(row) + [""] * (width - len(row)) for row in body]
    table = doc.add_table(rows=len(normalized), cols=width)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    widths = _column_widths(normalized)
    total_index = len(normalized) - 1 if columns else -1
    for row_index, row in enumerate(normalized):
        is_header = row_index == 0
        is_total = row_index == total_index
        for column_index, value in enumerate(row):
            cell = table.cell(row_index, column_index)
            cell.width = Cm(widths[column_index])
            paragraph = cell.paragraphs[0]
            paragraph.paragraph_format.line_spacing = 1.15
            paragraph.paragraph_format.line_spacing_rule = None
            paragraph.paragraph_format.space_after = Pt(2)
            run = paragraph.add_run(value)
            _style_run(run, font=scheme.body, size=scheme.size_body - 1,
                       bold=(is_header or is_total))
            if is_header:
                _shade(cell, HEADER_FILL)
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            elif is_total:
                _shade(cell, ZEBRA_FILL)
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER if column_index == 0                     else WD_ALIGN_PARAGRAPH.RIGHT
            elif row_index % 2 == 0:
                # 斑马纹:隔一行一层极浅灰(表头与合计行各有自己的底色,不参与)
                _shade(cell, ZEBRA_FILL)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


def _column_widths(rows: list[list[str]], *, total_cm: float = 15.0) -> list[float]:
    """按内容长度估列宽 —— 让"表格看起来像人排的",而不是等宽糊成一片。"""
    columns = len(rows[0])
    weights: list[float] = []
    for index in range(columns):
        longest = max((len(row[index]) for row in rows), default=1)
        weights.append(max(min(longest, 40), 4))
    total = sum(weights) or 1
    widths = [max(total_cm * weight / total, 1.6) for weight in weights]
    scale = total_cm / sum(widths)
    return [round(width * scale, 2) for width in widths]


def _shade(cell, fill: str) -> None:  # noqa: ANN001
    tcpr = cell._tc.get_or_add_tcPr()
    shade = OxmlElement("w:shd")
    shade.set(qn("w:val"), "clear")
    shade.set(qn("w:color"), "auto")
    shade.set(qn("w:fill"), fill)
    tcpr.append(shade)


# -- 封面 / 页眉 / 页脚 -----------------------------------------------------
def _add_cover(doc, *, title: str, subtitle: str, author: str, when: str, scheme: Scheme) -> None:  # noqa: ANN001
    for _ in range(4):
        doc.add_paragraph()
    line = doc.add_paragraph()
    line.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _style_run(line.add_run(title), font=scheme.heading, size=SIZE_TITLE, bold=True)
    if subtitle:
        sub = doc.add_paragraph()
        sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
        sub.paragraph_format.space_before = Pt(6)
        _style_run(sub.add_run(subtitle), font=scheme.heading2, size=14, color=GRAY)
    for _ in range(6):
        doc.add_paragraph()
    stamp = " · ".join(item for item in (author, when) if item)
    if stamp:
        foot = doc.add_paragraph()
        foot.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _style_run(foot.add_run(stamp), font=scheme.ui, size=11, color=GRAY)
    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)


def _setup_header(doc, text: str, *, scheme: Scheme) -> None:  # noqa: ANN001
    paragraph = doc.sections[0].header.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    _style_run(paragraph.add_run(text), font=scheme.ui, size=SIZE_SMALL, color=GRAY)
    ppr = paragraph._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), "BFBFBF")
    borders.append(bottom)
    ppr.append(borders)


def _setup_footer(doc, *, scheme: Scheme) -> None:  # noqa: ANN001
    paragraph = doc.sections[0].footer.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _style_run(paragraph.add_run("第 "), font=scheme.ui, size=SIZE_SMALL, color=GRAY)
    _add_field(paragraph, "PAGE", scheme=scheme)
    _style_run(paragraph.add_run(" 页"), font=scheme.ui, size=SIZE_SMALL, color=GRAY)


def _add_field(paragraph, code: str, *, scheme: Scheme) -> None:  # noqa: ANN001
    """插入域代码(页码)。⭐ 必须做成域,不能写死数字 —— 否则用户一改内容页码就错。"""
    run = paragraph.add_run()
    _style_run(run, font=scheme.ui, size=SIZE_SMALL, color=GRAY)
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = code
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.append(begin)
    run._r.append(instr)
    run._r.append(end)


def today_text(when: date_type | None = None) -> str:
    """封面日期。取本地日期即可 —— 调用方(工具层)会把 settings 时区的今天传进来。"""
    moment = when or date_type.today()
    return moment.strftime("%Y 年 %m 月 %d 日")


__all__ = ["OfficeWriteError", "DocxFacts", "Scheme", "SCHEMES", "DEFAULT_SCHEME",
           "build_docx", "verify_docx", "plan_total", "today_text"]
