"""Markdown 表格 → Excel(.xlsx) —— M12 / P1-4。

合同(与 docx 那侧共用同一份 markdown 子集解析,求和规则也共用 `plan_total`):
  * 内容里的**每张 Markdown 表**变成一张工作表,表名取它上面最近的那个标题;
  * 表头加粗 + 中灰底 + **冻结首行**;数据行**斑马纹**;列宽按内容估算;
  * **数字写成真数字**(能求和),并在需要时**追加一行「合计」——用公式,不是算完写死**;
  * 没有表格时:整篇按行铺进一张单列表(不报错) —— 宁可给一张朴素表,也不给空文件。

⭐ 两条值得单独写下来的:
  * "真数字":全写成文本的话,用户在 Excel 里一求和就是 0,而他要的正是一张能算的表;
  * "合计用公式":写死数字的话,用户改一格数据合计就不对了 —— 公式会自己跟着变。
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

from . import markdown_lite
from .docx_writer import OfficeWriteError, plan_total

# ⚠️ 延迟导入:没装 openpyxl 时只有"写 Excel"会失败(照 D82 的纪律)。
try:
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    XLSX_AVAILABLE = True
except ImportError:  # pragma: no cover
    XLSX_AVAILABLE = False
    Workbook = load_workbook = Alignment = Font = PatternFill = None  # type: ignore[assignment]
    Border = Side = get_column_letter = None  # type: ignore[assignment]


def _require_xlsx() -> None:
    if not XLSX_AVAILABLE:
        raise OfficeWriteError(
            "写 Excel 需要 openpyxl,当前环境里没装。请在项目虚拟环境里执行 "
            "pip install openpyxl(依赖清单见 pyproject.toml)"
        )


SHEET_NAME_BAD = re.compile(r"[\\/*?:\[\]]")
HEADER_FILL = PatternFill("solid", fgColor="D9D9D9") if XLSX_AVAILABLE else None
ZEBRA_FILL = PatternFill("solid", fgColor="F5F5F5") if XLSX_AVAILABLE else None
MAX_SHEET_ROWS = 5000
MAX_SHEET_COLS = 60


@dataclass
class XlsxFacts:
    sheets: list[str] = field(default_factory=list)
    headers: list[list[str]] = field(default_factory=list)
    rows: int = 0
    has_total: bool = False


def build_xlsx(markdown: str, *, fallback_name: str = "表格") -> tuple[bytes, XlsxFacts]:
    _require_xlsx()
    document = markdown_lite.parse(markdown)
    tables: list[tuple[str, list[list[str]]]] = []
    current = ""
    for block in document.blocks:
        if block.kind == "heading":
            current = block.text
        elif block.kind == "table":
            tables.append((current, block.rows))
    if not tables:
        lines = [block.text for block in document.blocks if block.text]
        if not lines:
            raise OfficeWriteError("内容里没有任何可写进表格的东西")
        tables = [(fallback_name, [[line] for line in lines])]

    workbook = Workbook()
    workbook.remove(workbook.active)
    facts = XlsxFacts()
    used: set[str] = set()
    for index, (heading, rows) in enumerate(tables, 1):
        if len(rows) > MAX_SHEET_ROWS:
            raise OfficeWriteError("第 " + str(index) + " 张表有 " + str(len(rows)) + " 行,超过 "
                                   + str(MAX_SHEET_ROWS) + " 行上限;请拆成几个文件")
        name = _sheet_name(heading or ("表" + str(index)), used)
        sheet = workbook.create_sheet(title=name)
        facts.sheets.append(name)
        width = max(len(row) for row in rows)
        if width > MAX_SHEET_COLS:
            raise OfficeWriteError("第 " + str(index) + " 张表有 " + str(width) + " 列,太宽了")
        for row_index, row in enumerate(rows, 1):
            for column_index in range(width):
                value = row[column_index] if column_index < len(row) else ""
                cell = sheet.cell(row=row_index, column=column_index + 1, value=_coerce(value))
                cell.alignment = Alignment(vertical="center",
                                           wrap_text=(column_index > 0 and len(str(value)) > 24))
                if row_index == 1:
                    cell.font = Font(bold=True)
                    cell.fill = HEADER_FILL
                    cell.alignment = Alignment(horizontal="center", vertical="center")
                elif row_index % 2 == 0:
                    # 斑马纹:与 Word 那一侧同一个色号,隔一行一层极浅灰
                    cell.fill = ZEBRA_FILL
        total_columns = plan_total(rows) if len(rows) >= 3 else []
        if total_columns:
            last_data_row = len(rows)
            total_row = last_data_row + 1
            label = sheet.cell(row=total_row, column=1, value="合计")
            label.font = Font(bold=True)
            label.alignment = Alignment(horizontal="center", vertical="center")
            label.fill = ZEBRA_FILL
            for column_index in total_columns:
                letters = get_column_letter(column_index + 1)
                cell = sheet.cell(row=total_row, column=column_index + 1,
                                  value="=SUM(" + letters + "2:" + letters + str(last_data_row) + ")")
                cell.font = Font(bold=True)
                cell.fill = ZEBRA_FILL
                cell.alignment = Alignment(horizontal="right", vertical="center")
                cell.border = Border(top=Side(style="thin"))
            for column_index in range(1, width + 1):
                if column_index - 1 in total_columns or column_index == 1:
                    continue
                sheet.cell(row=total_row, column=column_index).fill = ZEBRA_FILL
            facts.has_total = True
        for column_index in range(width):
            letters = get_column_letter(column_index + 1)
            longest = max((_visual_len(row[column_index]) if column_index < len(row) else 0)
                          for row in rows)
            sheet.column_dimensions[letters].width = min(max(longest + 3, 9), 42)
        sheet.freeze_panes = "A2"
        facts.headers.append([str(cell) for cell in rows[0]])
        facts.rows += max(len(rows) - 1, 0)

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue(), facts


def verify_xlsx(data: bytes, facts: XlsxFacts) -> list[str]:
    """回读自检:表名、表头、合计行对不上就报问题(空列表 = 通过)。"""
    _require_xlsx()
    problems: list[str] = []
    try:
        workbook = load_workbook(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        return ["生成的 xlsx 打不开:" + type(exc).__name__ + " " + str(exc)[:80]]
    if workbook.sheetnames != facts.sheets:
        problems.append("工作表名不对:期望 " + "、".join(facts.sheets)
                        + " 实际 " + "、".join(workbook.sheetnames))
    for index, header in enumerate(facts.headers):
        if index >= len(workbook.sheetnames):
            break
        sheet = workbook[workbook.sheetnames[index]]
        got = [str(cell.value if cell.value is not None else "") for cell in sheet[1]][:len(header)]
        if [item for item in header if item] and got != header[:len(got)]:
            problems.append("第 " + str(index + 1) + " 张表的表头对不上")
    if facts.has_total:
        sheet = workbook[workbook.sheetnames[-1]]
        last = [str(cell.value if cell.value is not None else "") for cell in sheet[sheet.max_row]]
        if "合计" not in last:
            problems.append("合计行没写上")
        elif not any(value.startswith("=SUM(") for value in last):
            problems.append("合计行不是公式(用户改数据后会对不上)")
    return problems


def _sheet_name(raw: str, used: set[str]) -> str:
    name = SHEET_NAME_BAD.sub("_", str(raw or "").strip())[:28] or "表"
    candidate = name
    index = 2
    while candidate in used:
        candidate = name[:26] + "_" + str(index)
        index += 1
    used.add(candidate)
    return candidate


def _coerce(value: str) -> object:
    """看起来是数字就写成数字(Excel 才能求和);带千分位/百分号/货币符号的先去掉再判。"""
    text = str(value or "").strip()
    if not text:
        return ""
    cleaned = text.replace(",", "").replace("，", "")
    for symbol in ("¥", "￥", "$", "元"):
        cleaned = cleaned.replace(symbol, "")
    percent = cleaned.endswith("%")
    if percent:
        cleaned = cleaned[:-1]
    try:
        if re.fullmatch(r"[+-]?\d+", cleaned):
            return int(cleaned)
        if re.fullmatch(r"[+-]?\d*\.\d+", cleaned):
            return float(cleaned)
    except ValueError:
        return text
    return text


def _visual_len(text: str) -> int:
    """中文按两个字符宽算 —— 不然中文列会被压得很窄。"""
    return sum(2 if ord(char) > 0x2E80 else 1 for char in str(text or ""))


__all__ = ["XlsxFacts", "build_xlsx", "verify_xlsx", "OfficeWriteError"]
