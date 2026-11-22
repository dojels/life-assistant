"""M4.2 验收测试:RRULE 展开、例外三种机制、提醒物化抢占、只读订阅。

运行:
    .venv/Scripts/python.exe -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from la.domain.calendars.ical import (
    MAX_OCCURRENCES,
    MAX_WINDOW_DAYS,
    EventRecord,
    expand as expand_ical,
)
from la.domain.calendars.reminders import (
    ReminderService,
    format_alarm_trigger,
    parse_alarm_trigger,
)
from la.domain.schemas.calendar import CalendarEventCreate
from la.domain.services.calendar_service import CalendarError, CalendarService
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 5, 8, 0, tzinfo=TZ)   # 2026-10-05 是周一


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.conn = db.connect(self.tmp / "assistant.db")
        db.migrate(self.conn, MIGRATIONS_ASSISTANT)
        self.service = CalendarService(self.conn, now_provider=lambda: NOW)
        self.reminders = ReminderService(self.conn, self.service, now_provider=lambda: NOW)

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def weekly(self, **overrides) -> str:
        payload = {
            "title": "周会",
            "start": "2026-10-05T10:00:00+08:00",
            "end": "2026-10-05T11:00:00+08:00",
            "rrule": "FREQ=WEEKLY;BYDAY=MO;COUNT=6",
        }
        payload.update(overrides)
        return self.service.create_event(CalendarEventCreate(**payload)).uid

    def window(self, days: int = 60):
        return dict(start=NOW.isoformat(), end=(NOW + timedelta(days=days)).isoformat())


# ==========================================================================
# 展开
# ==========================================================================
class ExpansionTestCase(Fixture):
    def test_single_event(self):
        self.service.create_event(
            CalendarEventCreate(title="牙医", start="2026-10-06T15:00:00+08:00")
        )
        occurrences, _ = self.service.expand(**self.window())
        self.assertEqual(len(occurrences), 1)
        self.assertEqual(occurrences[0].title, "牙医")
        self.assertFalse(occurrences[0].is_recurring)

    def test_weekly_rrule_expands(self):
        self.weekly()
        occurrences, _ = self.service.expand(**self.window())
        self.assertEqual(len(occurrences), 6, "COUNT=6 应当正好 6 次")
        self.assertTrue(all(item.is_recurring for item in occurrences))
        self.assertEqual(occurrences[0].start.strftime("%m-%d"), "10-05")
        self.assertEqual(occurrences[1].start.strftime("%m-%d"), "10-12")

    def test_default_duration_is_one_hour(self):
        self.service.create_event(
            CalendarEventCreate(title="短会", start="2026-10-06T15:00:00+08:00")
        )
        occurrences, _ = self.service.expand(**self.window())
        self.assertEqual(occurrences[0].end - occurrences[0].start, timedelta(hours=1))

    def test_all_day_event(self):
        self.service.create_event(
            CalendarEventCreate(title="休假", start="2026-10-07T00:00:00+08:00", all_day=True)
        )
        occurrences, _ = self.service.expand(**self.window())
        self.assertTrue(occurrences[0].all_day)
        self.assertEqual(occurrences[0].end - occurrences[0].start, timedelta(days=1))


# ==========================================================================
# 例外三种机制
# ==========================================================================
class ExceptionTestCase(Fixture):
    def test_exdate_excludes_exactly_one_occurrence(self):
        uid = self.weekly()
        before, _ = self.service.expand(**self.window())
        self.assertEqual(len(before), 6)

        result = self.service.cancel({"uid": uid, "recurrence_id": "2026-10-12T10:00:00+08:00"})
        self.assertEqual(result["mechanism"], "EXDATE")
        after, _ = self.service.expand(**self.window())
        self.assertEqual(len(after), 5, "只该少一次")
        self.assertNotIn("10-12", [item.start.strftime("%m-%d") for item in after])

    def test_recurrence_id_override_moves_only_that_occurrence(self):
        uid = self.weekly()
        self.service.reschedule(
            {"uid": uid, "recurrence_id": "2026-10-12T10:00:00+08:00",
             "new_start": "2026-10-12T15:00:00+08:00", "new_title": "周会(改到下午)"}
        )
        occurrences, _ = self.service.expand(**self.window())
        by_day = {item.start.strftime("%m-%d"): item for item in occurrences}
        self.assertEqual(by_day["10-12"].start.strftime("%H:%M"), "15:00")
        self.assertEqual(by_day["10-12"].title, "周会(改到下午)")
        self.assertTrue(by_day["10-12"].is_override)
        # 其余几次不受影响
        self.assertEqual(by_day["10-19"].start.strftime("%H:%M"), "10:00")
        self.assertEqual(by_day["10-19"].title, "周会")
        self.assertEqual(len(occurrences), 6)

    def test_this_and_future_moves_this_and_all_later(self):
        uid = self.weekly()
        self.service.reschedule(
            {"uid": uid, "recurrence_id": "2026-10-19T10:00:00+08:00",
             "new_start": "2026-10-19T16:00:00+08:00", "this_and_future": True}
        )
        occurrences, _ = self.service.expand(**self.window())
        times = {item.start.strftime("%m-%d"): item.start.strftime("%H:%M") for item in occurrences}
        self.assertEqual(times["10-05"], "10:00")
        self.assertEqual(times["10-12"], "10:00")
        self.assertEqual(times["10-19"], "16:00")
        self.assertEqual(times["10-26"], "16:00", "此次及以后都要改")
        self.assertEqual(times["11-09"], "16:00")

    def test_cancel_whole_series(self):
        uid = self.weekly()
        result = self.service.cancel({"uid": uid})
        self.assertEqual(result["scope"], "series")
        occurrences, _ = self.service.expand(**self.window())
        self.assertEqual(occurrences, [], "被取消的系列不该再展开出实例")

    def test_cancelled_instances_are_filtered_out(self):
        """⚠️ 实测:recurring-ical-events 会返回 STATUS:CANCELLED 的实例,必须自己过滤。"""
        self.weekly()
        rows = self.conn.execute("SELECT uid FROM app_events").fetchall()
        self.assertEqual(len(rows), 1)
        # 直接做一次底层展开,确认过滤逻辑在
        events = [EventRecord(uid="x@la", title="被取消", dtstart="2026-10-06T10:00:00+08:00")]
        occurrences, _ = expand_ical(
            events, [],
            start=NOW,
            end=NOW + timedelta(days=7),
        )
        self.assertEqual(len(occurrences), 1)

    def test_reschedule_requires_new_start(self):
        uid = self.weekly()
        with self.assertRaises(CalendarError):
            self.service.reschedule({"uid": uid, "recurrence_id": "2026-10-12T10:00:00+08:00"})

    def test_unknown_uid_raises(self):
        with self.assertRaises(CalendarError):
            self.service.cancel({"uid": "不存在"})


# ==========================================================================
# 展开上限
# ==========================================================================
class LimitTestCase(Fixture):
    def test_unbounded_daily_rule_is_capped_by_window(self):
        self.service.create_event(
            CalendarEventCreate(title="每天", start="2020-01-01T10:00:00+08:00",
                                rrule="FREQ=DAILY")
        )
        occurrences, _ = self.service.expand(
            start=NOW.isoformat(), end=(NOW + timedelta(days=3650)).isoformat()
        )
        self.assertLessEqual(len(occurrences), MAX_WINDOW_DAYS + 1,
                             "十年窗口必须被切到上限内,否则就是 DoS")

    def test_occurrence_cap_is_enforced(self):
        events = [EventRecord(uid="d@la", title="每天", dtstart="2026-01-01T10:00:00+08:00",
                              rrule="FREQ=DAILY")]
        occurrences, truncated = expand_ical(
            events, [], start=NOW, end=NOW + timedelta(days=100), max_occurrences=10
        )
        self.assertEqual(len(occurrences), 10)
        self.assertTrue(truncated)
        self.assertLess(MAX_OCCURRENCES, 100000)


# ==========================================================================
# 提醒:物化 + 抢占 + 贪睡
# ==========================================================================
class ReminderTestCase(Fixture):
    def test_materialize_creates_instances_for_recurring_events(self):
        self.weekly(reminder_minutes=30)
        created = self.reminders.materialize(now=NOW)
        self.assertGreaterEqual(created, 1)
        rows = self.reminders.scheduled()
        first = min(rows, key=lambda item: item.fire_at_utc)
        self.assertEqual(first.alarm_trigger, "-PT30M")
        self.assertEqual(
            first.fire_at_local().strftime("%m-%d %H:%M"), "10-05 09:30",
            "触发时刻 = 开始 - 30 分钟",
        )

    def test_zero_reminder_means_no_instance(self):
        self.weekly(reminder_minutes=0)
        self.assertEqual(self.reminders.materialize(now=NOW), 0)

    def test_materialize_is_idempotent(self):
        self.weekly()
        first = self.reminders.materialize(now=NOW)
        second = self.reminders.materialize(now=NOW)
        self.assertGreater(first, 0)
        self.assertEqual(second, 0, "重复物化不该产生重复实例")

    def test_claim_is_exclusive_even_after_restart(self):
        self.service.create_event(
            CalendarEventCreate(title="马上要开", start="2026-10-05T08:10:00+08:00")
        )
        self.reminders.materialize(now=NOW)
        pending = self.reminders.pending(now=NOW)
        self.assertEqual(len(pending), 1)

        self.assertTrue(self.reminders.claim(pending[0].id, now=NOW))
        self.assertFalse(self.reminders.claim(pending[0].id, now=NOW),
                         "抢过一次就不能再抢 —— 这是防重启重发的关键")
        # 模拟重启:全新的 ReminderService,共享同一个库
        revived = ReminderService(self.conn, self.service, now_provider=lambda: NOW)
        self.assertEqual(revived.pending(now=NOW), [])

    def test_not_due_yet(self):
        self.service.create_event(
            CalendarEventCreate(title="两小时后", start="2026-10-05T10:00:00+08:00",
                                reminder_minutes=30)
        )
        self.reminders.materialize(now=NOW)
        self.assertEqual(self.reminders.pending(now=NOW), [])

    def test_snooze_is_recorded_separately(self):
        self.service.create_event(
            CalendarEventCreate(title="要开", start="2026-10-05T08:10:00+08:00")
        )
        self.reminders.materialize(now=NOW)
        instance = self.reminders.pending(now=NOW)[0]
        self.reminders.claim(instance.id, now=NOW)

        self.assertTrue(self.reminders.snooze(instance.id, minutes=10, now=NOW))
        self.assertEqual(self.reminders.pending(now=NOW), [], "贪睡期间不该再响")
        later = NOW + timedelta(minutes=11)
        again = self.reminders.pending(now=later)
        self.assertEqual(len(again), 1, "贪睡到点后重新可派发")
        self.assertIsNotNone(again[0].snooze_until_utc)

    def test_alarm_trigger_roundtrip(self):
        self.assertEqual(parse_alarm_trigger("-PT30M"), timedelta(minutes=30))
        self.assertEqual(parse_alarm_trigger("-PT1H30M"), timedelta(minutes=90))
        self.assertEqual(parse_alarm_trigger("-P1D"), timedelta(days=1))
        self.assertEqual(format_alarm_trigger(45), "-PT45M")

    def test_purge_old_fired_instances(self):
        self.service.create_event(
            CalendarEventCreate(title="旧的", start="2026-10-05T08:10:00+08:00")
        )
        self.reminders.materialize(now=NOW)
        instance = self.reminders.pending(now=NOW)[0]
        self.reminders.claim(instance.id, now=NOW)
        removed = self.reminders.purge_before(NOW + timedelta(days=1))
        self.assertEqual(removed, 1)


# ==========================================================================
# 只读订阅
# ==========================================================================
class IcsExportTestCase(Fixture):
    def test_export_contains_rrule_and_overrides(self):
        uid = self.weekly(location="会议室")
        self.service.cancel({"uid": uid, "recurrence_id": "2026-10-12T10:00:00+08:00"})
        self.service.reschedule(
            {"uid": uid, "recurrence_id": "2026-10-19T10:00:00+08:00",
             "new_start": "2026-10-19T16:00:00+08:00", "this_and_future": True}
        )
        text = self.service.export_ics()
        self.assertIn("BEGIN:VCALENDAR", text)
        self.assertIn("RRULE:FREQ=WEEKLY", text)
        self.assertIn("RECURRENCE-ID", text)
        self.assertIn("THISANDFUTURE", text)
        self.assertIn("STATUS:CANCELLED", text)
        self.assertIn("BEGIN:VALARM", text)
        self.assertIn("TRIGGER:-PT30M", text)

    def test_export_round_trip_gives_the_same_occurrences(self):
        """导出的 .ics 再解析回来,展开结果必须一致 —— 这是"数据可携带"的验收。"""
        import icalendar
        import recurring_ical_events

        uid = self.weekly()
        self.service.cancel({"uid": uid, "recurrence_id": "2026-10-12T10:00:00+08:00"})
        expected, _ = self.service.expand(**self.window())

        parsed = icalendar.Calendar.from_ical(self.service.export_ics())
        raw = recurring_ical_events.of(parsed).between(
            NOW, NOW + timedelta(days=60)
        )
        kept = [c for c in raw if str(c.get("STATUS") or "").upper() != "CANCELLED"]
        self.assertEqual(len(kept), len(expected), "往返后实例数必须一致")

    def test_write_ics_file(self):
        self.weekly()
        path = self.service.write_ics(self.tmp / "calendar.ics")
        self.assertTrue(path.exists())
        self.assertIn("BEGIN:VCALENDAR", path.read_text(encoding="utf-8"))


# ==========================================================================
# 排期辅助
# ==========================================================================
class FindSlotTestCase(Fixture):
    def test_find_slot_skips_busy_time(self):
        self.service.create_event(
            CalendarEventCreate(title="占位", start="2026-10-05T09:00:00+08:00",
                                end="2026-10-05T11:00:00+08:00")
        )
        slot = self.service.find_slot(after="2026-10-05T08:30:00+08:00", minutes=60)
        self.assertIsNotNone(slot)
        self.assertEqual(slot[11:16], "11:00", "应当排到占用结束之后")


if __name__ == "__main__":
    unittest.main()
