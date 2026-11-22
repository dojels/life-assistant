"""规则引擎:trigger -> 编译成查询 -> 执行 -> action(借鉴 Firefly III)。

三条设计要点(能力域 §5.1):
  1. **规则进库,不硬编码** —— 改规则不需要改代码;
  2. **人工纠正回写成规则** —— 这是"越用越准"的正确形态,而不是去微调模型;
  3. 规则命中计数,便于看出哪条规则在起作用。
"""

from __future__ import annotations
from ..base.clock import utc_now_iso as _utc_now

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

MATCH_FIELDS = ("peer", "item", "method", "amount", "time_window")
MATCH_OPS = ("contains", "equals", "gt", "lt", "in_range")

ORIGIN_MANUAL = "manual"
ORIGIN_LEARNED = "learned"

DEFAULT_PRIORITY = 100
LEARNED_PRIORITY = 50




@dataclass
class Rule:
    id: int
    priority: int
    name: str | None
    match_field: str
    match_op: str
    match_value: str
    target_account_id: int | None
    target_category_id: int | None
    add_tag: str | None
    origin: str
    enabled: bool
    hit_count: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Rule":
        return cls(
            id=int(row["id"]),
            priority=int(row["priority"]),
            name=row["name"],
            match_field=row["match_field"],
            match_op=row["match_op"],
            match_value=row["match_value"],
            target_account_id=row["target_account_id"],
            target_category_id=row["target_category_id"],
            add_tag=row["add_tag"],
            origin=row["origin"],
            enabled=bool(row["enabled"]),
            hit_count=int(row["hit_count"]),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "priority": self.priority,
            "match": self.match_field + " " + self.match_op + " " + self.match_value,
            "target_category_id": self.target_category_id,
            "target_account_id": self.target_account_id,
            "origin": self.origin,
            "hit_count": self.hit_count,
        }


@dataclass
class Subject:
    """被规则判断的一行(导入行或已有交易)。"""

    peer: str | None = None
    item: str | None = None
    method: str | None = None
    amount_cents: int = 0
    ts: str | None = None


def rule_matches(rule: Rule, subject: Subject) -> bool:
    value = {
        "peer": subject.peer,
        "item": subject.item,
        "method": subject.method,
        "amount": str(subject.amount_cents),
        "time_window": (subject.ts or "")[11:16],
    }.get(rule.match_field)
    if value is None:
        return False
    text = str(value)
    target = rule.match_value
    if rule.match_op == "contains":
        return target in text
    if rule.match_op == "equals":
        return text == target
    if rule.match_op in ("gt", "lt"):
        try:
            left, right = int(text), int(target)
        except ValueError:
            return False
        return left > right if rule.match_op == "gt" else left < right
    if rule.match_op == "in_range":
        # "11:00-14:00" 这类时间窗 —— 用来区分午晚餐
        if "-" not in target:
            return False
        low, _, high = target.partition("-")
        return low.strip() <= text <= high.strip()
    return False


