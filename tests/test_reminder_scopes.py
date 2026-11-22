"""R4 验收:日程提醒/待办闹钟只提醒对应用户。"""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from la import config
from la.bridge.reminder_push import ReminderPusher
from la.domain.schemas.calendar import CalendarEventCreate
from la.domain.services.calendar_service import CalendarService
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT, MIGRATIONS_MEMORY
from la.tasks.schedules import ScheduleStore
from la.tasks.todos import TodoStore

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 12, 19, 0, tzinfo=TZ)


class ReminderScopeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.memory = db.connect(self.tmp / "memory.db")
        db.migrate(self.memory, MIGRATIONS_MEMORY)
        self.assistant = db.connect(self.tmp / "assistant.db")
        db.migrate(self.assistant, MIGRATIONS_ASSISTANT)

    def tearDown(self) -> None:
        self.memory.close()
        self.assistant.close()
        self._tmp.cleanup()

    async def test_calendar_reminder_targets_the_event_owner(self):
        service = CalendarService(self.assistant, member_id=2)
        start = NOW + timedelta(minutes=60)
        event = service.create_event(CalendarEventCreate(
            title="妈妈的会",
            start=start.isoformat(timespec="seconds"),
            end=(start + timedelta(hours=1)).isoformat(timespec="seconds"),
            reminder_minutes=30,
        ))
        self.assertEqual(event.owner_id, 2)
        self.assertEqual(event.reminder_target_member_id, 2)

        async def send(text: str):
            return True, "ok"

        async def send_to(text: str, member_id: int | None):
            return True, "ok"

        pusher = ReminderPusher(
            self.memory, self.assistant, send=send, send_to=send_to,
            settings=dataclasses.replace(
                config.get_settings(reload=True), timezone="Asia/Shanghai"
            ),
            log=lambda line: None,
        )
        try:
            pusher.materialize(NOW)
            items = pusher._calendar_items(NOW + timedelta(minutes=31))
            self.assertEqual([item.target_member_id for item in items], [2])
        finally:
            pusher.close()

    async def test_todo_alarm_targets_the_todo_owner(self):
        todos = TodoStore(self.memory, member_id=3)
        todo = todos.add(
            "爸爸的会",
            due_at_local=(NOW + timedelta(hours=1)).isoformat(timespec="seconds"),
            remind_minutes_before=0,
        )
        self.assertEqual(todo.owner_id, 3)
        alarms = ScheduleStore(self.memory).for_todo(todo.id)
        self.assertEqual(len(alarms), 1)
        self.assertEqual(alarms[0].target_member_id, 3)


if __name__ == "__main__":
    unittest.main()
