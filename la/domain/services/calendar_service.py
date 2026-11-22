"""日程服务:事件 CRUD + 例外三种机制 + 展开 + 只读订阅。

⭐ 三条铁律(能力域 §5.2):
  1. **绝不自己实现 RRULE** —— 库里存原文,展开交给 recurring-ical-events;
  2. **例外必须拆成三种** —— RECURRENCE-ID(覆盖某一次)/ EXDATE(排除某一次)/
     RANGE=THISANDFUTURE(此次及以后),不能只有一个"改期"概念;
  3. **展开与触发是两种时间语义** —— 展开用事件本地时区,提醒触发存 UTC。
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ... import config
from ...memory.archive import now_iso
from .. import ownership
from ..receipts import DOMAIN_CALENDAR, Receipt, ReceiptError, ReceiptLog
from ..calendars.ical import (
    EventRecord,
    Occurrence,
    OverrideRecord,
    build_calendar,
    expand as expand_ical,
)
from ..schemas.calendar import CalendarEventCreate, OccurrenceRead

# ⚠️ 单一来源(2026-10-17 审计):这个串原先在 8 个模块各写一份。
#    改 .env 的 LA_TIMEZONE 而这里不动,就会出现"提示词说伦敦、提醒按上海响"。
#    真正决定每个服务用哪个时区的是**构造参数 tzid**(由 build_services 注入 settings.timezone),
#    这里只是"没注入时的兜底"。
DEFAULT_TZID = config.DEFAULT_TIMEZONE
DEFAULT_DURATION_MINUTES = 60
RANGE_SINGLE = "single"
RANGE_THIS_AND_FUTURE = "thisandfuture"


class CalendarError(RuntimeError):
    """日程域的领域错误。"""


def _parse(moment: str, tzid: str = DEFAULT_TZID) -> datetime:
    try:
        parsed = datetime.fromisoformat(moment)
    except ValueError as exc:
        raise CalendarError("时间格式必须是 ISO8601: " + str(moment)) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(tzid))
    return parsed


@dataclass
class EventRow:
    id: int
    uid: str
    title: str
    dtstart: str
    dtend: str | None
    tzid: str
    rrule: str | None
    location: str | None
    description: str | None
    all_day: bool
    status: str
    reminder_minutes: int = 30
    #: 事件归属(日程不可共享，创建者是谁就只提醒谁)
    owner_id: int | None = None
    reminder_target_member_id: int | None = None

    def as_record(self) -> EventRecord:
        return EventRecord(
            uid=self.uid,
            title=self.title,
            dtstart=self.dtstart,
            dtend=self.dtend,
            tzid=self.tzid,
            rrule=self.rrule,
            location=self.location or "",
            description=self.description or "",
            all_day=self.all_day,
            status=self.status,
            reminder_minutes=self.reminder_minutes,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "title": self.title,
            "start": self.dtstart,
            "end": self.dtend,
            "rrule": self.rrule,
            "location": self.location,
            "status": self.status,
        }


def _row_to_event(row: sqlite3.Row) -> EventRow:
    return EventRow(
        id=int(row["id"]),
        uid=row["uid"],
        title=row["title"],
        dtstart=row["dtstart"],
        dtend=row["dtend"],
        tzid=row["tzid"],
        rrule=row["rrule"],
        location=row["location"],
        description=row["description"],
        all_day=bool(row["all_day"]),
        status=row["status"],
        reminder_minutes=int(row["reminder_minutes"]),
        owner_id=(int(row["owner_id"]) if row["owner_id"] is not None else None),
        reminder_target_member_id=(
            int(row["reminder_target_member_id"])
            if row["reminder_target_member_id"] is not None else None
        ),
    )


class CalendarService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        audit: Callable[..., None] | None = None,
        now_provider: Callable[[], datetime] | None = None,
        tzid: str = DEFAULT_TZID,
        receipts: ReceiptLog | None = None,
        member_id: int | None = None,
    ) -> None:
        self.conn = conn
        self.member_id = member_id
        self._audit = audit
        self._now = now_provider
        # ⚠️ 时区也注入(2026-10-17 审计):它必须与"现在"来自同一处,
        #    否则会出现"时钟按伦敦、分组按上海"的分裂。
        self.tzid = tzid
        # 统一写入回执(M7):日程的每次写入也走同一个前端机制
        self.receipts = receipts

    def _current_time(self) -> datetime:
        if self._now is not None:
            return self._now()
        return datetime.now(ZoneInfo(self.tzid))

    def _owner_clause(self) -> tuple[str, list[object]]:
        return ownership.owner_filter(getattr(self, "member_id", None))

    # -- 读 ---------------------------------------------------------------
    def get(self, uid: str) -> EventRow | None:
        clause, params = self._owner_clause()
        row = self.conn.execute(
            "SELECT * FROM app_events WHERE uid = ? AND " + clause, (uid, *params)
        ).fetchone()
        return _row_to_event(row) if row else None

    def list_events(self, *, limit: int = 200) -> list[EventRow]:
        clause, params = self._owner_clause()
        rows = self.conn.execute(
            "SELECT * FROM app_events WHERE " + clause + " ORDER BY dtstart LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [_row_to_event(row) for row in rows]

    def overrides_for(self, event_id: int) -> list[tuple[EventRow, OverrideRecord]]:
        clause, params = self._owner_clause()
        base = self.conn.execute(
            "SELECT * FROM app_events WHERE id = ? AND " + clause, (event_id, *params)
        ).fetchone()
        if base is None:
            return []
        event = _row_to_event(base)
        rows = self.conn.execute(
            "SELECT * FROM app_event_overrides WHERE event_id = ? ORDER BY recurrence_id",
            (event_id,),
        ).fetchall()
        return [
            (
                event,
                OverrideRecord(
                    recurrence_id=row["recurrence_id"],
                    is_cancelled=bool(row["is_cancelled"]),
                    range_mode=row["range_mode"],
                    dtstart=row["dtstart"],
                    dtend=row["dtend"],
                    title=row["title"],
                    location=row["location"],
                    description=row["description"],
                ),
            )
            for row in rows
        ]

    def all_overrides(self) -> list[tuple[EventRow, OverrideRecord]]:
        found: list[tuple[EventRow, OverrideRecord]] = []
        for event in self.list_events(limit=1000):
            found.extend(self.overrides_for(event.id))
        return found

    def count_overrides(self, event_id: int) -> int:
        row = self.conn.execute(
            "SELECT count(*) FROM app_event_overrides WHERE event_id = ?", (event_id,)
        ).fetchone()
        return int(row[0])

    # -- 写 ---------------------------------------------------------------
    def create_event(
        self, payload: CalendarEventCreate | dict[str, Any], *, actor: str = "agent",
        session_id: str | None = None, uid: str | None = None,
    ) -> EventRow:
        if not isinstance(payload, CalendarEventCreate):
            payload = CalendarEventCreate.model_validate(payload)
        # ⚠️ 必须用**注入的**时区(2026-10-18 审计修):这里原先是模块常量
        #    DEFAULT_TZID,改 LA_TIMEZONE 之后页面/提醒按新时区走,而新建事件仍按上海
        #    解释并落 tzid='Asia/Shanghai' —— 偏移错一小时,且从数据里看不出来。
        tzid = self.tzid
        start = _parse(payload.start, tzid)
        if payload.end:
            end = _parse(payload.end, tzid)
        elif payload.all_day:
            end = start + timedelta(days=1)
        else:
            end = start + timedelta(minutes=DEFAULT_DURATION_MINUTES)
        if end <= start:
            raise CalendarError("结束时间必须晚于开始时间")

        event_uid = uid or "la-" + uuid.uuid4().hex[:16]
        calendar_row = self.conn.execute(
            "SELECT id FROM app_calendars ORDER BY id LIMIT 1"
        ).fetchone()
        calendar_id = int(calendar_row["id"]) if calendar_row else None
        owner = ownership.owner_for_new(
            self.conn, ownership.DOMAIN_CALENDAR, getattr(self, "member_id", None)
        )
        self.conn.execute(
            "INSERT INTO app_events(calendar_id, uid, title, description, location, dtstart,"
            " dtend, all_day, tzid, rrule, reminder_minutes, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?)",
            (
                calendar_id, event_uid, payload.title.strip(), payload.description,
                payload.location, start.isoformat(timespec="seconds"),
                end.isoformat(timespec="seconds"), 1 if payload.all_day else 0, tzid,
                payload.rrule, max(int(payload.reminder_minutes), 0), now_iso(), now_iso(),
            ),
        )
        if owner is not None:
            self.conn.execute(
                "UPDATE app_events SET owner_id = ?, reminder_target_member_id = ?"
                " WHERE uid = ?",
                (owner, owner, event_uid),
            )
        self.conn.execute(
            "INSERT INTO ops_outbox(event_type, payload, idempotency_key, created_at)"
            " VALUES (?, ?, ?, ?)",
            (
                "calendar.event.created",
                _json({"uid": event_uid, "title": payload.title, "start": start.isoformat(),
                       "rrule": payload.rrule, "reminder_minutes": payload.reminder_minutes}),
                "outbox:event:" + event_uid,
                now_iso(),
            ),
        )
        self._receipt_created(event_uid, payload, start=start, actor=actor, session_id=session_id)
        self.conn.commit()
        if self._audit is not None:
            self._audit(
                actor=actor, action="calendar.event.create", target="event:" + event_uid,
                detail={"title": payload.title, "start": start.isoformat(), "rrule": payload.rrule},
                session_id=session_id,
            )
            self.conn.commit()
        event = self.get(event_uid)
        assert event is not None
        return event

    def reschedule(
        self, payload: Any, *, actor: str = "agent", session_id: str | None = None
    ) -> dict[str, Any]:
        """改期。三种例外机制由参数决定走哪一种。"""
        if not isinstance(payload, dict):
            payload = payload.model_dump()
        uid = str(payload.get("uid") or "")
        event = self.get(uid)
        if event is None:
            raise CalendarError("事件不存在: " + uid)
        recurrence_id = payload.get("recurrence_id")
        new_start = payload.get("new_start")
        if not new_start:
            raise CalendarError("必须给出 new_start")

        if not recurrence_id:
            # 没有指定哪一次 = 改整个系列(或单次事件本身)
            start = _parse(new_start, event.tzid)
            end = _parse(payload["new_end"], event.tzid) if payload.get("new_end") else None
            previous = {"dtstart": event.dtstart, "dtend": event.dtend, "title": event.title}
            self.conn.execute(
                "UPDATE app_events SET dtstart = ?, dtend = COALESCE(?, dtend),"
                " title = COALESCE(?, title), updated_at = ? WHERE uid = ?",
                (start.isoformat(timespec="seconds"),
                 end.isoformat(timespec="seconds") if end else None,
                 payload.get("new_title"), now_iso(), uid),
            )
            if self.receipts is not None:
                self.receipts.record(
                    kind="calendar.event",
                    domain=DOMAIN_CALENDAR,
                    label="改期 " + str(event.title)[:24] + " → " + start.strftime("%m-%d %H:%M"),
                    detail={"uid": uid, "start": start.isoformat(timespec="seconds"),
                            "mode": "series", "op": "reschedule"},
                    undo_plan={"type": "event", "uid": uid, "op": "reschedule_series",
                               "previous": previous, "event_id": event.id},
                    actor=actor, session_id=session_id,
                )
            self.conn.commit()
            self._emit("calendar.event.updated", {"uid": uid, "start": start.isoformat()})
            return {"uid": uid, "mode": "series", **self.get(uid).as_dict()}

        recurrence = _parse(str(recurrence_id), event.tzid)
        start = _parse(new_start, event.tzid)
        duration = None
        if event.dtend:
            duration = _parse(event.dtend, event.tzid) - _parse(event.dtstart, event.tzid)
        end = _parse(payload["new_end"], event.tzid) if payload.get("new_end") else (
            start + duration if duration else start + timedelta(minutes=DEFAULT_DURATION_MINUTES)
        )
        range_mode = RANGE_THIS_AND_FUTURE if payload.get("this_and_future") else RANGE_SINGLE
        previous = self._snapshot_override(event.id, recurrence.isoformat(), range_mode)
        self._upsert_override(
            event.id,
            recurrence.isoformat(),
            range_mode=range_mode,
            dtstart=start.isoformat(timespec="seconds"),
            dtend=end.isoformat(timespec="seconds"),
            title=payload.get("new_title"),
        )
        if self.receipts is not None:
            self.receipts.record(
                kind="calendar.event",
                domain=DOMAIN_CALENDAR,
                label=("改期这一次 " if range_mode == RANGE_SINGLE else "改期此次及以后 ")
                + str(event.title)[:20] + " → " + start.strftime("%m-%d %H:%M"),
                detail={"uid": uid, "recurrence_id": recurrence.isoformat(),
                        "start": start.isoformat(timespec="seconds"), "mode": range_mode,
                        "op": "reschedule"},
                undo_plan={"type": "event", "uid": uid, "op": "reschedule_override",
                           "event_id": event.id, "recurrence_id": recurrence.isoformat(),
                           "range_mode": range_mode, "previous": previous},
                actor=actor, session_id=session_id,
            )
        # 例外与回执必须同一事务提交(以前 helper 自己 commit,回执在另一个事务里)
        self.conn.commit()
        self._emit(
            "calendar.event.rescheduled",
            {"uid": uid, "recurrence_id": recurrence.isoformat(),
             "new_start": start.isoformat(), "range": range_mode},
        )
        return {
            "uid": uid,
            "mode": "thisandfuture" if range_mode == RANGE_THIS_AND_FUTURE else "single",
            "recurrence_id": recurrence.isoformat(),
            "new_start": start.isoformat(timespec="seconds"),
        }

    def cancel(
        self, payload: Any, *, actor: str = "agent", session_id: str | None = None
    ) -> dict[str, Any]:
        """取消。指定 recurrence_id = EXDATE(只排除那一次);不指定 = 取消整个系列。"""
        if not isinstance(payload, dict):
            payload = payload.model_dump()
        uid = str(payload.get("uid") or "")
        event = self.get(uid)
        if event is None:
            raise CalendarError("事件不存在: " + uid)
        recurrence_id = payload.get("recurrence_id")

        if not recurrence_id:
            self.conn.execute(
                "UPDATE app_events SET status = 'cancelled', updated_at = ? WHERE uid = ?",
                (now_iso(), uid),
            )
            if self.receipts is not None:
                self.receipts.record(
                    kind="calendar.event",
                    domain=DOMAIN_CALENDAR,
                    label="取消日程 " + str(event.title)[:28],
                    detail={"uid": uid, "op": "cancel", "scope": "series"},
                    undo_plan={"type": "event", "uid": uid, "op": "cancel_series",
                               "event_id": event.id},
                    actor=actor, session_id=session_id,
                )
            self.conn.commit()
            self._emit("calendar.event.cancelled", {"uid": uid, "scope": "series"})
            return {"uid": uid, "scope": "series", "status": "cancelled"}

        recurrence = _parse(str(recurrence_id), event.tzid)
        previous = self._snapshot_override(event.id, recurrence.isoformat(), RANGE_SINGLE)
        self._upsert_override(
            event.id, recurrence.isoformat(), range_mode=RANGE_SINGLE, is_cancelled=True
        )
        if self.receipts is not None:
            self.receipts.record(
                kind="calendar.event",
                domain=DOMAIN_CALENDAR,
                label="取消一次 " + str(event.title)[:20] + "(" + recurrence.strftime("%m-%d %H:%M") + ")",
                detail={"uid": uid, "op": "cancel", "scope": "single",
                        "recurrence_id": recurrence.isoformat()},
                undo_plan={"type": "event", "uid": uid, "op": "cancel_single",
                           "event_id": event.id, "recurrence_id": recurrence.isoformat(),
                           "range_mode": RANGE_SINGLE, "previous": previous},
                actor=actor, session_id=session_id,
            )
        self.conn.commit()
        self._emit(
            "calendar.event.cancelled",
            {"uid": uid, "scope": "single", "recurrence_id": recurrence.isoformat()},
        )
        return {
            "uid": uid,
            "scope": "single",
            "recurrence_id": recurrence.isoformat(timespec="seconds"),
            "mechanism": "EXDATE",
        }

    def _upsert_override(
        self,
        event_id: int,
        recurrence_id: str,
        *,
        range_mode: str,
        is_cancelled: bool = False,
        dtstart: str | None = None,
        dtend: str | None = None,
        title: str | None = None,
        clear_title: bool = False,
    ) -> None:
        # 撤销时 previous.title 可能是 NULL(这条例外本来就没设标题)。
        # COALESCE 能保留“改期时没给标题”的旧标题,却清不掉标题;所以撤销
        # 走 clear_title=True 显式写 NULL(2026-10-18 审计)。
        title_expr = "NULL" if clear_title else "COALESCE(excluded.title, title)"
        self.conn.execute(
            "INSERT INTO app_event_overrides(event_id, recurrence_id, is_cancelled, range_mode,"
            " dtstart, dtend, title, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(event_id, recurrence_id, range_mode) DO UPDATE SET"
            " is_cancelled = excluded.is_cancelled, dtstart = excluded.dtstart,"
            " dtend = excluded.dtend, title = " + title_expr,
            (event_id, recurrence_id, 1 if is_cancelled else 0, range_mode,
             dtstart, dtend, title, now_iso()),
        )
        # ⚠️ **不在这里 commit**:例外与它的回执/发件箱必须同一事务(调用方统一提交)。
        #    以前 helper 自己提交,进程在"例外已写、回执没写"之间死掉就永远没有撤销入口。


    # -- 写入回执(M7)-----------------------------------------------------
    def _receipt_created(
        self, uid: str, payload: Any, *, start: datetime,
        actor: str = "agent", session_id: str | None = None,
    ) -> int | None:
        if self.receipts is None:
            return None
        when = start.strftime("%m-%d %H:%M")
        repeat = "(重复)" if getattr(payload, "rrule", None) else ""
        return self.receipts.record(
            kind="calendar.event",
            domain=DOMAIN_CALENDAR,
            label="记一个日程 " + when + " " + str(payload.title).strip()[:32] + repeat,
            detail={
                "uid": uid,
                "title": payload.title,
                "start": start.isoformat(timespec="seconds"),
                "rrule": getattr(payload, "rrule", None),
                "op": "create",
            },
            undo_plan={"type": "event", "uid": uid, "op": "create"},
            actor=actor,
            session_id=session_id,
        )

    def revert_event(self, receipt: Receipt) -> str:
        """撤销日程写入:新建 -> 取消;改期/取消 -> 按 op 退回去。"""
        plan = receipt.undo_plan or {}
        uid = str(plan.get("uid") or "")
        if not uid:
            raise ReceiptError("这条日程回执没有 uid,撤不了")
        op = str(plan.get("op") or "")
        stamp = now_iso()
        if op == "create":
            self.conn.execute(
                "UPDATE app_events SET status = 'cancelled', updated_at = ? WHERE uid = ?",
                (stamp, uid),
            )
            label = "已取消刚建的日程"
        elif op == "cancel_series":
            self.conn.execute(
                "UPDATE app_events SET status = 'confirmed', updated_at = ? WHERE uid = ?",
                (stamp, uid),
            )
            label = "已把日程恢复成未取消"
        elif op in ("reschedule_series", "reschedule_override", "cancel_single"):
            self._revert_override(plan)
            label = "已撤销这次改期/例外"
        else:
            raise ReceiptError("不认识的日程回执类型: " + op)
        self._emit("calendar.event.reverted", {"uid": uid, "op": op})
        self.conn.commit()
        return label

    def _revert_override(self, plan: dict[str, Any]) -> None:
        event_id = plan.get("event_id")
        recurrence_id = plan.get("recurrence_id")
        range_mode = plan.get("range_mode") or RANGE_SINGLE
        if event_id is None or not recurrence_id:
            raise ReceiptError("这条回执缺少例外定位信息,撤不了")
        previous = plan.get("previous")
        if previous is None:
            # 之前没有这条例外 -> 整行删掉(删除的是"刚加的那个例外",不是用户数据)
            self.conn.execute(
                "DELETE FROM app_event_overrides WHERE event_id = ? AND recurrence_id = ?"
                " AND range_mode = ?",
                (int(event_id), str(recurrence_id), str(range_mode)),
            )
        else:
            self._upsert_override(
                int(event_id),
                str(recurrence_id),
                range_mode=str(range_mode),
                is_cancelled=bool(previous.get("is_cancelled")),
                dtstart=previous.get("dtstart"),
                dtend=previous.get("dtend"),
                title=previous.get("title"),
                clear_title=previous.get("title") is None,
            )

    def _snapshot_override(self, event_id: int, recurrence_id: str, range_mode: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT is_cancelled, dtstart, dtend, title FROM app_event_overrides"
            " WHERE event_id = ? AND recurrence_id = ? AND range_mode = ?",
            (event_id, recurrence_id, range_mode),
        ).fetchone()
        if row is None:
            return None
        return {
            "is_cancelled": bool(row["is_cancelled"]),
            "dtstart": row["dtstart"],
            "dtend": row["dtend"],
            "title": row["title"],
        }

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO ops_outbox(event_type, payload, idempotency_key, created_at)"
            " VALUES (?, ?, ?, ?)",
            (
                event_type,
                _json(payload),
                "outbox:" + event_type + ":" + uuid.uuid4().hex[:16],
                now_iso(),
            ),
        )
        self.conn.commit()

    # -- 展开与导出 -------------------------------------------------------
    def expand(
        self, *, start: datetime | str, end: datetime | str, tzid: str | None = None
    ) -> tuple[list[Occurrence], bool]:
        events = [row.as_record() for row in self.list_events(limit=1000)]
        overrides = self.all_overrides()
        tzid = tzid or self.tzid
        start_moment = _parse(start, tzid) if isinstance(start, str) else start
        end_moment = _parse(end, tzid) if isinstance(end, str) else end
        return expand_ical(events, overrides, start=start_moment, end=end_moment, tzid=tzid)

    def list_occurrences(
        self, *, start: str, end: str, tzid: str | None = None, limit: int = 200
    ) -> tuple[list[OccurrenceRead], bool]:
        occurrences, truncated = self.expand(start=start, end=end, tzid=tzid)
        return (
            [
                OccurrenceRead(
                    uid=item.uid,
                    instance_id=item.instance_id,
                    original_start=item.original_start,
                    start=item.start.isoformat(timespec="seconds"),
                    end=item.end.isoformat(timespec="seconds") if item.end else None,
                    title=item.title,
                    location=item.location,
                    all_day=item.all_day,
                    is_override=item.is_override,
                    is_recurring=item.is_recurring,
                )
                for item in occurrences[:limit]
            ],
            truncated or len(occurrences) > limit,
        )

    def export_ics(self) -> str:
        """只读 .ics 订阅 —— 不做 CalDAV,省掉 80% 复杂度。"""
        calendar = build_calendar(
            [row.as_record() for row in self.list_events(limit=2000)], self.all_overrides()
        )
        return calendar.to_ical().decode("utf-8")

    def write_ics(self, path: str | Path) -> Path:
        target = Path(path)
        target.write_text(self.export_ics(), encoding="utf-8")
        return target

    def find_slot(self, *, after: str, minutes: int = 60, tzid: str | None = None) -> str | None:
        """从 after 开始往后找一个至少 minutes 分钟的空档(简单版:只看同一天 08:00–22:00)。"""
        tzid = tzid or self.tzid
        moment = _parse(after, tzid)
        busy_end = moment
        for offset in range(0, 8):
            day_start = (moment + timedelta(days=offset)).replace(
                hour=8, minute=0, second=0, microsecond=0
            )
            day_end = day_start.replace(hour=22)
            occurrences, _ = self.expand(start=day_start, end=day_end, tzid=tzid)
            cursor = max(day_start, moment)
            for item in sorted(occurrences, key=lambda x: x.start):
                finish = item.end or (item.start + timedelta(minutes=DEFAULT_DURATION_MINUTES))
                if item.start - cursor >= timedelta(minutes=minutes):
                    return cursor.isoformat(timespec="seconds")
                cursor = max(cursor, finish)
            if day_end - cursor >= timedelta(minutes=minutes):
                return cursor.isoformat(timespec="seconds")
        del busy_end
        return None


def _json(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, default=str)
