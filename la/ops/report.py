"""周报 / 月报:数字全部来自 SQL,模型只负责叙述。

⭐ 这条是"金额一律由代码计算"铁律的延伸 —— 报表里的每一个数字都必须能复算。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .. import config
from .backup import directory_size_mb


@dataclass
class WeeklyReport:
    start: str
    end: str
    expenses_total_cents: int = 0
    by_category: dict[str, int] = field(default_factory=dict)
    txn_count: int = 0
    drafts_pending: int = 0
    workouts: int = 0
    met_minutes: float = 0.0
    weight_change_kg: float | None = None
    reviews_done: int = 0
    problems_added: int = 0
    due_today: int = 0
    data_size_mb: float = 0.0
    source: str = "ops"

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start, "end": self.end,
            "expenses_total_cents": self.expenses_total_cents,
            "expenses_total_yuan": round(self.expenses_total_cents / 100, 2),
            "by_category": dict(self.by_category),
            "txn_count": self.txn_count,
            "drafts_pending": self.drafts_pending,
            "workouts": self.workouts,
            "met_minutes": round(self.met_minutes, 1),
            "weight_change_kg": self.weight_change_kg,
            "reviews_done": self.reviews_done,
            "problems_added": self.problems_added,
            "due_today": self.due_today,
            "data_size_mb": self.data_size_mb,
            "source": self.source,
        }

    def render(self) -> str:
        lines = ["【" + self.start + " ~ " + self.end + " 小结】"]
        if self.txn_count:
            lines.append(
                "- 支出约 ¥" + str(round(self.expenses_total_cents / 100, 2))
                + "(" + str(self.txn_count) + " 笔)"
            )
            top = sorted(self.by_category.items(), key=lambda item: -item[1])[:3]
            if top:
                lines.append(
                    "  花得最多:" + "、".join(
                        name + " ¥" + str(round(cents / 100, 2)) for name, cents in top
                    )
                )
        else:
            lines.append("- 这段时间没有已确认的支出")
        if self.drafts_pending:
            lines.append("- 有 " + str(self.drafts_pending) + " 笔草稿等你确认")
        if self.workouts:
            lines.append("- 运动 " + str(self.workouts) + " 次 / "
                         + str(round(self.met_minutes)) + " MET-分钟")
        if self.weight_change_kg is not None:
            direction = "上升" if self.weight_change_kg > 0 else "下降"
            lines.append("- 体重趋势" + direction + "约 "
                         + str(round(abs(self.weight_change_kg), 2)) + " kg")
        if self.reviews_done or self.problems_added:
            lines.append("- 错题:复习 " + str(self.reviews_done) + " 次 / 新增 "
                         + str(self.problems_added) + " 道 / 今日待复习 " + str(self.due_today))
        lines.append("- 数据占用 " + str(self.data_size_mb) + " MB")
        return "\n".join(lines)


def build_report(
    assistant: sqlite3.Connection, memory: sqlite3.Connection, *, days: int = 7,
    now: datetime | None = None,
) -> WeeklyReport:
    # ⚠️ 用配置时区的"现在"(2026-10-18 审计修):裸 datetime.now() 是 OS 本地时区,
    #    与全项目的 settings.now() 可能不是同一天 —— 报表窗口会整体偏一天。
    moment = now or config.get_settings().now()
    end_date = (moment + timedelta(days=1)).date()
    start_date = end_date - timedelta(days=days)
    start, end = start_date.isoformat(), end_date.isoformat()

    report = WeeklyReport(start=start, end=(end_date - timedelta(days=1)).isoformat())

    total = assistant.execute(
        "SELECT COALESCE(SUM(p.amount_cents), 0) AS total, COUNT(DISTINCT t.id) AS n"
        " FROM app_postings p JOIN app_txns_live t ON t.id = p.txn_id"
        " JOIN app_accounts a ON a.id = p.account_id"
        " WHERE a.kind = 'expense' AND t.status = 'confirmed'"
        " AND t.ts >= ? AND t.ts < ?",
        (start, end),
    ).fetchone()
    report.expenses_total_cents = int(total["total"])
    report.txn_count = int(total["n"])

    for row in assistant.execute(
        "SELECT COALESCE(c.name, '(未分类)') AS key, SUM(p.amount_cents) AS total"
        " FROM app_postings p JOIN app_txns_live t ON t.id = p.txn_id"
        " JOIN app_accounts a ON a.id = p.account_id"
        " LEFT JOIN app_categories c ON c.id = p.category_id"
        " WHERE a.kind = 'expense' AND t.status = 'confirmed' AND t.ts >= ? AND t.ts < ?"
        " GROUP BY key ORDER BY total DESC",
        (start, end),
    ).fetchall():
        report.by_category[str(row["key"])] = int(row["total"])

    report.drafts_pending = int(
        assistant.execute("SELECT count(*) FROM app_txns_live WHERE status = 'draft'").fetchone()[0]
    )
    workouts = assistant.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(w.duration_min * COALESCE(a.met, 4.0)), 0) AS met"
        " FROM app_workouts w LEFT JOIN app_activity_types a ON a.id = w.activity_id"
        " WHERE w.local_date >= ? AND w.local_date < ?",
        (start, end),
    ).fetchone()
    report.workouts = int(workouts["n"])
    report.met_minutes = float(workouts["met"])

    # ⚠️ app_measurements 住在 assistant.db(领域数据),不是 memory.db。
    # 记忆库只存"发生过什么",体测是领域事实。
    rows = assistant.execute(
        "SELECT local_date, AVG(value) AS v FROM app_measurements"
        " WHERE metric_code = 'weight' AND local_date >= ? AND local_date < ?"
        " GROUP BY local_date ORDER BY local_date",
        (start, end),
    ).fetchall()
    if len(rows) >= 2:
        from ..domain.health.metrics import ewma_trend

        points = ewma_trend([(row["local_date"], float(row["v"])) for row in rows])
        report.weight_change_kg = round(points[-1].trend - points[0].trend, 3)

    report.reviews_done = int(
        assistant.execute(
            "SELECT count(*) FROM app_review_logs WHERE substr(ts_utc, 1, 10) >= ?"
            " AND substr(ts_utc, 1, 10) < ?",
            (start, end),
        ).fetchone()[0]
    )
    report.problems_added = int(
        assistant.execute(
            "SELECT count(*) FROM app_problems WHERE substr(created_at, 1, 10) >= ?"
            " AND substr(created_at, 1, 10) < ?",
            (start, end),
        ).fetchone()[0]
    )
    report.due_today = int(
        assistant.execute(
            "SELECT count(*) FROM app_review_units WHERE suspended = 0 AND due_date_local <= ?",
            (moment.date().isoformat(),),
        ).fetchone()[0]
    )
    report.data_size_mb = directory_size_mb()
    return report
