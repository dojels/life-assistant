"""iCalendar 编解码与展开。

**绝不自己实现 RRULE。** 库里存 RFC5545 原文,展开交给 recurring-ical-events
(它把 icalendar 与 python-dateutil 接起来,处理 RRULE + RECURRENCE-ID + EXDATE + 时区)。

⚠️ 实测得到的四条硬事实(见 docs/决策记录.md D32):
  1. RECURRENCE-ID 覆盖 ✅ 生效(改时间、改标题);
  2. EXDATE 排除 ✅ 生效;
  3. RECURRENCE-ID;RANGE=THISANDFUTURE ✅ 生效(此次及以后都改);
  4. **STATUS:CANCELLED 的实例照样会被返回** —— 必须由我们过滤掉;
     并且无界 RRULE 在十年窗口里能展开出 3653 个实例 —— **上限必须自己设**。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events

# 单次查询允许展开的最大实例数(防 DoS;Radicale 的同类配置默认也是 10000)
MAX_OCCURRENCES = 2000
# 单次查询允许的最大窗口
MAX_WINDOW_DAYS = 400
# 展开粒度:一次最多算 90 天,超长窗口切段处理
SLICE_DAYS = 90


@dataclass
class Occurrence:
    """一个**具体实例**。

    ⚠️ 实例身份是 `start`(它实际发生的时间),不是 RECURRENCE-ID。
    原因(实测):`RANGE=THISANDFUTURE` 的覆盖会把**系列级**的 RECURRENCE-ID
    附加到此后每一个实例上 —— 用那个字段做身份会把它们全折叠成一个。
    """

    uid: str
    start: datetime
    end: datetime | None
    title: str
    location: str = ""
    description: str = ""
    all_day: bool = False
    is_override: bool = False
    is_recurring: bool = False
    original_start: str | None = None   # 被覆盖的那一次的原开始时间(RFC 的 RECURRENCE-ID)

    @property
    def instance_id(self) -> str:
        return self.start.isoformat()

    def as_dict(self) -> dict:
        return {
            "uid": self.uid,
            "instance_id": self.instance_id,
            "original_start": self.original_start,
            "start": self.start.isoformat(),
            "end": self.end.isoformat() if self.end else None,
            "title": self.title,
            "location": self.location,
            "all_day": self.all_day,
            "is_override": self.is_override,
            "is_recurring": self.is_recurring,
        }


@dataclass
class EventRecord:
    """喂给 ICS 生成器的行(来自 app_events / app_event_overrides)。"""

    uid: str
    title: str
    dtstart: str
    dtend: str | None = None
    tzid: str = "Asia/Shanghai"
    rrule: str | None = None
    location: str = ""
    description: str = ""
    all_day: bool = False
    status: str = "confirmed"
    reminder_minutes: int = 30


@dataclass
class OverrideRecord:
    recurrence_id: str
    is_cancelled: bool = False
    range_mode: str = "single"
    dtstart: str | None = None
    dtend: str | None = None
    title: str | None = None
    location: str | None = None
    description: str | None = None


def _parse(moment: str, tzid: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(moment)
    except ValueError:
        parsed = datetime.fromisoformat(moment[:19])
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(tzid))
    return parsed


def build_calendar(
    events: Sequence[EventRecord], overrides: Sequence[tuple[EventRecord, OverrideRecord]]
) -> icalendar.Calendar:
    """从库里的行拼出一份 ICS —— 展开与导出共用同一条路径。"""
    calendar = icalendar.Calendar()
    calendar.add("VERSION", "2.0")
    calendar.add("PRODID", "-//life-assistant//calendar//CN")

    for record in events:
        component = icalendar.Event()
        component.add("UID", record.uid)
        component.add("SUMMARY", record.title)
        start = _parse(record.dtstart, record.tzid)
        if record.all_day:
            component.add("DTSTART", start.date())
            if record.dtend:
                component.add("DTEND", _parse(record.dtend, record.tzid).date())
        else:
            component.add("DTSTART", start)
            if record.dtend:
                component.add("DTEND", _parse(record.dtend, record.tzid))
        if record.rrule:
            component.add("RRULE", icalendar.vRecur.from_ical(record.rrule))
        if record.location:
            component.add("LOCATION", record.location)
        if record.description:
            component.add("DESCRIPTION", record.description)
        if record.status == "cancelled":
            component.add("STATUS", "CANCELLED")
        if record.reminder_minutes > 0:
            alarm = icalendar.Alarm()
            alarm.add("ACTION", "DISPLAY")
            alarm.add("DESCRIPTION", record.title)
            alarm.add("TRIGGER", timedelta(minutes=-record.reminder_minutes))
            component.add_component(alarm)
        calendar.add_component(component)

    for base, override in overrides:
        component = icalendar.Event()
        component.add("UID", base.uid)
        recurrence_id = _parse(override.recurrence_id, base.tzid)
        if override.range_mode == "thisandfuture":
            component.add("RECURRENCE-ID", recurrence_id, parameters={"RANGE": "THISANDFUTURE"})
        else:
            component.add("RECURRENCE-ID", recurrence_id)
        if override.is_cancelled:
            component.add("DTSTART", recurrence_id)
            component.add("SUMMARY", base.title)
            component.add("STATUS", "CANCELLED")
        else:
            start = _parse(override.dtstart or override.recurrence_id, base.tzid)
            component.add("DTSTART", start)
            if override.dtend:
                component.add("DTEND", _parse(override.dtend, base.tzid))
            elif base.dtend:
                duration = _parse(base.dtend, base.tzid) - _parse(base.dtstart, base.tzid)
                component.add("DTEND", start + duration)
            component.add("SUMMARY", override.title or base.title)
            if override.location or base.location:
                component.add("LOCATION", override.location or base.location)
        calendar.add_component(component)
    return calendar


def expand(
    events: Sequence[EventRecord],
    overrides: Sequence[tuple[EventRecord, OverrideRecord]],
    *,
    start: datetime,
    end: datetime,
    tzid: str = "Asia/Shanghai",
    max_occurrences: int = MAX_OCCURRENCES,
) -> tuple[list[Occurrence], bool]:
    """把 [start, end) 之间的实例展开出来。返回 (实例表, 是否被截断)。"""
    if end <= start:
        return [], False
    window_days = (end - start).days
    window_capped = window_days > MAX_WINDOW_DAYS
    if window_capped:
        end = start + timedelta(days=MAX_WINDOW_DAYS)

    calendar = build_calendar(events, overrides)
    recurring_uids = {record.uid for record in events if record.rrule}
    found: dict[tuple[str, str], Occurrence] = {}
    truncated = False
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=SLICE_DAYS), end)
        try:
            components = recurring_ical_events.of(
                calendar, keep_recurrence_attributes=True
            ).between(cursor, chunk_end)
        except Exception:
            # 单条日历坏掉不该让整次查询失败
            components = []
        for component in components:
            # ⚠️ 实测:被取消的实例照样会被返回,必须自己过滤
            if str(component.get("STATUS") or "").upper() == "CANCELLED":
                continue
            uid = str(component.get("UID") or "")
            raw_start = component.get("DTSTART").dt
            is_all_day = not isinstance(raw_start, datetime)
            moment = (
                datetime.combine(raw_start, datetime.min.time(), tzinfo=ZoneInfo(tzid))
                if is_all_day
                else raw_start
            )
            recurrence_attribute = component.get("RECURRENCE-ID")
            original_start = (
                recurrence_attribute.dt.isoformat()
                if recurrence_attribute is not None
                and isinstance(recurrence_attribute.dt, datetime)
                else None
            )
            # ⚠️ keep_recurrence_attributes 会把系列属性附加到**每一个**生成实例上。
            # 只有"原时间 ≠ 实际时间"才说明这一次真的被单独改过。不改就会让
            # 所有重复实例都被标成"已被覆盖"。
            if original_start is not None and original_start == moment.isoformat():
                original_start = None
            raw_end = component.get("DTEND")
            finish = None
            if raw_end is not None:
                value = raw_end.dt
                finish = (
                    datetime.combine(value, datetime.min.time(), tzinfo=ZoneInfo(tzid))
                    if not isinstance(value, datetime)
                    else value
                )
            # 去重键 = (uid, 实际开始时间):同一实例在切片边界上只会出现一次
            key = (uid, moment.isoformat())
            if key in found:
                continue
            found[key] = Occurrence(
                uid=uid,
                start=moment,
                original_start=original_start,
                end=finish,
                title=str(component.get("SUMMARY") or ""),
                location=str(component.get("LOCATION") or ""),
                description=str(component.get("DESCRIPTION") or ""),
                all_day=is_all_day,
                is_override=original_start is not None,
                is_recurring=uid in recurring_uids,
            )
        cursor = chunk_end

    ordered = sorted(found.values(), key=lambda item: (item.start, item.uid))
    if len(ordered) > max_occurrences:
        return ordered[:max_occurrences], True
    # 窗口被砍同样算截断 —— 调用方必须知道"你看到的不全"
    return ordered, truncated or window_capped
