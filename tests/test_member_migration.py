"""P4 验收:旧 web / bridge 会话合并成 member:<id>,摘要/事实不丢。

运行:
    .venv/Scripts/python.exe -m unittest tests.test_member_migration -v
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from la.memory.archive import EventLog
from la.memory.store import MemoryStore
from la.storage import db
from la.storage.migrations import MIGRATIONS_MEMORY

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MEMBER_SESSION = "member:1"


class MemberMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.archive = self.tmp / "archive"
        self.archive.mkdir()
        self.memory_db = self.tmp / "memory.db"
        self.assistant_db = self.tmp / "assistant.db"
        self.conn = db.connect(self.memory_db)
        db.migrate(self.conn, MIGRATIONS_MEMORY)
        self.store = MemoryStore(self.conn, archive_dir=self.archive)

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def _seed(self) -> None:
        self.store.append("web", "user/message", {"text": "w1"}, time="2026-10-01T10:00:00+08:00")
        self.store.append("bridge:wxid_mom", "user/message", {"text": "b1"}, time="2026-10-01T10:01:00+08:00")
        self.store.append("web", "assistant/message", {"text": "w2"}, time="2026-10-01T10:02:00+08:00")
        self.store.add_fact("妈妈喜欢喝茶", session_id="web")
        self.store.add_fact("微信里说过要买茶叶", session_id="bridge:wxid_mom")
        for session, text, start, end in (
            ("web", "web 摘要", "2026-10-01T10:00:00+08:00", "2026-10-01T10:02:00+08:00"),
            ("bridge:wxid_mom", "微信摘要", "2026-10-01T10:01:00+08:00", "2026-10-01T10:01:30+08:00"),
        ):
            self.conn.execute(
                "INSERT INTO summary_pool(session_id, start_seq, end_seq, start_time, end_time,"
                " text, anchors, tags, category, created_at)"
                " VALUES (?, 1, 1, ?, ?, ?, '[]', '[]', 'periodic', ?)",
                (session, start, end, text, "2026-10-01T10:00:00+08:00"),
            )
        self.conn.commit()

    def _run(self, *extra: str) -> subprocess.CompletedProcess:
        cmd = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "migrate_member_sessions.py"),
            "--memory-db", str(self.memory_db),
            "--assistant-db", str(self.assistant_db),
            "--archive-dir", str(self.archive),
            "--member", "1",
            "--sessions", "web,bridge:wxid_mom",
            "--weixin-contact", "wxid_mom",
            *extra,
        ]
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env,
        )

    def test_dry_run_does_not_write(self):
        self._seed()
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.archive / "member%3A1.jsonl").exists())
        facts = self.conn.execute(
            "SELECT COUNT(*) FROM facts WHERE session_id IN ('web', 'bridge:wxid_mom')"
        ).fetchone()[0]
        self.assertEqual(facts, 2)

    def test_apply_merges_events_and_keeps_summaries(self):
        self._seed()
        result = self._run("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("合并后事件: 3", result.stdout)

        rows = self.conn.execute(
            "SELECT seq, time, type, data FROM events WHERE session_id = ? ORDER BY seq",
            (MEMBER_SESSION,),
        ).fetchall()
        self.assertEqual([row[0] for row in rows], [1, 2, 3])
        self.assertEqual([row[1] for row in rows], sorted(row[1] for row in rows))
        self.assertEqual(
            [json.loads(row[3])["text"] for row in rows], ["w1", "b1", "w2"]
        )

        old_events = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE session_id IN ('web', 'bridge:wxid_mom')"
        ).fetchone()[0]
        self.assertEqual(old_events, 0)
        facts = self.conn.execute(
            "SELECT COUNT(*) FROM facts WHERE session_id = ?", (MEMBER_SESSION,)
        ).fetchone()[0]
        self.assertEqual(facts, 2)
        summaries = self.conn.execute(
            "SELECT start_seq, end_seq FROM summary_pool WHERE session_id = ? ORDER BY id",
            (MEMBER_SESSION,),
        ).fetchall()
        self.assertEqual(len(summaries), 2)
        self.assertTrue(all(start < 0 and end < 0 for start, end in summaries))

        archive = self.archive / "member%3A1.jsonl"
        self.assertTrue(archive.exists())
        self.assertEqual(len(archive.read_text(encoding="utf-8").splitlines()), 3)
        self.assertTrue((self.archive / "web.jsonl").exists())
        self.assertTrue((self.archive / "bridge%3Awxid_mom.jsonl").exists())

        assistant = sqlite3.connect(str(self.assistant_db))
        try:
            binding = assistant.execute(
                "SELECT member_id FROM app_member_bindings"
                " WHERE channel = 'weixin' AND external_id = 'wxid_mom'"
            ).fetchone()
            self.assertEqual(binding, (1,))
        finally:
            assistant.close()

    def test_force_rebuild_is_idempotent(self):
        self._seed()
        self.assertEqual(self._run("--apply").returncode, 0)
        second = self._run("--apply", "--force")
        self.assertEqual(second.returncode, 0, second.stderr)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE session_id = ?", (MEMBER_SESSION,)
        ).fetchone()[0]
        self.assertEqual(count, 3)
        self.assertEqual(
            len((self.archive / "member%3A1.jsonl").read_text(encoding="utf-8").splitlines()),
            3,
        )


if __name__ == "__main__":
    unittest.main()
