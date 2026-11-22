"""导出为 Beancount 文本 —— 既是数据退出保险,也符合 Isolation of Inputs 哲学。

Beancount 期望的形态:

    2026-10-08 * "老张牛肉面" "午饭"
      Expenses:餐饮     38.00 CNY
      Assets:支付宝    -38.00 CNY

这正是我们库里已经存的符号约定(支出腿为正、资产腿为负),所以导出是**直译**而不是换算。
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

ACCOUNT_PREFIX = {
    "asset": "Assets",
    "liability": "Liabilities",
    "income": "Income",
    "expense": "Expenses",
    "equity": "Equity",
}

HEADER = """; 由家庭助手导出 —— 只读快照,可用 bean-check / fava 校验
; 符号约定:支出腿为正、资产腿为负(标准复式记账)"""


def account_full_name(kind: str, name: str) -> str:
    prefix = ACCOUNT_PREFIX.get(kind, "Equity")
    return prefix + ":" + str(name).replace(" ", "_")


def format_amount(cents: int) -> str:
    return str((Decimal(int(cents)) / Decimal(100)).quantize(Decimal("0.01")))


def export_beancount(
    conn: sqlite3.Connection,
    *,
    start: str | None = None,
    end: str | None = None,
    statuses: tuple[str, ...] = ("confirmed",),
) -> str:
    """导出 [start, end) 区间内的交易。默认只导已确认的。"""
    placeholders = ",".join("?" for _ in statuses)
    sql = (
        "SELECT id, ts, payee, narration, status FROM app_txns_live"
        " WHERE status IN (" + placeholders + ")"
    )
    params: list[object] = list(statuses)
    if start:
        sql += " AND ts >= ?"
        params.append(start)
    if end:
        sql += " AND ts < ?"
        params.append(end)
    sql += " ORDER BY ts, id"

    accounts: dict[tuple[str, str], str] = {}
    for row in conn.execute("SELECT name, kind FROM app_accounts ORDER BY id").fetchall():
        accounts[(row["kind"], row["name"])] = account_full_name(row["kind"], row["name"])

    lines: list[str] = [HEADER]
    if start or end:
        lines.append("; 区间:" + (start or "-inf") + " .. " + (end or "+inf"))

    txns = conn.execute(sql, params).fetchall()
    if txns:
        lines.append("")
        lines.append("; ---- 账户声明 ----")
        for (kind, name), full in accounts.items():
            if kind in ("expense", "income"):
                continue  # 收支类账户由 auto_accounts 插件处理即可
            lines.append(("1970-01-01 open " + full).ljust(48) + "CNY")

    for txn in txns:
        postings = conn.execute(
            "SELECT p.amount_cents, a.name AS account, a.kind AS kind, c.name AS category"
            " FROM app_postings p JOIN app_accounts a ON a.id = p.account_id"
            " LEFT JOIN app_categories c ON c.id = p.category_id"
            " WHERE p.txn_id = ? ORDER BY p.id",
            (txn["id"],),
        ).fetchall()
        payee = txn["payee"] or ""
        narration = txn["narration"] or ""
        flag = "*" if txn["status"] == "confirmed" else "!"
        lines.append("")
        lines.append(txn["ts"][:10] + " " + flag + ' "' + payee + '" "' + narration + '"')
        for posting in postings:
            if posting["kind"] == "expense" and posting["category"]:
                full = account_full_name("expense", posting["category"])
            else:
                full = accounts.get(
                    (posting["kind"], posting["account"]),
                    account_full_name(posting["kind"], posting["account"]),
                )
            amount = format_amount(int(posting["amount_cents"]))
            lines.append("  " + full.ljust(30) + " " + amount.rjust(10) + " CNY")

    lines.append("")
    lines.append("; 共 " + str(len(txns)) + " 笔交易")
    return "\n".join(lines) + "\n"
