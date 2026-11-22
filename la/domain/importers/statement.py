"""账单导入:支付宝 / 微信 的 CSV 与 XLSX。

⚠️ 两家都没有公开且稳定的导出规范,而且文件**头部有说明行、尾部有汇总行**。
所以这里刻意**不硬编码列位置**,而是:

  1. 先找到真正的表头行(含"交易时间"或"金额");
  2. 按关键词**模糊匹配**列名(交易对方/对方、商品说明/商品、金额、收/支…);
  3. 跳过汇总行与无法解析的行,并在结果里**如实报告跳过了多少**。

XLSX 的读取(M12 起)**在基础层**:la/base/extract.py 用标准库 zipfile + ElementTree 解析,
不引第三方依赖 —— 微信导出的就是 xlsx。本文件只调用它,不再自带一份。
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import datetime
from pathlib import Path

SOURCE_ALIPAY = "import:alipay"
SOURCE_WECHAT = "import:wechat"
SOURCE_CSV = "import:csv"

ENCODINGS = ("utf-8-sig", "utf-8", "gbk", "gb18030")

HEADER_HINTS = ("交易时间", "交易创建时间", "金额", "收/支")

# 字段 -> 列名关键词(按优先级;越靠前越先匹配)
COLUMN_HINTS: dict[str, tuple[str, ...]] = {
    "ts": ("交易时间", "交易创建时间", "时间"),
    "direction": ("收/支", "收支", "收入/支出"),
    "amount": ("金额(元)", "金额", "发生金额"),
    "payee": ("交易对方", "对方", "交易对象"),
    "item": ("商品说明", "商品", "交易描述", "交易内容"),
    "method": ("收/付款方式", "支付方式", "付款方式"),
    "status": ("当前状态", "交易状态", "状态"),
    "order_id": ("交易订单号", "交易单号", "订单号"),
    "merchant_order_id": ("商家订单号", "商户单号"),
    "category": ("交易分类", "交易类型", "消费类别"),
    "remark": ("备注",),
}

SKIP_MARKERS = ("汇总", "合计", "总计", "笔数", "已全部展示", "共", "说明")

_AMOUNT = re.compile(r"[-+]?\d+(?:\.\d+)?")
_TIME_PATTERNS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y年%m月%d日 %H:%M:%S",
)


@dataclass
class StatementRow:
    ts: str
    direction: str          # 支出 | 收入 | 不计收支 | 空
    amount_cents: int
    payee: str | None = None
    item: str | None = None
    method: str | None = None
    status: str | None = None
    order_id: str | None = None
    merchant_order_id: str | None = None
    category: str | None = None
    remark: str | None = None
    line_number: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def is_expense(self) -> bool:
        return self.direction not in ("收入", "不计收支") and self.amount_cents > 0


@dataclass
class StatementParseResult:
    source: str
    rows: list[StatementRow] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)
    header_line: int = 0
    total_lines: int = 0

    @property
    def expenses(self) -> list[StatementRow]:
        return [row for row in self.rows if row.is_expense]

    @property
    def incomes(self) -> list[StatementRow]:
        """收入行(M9)。支付宝/微信的「收/支」列写了「收入」的那些。"""
        return [row for row in self.rows if row.direction == "收入" and row.amount_cents > 0]

    @property
    def importable(self) -> list[StatementRow]:
        """真正会入库的行:支出 + 收入。

        ⚠️ 「不计收支」的行(转账、理财申赎、余额宝转出)**仍然跳过** ——
        它们既不是消费也不是收入,记进来会让"花了多少"直接失真。
        跳过的数量会在导入报告里如实说明,不静默吞掉。
        """
        return [row for row in self.rows if row.amount_cents > 0 and row.direction != "不计收支"]

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "header_line": self.header_line,
            "total_lines": self.total_lines,
            "parsed": len(self.rows),
            "expenses": len(self.expenses),
            "skipped": len(self.skipped),
            "skipped_samples": self.skipped[:5],
        }


# --------------------------------------------------------------------------
# 读文件
# --------------------------------------------------------------------------
def read_text(path: str | Path) -> str:
    data = Path(path).read_bytes()
    for encoding in ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def read_rows(path: str | Path) -> list[list[str]]:
    """CSV 或 XLSX 都能读。返回原始二维表。"""
    target = Path(path)
    if target.suffix.lower() in (".xlsx", ".xlsm"):
        return read_xlsx(target)
    text = read_text(target)
    # 支付宝/微信的 CSV 都可能是制表符分隔
    sample = text[:4096]
    delimiter = "\t" if sample.count("\t") > sample.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    return [[cell.strip() for cell in row] for row in reader]


def read_xlsx(path: str | Path) -> list[list[str]]:
    """XLSX 读取 —— **实现只有一份**,在基础层(la/base/extract.py,M12)。

    以前这段逻辑写死在本文件里("微信导出的就是 xlsx,不值得为它引依赖"),
    于是"读一张表"这件事在别的域要再抄一遍。M12 把它提到基础层的抽取层:
    账单导入、素材阅读、file(action=read) 走的是同一份实现。
    """
    from ...base.extract import read_xlsx_rows

    return read_xlsx_rows(path)



# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------
def detect_source(rows: list[list[str]], header_line: int, fallback: str | None = None) -> str:
    if fallback:
        return fallback
    head = " ".join(" ".join(row) for row in rows[: max(header_line, 1) + 1])
    if "支付宝" in head:
        return SOURCE_ALIPAY
    if "微信" in head:
        return SOURCE_WECHAT
    return SOURCE_CSV


def find_header(rows: list[list[str]]) -> int | None:
    for index, row in enumerate(rows[:30]):
        joined = "".join(row)
        if sum(1 for hint in HEADER_HINTS if hint in joined) >= 2:
            return index
    return None


def map_columns(header: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for field_name, hints in COLUMN_HINTS.items():
        for hint in hints:
            found = next(
                (i for i, cell in enumerate(header) if hint in (cell or "")),
                None,
            )
            if found is not None:
                mapping[field_name] = found
                break
    return mapping


def parse_amount(text: str) -> int | None:
    """'¥38.00' / '38.00' / '-38' -> 3800 分。"""
    cleaned = (text or "").replace(",", "").replace("￥", "").replace("¥", "").strip()
    if not cleaned:
        return None
    match = _AMOUNT.search(cleaned)
    if not match:
        return None
    # ⚠️ 不能用 float 中转(2026-10-18 审计):`round(1.005 * 100)` 是 100 而不是 101,
    #    导入会比界面少 1 分。金额的唯一口径是 Decimal + ROUND_HALF_UP(与 web 表单一致)。
    try:
        value = Decimal(match.group(0))
        return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return None


def parse_time(text: str) -> str | None:
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    for pattern in _TIME_PATTERNS:
        try:
            return datetime.strptime(cleaned, pattern).isoformat(timespec="seconds")
        except ValueError:
            continue
    return None


def _is_skip_row(row: list[str]) -> bool:
    joined = "".join(row)
    if not joined.strip():
        return True
    return any(marker in joined for marker in SKIP_MARKERS) and not parse_time(
        row[0] if row else ""
    )


def parse_statement(path: str | Path, *, source: str | None = None) -> StatementParseResult:
    rows = read_rows(path)
    header_line = find_header(rows)
    if header_line is None:
        return StatementParseResult(
            source=source or SOURCE_CSV, skipped=[(0, "找不到表头行")], total_lines=len(rows)
        )
    header = rows[header_line]
    columns = map_columns(header)
    resolved_source = detect_source(rows, header_line, source)
    result = StatementParseResult(
        source=resolved_source, header_line=header_line + 1, total_lines=len(rows)
    )

    def cell(row: list[str], name: str) -> str:
        index = columns.get(name)
        if index is None or index >= len(row):
            return ""
        return (row[index] or "").strip()

    for offset, row in enumerate(rows[header_line + 1:], start=header_line + 2):
        if _is_skip_row(row):
            continue
        ts = parse_time(cell(row, "ts"))
        amount = parse_amount(cell(row, "amount"))
        if ts is None or amount is None:
            result.skipped.append((offset, "时间或金额无法解析: " + "|".join(row)[:80]))
            continue
        result.rows.append(
            StatementRow(
                ts=ts,
                direction=cell(row, "direction"),
                amount_cents=abs(amount),
                payee=cell(row, "payee") or None,
                item=cell(row, "item") or None,
                method=cell(row, "method") or None,
                status=cell(row, "status") or None,
                order_id=cell(row, "order_id") or None,
                merchant_order_id=cell(row, "merchant_order_id") or None,
                category=cell(row, "category") or None,
                remark=cell(row, "remark") or None,
                line_number=offset,
            )
        )
    return result
