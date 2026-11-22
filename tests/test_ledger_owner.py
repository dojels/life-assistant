"""R2b 验收:记账 owner_id 的写入与读取过滤。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from la.domain import ownership
from la.domain.schemas.ledger import ExpenseCreate
from la.domain.services.ledger_service import LedgerError, LedgerService
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT


class LedgerOwnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.conn = db.connect(self.tmp / "assistant.db")
        db.migrate(self.conn, MIGRATIONS_ASSISTANT)
        ownership.set_share_mode(self.conn, "ledger", "own")
        self.service = LedgerService(self.conn, member_id=2)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _record(self, member: int, payee: str):
        db.set_member(self.conn, member)
        self.service.member_id = member
        return self.service.record_expense(
            ExpenseCreate(
                amount_cents=1000, payee=payee, category="餐饮",
                ts="2026-10-08T12:00:00+08:00",
            ),
            status="confirmed",
        )

    def test_own_mode_writes_owner_and_filters_views(self) -> None:
        mom = self._record(2, "妈妈的午饭")
        dad = self._record(3, "爸爸的午饭")
        self.assertEqual(
            self.conn.execute("SELECT owner_id FROM app_txns WHERE id = ?", (mom.id,)).fetchone()[0],
            2,
        )
        self.assertEqual(
            self.conn.execute("SELECT owner_id FROM app_txns WHERE id = ?", (dad.id,)).fetchone()[0],
            3,
        )

        db.set_member(self.conn, 2)
        self.service.member_id = 2
        seen = self.service.list_txns(period="2026-10")
        self.assertEqual([row["id"] for row in seen], [mom.id])
        with self.assertRaises(LedgerError):
            self.service.read(dad.id)

        db.set_member(self.conn, 3)
        self.service.member_id = 3
        self.assertEqual([row["id"] for row in self.service.list_txns(period="2026-10")], [dad.id])

    def test_family_mode_writes_null_and_is_visible_to_all(self) -> None:
        ownership.set_share_mode(self.conn, "ledger", "family")
        family = self._record(2, "全家的菜")
        self.assertIsNone(
            self.conn.execute("SELECT owner_id FROM app_txns WHERE id = ?", (family.id,)).fetchone()[0]
        )
        for member in (2, 3):
            db.set_member(self.conn, member)
            rows = self.service.list_txns(period="2026-10")
            self.assertIn(family.id, [row["id"] for row in rows])


if __name__ == "__main__":
    unittest.main()
