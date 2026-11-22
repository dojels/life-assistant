"""I0 验收:成员管理、PIN 哈希、登录票据。

运行:
    .venv/Scripts/python.exe -m unittest tests.test_identity_backend -v
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from la.family import members
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT
from la.web import auth


class IdentityBackendTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.conn = db.connect(self.tmp / "assistant.db")
        db.migrate(self.conn, MIGRATIONS_ASSISTANT)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_migration_adds_auth_columns_and_sessions_table(self) -> None:
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(app_members)")}
        self.assertTrue({"pin_hash", "pin_salt", "pin_updated_at"} <= cols)
        tables = {
            row[0]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertIn("app_sessions", tables)
        self.assertEqual(len(members.list_members(self.conn)), 1)
        self.assertFalse(members.has_any_pin(self.conn))

    def test_create_member_with_pin_and_verify(self) -> None:
        member = members.create_member(self.conn, "妈妈", role="parent", pin="123456")
        self.assertEqual(member.name, "妈妈")
        self.assertEqual(member.role, "parent")
        self.assertTrue(member.has_pin)
        self.assertTrue(members.verify_pin(self.conn, member.id, "123456"))
        self.assertFalse(members.verify_pin(self.conn, member.id, "654321"))
        self.assertFalse(members.verify_pin(self.conn, member.id, "12345"))
        row = self.conn.execute(
            "SELECT pin_hash, pin_salt FROM app_members WHERE id = ?", (member.id,)
        ).fetchone()
        self.assertNotEqual(row["pin_hash"], "123456")
        self.assertNotEqual(row["pin_salt"], "")

    def test_set_pin_replaces_the_old_one(self) -> None:
        member = members.create_member(self.conn, "爸爸", pin="111111")
        members.set_pin(self.conn, member.id, "222222")
        self.assertFalse(members.verify_pin(self.conn, member.id, "111111"))
        self.assertTrue(members.verify_pin(self.conn, member.id, "222222"))

    def test_validation_and_last_admin_guard(self) -> None:
        with self.assertRaises(ValueError):
            members.create_member(self.conn, "  ")
        with self.assertRaises(ValueError):
            members.create_member(self.conn, "孩子", role="boss")
        with self.assertRaises(ValueError):
            members.create_member(self.conn, "孩子", pin="abc")
        with self.assertRaises(ValueError):
            members.deactivate_member(self.conn, 1)

    def test_session_create_resolve_revoke(self) -> None:
        member = members.create_member(self.conn, "妈妈", pin="123456")
        token = auth.create_session(self.conn, member.id)
        self.assertNotEqual(token, "")
        self.assertEqual(auth.resolve_session(self.conn, token).id, member.id)
        self.assertTrue(auth.revoke_session(self.conn, token))
        self.assertIsNone(auth.resolve_session(self.conn, token))
        self.assertFalse(auth.revoke_session(self.conn, token))

    def test_expired_and_deactivated_sessions_are_rejected(self) -> None:
        member = members.create_member(self.conn, "妈妈", pin="123456")
        token = auth.create_session(self.conn, member.id)
        self.conn.execute(
            "UPDATE app_sessions SET expires_at = '2000-01-01T00:00:00+00:00'"
            " WHERE token_hash = ?",
            (auth.hash_token(token),),
        )
        self.conn.commit()
        self.assertIsNone(auth.resolve_session(self.conn, token))

        second = auth.create_session(self.conn, member.id)
        members.deactivate_member(self.conn, member.id)
        self.assertIsNone(auth.resolve_session(self.conn, second))

    def test_login_throttle_is_persisted_in_db(self) -> None:
        self.assertTrue(auth.allow_attempt(self.conn, "login", 1, "ip", limit=2))
        auth.record_failure(self.conn, "login", 1, "ip")
        auth.record_failure(self.conn, "login", 1, "ip")
        self.assertFalse(auth.allow_attempt(self.conn, "login", 1, "ip", limit=2))
        self.assertEqual(auth.failure_count(self.conn, "login", 1, "ip"), 2)

        auth.clear_failures(self.conn, "login", 1, "ip")
        self.assertEqual(auth.failure_count(self.conn, "login", 1, "ip"), 0)
        self.assertTrue(auth.allow_attempt(self.conn, "login", 1, "ip", limit=2))

        # 已过期的记录会被清掉,不再挡人
        auth.record_failure(self.conn, "login", 1, "ip")
        self.conn.execute(
            "UPDATE app_login_failures SET ts = '2000-01-01T00:00:00+00:00'"
        )
        self.conn.commit()
        self.assertTrue(auth.allow_attempt(self.conn, "login", 1, "ip", limit=2))


if __name__ == "__main__":
    unittest.main()
