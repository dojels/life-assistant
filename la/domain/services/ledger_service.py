"""记账服务:业务规则唯一允许居住的地方。

三条不变量(M4.0 的验收):
  1. **每笔交易的 posting 之和必须为零** —— 在 Python 层校验,SQL 约束表达不了它;
  2. **领域写入与发件箱事件同事务** —— 不会出现"账记了但记忆不知道";
  3. **助手写入的一律是 draft**(直写开关打开时才是 confirmed)—— "AI 不直接写账本"的落地方式。

符号约定:标准复式记账 —— 支出腿为**正**,资产腿为**负**,两者之和为零。
"这个月花了多少" = 对 kind='expense' 的账户的 posting 求和。

M9 起同一套代码也管**收入**(kind='income' 的账户腿为负,资产腿为正):
"这个月收了多少" = 对 kind='income' 的 posting 求和再取相反数。
方向由 direction 字段决定,**不靠金额正负** —— 让模型判断符号是自找麻烦。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from ...memory.archive import now_iso
from ...storage import db
from .. import ownership
from ..categories import CATEGORY_ALIASES, CategoryAdvice, CategoryAdvisor
from ..dedup import file_fingerprint, find_duplicate
from ..importers.statement import SOURCE_CSV, parse_statement
from ..receipts import DOMAIN_LEDGER, Receipt, ReceiptError, ReceiptLog
from ..rules import RuleEngine, Subject
from ..undo import UndoError, UndoLog
from ..schemas.ledger import (
    ExpenseCreate,
    ExpenseQuery,
    ExpenseQueryResult,
    ExpenseRead,
    GroupRow,
    TxnCreate,
)

# 撤销日志里 kind 的取值。撤销是**按类型分流**的:不同类别的反向操作不一样。
KIND_SOFT_DELETE = "ledger.soft_delete"

# 统一写入回执的 kind(M7)。一本账只有一种回执类型,反向操作由 undo_plan 里的
# "之前是什么状态"描述 —— 新建/确认/作废/删除都能用同一个 reverter 退回。
KIND_RECEIPT_TXN = "ledger.txn"
KIND_RECEIPT_IMPORT = "ledger.import"
# 新建分类也是一次写入:它改了账本的维度,而且和别的写入一样要能撤销。
KIND_RECEIPT_CATEGORY = "ledger.category"

# 人话标签:状态名 -> 中文
STATUS_LABELS = {"draft": "草稿", "confirmed": "已入账", "void": "已作废", "deleted": "已删除"}
DEFAULT_ASSET_ACCOUNT = "支付宝"
EXPENSE_ACCOUNT = "支出"
INCOME_ACCOUNT = "收入"
DIRECTION_LABELS = {"expense": "支出", "income": "收入"}
# 来源 -> 人话。**展示用**:页面不该把 ui / chat / import:alipay 这种内部标识直接摊给用户。
SOURCE_LABELS = {
    "ui": "界面记账",
    "chat": "对话记账",
    "manual": "手动",
    "import": "账单导入",
    "import:alipay": "支付宝账单",
    "import:wechat": "微信账单",
    "import:csv": "CSV 导入",
}
# 方向 -> 它挂在哪个账户腿上。支出腿挂在 "支出" 账户,收入腿挂在 "收入" 账户。
DIRECTION_ACCOUNTS = {"expense": EXPENSE_ACCOUNT, "income": INCOME_ACCOUNT}

# ⚠️ 别名表搬去了 domain/categories.py(分类候选与解析用的是同一份),这里只做**转发**,
#    免得两个模块各持一份、迟早对不上。


class LedgerError(RuntimeError):
    """记账域的领域错误(会变成 HTTP 400 / 工具的可操作错误信息)。"""


class UnknownAccountError(LedgerError):
    pass


class UnknownCategoryError(LedgerError):
    pass


class UnbalancedTransactionError(LedgerError):
    pass


@dataclass
class ImportReport:
    """导入结果。**如实报告跳过了什么** —— 静默跳过比报错更危险。"""

    source: str
    file_name: str
    file_sha256: str
    already_imported: bool = False
    batch_id: int | None = None
    parsed: int = 0
    imported: int = 0
    # 其中有多少笔是收入(M9)。报告要能回答"这次导入把收支各记了多少"。
    imported_income: int = 0
    skipped_duplicate: int = 0
    skipped_fuzzy: int = 0
    skipped_failed: int = 0
    rule_hits: int = 0
    fuzzy_candidates: list[dict[str, Any]] = field(default_factory=list)
    #: 本次是否只是预演(没有真的写入)。模型/界面必须能区分“说将导入”与“已导入”。
    dry_run: bool = False
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "file": self.file_name,
            "file_sha256": self.file_sha256[:16],
            "already_imported": self.already_imported,
            "batch_id": self.batch_id,
            "parsed": self.parsed,
            "imported": self.imported,
            "dry_run": bool(self.dry_run),
            "imported_income": self.imported_income,
            "skipped_duplicate": self.skipped_duplicate,
            "skipped_fuzzy": self.skipped_fuzzy,
            "skipped_failed": self.skipped_failed,
            "rule_hits": self.rule_hits,
            "fuzzy_candidates": self.fuzzy_candidates[:5],
            "note": self.note,
        }

    def summary(self) -> str:
        if self.already_imported:
            return "该文件已导入过(" + self.file_sha256[:12] + "),未重复入账。"
        income_note = (
            "(其中收入 " + str(self.imported_income) + " 笔)" if self.imported_income else ""
        )
        return (
            ("【预演,未写入】" if self.dry_run else "")
            + "导入 " + self.file_name + ":新增 " + str(self.imported) + " 笔" + income_note + ","
            "重复跳过 " + str(self.skipped_duplicate) + " 笔,"
            "存疑待确认 " + str(self.skipped_fuzzy) + " 笔,"
            "无法解析 " + str(self.skipped_failed) + " 行;"
            "规则命中 " + str(self.rule_hits) + " 次。"
        )


def format_cents(amount_cents: int) -> str:
    """分 -> 中文金额。回执文案与前端都用它,不各自拼一遍。"""
    cents = int(amount_cents or 0)
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    yuan, fen = divmod(cents, 100)
    if fen:
        return sign + str(yuan) + "." + str(fen).rjust(2, "0") + " 元"
    return sign + str(yuan) + " 元"


def _parse_ts(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(ts)
    except ValueError as exc:
        raise LedgerError("时间格式必须是 ISO8601: " + str(ts)) from exc


def period_bounds(period: str, now: datetime) -> tuple[str, str]:
    """把 2026-10 / 2026-Q4 / 2026 / 2026-10-01 / a..b / 今天 / 本月 / 上月 解析成 [start, end)。"""
    text = (period or "").strip()
    if not text:
        raise LedgerError("必须给出统计周期")

    if ".." in text:
        left, _, right = text.partition("..")
        raw_end = right.strip()
        start = _parse_ts(left.strip())
        end = _parse_ts(raw_end)
        # ⚠️ schema 的示例就是 `2026-10-01..2026-10-31`(见 ExpenseQuery.period):
        #    用户写的是“到 10 月 31 日”,不是“到 10 月 31 日 0 点”。所以**只有日期**的
        #    右端按“含当天”处理(加一天),带时刻的右端仍按半开区间原样使用。
        #    以前一律原样返回,10 月 31 日整整一天都不统计,而且没有任何提示。
        try:
            if len(raw_end) <= 10 and "T" not in raw_end and " " not in raw_end:
                end = end + timedelta(days=1)
        except OverflowError as exc:
            raise LedgerError("时间区间超出支持范围: " + text) from exc
        # 边界统一成与 now 同一个时区,避免 naive 字符串和库里 `+08:00` 的 ts 做字符串比较。
        if now.tzinfo is not None:
            if start.tzinfo is None:
                start = start.replace(tzinfo=now.tzinfo)
            if end.tzinfo is None:
                end = end.replace(tzinfo=now.tzinfo)
        if end <= start:
            raise LedgerError("时间区间的结束必须晚于开始: " + text)
        return start.isoformat(), end.isoformat()

    # ⚠️ 2026-11-17 修:模型问「今天」时最自然的写法就是单个日期(2026-11-16),
    #    而这里以前只认月/季/年/区间/今天 —— 于是它抛 LedgerError,被注册表包成
    #    HANDLER_ERROR,自省报表就把它记成"工具真故障"(每天 22:00 的日报各一次)。
    #    用户说「单日」不是歧义,就直接按 [当天 00:00, 次日 00:00) 收下。
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        try:
            day = datetime.fromisoformat(text)
        except ValueError as exc:
            raise LedgerError("日期不合法(要 YYYY-MM-DD 且真实存在): " + text) from exc
        if now.tzinfo is not None and day.tzinfo is None:
            day = day.replace(tzinfo=now.tzinfo)
        try:
            return day.isoformat(), (day + timedelta(days=1)).isoformat()
        except OverflowError as exc:
            raise LedgerError("时间区间超出支持范围: " + text) from exc

    if len(text) == 7 and text[4] == "-" and text[5:].isdigit():
        year, month = int(text[:4]), int(text[5:])
        if not 1 <= month <= 12:
            raise LedgerError("月份必须在 1..12")
        try:
            start = datetime(year, month, 1, tzinfo=now.tzinfo)
            end = datetime(year + (month // 12), (month % 12) + 1, 1, tzinfo=now.tzinfo)
        except (ValueError, OverflowError) as exc:
            raise LedgerError("年份超出支持范围(1..9999): " + text) from exc
        return start.isoformat(), end.isoformat()

    if len(text) == 7 and text[4] == "-" and text[5] in "Qq" and text[6].isdigit():
        year, quarter = int(text[:4]), int(text[6])
        if not 1 <= quarter <= 4:
            raise LedgerError("季度只能是 Q1..Q4")
        start_month = (quarter - 1) * 3 + 1
        try:
            start = datetime(year, start_month, 1, tzinfo=now.tzinfo)
            end_month = start_month + 3
            if end_month > 12:
                end = datetime(year + 1, end_month - 12, 1, tzinfo=now.tzinfo)
            else:
                end = datetime(year, end_month, 1, tzinfo=now.tzinfo)
        except (ValueError, OverflowError) as exc:
            raise LedgerError("年份超出支持范围(1..9999): " + text) from exc
        return start.isoformat(), end.isoformat()

    if len(text) == 4 and text.isdigit():
        year = int(text)
        try:
            return (
                datetime(year, 1, 1, tzinfo=now.tzinfo).isoformat(),
                datetime(year + 1, 1, 1, tzinfo=now.tzinfo).isoformat(),
            )
        except (ValueError, OverflowError) as exc:
            raise LedgerError("年份超出支持范围(1..9999): " + text) from exc

    if text in ("本月", "这个月"):
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = (start + timedelta(days=32)).replace(day=1)
        return start.isoformat(), end.isoformat()

    if text in ("上月", "上个月"):
        this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start = (this_month - timedelta(days=1)).replace(day=1)
        return start.isoformat(), this_month.isoformat()

    if text == "今天":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.isoformat(), (start + timedelta(days=1)).isoformat()

    raise LedgerError(
        "无法识别的周期格式: " + text
        + "(试 2026-10 / 2026-Q4 / 2026 / 2026-10-01 / "
        "2026-10-01..2026-10-31 / 今天 / 本月)"
    )


class LedgerService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        audit: Callable[..., None] | None = None,
        now_provider: Callable[[], datetime] | None = None,
        tzid: str = "Asia/Shanghai",
        receipts: ReceiptLog | None = None,
        member_id: int | None = None,
    ) -> None:
        self.conn = conn
        self.member_id = member_id
        self._audit = audit
        self._now = now_provider
        self.tzid = tzid
        self.undo = UndoLog(conn)
        # 统一写入回执(M7)。给了就在每次写入时记一条,前端据此显示"写了什么 + 撤销"。
        self.receipts = receipts

    def _current_time(self) -> datetime:
        # ⚠️ 兜底也必须用**配置的**时区(2026-10-18 审计修):以前是
        #    datetime.now().astimezone()(操作系统时区),而其余三个服务用配置时区 ——
        #    同一台机器上"本月"的起止因此可能不同。
        if self._now is not None:
            return self._now()
        return datetime.now(ZoneInfo(self.tzid))

    def _owner_clause(self, column: str = "owner_id") -> tuple[str, list[object]]:
        # 唯一实现:domain/ownership.py(以前四个服务各写一份 SQL 片段,
        # calendar 那份还少了 column 参数,已经分叉)。
        return ownership.owner_filter(getattr(self, "member_id", None), column)

    def _stamp_owner(self, txn_id: int) -> None:
        """按当前共享模式给新交易写 owner_id(全家 = 不写,NULL)。"""
        owner = ownership.owner_for_new(
            self.conn, ownership.DOMAIN_LEDGER, getattr(self, "member_id", None)
        )
        if owner is not None:
            self.conn.execute(
                "UPDATE app_txns SET owner_id = ? WHERE id = ?", (owner, int(txn_id))
            )

    # -- 写入回执(M7 项目级机制)------------------------------------------
    def _record_txn_receipt(
        self,
        txn_id: int,
        *,
        status: str,
        before: str | None = None,
        amount_cents: int = 0,
        ts: str | None = None,
        payee: str | None = None,
        narration: str | None = None,
        category_name: str | None = None,
        account_name: str | None = None,
        source: str = "agent",
        actor: str = "agent",
        session_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> int | None:
        """记一条账本回执。**不 commit** —— 与领域写入同一个事务。

        未传的字段(对方/分类/金额)从库里补 —— 调用点就不必每人拼一遍。
        """
        if self.receipts is None:
            return None
        row = self.conn.execute(
            "SELECT * FROM app_txns WHERE id = ?", (int(txn_id),)
        ).fetchone()
        if row is None:
            return None
        if payee is None:
            payee = row["payee"]
        if narration is None:
            narration = row["narration"]
        direction, leg_amount, leg_category, leg_account = self.txn_legs(int(txn_id))
        if amount_cents <= 0:
            amount_cents = leg_amount
        if category_name is None:
            category_name = leg_category
        if account_name is None:
            account_name = leg_account

        amount_text = format_cents(amount_cents)
        kind_label = "" if direction == "expense" else "收入"
        if status == "deleted":
            label = "删掉一笔" + kind_label + " #" + str(txn_id) + " " + amount_text
        elif before is None or before == status:
            label = "记一笔" + kind_label + " " + amount_text
        else:
            label = (
                "这笔 " + amount_text + " 从「" + STATUS_LABELS.get(before, before)
                + "」改成「" + STATUS_LABELS.get(status, status) + "」"
            )
        for piece in (payee, category_name, narration):
            text = (str(piece).strip() if piece else "")
            if text:
                label += " · " + text[:24]
                break
        if account_name:
            label += "(" + str(account_name) + ")"

        plan: dict[str, Any] = {
            "type": "txn",
            "txn_id": int(txn_id),
            "before": before,
            "status": status,
        }
        if extra:
            plan.update(extra)
        return self.receipts.record(
            kind=KIND_RECEIPT_TXN,
            domain=DOMAIN_LEDGER,
            label=label,
            detail={
                "txn_id": int(txn_id),
                "ts": ts or row["ts"],
                "payee": payee,
                "narration": narration,
                "amount_cents": int(amount_cents),
                "amount_text": amount_text,
                "category": category_name,
                "account": account_name,
                "status": status,
                "before_status": before,
                "direction": direction,
                "direction_label": DIRECTION_LABELS.get(direction, direction),
                "source": source,
                "op": str((extra or {}).get("op") or ("delete" if status == "deleted" else "record")),
            },
            undo_plan=plan,
            actor=actor,
            session_id=session_id,
        )

    # -- 撤销(由统一回执调用)--------------------------------------------
    def revert_txn(self, receipt: Receipt) -> str:
        """把一条回执对应的事务退回写入前:新建 -> 删掉;改状态 -> 改回去。

        ⚠️ 这里**不记新回执** —— 撤销本身不是"一次新的写入",它只是把账退回去。
        撤回这件事记在 ops_receipts.undone_at 与 ops_audit 里(两处都只追加)。
        """
        if self.receipts is None:
            raise ReceiptError("账本没有接上回执日志,无法撤销")
        plan = receipt.undo_plan or {}
        op = str(plan.get("op") or "")
        if str(plan.get("type") or "") == "category":
            return self.revert_category(receipt)
        if op == "correct":
            return self._revert_correction(receipt, plan)
        if op == "link_refund":
            return self._revert_refund_link(receipt, plan)
        if op == "ledger_import" or str(plan.get("type") or "") == "ledger_import":
            return self._revert_import(receipt, plan)
        return self._revert_state(plan)

    # -- 查询辅助 ---------------------------------------------------------
    def account_id(self, name: str) -> int:
        row = self.conn.execute(
            "SELECT id FROM app_accounts WHERE name = ? AND archived = 0", (name,)
        ).fetchone()
        if row is None:
            known = [item[0] for item in self._list_names("app_accounts")]
            raise UnknownAccountError("没有这个账户: " + name + ";可用账户:" + "/".join(known))
        return int(row["id"])

    def category_id(self, name: str, kind: str | None = None) -> int | None:
        """分类名 -> id。kind 给了就**只在那一类里找**(支出分类和收入分类可能同名)。"""
        if not name:
            return None
        if kind:
            row = self.conn.execute(
                "SELECT id FROM app_categories WHERE name = ? AND kind = ? AND archived = 0",
                (name, kind),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT id FROM app_categories WHERE name = ? AND archived = 0", (name,)
            ).fetchone()
        if row is None:
            known = self.list_categories(kind)
            raise UnknownCategoryError("没有这个分类: " + name + ";可用分类:" + "/".join(known))
        return int(row["id"])

    def txn_legs(self, txn_id: int) -> tuple[str, int, str | None, str | None]:
        """一笔账的「方向 / 金额(分,恒正)/ 分类 / 资金账户」。**全项目唯一口径。**

        支出:分类挂在 kind='expense' 的腿上,资金账户是资产/负债那条;
        收入:分类挂在 kind='income' 的腿上,资金账户同样是资产那条。
        认不出来的老数据一律当支出 —— 回执渲染失败不该把写入本身也拖失败。
        """
        rows = self.conn.execute(
            "SELECT p.amount_cents, a.kind AS account_kind, a.name AS account,"
            " c.name AS category FROM app_postings p"
            " JOIN app_accounts a ON a.id = p.account_id"
            " LEFT JOIN app_categories c ON c.id = p.category_id"
            " WHERE p.txn_id = ? ORDER BY p.id",
            (int(txn_id),),
        ).fetchall()
        direction = "expense"
        amount = 0
        category = None
        account = None
        for row in rows:
            kind = str(row["account_kind"])
            if kind in ("expense", "income"):
                direction = "expense" if kind == "expense" else "income"
                amount = abs(int(row["amount_cents"]))
                category = row["category"]
            elif kind in ("asset", "liability"):
                account = row["account"]
        return direction, amount, category, account

    def leg_ids(self, txn_id: int) -> tuple[int | None, int | None]:
        """(分类腿的 posting id, 资金腿的 posting id)。纠正金额/分类时**只动这两条**。"""
        rows = self.conn.execute(
            "SELECT p.id, a.kind AS account_kind FROM app_postings p"
            " JOIN app_accounts a ON a.id = p.account_id WHERE p.txn_id = ? ORDER BY p.id",
            (int(txn_id),),
        ).fetchall()
        category_leg = None
        asset_leg = None
        for row in rows:
            kind = str(row["account_kind"])
            if kind in ("expense", "income"):
                category_leg = int(row["id"])
            elif kind in ("asset", "liability") and asset_leg is None:
                asset_leg = int(row["id"])
        return category_leg, asset_leg

    def _list_names(self, table: str) -> list[tuple[str]]:
        return self.conn.execute(
            "SELECT name FROM " + table + " WHERE archived = 0 ORDER BY id"
        ).fetchall()

    # -- 写 ---------------------------------------------------------------
    def record_expense(
        self,
        payload: ExpenseCreate | dict[str, Any],
        *,
        actor: str = "agent",
        session_id: str | None = None,
        idempotency_key: str | None = None,
        status: str = "draft",
    ) -> ExpenseRead:
        """记一笔账(支出或收入)。领域写入与 outbox 事件在**同一个事务**里。

        方向由 payload.direction 决定:
            expense -> 支出腿 +amount,资产腿 -amount
            income  -> 资产腿 +amount,收入腿 -amount
        两条腿相加恒为零,写入前在 Python 层校验。
        """
        if not isinstance(payload, TxnCreate):
            try:
                payload = TxnCreate.model_validate(payload)
            except ValidationError as exc:
                raise LedgerError("参数不合法: " + exc.errors()[0]["msg"]) from exc

        if status not in ("draft", "confirmed"):
            raise LedgerError("状态只能是 draft 或 confirmed")
        direction = payload.direction if payload.direction in DIRECTION_ACCOUNTS else "expense"

        moment = _parse_ts(payload.ts) if payload.ts else self._current_time()
        ts = moment.isoformat(timespec="seconds")
        payee = (payload.payee or "").strip() or None
        narration = (payload.narration or "").strip() or None

        asset_account = self.account_id(payload.account or DEFAULT_ASSET_ACCOUNT)

        # ⭐ "先规则、规则未命中再靠模型"(能力域 §9.2):用户没说分类时,
        # 先让规则引擎试一次 —— 否则"人工纠正回写成规则"只对导入生效,对话记账永远学不会。
        category_name = payload.category or ""
        rule_engine = RuleEngine(self.conn)
        applied_rule = None
        if not category_name:
            applied_rule = rule_engine.match(
                Subject(
                    peer=payee,
                    item=narration,
                    method=payload.account,
                    amount_cents=payload.amount_cents,
                    ts=ts,
                )
            )
            if applied_rule is not None:
                category_name = rule_engine.category_name(applied_rule.target_category_id) or ""
        # 分类必须属于这个方向:把"工资"填进支出、把"餐饮"填进收入都是错的。
        # 实际校验统一在 _create_txn(strict_category=True) 里做(唯一写入原语)。

        # ⚠️ 默认键必须**每次唯一**(2026-10-18 审计修):它以前是
        #    direction+ts+金额+对方,而 ts 只到秒 —— 同一秒里记两笔一模一样的账,
        #    第二笔会静默命中第一笔的 id(工具还回 recorded=True,模型据此说"两笔都记好了")。
        #    幂等只该由调用方**显式**给键来表达(网页的 _idem、导入的 import:...)。
        key = idempotency_key or (
            direction + ":" + ts + ":" + str(payload.amount_cents) + ":" + (payee or "-")
            + ":" + uuid.uuid4().hex[:10]
        )
        existing = self.conn.execute(
            "SELECT id FROM app_txns_live WHERE idempotency_key = ?", (key,)
        ).fetchone()
        if existing is not None:
            return self.read(int(existing["id"]))

        # 交易 + 两腿 + outbox + 回执在**同一个** db.transaction() 里:
        # 失败时四样一起回滚,不再靠每个调用点自己记得 rollback(2026-10-18 架构收口)。
        with db.transaction(self.conn):
            txn_id = self._create_txn(
                ts=ts, payee=payee, narration=narration, source=payload.source,
                status=status, amount_cents=payload.amount_cents,
                category_name=category_name or None, account_id=asset_account,
                direction=direction, idempotency_key=key,
                outbox_key="outbox:" + key,
                category_label=payload.category or category_name or None,
                strict_category=True,
            )
            self._record_txn_receipt(
                txn_id,
                ts=ts,
                payee=payee,
                narration=narration,
                amount_cents=payload.amount_cents,
                category_name=category_name,
                account_name=payload.account or DEFAULT_ASSET_ACCOUNT,
                status=status,
                before=None,
                source=payload.source,
                actor=actor,
                session_id=session_id,
            )

        if applied_rule is not None:
            rule_engine.record_hit(applied_rule.id)
            self.conn.commit()

        if self._audit is not None:
            self._audit(
                actor=actor,
                action="ledger.txn.create",
                target="txn:" + str(txn_id),
                detail={
                    "amount_cents": payload.amount_cents,
                    "direction": direction,
                    "category": payload.category or category_name or None,
                    "status": status,
                    "idempotency_key": key,
                },
                session_id=session_id,
            )
            self.conn.commit()

        return self.read(txn_id)

    def confirm(self, txn_id: int, *, actor: str = "user", session_id: str | None = None) -> ExpenseRead:
        return self._set_status(txn_id, "confirmed", actor=actor, session_id=session_id)

    def void(self, txn_id: int, *, actor: str = "user", session_id: str | None = None) -> ExpenseRead:
        return self._set_status(txn_id, "void", actor=actor, session_id=session_id)

    # -- 软删除与撤销(M6.3)-----------------------------------------------
    def soft_delete(
        self, txn_id: int, *, reason: str = "", actor: str = "user",
        session_id: str | None = None,
    ) -> ExpenseRead:
        """删掉一笔交易 —— 但只是打上 deleted_at,随时能撤销。

        ⚠️ 读路径走 app_txns_live 视图,所以删除后它就"不存在"了;
        这就是为什么删之前必须先 read() —— 删完就读不到了。
        """
        snapshot = self.read(txn_id)
        row = self.conn.execute("SELECT * FROM app_txns_live WHERE id = ?", (txn_id,)).fetchone()
        if row is None:
            raise LedgerError("交易不存在或已删除: " + str(txn_id))
        before = {key: row[key] for key in row.keys()}
        undo_id = self.undo.record(
            kind=KIND_SOFT_DELETE,
            label="删除交易 #" + str(txn_id) + " " + str(row["payee"] or "")
                  + " " + str(snapshot.amount_cents / 100) + " 元" + ("(" + reason + ")" if reason else ""),
            table_name="app_txns", row_id=txn_id, before=before,
            actor=actor, session_id=session_id,
        )
        stamp = now_iso()
        self.conn.execute(
            "UPDATE app_txns SET deleted_at = ?, version = version + 1, updated_at = ?"
            " WHERE id = ?",
            (stamp, stamp, txn_id),
        )
        # 发件箱 key 带时间戳:删->撤->再删是**两件不同的事**,不能被幂等键吃掉。
        self.conn.execute(
            "INSERT INTO ops_outbox(event_type, payload, idempotency_key, created_at)"
            " VALUES (?, ?, ?, ?)",
            ("ledger.txn.deleted",
             json.dumps({"txn_id": txn_id, "reason": reason, "undo_id": undo_id},
                        ensure_ascii=False),
             "outbox:txn:" + str(txn_id) + ":deleted:" + stamp
             + ":" + uuid.uuid4().hex[:8], stamp),
        )
        self._record_txn_receipt(
            txn_id,
            status="deleted",
            before=str(row["status"]),
            amount_cents=snapshot.amount_cents,
            payee=row["payee"],
            category_name=snapshot.category,
            actor=actor,
            session_id=session_id,
            extra={"undo_log_id": undo_id, "reason": reason},
        )
        self.conn.commit()
        if self._audit is not None:
            self._audit(
                actor=actor, action="ledger.txn.deleted", target="txn:" + str(txn_id),
                detail={"reason": reason, "undo_id": undo_id, "amount_cents": snapshot.amount_cents},
                session_id=session_id,
            )
            self.conn.commit()
        return snapshot.model_copy(update={"status": "deleted"})

    def restore(self, txn_id: int, *, actor: str = "user",
                session_id: str | None = None) -> ExpenseRead:
        """把软删除的交易放回来。"""
        clause, params = self._owner_clause()
        row = self.conn.execute(
            "SELECT id FROM app_txns WHERE id = ? AND deleted_at IS NOT NULL"
            " AND " + clause,
            (txn_id, *params),
        ).fetchone()
        if row is None:
            raise LedgerError("没有处于已删除状态的交易: " + str(txn_id))
        stamp = now_iso()
        self.conn.execute(
            "UPDATE app_txns SET deleted_at = NULL, version = version + 1, updated_at = ?"
            " WHERE id = ?",
            (stamp, txn_id),
        )
        self.conn.execute(
            "INSERT INTO ops_outbox(event_type, payload, idempotency_key, created_at)"
            " VALUES (?, ?, ?, ?)",
            ("ledger.txn.restored", json.dumps({"txn_id": txn_id}, ensure_ascii=False),
             "outbox:txn:" + str(txn_id) + ":restored:" + stamp
             + ":" + uuid.uuid4().hex[:8], stamp),
        )
        self.conn.commit()
        if self._audit is not None:
            self._audit(
                actor=actor, action="ledger.txn.restored", target="txn:" + str(txn_id),
                detail={}, session_id=session_id,
            )
            self.conn.commit()
        return self.read(txn_id)

    def undo_last(
        self, *, actor: str = "user", session_id: str | None = None
    ) -> dict[str, Any]:
        """撤销最近一次可撤销的操作。

        ⚠️ 只撤销**删除**。把"撤销"做成通用时间机器看着很美,实际会让
        "我刚才到底撤回了什么"变得无法回答 —— 宁可少做。
        """
        entries = self.undo.recent(limit=1)
        if not entries:
            raise UndoError("没有可撤销的操作")
        entry = entries[0]
        if entry.kind != KIND_SOFT_DELETE:
            raise UndoError("最近一步不是删除,不能自动撤销:" + entry.label)
        self.undo.mark_used(entry.id)  # 先占坑:用过就作废,失败则一起回滚
        result = self.restore(entry.row_id, actor=actor, session_id=session_id)
        if self._audit is not None:
            self._audit(
                actor=actor, action="ledger.undo", target="undo:" + str(entry.id),
                detail={"label": entry.label, "txn_id": entry.row_id}, session_id=session_id,
            )
            self.conn.commit()
        return {"undo_id": entry.id, "label": entry.label, "txn_id": entry.row_id,
                "restored": result.model_dump(mode="json")}

    def _set_status(
        self, txn_id: int, status: str, *, actor: str, session_id: str | None
    ) -> ExpenseRead:
        clause, params = self._owner_clause()
        row = self.conn.execute(
            "SELECT status FROM app_txns_live WHERE id = ? AND " + clause,
            (txn_id, *params),
        ).fetchone()
        if row is None:
            raise LedgerError("交易不存在: " + str(txn_id))
        before = str(row["status"])
        stamp = now_iso()
        try:
            self.conn.execute(
                "UPDATE app_txns SET status = ?, updated_at = ? WHERE id = ?",
                (status, stamp, txn_id),
            )
            # 回执写在同一个事务里:状态改了,回执一定在(反之亦然)。
            self._record_txn_receipt(
                txn_id,
                status=status,
                before=before,
                amount_cents=0,  # 0 = 让回执自己按分录算(收支两种方向都算得对)
                actor=actor,
                session_id=session_id,
            )
            # ⚠️ key 必须带时间戳(与 soft_delete 同一口径):confirm→void→confirm
            #    是**三次不同的事**,只有 (txn, status) 会让第三次撞 UNIQUE,
            #    而那时前两条 DML 已经写在打开的事务里了(2026-10-18 审计修)。
            self.conn.execute(
                "INSERT INTO ops_outbox(event_type, payload, idempotency_key, created_at)"
                " VALUES (?, ?, ?, ?)",
                (
                    "ledger.txn." + status,
                    json.dumps({"txn_id": txn_id, "status": status}, ensure_ascii=False),
                    "outbox:txn:" + str(txn_id) + ":" + status + ":" + stamp
                    + ":" + uuid.uuid4().hex[:8],
                    stamp,
                ),
            )
            self.conn.commit()
        except Exception:
            # 半截事务比失败更坏:下一次无关 commit 会把它提交掉。
            self.conn.rollback()
            raise
        if self._audit is not None:
            self._audit(
                actor=actor,
                action="ledger.txn." + status,
                target="txn:" + str(txn_id),
                detail={"from": row["status"], "to": status},
                session_id=session_id,
            )
            self.conn.commit()
        return self.read(txn_id)

    # -- 读 ---------------------------------------------------------------
    def read(self, txn_id: int) -> ExpenseRead:
        clause, params = self._owner_clause()
        txn = self.conn.execute(
            "SELECT * FROM app_txns_live WHERE id = ? AND " + clause,
            (txn_id, *params),
        ).fetchone()
        if txn is None:
            raise LedgerError("交易不存在: " + str(txn_id))
        rows = self.conn.execute(
            "SELECT p.amount_cents, a.name AS account, a.kind AS account_kind,"
            " c.name AS category"
            " FROM app_postings p JOIN app_accounts a ON a.id = p.account_id"
            " LEFT JOIN app_categories c ON c.id = p.category_id"
            " WHERE p.txn_id = ? ORDER BY p.id",
            (txn_id,),
        ).fetchall()
        direction, amount_cents, category, asset = self.txn_legs(int(txn["id"]))
        return ExpenseRead(
            id=int(txn["id"]),
            ts=txn["ts"],
            payee=txn["payee"],
            narration=txn["narration"],
            status=txn["status"],
            source=txn["source"],
            direction=direction,
            amount_cents=amount_cents,
            category=category,
            account=asset,
            postings=[(row["account"], int(row["amount_cents"])) for row in rows],
        )

    def drafts(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """待确认的草稿(带方向与金额)。**界面只渲染,不自己写 SQL。**

        ⚠️ 这段查询以前住在 web/app.py 里(2026-10-18 审计):方向映射、金额口径
           都在路由层重算了一遍,与 list_txns 迟早漂移;而且它绕过了 owner 过滤。
        """
        clause, params = self._owner_clause("t.owner_id")
        rows = self.conn.execute(
            "SELECT t.id, t.ts, t.payee, t.status, t.source,"
            " MAX(CASE WHEN a.kind = 'expense' THEN 1 ELSE 0 END) AS is_expense,"
            " SUM(CASE WHEN a.kind IN ('expense', 'income') THEN ABS(p.amount_cents)"
            "          ELSE 0 END) AS cents"
            " FROM app_txns_live t JOIN app_postings p ON p.txn_id = t.id"
            " JOIN app_accounts a ON a.id = p.account_id"
            " WHERE t.status = 'draft' AND " + clause
            + " GROUP BY t.id ORDER BY t.id DESC LIMIT ?",
            (*params, max(1, int(limit))),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["direction"] = "expense" if int(item.pop("is_expense") or 0) else "income"
            out.append(item)
        return out

    def asset_accounts(self) -> list[str]:
        """可选的资金账户(资产/负债)。界面下拉用。"""
        return [
            str(row["name"])
            for row in self.conn.execute(
                "SELECT name FROM app_accounts WHERE archived = 0"
                " AND kind IN ('asset', 'liability') ORDER BY id"
            ).fetchall()
        ]

    def query_expenses(self, query: ExpenseQuery | dict[str, Any]) -> ExpenseQueryResult:
        """全部聚合在 SQL 里做 —— 模型**不参与任何计算**(能力域 §10.7②)。

        口径(三个数一次给全,免得前端各自再算一遍):
          * expense_cents / income_cents —— 区间内的支出与收入,**不随 direction 变**;
          * net_cents = 收入 - 支出(结余,可能为负);
          * total_cents —— 按 direction 选中的那份合计(all = 收支总和)。
        """
        if not isinstance(query, ExpenseQuery):
            try:
                query = ExpenseQuery.model_validate(query)
            except ValidationError as exc:
                raise LedgerError("参数不合法: " + exc.errors()[0]["msg"]) from exc

        start, end = period_bounds(query.period, self._current_time())
        statuses = ("draft", "confirmed") if query.include_drafts else ("confirmed",)
        placeholders = ",".join("?" for _ in statuses)
        direction = query.direction if query.direction in ("expense", "income", "all") else "expense"

        dimension_sql = {
            "category": "COALESCE(c.name, '(未分类)')",
            "payee": "COALESCE(t.payee, '(无对方)')",
            "account": "a.name",
            "day": "substr(t.ts, 1, 10)",
            "month": "substr(t.ts, 1, 7)",
        }[query.dimension]

        # 统一的符号口径:支出腿取正,收入腿取相反数(收入的 posting 本来就是负的)。
        # 于是一行 posting 的 signed 永远是"这笔账在它自己那个方向上值多少钱"。
        signed = "CASE WHEN a.kind = 'expense' THEN p.amount_cents ELSE -p.amount_cents END"
        expense_expr = "CASE WHEN a.kind = 'expense' THEN p.amount_cents ELSE 0 END"
        income_expr = "CASE WHEN a.kind = 'income' THEN -p.amount_cents ELSE 0 END"
        owner_sql, owner_params = self._owner_clause("t.owner_id")
        base_sql = (
            " FROM app_postings p"
            " JOIN app_txns_live t ON t.id = p.txn_id"
            " JOIN app_accounts a ON a.id = p.account_id"
            " LEFT JOIN app_categories c ON c.id = p.category_id"
            " WHERE a.kind IN ('expense', 'income') AND t.status IN (" + placeholders + ")"
            " AND t.ts >= ? AND t.ts < ?"
            " AND " + owner_sql
        )
        params: list[Any] = [*statuses, start, end, *owner_params]
        pick_sql = "" if direction == "all" else " AND a.kind = ?"
        pick_params: list[Any] = [] if direction == "all" else [direction]

        rows = self.conn.execute(
            "SELECT " + dimension_sql + " AS key, SUM(" + signed + ") AS total,"
            " COUNT(DISTINCT t.id) AS n, SUM(" + expense_expr + ") AS expense,"
            " SUM(" + income_expr + ") AS income" + base_sql + pick_sql
            + " GROUP BY key ORDER BY total DESC",
            [*params, *pick_params],
        ).fetchall()
        totals = self.conn.execute(
            "SELECT SUM(" + expense_expr + ") AS expense, SUM(" + income_expr + ") AS income,"
            " COUNT(DISTINCT t.id) AS n" + base_sql,
            params,
        ).fetchone()
        picked = self.conn.execute(
            "SELECT COALESCE(SUM(" + signed + "), 0) AS total, COUNT(DISTINCT t.id) AS n"
            + base_sql + pick_sql,
            [*params, *pick_params],
        ).fetchone()
        samples = [
            int(item["id"])
            for item in self.conn.execute(
                "SELECT DISTINCT t.id, t.ts FROM app_postings p"
                " JOIN app_txns_live t ON t.id = p.txn_id"
                " JOIN app_accounts a ON a.id = p.account_id"
                " WHERE a.kind IN ('expense', 'income') AND t.status IN (" + placeholders + ")"
                " AND t.ts >= ? AND t.ts < ? AND " + owner_sql + pick_sql
                + " ORDER BY t.ts DESC LIMIT 5",
                [*params, *pick_params],
            ).fetchall()
        ]

        expense_cents = int(totals["expense"] or 0)
        income_cents = int(totals["income"] or 0)
        note = "只统计已确认的交易" if not query.include_drafts else "含未确认草稿"
        note += ";口径 " + {"expense": "只看支出", "income": "只看收入", "all": "收支合计"}[direction]

        # -- 逐笔明细(用户 2026-10-16 点名要的那一层)------------------------
        # ⭐ 为什么它必须存在:合计答得了"花了多少",答不了"每一天的每一笔都是什么"。
        #    实测(真实库):dimension=day 只给「10-12 合计 76 元,4 笔」——
        #    同一天那 4 笔各是什么、各多少,模型**手里根本没有**,只能如实说"给不出明细"。
        # ⚠️ 口径跟上面那套聚合**必须一致**:同一个 period_bounds、同一个状态集、
        #    同一个 direction 筛法 —— 否则"明细加起来 ≠ 合计",那是比没有明细更糟的错。
        detail_txns: list[dict[str, Any]] = []
        detail_note = ""
        # ⚠️ 只有要明细时才给 txn_total:默认形状与以前**逐字一样**(不多一个字段、
        #    也就不多一份解释负担)。总笔数在聚合里本来就有(txn_count)。
        txn_total = int(picked["n"] or 0) if query.detail else 0
        if query.detail:
            rows_detail = self.list_txns(
                period=query.period, limit=query.detail_limit,
                include_drafts=query.include_drafts, direction=direction,
            )
            # ⚠️ 总笔数**只认聚合那条路**(picked["n"],上面已经按同一口径算过):
            #    拿 len(rows_detail) 当总数,截断时会把"200"说成这一段真只有 200 笔。
            detail_txns = [
                {
                    "id": int(item["id"]),
                    "ts": str(item["ts"]),
                    "day": str(item["day"]),
                    "direction": item["direction"],
                    "amount_cents": int(item["cents"]),
                    "payee": item.get("payee"),
                    "narration": item.get("narration"),
                    "category": item.get("category"),
                    "account": item.get("account"),
                    "status": str(item["status"]),
                    "source": str(item.get("source") or ""),
                }
                for item in rows_detail
            ]
            shown = len(detail_txns)
            if shown < txn_total:
                # ⚠️ 截断了就**说清楚** —— 不许让模型拿"看到的这些"当成全部
                detail_note = ("这一段共 " + str(txn_total) + " 笔,明细只带了最近 "
                               + str(shown) + " 笔(上限 " + str(int(query.detail_limit))
                               + ");要更早的那些就缩小 period 或调大 detail_limit。")
            else:
                detail_note = "这一段一共 " + str(shown) + " 笔,全在 txns 里(时刻倒序)。"
        return ExpenseQueryResult(
            period=query.period,
            start=start,
            end=end,
            dimension=query.dimension,
            direction=direction,
            total_cents=int(picked["total"] or 0),
            expense_cents=expense_cents,
            income_cents=income_cents,
            net_cents=income_cents - expense_cents,
            # ⚠️ 笔数按 direction 口径数(默认只数支出),否则"这个月花了多少笔"
            #    会把收入也算进去 —— 那是另一个问题。
            txn_count=int(picked["n"] or 0),
            groups=[
                GroupRow(
                    key=str(row["key"]),
                    total_cents=int(row["total"] or 0),
                    txn_count=int(row["n"] or 0),
                    expense_cents=int(row["expense"] or 0),
                    income_cents=int(row["income"] or 0),
                )
                for row in rows
            ],
            txns=detail_txns,
            txn_total=txn_total,
            detail_note=detail_note,
            source="ledger",
            sample_txn_ids=samples,
            note=note,
        )

    # -- 导入 -------------------------------------------------------------
    def import_statement(
        self,
        path: str | Path,
        *,
        source: str | None = None,
        status: str = "confirmed",
        actor: str = "import",
        session_id: str | None = None,
        dry_run: bool = False,
        apply_rules: bool = True,
    ) -> "ImportReport":
        """导入一份账单(支付宝 / 微信 的 csv 或 xlsx)。

        ⭐ 两道幂等闸:文件级 file_sha256 短路 + 逐行三级去重键。
        模糊命中(L3)**只报候选、绝不自动合并** —— 宁可让用户看一眼,也不要把两笔真账合成一笔。
        """
        target = Path(path)
        data = target.read_bytes()
        digest = file_fingerprint(data)

        existing = self.conn.execute(
            "SELECT id, imported_at, reverted_at FROM app_import_batches"
            " WHERE file_sha256 = ?", (digest,)
        ).fetchone()
        if existing is not None and existing["reverted_at"]:
            # ⚠️ 撤销导入后再导同一个文件:不能拿 sha256 短路(2026-10-18 审计)。
            #    恢复那批被作废的行,而不是重新插入(插入会撞 idempotency_key)。
            stamp = now_iso()
            cursor = self.conn.execute(
                "UPDATE app_txns SET status = 'confirmed', updated_at = ?"
                " WHERE import_batch_id = ? AND status = 'void'",
                (stamp, int(existing["id"])),
            )
            self.conn.execute(
                "UPDATE app_import_batches SET reverted_at = NULL WHERE id = ?",
                (int(existing["id"]),),
            )
            self.conn.commit()
            report = ImportReport(
                source=source or SOURCE_CSV,
                file_name=target.name,
                file_sha256=digest,
                batch_id=int(existing["id"]),
                imported=int(cursor.rowcount or 0),
                note="该文件上次的导入已撤销;本次已恢复那批交易。",
            )
            return report
        if existing is not None:
            return ImportReport(
                source=source or SOURCE_CSV,
                file_name=target.name,
                file_sha256=digest,
                already_imported=True,
                batch_id=int(existing["id"]),
                note="这个文件已经导入过(sha256 相同),直接短路。",
            )

        parsed = parse_statement(target, source=source)
        engine = RuleEngine(self.conn)
        report = ImportReport(
            source=parsed.source,
            file_name=target.name,
            file_sha256=digest,
            parsed=len(parsed.rows),
            skipped_failed=len(parsed.skipped),
            dry_run=bool(dry_run),
        )

        default_account = self.account_id(DEFAULT_ASSET_ACCOUNT)
        for row in parsed.importable:
            direction = "income" if row.direction == "收入" else "expense"
            category_name = row.category
            account_name = DEFAULT_ASSET_ACCOUNT
            rule = None
            if apply_rules:
                rule = engine.match(
                    Subject(
                        peer=row.payee, item=row.item, method=row.method,
                        amount_cents=row.amount_cents, ts=row.ts,
                    )
                )
                if rule is not None:
                    category_name = engine.category_name(rule.target_category_id) or category_name
                    account_name = engine.account_name(rule.target_account_id) or account_name

            category_name = self.resolve_category(category_name)

            decision = find_duplicate(
                self.conn,
                source=parsed.source,
                ts=row.ts,
                direction=row.direction or "支出",
                amount_cents=row.amount_cents,
                peer=row.payee,
                item=row.item,
                method=row.method,
                order_id=row.order_id,
            )
            if decision.level in ("L1", "L2"):
                report.skipped_duplicate += 1
                continue
            if decision.level == "L3":
                report.skipped_fuzzy += 1
                report.fuzzy_candidates.append(
                    {
                        "line": row.line_number,
                        "ts": row.ts,
                        "amount_cents": row.amount_cents,
                        "payee": row.payee,
                        "candidates": decision.fuzzy_candidates,
                    }
                )
                continue

            if dry_run:
                report.imported += 1
                continue

            self._insert_txn(
                ts=row.ts,
                payee=row.payee,
                narration=row.item or row.remark,
                source=parsed.source,
                status=status,
                amount_cents=row.amount_cents,
                category_name=category_name,
                account_id=self.account_id(account_name),
                dedup_key=decision.key,
                idempotency_key="import:" + digest[:16] + ":" + str(row.line_number),
                direction=direction,
            )
            report.imported += 1
            if direction == "income":
                report.imported_income += 1
            if rule is not None:
                engine.record_hit(rule.id)
                report.rule_hits += 1

        if not dry_run:
            cursor = self.conn.execute(
                "INSERT INTO app_import_batches(source, file_name, file_sha256, imported_at,"
                " row_count, imported_count, skipped_count, failed_count)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    parsed.source, target.name, digest, now_iso(), len(parsed.rows),
                    report.imported, report.skipped_duplicate + report.skipped_fuzzy,
                    report.skipped_failed,
                ),
            )
            report.batch_id = int(cursor.lastrowid or 0)
            # ⚠️ 批次行仍然**最后**建(中途崩掉时 sha256 不会被登记,重跑还能导),
            #    但每笔账要补上批次号 —— 否则「撤销这次导入」按 import_batch_id 找,
            #    命中 0 行,却仍然回报"已作废"(2026-10-18 审计修)。
            #    idempotency_key 是 import:<sha16>:<行号>,同一文件天然成组;
            #    只补 import_batch_id IS NULL 的行,不碰老数据。
            self.conn.execute(
                "UPDATE app_txns SET import_batch_id = ?"
                " WHERE import_batch_id IS NULL AND idempotency_key LIKE ?",
                (report.batch_id, "import:" + digest[:16] + ":%"),
            )
            self.conn.execute(
                "INSERT INTO ops_outbox(event_type, payload, idempotency_key, created_at)"
                " VALUES (?, ?, ?, ?)",
                (
                    "ledger.import.completed",
                    json.dumps(report.as_dict(), ensure_ascii=False, default=str),
                    "outbox:import:" + digest[:24],
                    now_iso(),
                ),
            )
            self.conn.commit()
            if self.receipts is not None and report.imported:
                import_kind = "批量导入" if report.imported > 1 else "导入"
                self.receipts.record(
                    kind=KIND_RECEIPT_IMPORT,
                    domain=DOMAIN_LEDGER,
                    label=(
                        import_kind + " " + target.name + ":新增 " + str(report.imported) + " 笔"
                        + ("(重复跳过 " + str(report.skipped_duplicate) + ")" if report.skipped_duplicate else "")
                    ),
                    detail={
                        "batch_id": report.batch_id,
                        "file": target.name,
                        "imported": report.imported,
                        "skipped_duplicate": report.skipped_duplicate,
                        "skipped_fuzzy": report.skipped_fuzzy,
                        "parsed": report.parsed,
                    },
                    undo_plan={
                        "type": "ledger_import",
                        "plan": {"batch_id": report.batch_id, "type": "void_batch"},
                    },
                    actor=actor,
                    session_id=session_id,
                )
                self.conn.commit()
            if self._audit is not None:
                self._audit(
                    actor=actor,
                    action="ledger.import",
                    target="batch:" + str(report.batch_id),
                    detail=report.as_dict(),
                    session_id=session_id,
                )
                self.conn.commit()
        return report

    def _category_exists(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM app_categories WHERE name = ? AND archived = 0", (name,)
        ).fetchone()
        return row is not None

    def resolve_category(self, name: str | None) -> str | None:
        """把账单里的分类名解析成我们的分类;解析不出来返回 None(不猜)。"""
        if not name:
            return None
        text = str(name).strip()
        if self._category_exists(text):
            return text
        alias = CATEGORY_ALIASES.get(text)
        if alias and self._category_exists(alias):
            return alias
        for row in self.conn.execute(
            "SELECT name FROM app_categories WHERE archived = 0 ORDER BY length(name) DESC"
        ).fetchall():
            if row["name"] in text:
                return str(row["name"])
        return None

    # -- 分类与方向(M9)---------------------------------------------------
    def list_categories(self, kind: str | None = None) -> list[str]:
        """已有分类清单。方向不给就两类都给。"""
        return CategoryAdvisor(self.conn).known(kind)

    def advise_category(
        self,
        *,
        payee: str | None = None,
        narration: str | None = None,
        direction: str = "expense",
        account: str | None = None,
        amount_cents: int = 0,
        ts: str | None = None,
    ) -> CategoryAdvice:
        """「这笔算什么」:规则优先,规则没命中就给一份排好序的候选。

        ⚠️ 它**只给建议** —— 挑哪个是模型的事,建新分类是用户的事(要弹确认框)。
        """
        advisor = CategoryAdvisor(self.conn)
        advice = CategoryAdvice(direction=direction, known=advisor.known(direction))
        engine = RuleEngine(self.conn)
        rule = engine.match(
            Subject(peer=payee, item=narration, method=account,
                    amount_cents=amount_cents, ts=ts)
        )
        if rule is not None:
            name = engine.category_name(rule.target_category_id)
            if name and advisor.exists(name, direction):
                advice.rule_category = name
                return advice
        advice.candidates = advisor.suggest(
            payee=payee, narration=narration, direction=direction
        )
        return advice

    def create_category(
        self,
        *,
        name: str,
        kind: str = "expense",
        reason: str = "",
        actor: str = "agent",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """新建一个分类。⭐ **调用方必须先问过用户** —— 领域层只负责写入与回执。

        幂等:同名同类已存在就直接返回既有那条(exists),不报错 —— 用户可能只是
        点慢了两下,或者模型重试了一次。
        """
        clean = (name or "").strip()
        if not clean:
            raise LedgerError("分类名不能为空")
        if len(clean) > 30:
            raise LedgerError("分类名太长(>30 字)")
        if kind not in ("expense", "income"):
            raise LedgerError("分类只能是 expense 或 income")

        existing = self.conn.execute(
            "SELECT id FROM app_categories WHERE name = ? AND kind = ? AND archived = 0",
            (clean, kind),
        ).fetchone()
        if existing is not None:
            return {
                "created": False, "status": "exists", "name": clean, "kind": kind,
                "category_id": int(existing["id"]), "receipt_id": None,
                "note": "「" + clean + "」已经在账本里了,直接用它。",
            }

        stamp = now_iso()
        cursor = self.conn.execute(
            "INSERT INTO app_categories(name, kind, archived) VALUES (?, ?, 0)", (clean, kind)
        )
        category_id = int(cursor.lastrowid or 0)
        receipt_id = None
        if self.receipts is not None:
            receipt_id = self.receipts.record(
                kind=KIND_RECEIPT_CATEGORY,
                domain=DOMAIN_LEDGER,
                label=("新建" + DIRECTION_LABELS[kind] + "分类「" + clean + "」"
                       + ("(" + reason + ")" if reason else "")),
                detail={
                    "category_id": category_id, "name": clean, "kind": kind,
                    "kind_label": DIRECTION_LABELS[kind], "reason": reason,
                },
                undo_plan={"type": "category", "op": "create", "category_id": category_id,
                           "name": clean, "kind": kind},
                actor=actor,
                session_id=session_id,
            )
        self.conn.execute(
            "INSERT INTO ops_outbox(event_type, payload, idempotency_key, created_at)"
            " VALUES (?, ?, ?, ?)",
            ("ledger.category.created",
             json.dumps({"category_id": category_id, "name": clean, "kind": kind},
                        ensure_ascii=False),
             "outbox:category:" + str(category_id), stamp),
        )
        self.conn.commit()
        if self._audit is not None:
            self._audit(
                actor=actor, action="ledger.category.create", target="category:" + str(category_id),
                detail={"name": clean, "kind": kind, "reason": reason}, session_id=session_id,
            )
            self.conn.commit()
        return {
            "created": True, "status": "created", "name": clean, "kind": kind,
            "category_id": category_id, "receipt_id": receipt_id,
            "note": "已新建分类「" + clean + "」,现在可以把这笔账记到它下面了。",
        }

    def revert_category(self, receipt: Receipt) -> str:
        """撤销"新建分类":没人用过就删掉,已经用过了就归档(并说清楚为什么)。"""
        plan = receipt.undo_plan or {}
        category_id = int(plan.get("category_id") or 0)
        name = str(plan.get("name") or "")
        row = self.conn.execute(
            "SELECT id, name FROM app_categories WHERE id = ?", (category_id,)
        ).fetchone()
        if row is None:
            raise ReceiptError("这个分类已经不存在了")
        used = int(
            self.conn.execute(
                "SELECT COUNT(*) FROM app_postings WHERE category_id = ?", (category_id,)
            ).fetchone()[0] or 0
        )
        if used:
            # 有账挂在它下面:删掉会连累统计(外键也会拦),改成归档 —— 名字不再可选,
            # 已记的账照旧。这是"宁可少做"的取舍。
            self.conn.execute(
                "UPDATE app_categories SET archived = 1 WHERE id = ?", (category_id,)
            )
            self.conn.commit()
            return (
                "分类「" + name + "」已经有 " + str(used)
                + " 笔账在用,所以是归档而不是删除:新账不再选它,已记的照旧。"
            )
        self.conn.execute("DELETE FROM app_categories WHERE id = ?", (category_id,))
        self.conn.execute(
            "INSERT INTO ops_outbox(event_type, payload, idempotency_key, created_at)"
            " VALUES (?, ?, ?, ?)",
            ("ledger.category.undone", json.dumps({"category_id": category_id, "name": name},
                                                  ensure_ascii=False),
             "outbox:category:" + str(category_id) + ":undone", now_iso()),
        )
        self.conn.commit()
        return "已删掉刚新建的分类「" + name + "」"

    # -- 分类管理(界面用,D164)--------------------------------------------
    def list_categories_detail(self, kind: str | None = None) -> list[dict[str, Any]]:
        """分类 + 用了多少笔 / 合计多少钱。界面「分类管理」直接渲染它。

        ⚠️ 统计口径跟账本页一致:只数**分类那条腿**(资产腿没有 category_id),
           所以 SUM(ABS(amount_cents)) 就是这一类下的总流水。
        """
        sql = (
            "SELECT c.id, c.name, c.kind, c.archived,"
            " COUNT(p.id) AS txn_count, COALESCE(SUM(ABS(p.amount_cents)), 0) AS total_cents"
            " FROM app_categories c"
            " LEFT JOIN app_postings p ON p.category_id = c.id"
        )
        params: list[Any] = []
        if kind in ("expense", "income"):
            sql += " WHERE c.kind = ?"
            params.append(kind)
        sql += " GROUP BY c.id ORDER BY c.archived ASC, c.kind ASC, c.id ASC"
        return [
            {
                "id": int(row["id"]),
                "name": str(row["name"]),
                "kind": str(row["kind"]),
                "archived": bool(int(row["archived"] or 0)),
                "txn_count": int(row["txn_count"] or 0),
                "total_cents": int(row["total_cents"] or 0),
            }
            for row in self.conn.execute(sql, params).fetchall()
        ]

    def _category_row(self, category_id: int) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT id, name, kind, archived FROM app_categories WHERE id = ?",
            (int(category_id),),
        ).fetchone()
        if row is None:
            raise LedgerError("找不到这个分类(可能已经被删了)")
        return row

    def rename_category(self, category_id: int, name: str) -> dict[str, Any]:
        """改名。⭐ 只改名字,不动 id —— 已记的账、已经学到的规则都还挂在它下面。"""
        clean = (name or "").strip()
        if not clean:
            raise LedgerError("分类名不能为空")
        if len(clean) > 30:
            raise LedgerError("分类名太长(>30 字)")
        row = self._category_row(category_id)
        if clean == str(row["name"]):
            return {"id": int(category_id), "name": clean, "kind": str(row["kind"]),
                    "old_name": clean, "changed": False}
        # ⚠️ 同名检查**不排除已归档**:唯一索引 (name, kind) 对归档行同样生效,
        #    漏掉它就会撞索引,抛出来的是一句看不懂的 sqlite3.IntegrityError。
        clash = self.conn.execute(
            "SELECT id FROM app_categories WHERE name = ? AND kind = ? AND id <> ?",
            (clean, str(row["kind"]), int(category_id)),
        ).fetchone()
        if clash is not None:
            raise LedgerError("已经有一个「" + clean + "」了(同类分类不能重名)")
        self.conn.execute(
            "UPDATE app_categories SET name = ? WHERE id = ?", (clean, int(category_id))
        )
        self.conn.commit()
        return {"id": int(category_id), "name": clean, "kind": str(row["kind"]),
                "old_name": str(row["name"]), "changed": True}

    def archive_category(self, category_id: int, *, archived: bool = True) -> dict[str, Any]:
        """归档 / 取消归档。

        ⚠️ 为什么不是"删除":已记的账外键指着它,删掉会把历史统计打出一个洞。
           归档 = 新账不再选它,历史照旧 —— 与 revert_category 同一套取舍。
        """
        row = self._category_row(category_id)
        used = int(
            self.conn.execute(
                "SELECT COUNT(*) FROM app_postings WHERE category_id = ?", (int(category_id),)
            ).fetchone()[0] or 0
        )
        self.conn.execute(
            "UPDATE app_categories SET archived = ? WHERE id = ?",
            (1 if archived else 0, int(category_id)),
        )
        self.conn.commit()
        return {"id": int(category_id), "name": str(row["name"]), "kind": str(row["kind"]),
                "archived": bool(archived), "used": used}

    # -- 页面用的读路径 ---------------------------------------------------
    def list_txns(
        self,
        *,
        period: str,
        limit: int = 200,
        include_drafts: bool = True,
        direction: str = "all",
    ) -> list[dict[str, Any]]:
        """一个周期内的明细(带方向/分类/账户)。口径与 query_expenses **同一套** SQL 表达式。

        ⚠️ direction 是**在 SQL 里筛的**,不是取回来再筛:先 LIMIT 再筛会让"只看支出"
        拿到一堆收入之外的空位(甚至一笔都不剩),而调用方以为那已经是全部。
        """
        start, end = period_bounds(period, self._current_time())
        statuses = ("draft", "confirmed") if include_drafts else ("confirmed",)
        placeholders = ",".join("?" for _ in statuses)
        pick = direction if direction in ("expense", "income") else ""
        pick_sql = "" if not pick else " AND a.kind = ?"
        pick_params: list[Any] = [] if not pick else [pick]
        owner_sql, owner_params = self._owner_clause("t.owner_id")
        rows = self.conn.execute(
            "SELECT t.id, t.ts, t.payee, t.narration, t.status, t.source,"
            " MAX(CASE WHEN a.kind = 'expense' THEN 1 ELSE 0 END) AS is_expense,"
            " SUM(CASE WHEN a.kind IN ('expense', 'income') THEN ABS(p.amount_cents)"
            "          ELSE 0 END) AS cents,"
            " (SELECT c.name FROM app_postings p2 JOIN app_categories c ON c.id = p2.category_id"
            "   WHERE p2.txn_id = t.id LIMIT 1) AS category,"
            " (SELECT a2.name FROM app_postings p2 JOIN app_accounts a2 ON a2.id = p2.account_id"
            "   WHERE p2.txn_id = t.id AND a2.kind IN ('asset', 'liability') LIMIT 1) AS account"
            " FROM app_txns_live t"
            " JOIN app_postings p ON p.txn_id = t.id"
            " JOIN app_accounts a ON a.id = p.account_id"
            " WHERE t.status IN (" + placeholders + ") AND t.ts >= ? AND t.ts < ?"
            " AND " + owner_sql
            + pick_sql
            + " GROUP BY t.id ORDER BY t.ts DESC, t.id DESC LIMIT ?",
            [*statuses, start, end, *owner_params, *pick_params, max(1, int(limit))],
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["direction"] = "expense" if int(item.pop("is_expense") or 0) else "income"
            item["cents"] = abs(int(item.get("cents") or 0))
            item["day"] = str(item["ts"])[:10]
            out.append(item)
        return out

    def monthly_trend(self, *, months: int = 6, end: datetime | None = None) -> list[dict[str, Any]]:
        """最近 N 个月(含当月)的收支。页面的趋势条用它 —— **数字同样来自 SQL**。"""
        now = end or self._current_time()
        index = now.year * 12 + (now.month - 1)
        out: list[dict[str, Any]] = []
        for offset in range(max(1, int(months)) - 1, -1, -1):
            cursor = index - offset
            period = str(cursor // 12).zfill(4) + "-" + str(cursor % 12 + 1).zfill(2)
            summary = self.query_expenses(ExpenseQuery(period=period, direction="all"))
            out.append(
                {
                    "period": period,
                    "expense_cents": summary.expense_cents,
                    "income_cents": summary.income_cents,
                    "net_cents": summary.net_cents,
                }
            )
        return out

    # -- 纠正与退款 -------------------------------------------------------
    def correct_last(
        self,
        *,
        category: str | None = None,
        payee: str | None = None,
        amount_cents: int | None = None,
        txn_id: int | None = None,
        actor: str = "user",
        session_id: str | None = None,
        learn: bool = True,
    ) -> ExpenseRead:
        """纠正最近一笔(或指定的一笔)交易。

        ⭐ 纠正之后**回写成规则** —— 下次同样的商户自动分对,这是"越用越准"的正确形态。
        """
        if txn_id is None:
            clause, params = self._owner_clause()
            row = self.conn.execute(
                "SELECT id FROM app_txns_live WHERE " + clause
                + " ORDER BY id DESC LIMIT 1",
                params,
            ).fetchone()
            if row is None:
                raise LedgerError("还没有任何交易可以纠正")
            txn_id = int(row["id"])

        current = self.read(txn_id)
        if payee is not None:
            self.conn.execute(
                "UPDATE app_txns SET payee = ?, updated_at = ? WHERE id = ?",
                (payee.strip() or None, now_iso(), txn_id),
            )
        direction, _leg_amount, _leg_category, _leg_account = self.txn_legs(txn_id)
        category_leg, asset_leg = self.leg_ids(txn_id)
        if category is not None:
            # ⚠️ 必须带 direction(2026-10-18 审计):支出/收入允许同名分类,
            #    不带 kind 的 fetchone 可能先命中另一个方向,于是收入交易被误报
            #    “这是支出分类”,尽管可用列表里就有同名收入分类。
            category_id = self.category_id(category, direction)
            if category_id is not None:
                found = self.conn.execute(
                    "SELECT kind FROM app_categories WHERE id = ?", (category_id,)
                ).fetchone()
                if found is not None and str(found["kind"]) != direction:
                    raise UnknownCategoryError(
                        "「" + category + "」是"
                        + DIRECTION_LABELS.get(str(found["kind"]), str(found["kind"]))
                        + "分类,这笔是" + DIRECTION_LABELS[direction]
                        + ";可用分类:" + "/".join(self.list_categories(direction))
                    )
            if category_leg is None:
                raise LedgerError("这笔交易没有可改的分类腿,数据可能坏了")
            self.conn.execute(
                "UPDATE app_postings SET category_id = ? WHERE id = ?", (category_id, category_leg)
            )
        if amount_cents is not None:
            if amount_cents <= 0:
                raise LedgerError("金额必须为正")
            # 支出的分类腿是正的,收入的分类腿是负的 —— 资金腿永远是它的相反数。
            signed = amount_cents if direction == "expense" else -amount_cents
            if category_leg is not None:
                self.conn.execute(
                    "UPDATE app_postings SET amount_cents = ? WHERE id = ?",
                    (signed, category_leg),
                )
            if asset_leg is not None:
                self.conn.execute(
                    "UPDATE app_postings SET amount_cents = ? WHERE id = ?",
                    (-signed, asset_leg),
                )
        self.conn.execute(
            "INSERT INTO ops_outbox(event_type, payload, idempotency_key, created_at)"
            " VALUES (?, ?, ?, ?)",
            (
                "ledger.txn.corrected",
                json.dumps(
                    {"txn_id": txn_id, "category": category, "payee": payee,
                     "amount_cents": amount_cents},
                    ensure_ascii=False,
                ),
                "outbox:correct:" + str(txn_id) + ":" + now_iso()
                + ":" + uuid.uuid4().hex[:8],
                now_iso(),
            ),
        )
        # ⭐ 先学规则、再写回执(同一事务):回执里要带"这次学到的规则 id",
        #    撤销时才能只删这一条 —— 回执是只追加表,写完就改不了了。
        learned = None
        if learn and category:
            learned = RuleEngine(self.conn).learn_from_correction(
                peer=payee or current.payee, category=category, commit=False
            )
        # 纠正也是写入:回执里带上"改之前是什么",撤销 = 改回去(并撤掉刚学的规则)。
        self._record_txn_receipt(
            txn_id,
            status=str(current.status),
            before=None,
            amount_cents=amount_cents or current.amount_cents,
            payee=payee or current.payee,
            category_name=category or current.category,
            actor=actor,
            session_id=session_id,
            extra={
                "op": "correct",
                "restore": {
                    "payee": current.payee,
                    "category": current.category,
                    "amount_cents": current.amount_cents,
                    "rule_id": int(learned.id) if learned is not None else 0,
                },
            },
        )
        self.conn.commit()
        if self._audit is not None:
            self._audit(
                actor=actor,
                action="ledger.txn.correct",
                target="txn:" + str(txn_id),
                detail={
                    "before": {"category": current.category, "payee": current.payee,
                               "amount_cents": current.amount_cents},
                    "after": {"category": category, "payee": payee, "amount_cents": amount_cents},
                    "learned_rule": learned.id if learned else None,
                },
                session_id=session_id,
            )
            self.conn.commit()
        return self.read(txn_id)

    def link_refund(
        self,
        refund_txn_id: int,
        original_txn_id: int,
        *,
        void_original: bool = False,
        actor: str = "user",
        session_id: str | None = None,
    ) -> ExpenseRead:
        """把一笔退款关联到原交易。

        ⚠️ 调研结论:开源导入工具只能处理"同一文件内可匹配的全额退款",
        跨文件退款与部分退款是**未解问题** —— 所以这里只做关联,判断交给人。
        """
        if refund_txn_id == original_txn_id:
            raise LedgerError("不能把一笔交易关联到它自己")
        self.conn.execute(
            "UPDATE app_txns SET refund_of_txn_id = ?, updated_at = ? WHERE id = ?",
            (original_txn_id, now_iso(), refund_txn_id),
        )
        if void_original:
            self.conn.execute(
                "UPDATE app_txns SET status = 'void', updated_at = ? WHERE id = ?",
                (now_iso(), original_txn_id),
            )
        self.conn.execute(
            "INSERT INTO ops_outbox(event_type, payload, idempotency_key, created_at)"
            " VALUES (?, ?, ?, ?)",
            (
                "ledger.refund.linked",
                json.dumps(
                    {"refund_txn_id": refund_txn_id, "original_txn_id": original_txn_id,
                     "void_original": void_original},
                    ensure_ascii=False,
                ),
                "outbox:refund:" + str(refund_txn_id) + ":" + uuid.uuid4().hex[:8],
                now_iso(),
            ),
        )
        snapshot = self.read(refund_txn_id)
        self._record_txn_receipt(
            refund_txn_id,
            status=str(snapshot.status),
            before=None,
            amount_cents=snapshot.amount_cents,
            payee=snapshot.payee,
            category_name=snapshot.category,
            actor=actor,
            session_id=session_id,
            extra={
                "op": "link_refund",
                "plan": {
                    "type": "clear_refund",
                    "refund_txn_id": refund_txn_id,
                    "original_txn_id": original_txn_id,
                    "restore_original_status": "confirmed" if void_original else None,
                },
            },
        )
        self.conn.commit()
        if self._audit is not None:
            self._audit(
                actor=actor,
                action="ledger.refund.link",
                target="txn:" + str(refund_txn_id),
                detail={"original_txn_id": original_txn_id, "void_original": void_original},
                session_id=session_id,
            )
            self.conn.commit()
        return self.read(refund_txn_id)

    # -- 导出 -------------------------------------------------------------
    def export_beancount(
        self, *, start: str | None = None, end: str | None = None,
        statuses: tuple[str, ...] = ("confirmed",),
    ) -> str:
        from ..beancount import export_beancount as _export

        return _export(self.conn, start=start, end=end, statuses=statuses)

    # -- 内部:统一的写入路径 ---------------------------------------------
    def _create_txn(
        self,
        *,
        ts: str,
        payee: str | None,
        narration: str | None,
        source: str,
        status: str,
        amount_cents: int,
        category_name: str | None,
        account_id: int,
        direction: str,
        idempotency_key: str,
        dedup_key: str | None = None,
        outbox_key: str | None = None,
        category_label: str | None = None,
        strict_category: bool = True,
    ) -> int:
        """**唯一的交易写入原语**(2026-10-18 架构收口)。

        对话记账与账单导入以前各写一套“插交易 + 两腿 + outbox”,已经分叉出
        不同的事务边界/分类容错/outbox 键。这里只做一件事:在一个已开启的事务里
        写这三样,**不 commit**(由调用方的 db.transaction() 统一提交/回滚)。
        """
        category = None
        if category_name:
            try:
                category = self.category_id(category_name, direction)
            except UnknownCategoryError:
                if strict_category:
                    raise
                # 导入:分类名与方向对不上就留空(未分类),不要记到错的方向。
                category = None
        category_account = self.account_id(DIRECTION_ACCOUNTS.get(direction, EXPENSE_ACCOUNT))
        if direction == "income":
            postings = (
                (account_id, None, int(amount_cents)),
                (category_account, category, -int(amount_cents)),
            )
        else:
            postings = (
                (category_account, category, int(amount_cents)),
                (account_id, None, -int(amount_cents)),
            )
        if sum(item[2] for item in postings) != 0:
            raise UnbalancedTransactionError("分录之和不为零,拒绝写入")
        cursor = self.conn.execute(
            "INSERT INTO app_txns(ts, payee, narration, source, status, idempotency_key,"
            " dedup_key, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, payee, narration, source, status, idempotency_key, dedup_key,
             now_iso(), now_iso()),
        )
        txn_id = int(cursor.lastrowid or 0)
        self._stamp_owner(txn_id)
        for account, category_id, amount in postings:
            self.conn.execute(
                "INSERT INTO app_postings(txn_id, account_id, category_id, amount_cents)"
                " VALUES (?, ?, ?, ?)",
                (txn_id, account, category_id, amount),
            )
        self._outbox(
            "ledger.txn.created",
            {
                "txn_id": txn_id,
                "ts": ts,
                "payee": payee,
                "amount_cents": int(amount_cents),
                "direction": direction,
                "category": category_label if category_label is not None else category_name,
                "status": status,
            },
            outbox_key or ("outbox:txn:" + str(txn_id)),
            now_iso(),
        )
        return txn_id

    def _insert_txn(
        self,
        *,
        ts: str,
        payee: str | None,
        narration: str | None,
        source: str,
        status: str,
        amount_cents: int,
        category_name: str | None,
        account_id: int,
        dedup_key: str | None,
        idempotency_key: str,
        direction: str = "expense",
    ) -> int:
        with db.transaction(self.conn):
            return self._create_txn(
                ts=ts, payee=payee, narration=narration, source=source, status=status,
                amount_cents=amount_cents, category_name=category_name,
                account_id=account_id, direction=direction,
                idempotency_key=idempotency_key, dedup_key=dedup_key,
                strict_category=False,
            )

    # -- 撤销:各分支的实现 -----------------------------------------------
    def _revert_state(self, plan: dict[str, Any]) -> str:
        """新建 -> 删掉;改状态 -> 改回去。"""
        txn_id = int(plan.get("txn_id") or 0)
        before = plan.get("before")
        now_status = str(plan.get("status") or "")
        stamp = now_iso()
        if before is None:
            if now_status == "deleted":
                raise ReceiptError("这笔已经删掉了,不能再删一次")
            self.conn.execute(
                "UPDATE app_txns SET deleted_at = ?, version = version + 1, updated_at = ?"
                " WHERE id = ?",
                (stamp, stamp, txn_id),
            )
            self._outbox("ledger.txn.undone", {"txn_id": txn_id, "action": "deleted"},
                         "outbox:txn:" + str(txn_id) + ":undone:" + stamp
                         + ":" + uuid.uuid4().hex[:8], stamp)
            self.conn.commit()
            return "已删掉 #" + str(txn_id) + ",账回到写入前"

        self.conn.execute(
            "UPDATE app_txns SET status = ?, deleted_at = NULL, version = version + 1,"
            " updated_at = ? WHERE id = ?",
            (str(before), stamp, txn_id),
        )
        self._outbox("ledger.txn.restored", {"txn_id": txn_id, "status": str(before)},
                     "outbox:txn:" + str(txn_id) + ":reverted:" + stamp
                     + ":" + uuid.uuid4().hex[:8], stamp)
        self.conn.commit()
        return "#" + str(txn_id) + " 退回到「" + STATUS_LABELS.get(str(before), str(before)) + "」"

    def _revert_correction(self, receipt: Receipt, plan: dict[str, Any]) -> str:
        """纠正的撤销 = 改回去,并撤掉这次纠正学到的规则(否则"撤销了但它还在自动分")。"""
        txn_id = int(plan.get("txn_id") or 0)
        restore = plan.get("restore") or {}
        if not restore:
            return self._revert_state(plan)
        category = restore.get("category")
        payee = restore.get("payee")
        amount_cents = restore.get("amount_cents")
        self.conn.execute(
            "UPDATE app_txns SET payee = ?, updated_at = ? WHERE id = ?",
            (payee, now_iso(), txn_id),
        )
        # ⚠️ 腿的身份来自 direction,不是金额正负(2026-10-18 审计修):
        #    收入交易的两条腿**都是负数**(资产腿 +,收入腿 -),以前用 amount_cents > 0
        #    找"分类腿",实际改的是资产腿,再把资产腿改成负数 —— 撤销后分录和不为零,
        #    「每笔之和为零」这条第一不变量被破坏。
        direction, _leg_amount, _leg_category, _leg_account = self.txn_legs(txn_id)
        category_leg, asset_leg = self.leg_ids(txn_id)
        # ⚠️ 用 `is not None` 而不是真值(2026-10-18 审计):纠正前本来没分类时
        #    category 是 None,旧判断会整段跳过 —— 回执说“已改回去”,新分类却留着。
        if category is not None or (category is None and "category" in restore):
            if category_leg is None:
                raise ReceiptError("这笔交易没有分类腿,撤销会把账改坏")
            self.conn.execute(
                "UPDATE app_postings SET category_id = ? WHERE id = ?",
                (self.category_id(category, direction), category_leg),
            )
        if amount_cents is not None:
            if category_leg is None or asset_leg is None:
                raise ReceiptError("这笔交易的分录不完整,撤销会把账改坏")
            signed = int(amount_cents) if direction == "expense" else -int(amount_cents)
            self.conn.execute(
                "UPDATE app_postings SET amount_cents = ? WHERE id = ?",
                (signed, category_leg),
            )
            self.conn.execute(
                "UPDATE app_postings SET amount_cents = ? WHERE id = ?",
                (-signed, asset_leg),
            )
        rule_id = int(restore.get("rule_id") or plan.get("rule_id") or 0)
        unlearned = RuleEngine(self.conn).unlearn_last(rule_id or None)
        stamp = now_iso()
        self._outbox("ledger.txn.corrected.undone", {"txn_id": txn_id},
                     "outbox:txn:" + str(txn_id) + ":correct-undone:" + stamp
                     + ":" + uuid.uuid4().hex[:8], stamp)
        self.conn.commit()
        return "已把 #" + str(txn_id) + " 改回去" + ("(顺带撤掉刚学的规则)" if unlearned else "")

    def _revert_refund_link(self, receipt: Receipt, plan: dict[str, Any]) -> str:
        refund_txn_id = int(plan.get("refund_txn_id") or 0)
        original_txn_id = plan.get("original_txn_id")
        stamp = now_iso()
        self.conn.execute(
            "UPDATE app_txns SET refund_of_txn_id = NULL, updated_at = ? WHERE id = ?",
            (stamp, refund_txn_id),
        )
        if plan.get("restore_original_status") and original_txn_id:
            self.conn.execute(
                "UPDATE app_txns SET status = ?, updated_at = ? WHERE id = ?",
                (str(plan["restore_original_status"]), stamp, int(original_txn_id)),
            )
        self._outbox("ledger.refund.unlinked",
                     {"refund_txn_id": refund_txn_id, "original_txn_id": original_txn_id},
                     "outbox:txn:" + str(refund_txn_id) + ":unlink:" + stamp
                     + ":" + uuid.uuid4().hex[:8], stamp)
        self.conn.commit()
        return "已解除 #" + str(refund_txn_id) + " 的退款关联"

    def _revert_import(self, receipt: Receipt, plan: dict[str, Any]) -> str:
        """导入的撤销 = 把这批交易作废(**不删行**)。

        ⚠️ 这是一条**兜底**撤销:因为它可能影响几十上百笔,连"明确要求才做得到"的
        确认门禁都绕开了 —— 所以作废而不是删除,且报告影响面。
        """
        # 回执里的批次号有两种历史形状:早期埋在 plan["plan"]["batch_id"],
        # 后来是顶层。两种都认 —— 这里的错会让「撤销导入」变成一句空话。
        nested = plan.get("plan") if isinstance(plan.get("plan"), dict) else {}
        batch_id = plan.get("batch_id") or nested.get("batch_id")
        if not batch_id:
            raise ReceiptError("这条导入回执没有批次号,撤不了")
        stamp = now_iso()
        cursor = self.conn.execute(
            "UPDATE app_txns SET status = 'void', updated_at = ?"
            " WHERE import_batch_id = ? AND status <> 'void'",
            (stamp, int(batch_id)),
        )
        affected = int(cursor.rowcount or 0)
        # 打上“已撤销”标记:同一文件再导入时恢复这批行,而不是被 sha256 短路。
        self.conn.execute(
            "UPDATE app_import_batches SET reverted_at = ? WHERE id = ?",
            (stamp, int(batch_id)),
        )
        self._outbox("ledger.import.undone", {"batch_id": int(batch_id), "affected": affected},
                     "outbox:import:" + str(batch_id) + ":undone:" + stamp
                     + ":" + uuid.uuid4().hex[:8], stamp)
        self.conn.commit()
        if affected == 0:
            return "这批导入已经全部作废过了"
        return "已作废这批导入的 " + str(affected) + " 笔(保留行,状态置为已作废)"

    def _outbox(self, event_type: str, payload: dict[str, Any], key: str, stamp: str) -> None:
        self.conn.execute(
            "INSERT INTO ops_outbox(event_type, payload, idempotency_key, created_at)"
            " VALUES (?, ?, ?, ?)",
            (event_type, json.dumps(payload, ensure_ascii=False), key, stamp),
        )

