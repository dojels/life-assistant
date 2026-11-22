"""提醒物化与派发 —— 整个日程模块最容易出 bug 的地方。

三条铁律(能力域 §5.2):

  1. **不预计算无限序列** —— 只物化 [now, now+horizon) 窗口内的实例。
     实测:无界 RRULE 在十年窗口里能展开出 3653 个实例。

  2. **重启不重复通知** —— 发送前用条件更新抢占:
     `UPDATE … SET fired_at=? WHERE id=? AND fired_at IS NULL` 并**检查受影响行数**。
     这是天然防并发与防重启重发的手段。

  3. **贪睡单独记** —— `snooze_until_utc` 不能复用 RRULE 的 REPEAT/DURATION,
     否则一旦重新展开就会把贪睡状态冲掉。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ... import config
from ...memory.archive import now_iso

DEFAULT_HORIZON_DAYS = 14
# ⚠️ 单一来源(2026-10-17 审计):这个串原先在 8 个模块各写一份。
#    改 .env 的 LA_TIMEZONE 而这里不动,就会出现"提示词说伦敦、提醒按上海响"。
#    真正决定每个服务用哪个时区的是**构造参数 tzid**(由 build_services 注入 settings.timezone),
#    这里只是"没注入时的兜底"。
DEFAULT_TZID = config.DEFAULT_TIMEZONE

_TRIGGER = re.compile(r"^-P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$")


def parse_alarm_trigger(trigger: str) -> timedelta:
    """'-PT30M' / '-PT1H30M' / '-P1D' -> 提前量。"""
    match = _TRIGGER.match((trigger or "").strip())
    if not match:
        return timedelta(minutes=30)
    days, hours, minutes = (int(value or 0) for value in match.groups())
    return timedelta(days=days, hours=hours, minutes=minutes)


def format_alarm_trigger(minutes: int) -> str:
    return "-PT" + str(int(minutes)) + "M"


@dataclass
class ReminderInstance:
    id: int
    event_uid: str
    recurrence_id: str
    alarm_trigger: str
    fire_at_utc: str
    fired_at: str | None
    snooze_until_utc: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ReminderInstance":
        return cls(
            id=int(row["id"]),
            event_uid=row["event_uid"],
            recurrence_id=row["recurrence_id"],
            alarm_trigger=row["alarm_trigger"],
            fire_at_utc=row["fire_at_utc"],
            fired_at=row["fired_at"],
            snooze_until_utc=row["snooze_until_utc"],
        )

    def fire_at_local(self, tzid: str = DEFAULT_TZID) -> datetime:
        return datetime.fromisoformat(self.fire_at_utc).astimezone(ZoneInfo(tzid))

    def as_dict(self, tzid: str = DEFAULT_TZID) -> dict:
        return {
            "id": self.id,
            "event_uid": self.event_uid,
            "recurrence_id": self.recurrence_id,
            "trigger": self.alarm_trigger,
            "fire_at": self.fire_at_local(tzid).isoformat(timespec="minutes"),
            "fired_at": self.fired_at,
            "snooze_until_utc": self.snooze_until_utc,
        }


class ReminderService:
    def __init__(self, conn: sqlite3.Connection, calendar, *, now_provider=None) -> None:
        self.conn = conn
        self.calendar = calendar
        self._now = now_provider

    def _moment(self) -> datetime:
        if self._now is not None:
            return self._now()
        return datetime.now(ZoneInfo(DEFAULT_TZID))

    # -- 物化 -------------------------------------------------------------
    def materialize(
        self, *, horizon_days: int = DEFAULT_HORIZON_DAYS, now: datetime | None = None
    ) -> int:
        """把未来 horizon_days 天内该提醒的实例写进表里。返回新增条数。"""
        moment = now or self._moment()
        horizon_end = moment + timedelta(days=horizon_days)
        occurrences, _ = self.calendar.expand(start=moment, end=horizon_end)

        reminders = {
            row["uid"]: int(row["reminder_minutes"])
            for row in self.conn.execute("SELECT uid, reminder_minutes FROM app_events")
        }
        created = 0
        for item in occurrences:
            minutes = reminders.get(item.uid, 0)
            if minutes <= 0:
                continue
            fire_local = item.start - timedelta(minutes=minutes)
            fire_utc = fire_local.astimezone(timezone.utc).isoformat(timespec="seconds")
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO app_reminder_instances(event_uid, recurrence_id,"
                " alarm_trigger, fire_at_utc, created_at) VALUES (?, ?, ?, ?, ?)",
                # 实例身份用**实际开始时间** —— THISANDFUTURE 之后每个实例的 RECURRENCE-ID 是同一个
                (item.uid, item.instance_id, format_alarm_trigger(minutes), fire_utc, now_iso()),
            )
            created += cursor.rowcount
        self.conn.commit()
        return created

    # -- 读 ---------------------------------------------------------------
    def pending(self, *, now: datetime | None = None, limit: int = 50) -> list[ReminderInstance]:
        stamp = (now or self._moment()).astimezone(timezone.utc).isoformat(timespec="seconds")
        rows = self.conn.execute(
            "SELECT * FROM app_reminder_instances WHERE fired_at IS NULL"
            " AND fire_at_utc <= ? AND (snooze_until_utc IS NULL OR snooze_until_utc <= ?)"
            " ORDER BY fire_at_utc LIMIT ?",
            (stamp, stamp, limit),
        ).fetchall()
        return [ReminderInstance.from_row(row) for row in rows]

    def scheduled(self, *, limit: int = 50) -> list[ReminderInstance]:
        rows = self.conn.execute(
            "SELECT * FROM app_reminder_instances ORDER BY fire_at_utc LIMIT ?", (limit,)
        ).fetchall()
        return [ReminderInstance.from_row(row) for row in rows]

    # -- 写 ---------------------------------------------------------------
    def claim(self, instance_id: int, now: datetime | None = None) -> bool:
        """抢占式派发:只有真正抢到的那一次才返回 True。重启、并发都不会重复提醒。"""
        stamp = (now or self._moment()).astimezone(timezone.utc).isoformat(timespec="seconds")
        cursor = self.conn.execute(
            "UPDATE app_reminder_instances SET fired_at = ? WHERE id = ? AND fired_at IS NULL",
            (stamp, instance_id),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    def snooze(self, instance_id: int, *, minutes: int = 10, now: datetime | None = None) -> bool:
        """贪睡:单独记一个 snooze_until_utc,并把 fired_at 清空让它重新可派发。"""
        if minutes <= 0:
            raise ValueError("贪睡分钟数必须为正")
        base = now or self._moment()
        until = (base + timedelta(minutes=minutes)).astimezone(timezone.utc)
        cursor = self.conn.execute(
            "UPDATE app_reminder_instances SET snooze_until_utc = ?, fired_at = NULL WHERE id = ?",
            (until.isoformat(timespec="seconds"), instance_id),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    def dismiss(self, instance_id: int, now: datetime | None = None) -> bool:
        stamp = (now or self._moment()).astimezone(timezone.utc).isoformat(timespec="seconds")
        cursor = self.conn.execute(
            "UPDATE app_reminder_instances SET dismissed_at = ?, fired_at = COALESCE(fired_at, ?)"
            " WHERE id = ?",
            (stamp, stamp, instance_id),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    def purge_before(self, moment: datetime) -> int:
        """清理很久以前已经处理过的实例(保持表小)。"""
        stamp = moment.astimezone(timezone.utc).isoformat(timespec="seconds")
        cursor = self.conn.execute(
            "DELETE FROM app_reminder_instances WHERE fire_at_utc < ? AND fired_at IS NOT NULL",
            (stamp,),
        )
        self.conn.commit()
        return cursor.rowcount
