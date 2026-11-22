"""R2b 验收:健康 measurements/workouts/food_logs 的 owner 过滤。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from la.domain import ownership
from la.domain.schemas.health import FoodLogCreate, MeasurementCreate, WorkoutCreate
from la.domain.services.health_service import HealthService
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT


class HealthOwnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.conn = db.connect(self.tmp / "assistant.db")
        db.migrate(self.conn, MIGRATIONS_ASSISTANT)
        self.health = HealthService(self.conn, member_id=2)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _use(self, member: int) -> None:
        db.set_member(self.conn, member)
        self.health.member_id = member

    def test_measurements_are_owner_scoped_and_same_time_can_coexist(self) -> None:
        self._use(2)
        self.health.log_measurement(
            MeasurementCreate(metric_code="weight", value=70,
                              measured_at="2026-10-08T08:00:00+08:00")
        )
        self._use(3)
        self.health.log_measurement(
            MeasurementCreate(metric_code="weight", value=80,
                              measured_at="2026-10-08T08:00:00+08:00")
        )
        rows = self.conn.execute(
            "SELECT owner_id, value FROM app_measurements WHERE metric_code='weight'"
            " ORDER BY owner_id"
        ).fetchall()
        self.assertEqual([(row[0], row[1]) for row in rows], [(2, 70.0), (3, 80.0)])

        self._use(2)
        self.assertEqual(self.health.latest("weight")["value"], 70.0)
        self._use(3)
        self.assertEqual(self.health.latest("weight")["value"], 80.0)

    def test_workout_and_food_are_owner_scoped(self) -> None:
        self._use(2)
        self.health.log_workout(WorkoutCreate(activity="跑步", duration_min=30,
                                             ts="2026-10-08T07:00:00+08:00"))
        self.health.log_food(FoodLogCreate(food_name="米饭", kcal_per_unit=200,
                                           ts="2026-10-08T12:00:00+08:00"))
        self._use(3)
        self.health.log_workout(WorkoutCreate(activity="瑜伽", duration_min=20,
                                             ts="2026-10-08T07:00:00+08:00"))
        self.assertEqual(
            self.conn.execute("SELECT owner_id FROM app_workouts ORDER BY owner_id").fetchall()[0][0],
            2,
        )
        self.assertEqual(
            self.conn.execute("SELECT owner_id FROM app_food_logs").fetchone()[0], 2
        )
        self._use(2)
        day = self.health.daily_summary("2026-10-08")
        self.assertEqual(day["workouts"], ["跑步"])
        self.assertGreater(day["intake_kcal"], 0)
        self._use(3)
        self.assertEqual(self.health.daily_summary("2026-10-08")["workouts"], ["瑜伽"])

    def test_health_cannot_be_shared(self) -> None:
        with self.assertRaises(ValueError):
            ownership.set_share_mode(self.conn, "health", "family")
        self._use(2)
        self.health.log_measurement(
            MeasurementCreate(metric_code="waist", value=80,
                              measured_at="2026-10-08T08:00:00+08:00")
        )
        self.assertEqual(
            self.conn.execute("SELECT owner_id FROM app_measurements WHERE metric_code='waist'").fetchone()[0],
            2,
        )
        self._use(3)
        self.assertIsNone(self.health.latest("waist"))


if __name__ == "__main__":
    unittest.main()
