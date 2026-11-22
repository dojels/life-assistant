"""R2b 验收:日程 owner_id 的写入与读取过滤。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from la.domain import ownership
from la.domain.schemas.calendar import CalendarEventCreate
from la.domain.services.calendar_service import CalendarService
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT


class CalendarOwnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.conn = db.connect(self.tmp / "assistant.db")
        db.migrate(self.conn, MIGRATIONS_ASSISTANT)
        self.service = CalendarService(self.conn, member_id=2)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _create(self, member: int, title: str):
        db.set_member(self.conn, member)
        self.service.member_id = member
        return self.service.create_event(
            CalendarEventCreate(title=title, start="2026-10-20T09:00:00+08:00")
        )

    def test_own_mode_filters_events(self) -> None:
        mom = self._create(2, "妈妈的会")
        dad = self._create(3, "爸爸的会")
        self.assertEqual(
            self.conn.execute("SELECT owner_id FROM app_events WHERE uid = ?", (mom.uid,)).fetchone()[0],
            2,
        )
        db.set_member(self.conn, 2)
        self.service.member_id = 2
        self.assertEqual([event.uid for event in self.service.list_events()], [mom.uid])
        self.assertIsNone(self.service.get(dad.uid))
        db.set_member(self.conn, 3)
        self.service.member_id = 3
        self.assertEqual([event.uid for event in self.service.list_events()], [dad.uid])

    def test_calendar_cannot_be_shared(self) -> None:
        with self.assertRaises(ValueError):
            ownership.set_share_mode(self.conn, "calendar", "family")
        event = self._create(2, "妈妈的私事")
        self.assertEqual(
            self.conn.execute("SELECT owner_id FROM app_events WHERE uid = ?", (event.uid,)).fetchone()[0],
            2,
        )
        db.set_member(self.conn, 3)
        self.service.member_id = 3
        self.assertNotIn(event.uid, [row.uid for row in self.service.list_events()])


if __name__ == "__main__":
    unittest.main()
