"""R2a 验收:领域 owner_id 列与共享模式。

运行:
    .venv/Scripts/python.exe -m unittest tests.test_ownership -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from la.domain import ownership
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT

OWNER_TABLES = (
    "app_txns", "app_events", "app_problems", "app_review_units",
    "app_measurements", "app_workouts", "app_food_logs",
)


class OwnershipTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.conn = db.connect(self.tmp / "assistant.db")
        db.migrate(self.conn, MIGRATIONS_ASSISTANT)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_owner_columns_exist(self) -> None:
        for table in OWNER_TABLES:
            cols = {row[1] for row in self.conn.execute("PRAGMA table_info(" + table + ")")}
            self.assertIn("owner_id", cols, table)

    def test_defaults_and_switch(self) -> None:
        self.assertEqual(ownership.get_share_mode(self.conn, "ledger"), "own")
        self.assertEqual(ownership.get_share_mode(self.conn, "calendar"), "own")
        self.assertEqual(ownership.get_share_mode(self.conn, "health"), "own")
        self.assertEqual(ownership.get_share_mode(self.conn, "study"), "family")
        ownership.set_share_mode(self.conn, "ledger", "family")
        self.assertEqual(ownership.get_share_mode(self.conn, "ledger"), "family")
        ownership.set_share_mode(self.conn, "ledger", "own")
        self.assertEqual(ownership.get_share_mode(self.conn, "ledger"), "own")
        with self.assertRaises(ValueError):
            ownership.set_share_mode(self.conn, "ledger", "maybe")
        for domain in ("calendar", "health"):
            with self.assertRaises(ValueError):
                ownership.set_share_mode(self.conn, domain, "family")
            self.assertEqual(ownership.get_share_mode(self.conn, domain), "own")

    def test_owner_for_new(self) -> None:
        self.assertEqual(ownership.owner_for_new(self.conn, "ledger", 2), 2)
        ownership.set_share_mode(self.conn, "ledger", "family")
        self.assertIsNone(ownership.owner_for_new(self.conn, "ledger", 2))
        self.assertIsNone(ownership.owner_for_new(self.conn, "ledger", None))
        self.assertEqual(ownership.owner_for_new(self.conn, "health", 2), 2)
        self.assertEqual(ownership.owner_for_new(self.conn, "calendar", 2), 2)
        self.assertIsNone(ownership.owner_for_new(self.conn, "study", 2))
        ownership.set_share_mode(self.conn, "study", "own")
        self.assertEqual(ownership.owner_for_new(self.conn, "study", 2), 2)

    def test_measurement_rebuild_preserves_rows(self) -> None:
        conn = db.connect(self.tmp / "rebuild.db")
        try:
            db.migrate(conn, MIGRATIONS_ASSISTANT[:22])
            conn.execute(
                "INSERT INTO app_measurements(metric_code, value, unit, measured_at,"
                " local_date, source, note, created_at, owner_id)"
                " VALUES ('weight', 70, 'kg', '2026-10-08T08:00:00+08:00',"
                " '2026-10-08', 'user', NULL, '2026-10-08T08:00:00+08:00', NULL)"
            )
            conn.commit()
            db.migrate(conn, MIGRATIONS_ASSISTANT)
            rows = conn.execute(
                "SELECT value, owner_id FROM app_measurements WHERE metric_code='weight'"
            ).fetchall()
            self.assertEqual([(row[0], row[1]) for row in rows], [(70.0, 1)])
            conn.execute(
                "INSERT INTO app_measurements(metric_code, value, unit, measured_at,"
                " local_date, source, note, created_at, owner_id)"
                " VALUES ('weight', 80, 'kg', '2026-10-08T08:00:00+08:00',"
                " '2026-10-08', 'user', NULL, '2026-10-08T08:00:00+08:00', 2)"
            )
            conn.commit()
        finally:
            conn.close()

    def test_visible_and_filter(self) -> None:
        self.assertTrue(ownership.visible(None, 2))
        self.assertTrue(ownership.visible(2, 2))
        self.assertFalse(ownership.visible(3, 2))
        self.assertTrue(ownership.visible(3, None))
        sql, params = ownership.owner_filter(2)
        self.assertIn("owner_id", sql)
        self.assertEqual(params, [2])
        sql, params = ownership.owner_filter(None)
        self.assertEqual(sql, "1=1")
        self.assertEqual(params, [])


if __name__ == "__main__":
    unittest.main()