class RuleEngine:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # -- 读 ---------------------------------------------------------------
    def list(self, *, enabled_only: bool = True) -> list[Rule]:
        sql = "SELECT * FROM app_rules"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY priority, id"
        return [Rule.from_row(row) for row in self.conn.execute(sql).fetchall()]

    def match(self, subject: Subject) -> Rule | None:
        for rule in self.list():
            if rule_matches(rule, subject):
                return rule
        return None

    def record_hit(self, rule_id: int) -> None:
        self.conn.execute(
            "UPDATE app_rules SET hit_count = hit_count + 1, updated_at = ? WHERE id = ?",
            (_utc_now(), rule_id),
        )

    def unlearn_last(self, rule_id: int | None = None) -> bool:
        """撤掉一条自动学到的规则(纠正被撤销时调用)。

        ⚠️ 只删 origin='learned' 的:手工建的规则是用户的决定,不该被一次撤销带走。
        ⚠️ 给了 rule_id 就**只删那一条**(回执里记着的、这次纠正学到的);
           没给才退回"最近一条学到的" —— 老回执没有 rule_id,这是兼容路径。
           以前一律取全局最后一条:撤销 A 的纠正会把 B 刚学的规则删掉。
        """
        if rule_id:
            cursor = self.conn.execute(
                "DELETE FROM app_rules WHERE id = ? AND origin = ?",
                (int(rule_id), ORIGIN_LEARNED),
            )
            self.conn.commit()
            return bool(cursor.rowcount)
        row = self.conn.execute(
            "SELECT id FROM app_rules WHERE origin = ? ORDER BY id DESC LIMIT 1",
            (ORIGIN_LEARNED,),
        ).fetchone()
        if row is None:
            return False
        self.conn.execute("DELETE FROM app_rules WHERE id = ?", (int(row["id"]),))
        self.conn.commit()
        return True

    # -- 写 ---------------------------------------------------------------
    def add(
        self,
        *,
        match_field: str,
        match_value: str,
        match_op: str = "contains",
        category: str | None = None,
        account: str | None = None,
        priority: int = DEFAULT_PRIORITY,
        origin: str = ORIGIN_MANUAL,
        name: str | None = None,
        commit: bool = True,
    ) -> Rule:
        if match_field not in MATCH_FIELDS:
            raise ValueError("未知匹配字段: " + str(match_field))
        if match_op not in MATCH_OPS:
            raise ValueError("未知匹配方式: " + str(match_op))
        value = (match_value or "").strip()
        if not value:
            raise ValueError("match_value 不能为空")

        category_id = self._category_id(category) if category else None
        account_id = self._account_id(account) if account else None

        existing = self.conn.execute(
            "SELECT id FROM app_rules WHERE match_field = ? AND match_op = ? AND match_value = ?",
            (match_field, match_op, value),
        ).fetchone()
        now = _utc_now()
        if existing is not None:
            # ⚠️ **人工规则不会被"学习"改成 learned**(2026-10-18 审计修):
            #    改 origin 之后,撤销一次纠正会按"最近一条 learned"把它整行删掉 ——
            #    用户的决定被当成系统学到的规则杀掉。origin 只升不降。
            existing_origin = self.conn.execute(
                "SELECT origin FROM app_rules WHERE id = ?", (existing["id"],)
            ).fetchone()
            kept_origin = (
                ORIGIN_MANUAL
                if existing_origin is not None and str(existing_origin["origin"]) == ORIGIN_MANUAL
                else origin
            )
            self.conn.execute(
                "UPDATE app_rules SET target_category_id = COALESCE(?, target_category_id),"
                " target_account_id = COALESCE(?, target_account_id), priority = ?,"
                " origin = ?, updated_at = ? WHERE id = ?",
                (category_id, account_id, priority, kept_origin, now, existing["id"]),
            )
            if commit:
                self.conn.commit()
            return self._get(int(existing["id"]))

        cursor = self.conn.execute(
            "INSERT INTO app_rules(priority, name, match_field, match_op, match_value,"
            " target_account_id, target_category_id, origin, enabled, hit_count, created_at,"
            " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)",
            (priority, name, match_field, match_op, value, account_id, category_id, origin, now, now),
        )
        if commit:
            self.conn.commit()
        return self._get(int(cursor.lastrowid or 0))

    def learn_from_correction(
        self, *, peer: str | None, category: str | None, account: str | None = None,
        commit: bool = True,
    ) -> Rule | None:
        """把一次人工纠正变成一条规则 —— 下次同样的商户自动分对。"""
        if not peer or not category:
            return None
        return self.add(
            commit=commit,
            match_field="peer",
            match_op="equals",
            match_value=peer,
            category=category,
            account=account,
            priority=LEARNED_PRIORITY,
            origin=ORIGIN_LEARNED,
            name="学到:" + peer,
        )

    # -- 内部 -------------------------------------------------------------
    def _get(self, rule_id: int) -> Rule:
        row = self.conn.execute("SELECT * FROM app_rules WHERE id = ?", (rule_id,)).fetchone()
        return Rule.from_row(row)

    def _category_id(self, name: str) -> int:
        row = self.conn.execute("SELECT id FROM app_categories WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise ValueError("未知分类: " + name)
        return int(row["id"])

    def _account_id(self, name: str) -> int:
        row = self.conn.execute("SELECT id FROM app_accounts WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise ValueError("未知账户: " + name)
        return int(row["id"])

    def category_name(self, category_id: int | None) -> str | None:
        if category_id is None:
            return None
        row = self.conn.execute(
            "SELECT name FROM app_categories WHERE id = ?", (category_id,)
        ).fetchone()
        return str(row["name"]) if row else None

    def account_name(self, account_id: int | None) -> str | None:
        if account_id is None:
            return None
        row = self.conn.execute(
            "SELECT name FROM app_accounts WHERE id = ?", (account_id,)
        ).fetchone()
        return str(row["name"]) if row else None
