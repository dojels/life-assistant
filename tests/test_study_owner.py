"""R2b 验收:错题/复习单元 owner 过滤。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from la.domain import ownership
from la.domain.schemas.study import ProblemCreate
from la.domain.services.study_service import StudyError, StudyService
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT


class StudyOwnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.conn = db.connect(self.tmp / "assistant.db")
        db.migrate(self.conn, MIGRATIONS_ASSISTANT)
        ownership.set_share_mode(self.conn, "study", "own")
        self.service = StudyService(self.conn, member_id=2)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _add(self, member: int, stem: str):
        db.set_member(self.conn, member)
        self.service.member_id = member
        return self.service.add_problem(
            ProblemCreate(subject="数学", stem_text=stem, answer_text="2",
                          knowledge_points=["加法"])
        )

    def test_own_mode_filters_problems_and_units(self) -> None:
        mom = self._add(2, "妈妈的题")
        dad = self._add(3, "爸爸的题")
        self.assertEqual(
            self.conn.execute("SELECT owner_id FROM app_problems WHERE id = ?", (mom.id,)).fetchone()[0],
            2,
        )
        unit_owners = {
            int(row[0])
            for row in self.conn.execute(
                "SELECT DISTINCT owner_id FROM app_review_units WHERE problem_id = ?", (mom.id,)
            )
        }
        self.assertEqual(unit_owners, {2})

        db.set_member(self.conn, 2)
        self.service.member_id = 2
        self.assertEqual([item.id for item in self.service.list_problems()], [mom.id])
        with self.assertRaises(StudyError):
            self.service.get_problem(dad.id)

        db.set_member(self.conn, 3)
        self.service.member_id = 3
        self.assertEqual([item.id for item in self.service.list_problems()], [dad.id])
        self.assertEqual(self.service.stats().total_problems, 1)

    def test_family_mode_writes_null_and_is_visible_to_all(self) -> None:
        ownership.set_share_mode(self.conn, "study", "family")
        problem = self._add(2, "全家的题")
        self.assertIsNone(
            self.conn.execute("SELECT owner_id FROM app_problems WHERE id = ?", (problem.id,)).fetchone()[0]
        )
        for member in (2, 3):
            db.set_member(self.conn, member)
            self.service.member_id = member
            self.assertIn(problem.id, [item.id for item in self.service.list_problems()])


if __name__ == "__main__":
    unittest.main()
