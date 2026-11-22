"""确定性调度器 —— 定时的事由代码直接推送,**不经模型自主决策**。

这是"暂缓自主性"(《自我生存》🅿️)的当前替代方案:
  * 不做心跳、不做自主激活、不做自我维护;
  * 到点的事(每日播报、错题清单)由本模块捡起,作为"简报"注入对话。

⚠️ **日程提醒(schedules)不归本模块管**。它需要一个真的送达渠道,而这里没有 ——
   抢先认领会把提醒标成"已通知"然后让它凭空消失。认领入口只有
   la/bridge/reminder_push.py 一个(先送达、后认领),本模块只留
   claim() / already_sent() 两件工具给它用。

防重复打扰靠 `dispatches` 表的唯一主键做**抢占式 INSERT**:
插得进去才算派发成功,插不进去说明已经发过了 —— 进程重启、并发跑都不会重发。
(与能力域 §5.2 提醒物化的 `UPDATE ... WHERE fired_at IS NULL` 是同一个思路。)
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..config import DEFAULT_TIMEZONE, Settings
from .goals import Goal, GoalStore
from .schedules import Schedule, ScheduleStore
from .todos import Todo, TodoStore

KIND_BRIEFING = "briefing"
KIND_REMINDER = "reminder"
KIND_STUDY = "study"      # 每日错题复习清单(到点推一次,一天只有一条)

WEEKDAY_NAMES = ("一", "二", "三", "四", "五", "六", "日")


@dataclass
class Dispatch:
    id: str
    kind: str
    text: str

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "text": self.text}


def _todo_when(todo: Any) -> str:
    """待办在简报里的"什么时候":有时刻写时刻(并说明会不会响),只有日期写日期。

    ⚠️ 模型看到的这份文本决定了它怎么跟用户复述 —— 所以"12:00 到点提醒"必须写出来,
       否则它会以为那只是"某天到期"。
    """
    at = getattr(todo, "due_at_local", None)
    if at:
        clock = str(at)[11:16]
        remind = getattr(todo, "remind_minutes_before", None)
        if remind is None:
            return "(" + clock + ")"
        if int(remind) == 0:
            return "(" + clock + " 到点提醒)"
        return "(" + clock + " 前 " + str(int(remind)) + " 分钟提醒)"
    if todo.due_date_local:
        return "(" + str(todo.due_date_local) + " 到期)"
    return ""


@dataclass
class Briefing:
    date_local: str
    weekday: str
    todos: list[Todo] = field(default_factory=list)
    goals: list[Goal] = field(default_factory=list)
    reminders: list[Schedule] = field(default_factory=list)
    yesterday_summary: str = ""

    def render(self) -> str:
        lines = [
            '<briefing date="' + self.date_local + '">',
            "今天是 " + self.date_local + " 星期" + self.weekday + "。",
        ]
        if self.todos:
            lines.append("待办(" + str(len(self.todos)) + " 条未完成):")
            for todo in self.todos:
                lines.append("- [ ] " + todo.content + _todo_when(todo))
        else:
            lines.append("待办:没有未完成的事项。")
        if self.goals:
            lines.append("长期目标:")
            for goal in self.goals:
                lines.append("- " + goal.render())
        # ⚠️ 挂在待办上的闹钟**不在这里重复列**(它已经写在那条待办后面了)——
        #    这正是"同一件事在简报里出现两次"的老毛病(D115)。
        loose = [item for item in self.reminders if not getattr(item, "todo_id", None)]
        if loose:
            lines.append("已设置的提醒:")
            for reminder in loose:
                lines.append("- " + reminder.prompt)
        if self.yesterday_summary:
            lines.append("最近的记忆:" + self.yesterday_summary)
        lines.append("</briefing>")
        return "\n".join(lines)


class Dispatcher:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        settings: Settings | None = None,
        todos: TodoStore | None = None,
        goals: GoalStore | None = None,
        schedules: ScheduleStore | None = None,
        summaries_between: Any = None,
        briefing_hour: int = 9,
        study_digest: Callable[[datetime], str | None] | None = None,
    ) -> None:
        self.conn = conn
        self.settings = settings
        self.todos = todos or TodoStore(conn)
        self.goals = goals or GoalStore(conn)
        self.schedules = schedules or ScheduleStore(conn)
        self._summaries_between = summaries_between
        self.briefing_hour = briefing_hour
        # 错题复习清单的**提供者**。⚠️ 为什么传回调而不是传 StudyService:
        #   错题在 assistant.db,调度器住在 memory.db —— 直接持有对方连接是跨库操作,
        #   而跨库没有事务可言(与 ops_receipts 分两份是同一个理由)。
        #   传进来的只是一个纯函数:给我时间,要么给我一段文本,要么给我 None。
        self.study_digest = study_digest

    # -- 抢占 -------------------------------------------------------------
    def claim(self, dispatch_id: str, kind: str, text: str, *, now: datetime) -> bool:
        try:
            self.conn.execute(
                "INSERT INTO dispatches(id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                (dispatch_id, kind, text, now.isoformat(timespec="seconds")),
            )
        except sqlite3.IntegrityError:
            self.conn.rollback()
            return False
        self.conn.commit()
        return True

    def already_sent(self, dispatch_id: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM dispatches WHERE id = ?", (dispatch_id,)).fetchone()
        return row is not None

    # -- 每日简报 ---------------------------------------------------------
    def build_briefing(self, now: datetime) -> Briefing:
        today = now.strftime("%Y-%m-%d")
        yesterday_start = (now - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
        yesterday_end = now.strftime("%Y-%m-%dT00:00:00")
        summary_text = ""
        if self._summaries_between is not None:
            rows = self._summaries_between(yesterday_start, yesterday_end)
            if rows:
                summary_text = " ".join(str(rows[0].text).split())[:200]
        return Briefing(
            date_local=today,
            weekday=WEEKDAY_NAMES[now.weekday()],
            todos=self.todos.list(status="pending"),
            goals=self.goals.list(open_only=True),
            reminders=self.schedules.list(),
            yesterday_summary=summary_text,
        )

    def briefing_due(self, now: datetime) -> bool:
        if now.hour < self.briefing_hour:
            return False
        return not self.already_sent(KIND_BRIEFING + ":" + now.strftime("%Y-%m-%d"))

    def deliver_briefing(self, now: datetime) -> Dispatch | None:
        """到点就派发一次;同一天只派发一次(靠主键抢占)。"""
        if not self.briefing_due(now):
            return None
        briefing = self.build_briefing(now)
        text = briefing.render()
        dispatch_id = KIND_BRIEFING + ":" + briefing.date_local
        if not self.claim(dispatch_id, KIND_BRIEFING, text, now=now):
            return None
        # 简报真的派发出去了 = 新的一天真的开始了 -> 所有开放目标推进一轮。
        # 放在这里而不是会话里,是因为"推进一轮"取决于**是否派发成功**,
        # 而不是取决于"谁调用了它"(会话 / CLI / 以后的任务计划程序都一样)。
        for goal in self.goals.list(open_only=True):
            self.goals.bump_round(goal.id)
        return Dispatch(id=dispatch_id, kind=KIND_BRIEFING, text=text)

    # -- 错题复习推送 -----------------------------------------------------
    def deliver_study_push(self, now: datetime) -> Dispatch | None:
        """到点就推一次今天的错题复习清单;同一天只推一次(靠主键抢占)。

        ⭐ 这是"暂缓自主性"的替代方案里最典型的一件事:**到点的事由代码推**。
        什么时候推、推几道、推给谁,全部来自 app_study_settings 与复习单元表,
        模型在这条链路上一行都不参与 —— 它欠费、断网、挂了都不影响推送。
        """
        if self.study_digest is None:
            return None
        try:
            text = self.study_digest(now)
        except Exception:
            # 错题库还没建好 / 读失败:推送失败不该让整个派发崩掉
            return None
        if not text:
            return None
        dispatch_id = KIND_STUDY + ":" + now.strftime("%Y-%m-%d")
        if not self.claim(dispatch_id, KIND_STUDY, text, now=now):
            return None
        return Dispatch(id=dispatch_id, kind=KIND_STUDY, text=text)

    def study_push_today(self, now: datetime) -> Dispatch | None:
        """今天那条错题推送(**已经推过的**)。给"刷新页面还能看见今天推了什么"用。"""
        dispatch_id = KIND_STUDY + ":" + now.strftime("%Y-%m-%d")
        row = self.conn.execute(
            "SELECT id, kind, payload FROM dispatches WHERE id = ?", (dispatch_id,)
        ).fetchone()
        if row is None:
            return None
        return Dispatch(id=str(row["id"]), kind=str(row["kind"]), text=str(row["payload"]))

    def recent_dispatches(self, *, kind: str | None = None, limit: int = 20) -> list[Dispatch]:
        """最近派发过什么 —— 界面上那条"今天已经推过了"的横幅就读它。"""
        sql = "SELECT id, kind, payload FROM dispatches"
        params: list[Any] = []
        if kind:
            sql += " WHERE kind = ?"
            params.append(kind)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(int(limit))
        return [
            Dispatch(id=str(row["id"]), kind=str(row["kind"]), text=str(row["payload"]))
            for row in self.conn.execute(sql, params).fetchall()
        ]

    # -- 汇总 -------------------------------------------------------------
    def deliver_pending(self, now: datetime) -> list[Dispatch]:
        """会话开始时调用一次:把到点的**播报**捡起来。

        ⚠️ **日程提醒(schedules)刻意不在这里**。它唯一的认领入口是
        la/bridge/reminder_push.py:认领 = 已通知,而会话这条链路根本没有送达渠道
        —— _briefing_text() 只放 briefing/study,reminder 被显式过滤掉,
        也没有任何界面读 pending_dispatches。在这里认领过一次,提醒就被标成
        "已通知"然后凭空消失,而且 at 类型的 claim 只在 last_fired_at IS NULL
        时成功 —— 这条提醒再也不会响第二次。实测:2026-10-12 19:10:20 三条提醒
        被一次性吃掉,用户当天一条都没收到。

        一般化的规矩(承 D34):抢占式资源只能有一个认领入口,而且那个入口
        必须站在**送达成功**的后面。
        """
        items: list[Dispatch] = []
        briefing = self.deliver_briefing(now)
        if briefing is not None:
            items.append(briefing)
        study = self.deliver_study_push(now)
        if study is not None:
            items.append(study)
        return items
