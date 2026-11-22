"""M4.2 端到端验收:RRULE 展开、例外三种机制、提醒重启不重发、只读订阅。

    .venv/Scripts/python.exe scripts/verify_m4_2.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
DATA_DIR = tempfile.mkdtemp(prefix="la-verify42-")

os.environ["LA_DATA_DIR"] = DATA_DIR
os.environ["LA_LLM_PROVIDER"] = "fake"
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.path.insert(0, str(BASE_DIR))

TZ = ZoneInfo("Asia/Shanghai")
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + ("   " + detail if detail else ""))


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, "-m", "la", *args], cwd=str(BASE_DIR), env=dict(os.environ),
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )


def main() -> int:
    from la.domain.audit import AuditSink
    from la.domain.calendars.reminders import ReminderService
    from la.domain.schemas.calendar import CalendarEventCreate
    from la.domain.services.calendar_service import CalendarService
    from la.storage import db

    print("验收数据目录: " + DATA_DIR)
    now = datetime.now(TZ)
    # 把"下周一 10:00"作为系列起点
    days_ahead = (7 - now.weekday()) % 7 or 7
    first = (now + timedelta(days=days_ahead)).replace(hour=10, minute=0, second=0, microsecond=0)

    conn = db.connect_assistant()
    service = CalendarService(conn, audit=AuditSink(conn))
    reminders = ReminderService(conn, service)

    print("\n[1/5] 建重复日程并展开")
    uid = service.create_event(CalendarEventCreate(
        title="周会", start=first.isoformat(),
        rrule="FREQ=WEEKLY;BYDAY=MO;COUNT=6", location="会议室",
    )).uid
    window = dict(start=now.isoformat(), end=(first + timedelta(days=45)).isoformat())
    occurrences, _ = service.expand(**window)
    check("重复事件展开成 6 次", len(occurrences) == 6, "实际 " + str(len(occurrences)))
    check("每次都是一小时", all((o.end - o.start) == timedelta(hours=1) for o in occurrences))

    print("\n[2/5] 例外三种机制")
    second = occurrences[1].start
    third = occurrences[2].start
    service.cancel({"uid": uid, "recurrence_id": second.isoformat()})
    after_cancel, _ = service.expand(**window)
    check("EXDATE:只少一次", len(after_cancel) == 5, "实际 " + str(len(after_cancel)))
    check("EXDATE:被排除的那次真的不见了",
          second.strftime("%m-%d") not in [o.start.strftime("%m-%d") for o in after_cancel])

    service.reschedule({"uid": uid, "recurrence_id": third.isoformat(),
                        "new_start": third.replace(hour=16).isoformat(),
                        "new_title": "周会(改到下午)"})
    after_move, _ = service.expand(**window)
    moved = {o.start.strftime("%m-%d"): o for o in after_move}
    check("RECURRENCE-ID:那一次被挪到 16:00", moved[third.strftime("%m-%d")].start.hour == 16)
    check("RECURRENCE-ID:标题也换了", moved[third.strftime("%m-%d")].title == "周会(改到下午)")
    check("RECURRENCE-ID:其余几次不受影响",
          moved[occurrences[0].start.strftime("%m-%d")].start.hour == 10)
    check("实例带 original_start 便于再改", moved[third.strftime("%m-%d")].original_start is not None)

    later = occurrences[4].start
    service.reschedule({"uid": uid, "recurrence_id": later.isoformat(),
                        "new_start": later.replace(hour=18).isoformat(), "this_and_future": True})
    after_future, _ = service.expand(**window)
    times = {o.start.strftime("%m-%d"): o.start.hour for o in after_future}
    check("THISANDFUTURE:此次及以后都改",
          times[later.strftime("%m-%d")] == 18
          and times[occurrences[5].start.strftime("%m-%d")] == 18)
    check("THISANDFUTURE:之前的不受影响",
          times[occurrences[0].start.strftime("%m-%d")] == 10)

    print("\n[3/5] 展开上限")
    service.create_event(CalendarEventCreate(
        title="每天", start="2020-01-01T10:00:00+08:00", rrule="FREQ=DAILY", reminder_minutes=0
    ))
    huge, huge_truncated = service.expand(
        start=now.isoformat(), end=(now + timedelta(days=3650)).isoformat()
    )
    one_year, _ = service.expand(start=now.isoformat(), end=(now + timedelta(days=400)).isoformat())
    check("⭐ 十年窗口被切到上限内(防 DoS)", huge_truncated and len(huge) == len(one_year),
          "返回 " + str(len(huge)) + " 个实例(不设上限会是 3650+)")
    check("截断会被如实上报(调用方知道看到的不全)", huge_truncated)

    print("\n[4/5] 提醒:重启不重发")
    soon = now + timedelta(minutes=5)
    service.create_event(CalendarEventCreate(
        title="马上要开的会", start=soon.isoformat(), reminder_minutes=30
    ))
    created = reminders.materialize(now=now)
    check("物化出了提醒实例", created >= 1, str(created) + " 条")
    pending = reminders.pending(now=now)
    check("到点的提醒能被捡起来", len(pending) >= 1, str(len(pending)) + " 条")

    proc = run("--reminders")
    check("独立进程派发了提醒", "⏰" in proc.stdout, proc.stdout.strip()[:80].replace("\n", " "))
    proc = run("--reminders")
    check("⭐ 重启后不重复提醒(抢占式更新)", "没有到点的提醒" in proc.stdout,
          proc.stdout.strip()[:80].replace("\n", " "))

    print("\n[5/5] 只读订阅与 CLI")
    ics_path = Path(DATA_DIR) / "calendar.ics"
    proc = run("--ics", str(ics_path))
    check("导出 .ics 成功", ics_path.exists() and "已导出" in proc.stdout)
    text = ics_path.read_text(encoding="utf-8")
    check(".ics 含 RRULE 与例外", "RRULE" in text and "RECURRENCE-ID" in text)
    check(".ics 含 VALARM 提醒", "BEGIN:VALARM" in text)

    import icalendar
    import recurring_ical_events

    parsed = icalendar.Calendar.from_ical(text)
    raw = recurring_ical_events.of(parsed).between(now, first + timedelta(days=45))
    kept = [c for c in raw if str(c.get("STATUS") or "").upper() != "CANCELLED"]
    current, _ = service.expand(**window)
    check("导出再解析的实例数与内部一致", len(kept) == len(current),
          "导出 " + str(len(kept)) + " vs 内部 " + str(len(current)))

    proc = run("--agenda", "60")
    check("--agenda 能列出日程", "周会" in proc.stdout, proc.stdout.strip()[:80].replace("\n", " "))
    check("--agenda 标出被改过的那一次", "✎" in proc.stdout)
    check("--agenda 不会把普通重复实例误标成被改过",
          proc.stdout.count("✎") <= 4, "✎ 出现 " + str(proc.stdout.count("✎")) + " 次")

    conn.close()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n结果: " + str(passed) + "/" + str(len(RESULTS)) + " 项通过")
    for name, ok, _ in RESULTS:
        if not ok:
            print("  未通过: " + name)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())