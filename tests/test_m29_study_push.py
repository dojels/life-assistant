"""D162:每日错题推送真的发到微信(文本 + 合并听写)。"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from la.bridge.study_push import StudyPusher
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT

NOW = datetime(2026, 10, 24, 19, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


class FakeStudy:
    def __init__(self, *, targets=(), text="📚 今天的错题复习", groups=None, time="19:30"):
        self._settings = types.SimpleNamespace(
            push_enabled=True, push_time_local=time, push_targets=tuple(targets),
        )
        self._text = text
        self._groups = groups if groups is not None else []
        self.artifacts = _Artifacts()

    def settings(self):
        return self._settings

    def push_text(self, *, now=None):
        return self._text

    def merged_audio_today(self):
        return list(self._groups)

    def now(self):
        return NOW


class _Artifacts:
    def __init__(self):
        self.root = Path(tempfile.gettempdir())

    def get(self, artifact_id):
        return types.SimpleNamespace(
            path=lambda root: Path(root) / ("audio-" + str(artifact_id) + ".mp3")
        )


class _PusherBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "assistant.db"
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        db.migrate(conn, MIGRATIONS_ASSISTANT)
        conn.close()
        self.sent: list[tuple[str, int | None]] = []
        self.files: list[tuple[str, str, int | None]] = []
        self.ok = True

    def tearDown(self):
        self._tmp.cleanup()

    async def _send(self, text, member):
        if not self.ok:
            return False, "没有 context_token"
        self.sent.append((text, member))
        return True, "ok"

    async def _send_file(self, path, name, member=None):
        self.files.append((str(path), str(name), member))
        return True, "ok"

    def _pusher(self, study):
        return StudyPusher(
            send=self._send,
            send_file=self._send_file,
            service_factory=lambda: study,
            conn_factory=lambda: self._conn(),
            retry_s=0.0,   # 默认 5 分钟冷却;这一组测的是"下一轮就重试"
            now=lambda: NOW,
        )

    def _conn(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _conn_count(self, key_prefix: str) -> int:
        conn = self._conn()
        try:
            return int(conn.execute(
                "SELECT COUNT(*) FROM sys_settings WHERE key LIKE ?", (key_prefix + "%",)
            ).fetchone()[0])
        finally:
            conn.close()

    def _markers(self) -> int:
        """只数"发过了"的标记 —— 失败原因也放在 study.push.* 下,别混进来。"""
        conn = self._conn()
        try:
            return int(conn.execute(
                "SELECT COUNT(*) FROM sys_settings WHERE key LIKE 'study.push.20%'"
                " AND key NOT LIKE 'study.push.err.%'"
            ).fetchone()[0])
        finally:
            conn.close()

class PusherTest(_PusherBase):
    def test_sends_once_then_remembers(self):
        study = FakeStudy(targets=(2, 3))
        pusher = self._pusher(study)
        asyncio.run(pusher.tick())
        self.assertEqual(self.sent, [("📚 今天的错题复习", 2), ("📚 今天的错题复习", 3)])
        self.assertEqual(self._conn_count("study.push.2026-10-24."), 2)
        asyncio.run(pusher.tick())
        self.assertEqual(len(self.sent), 2, "同一天不该发第二遍")

    def test_failed_send_does_not_mark_and_retries(self):
        study = FakeStudy(targets=(2,))
        pusher = self._pusher(study)
        self.ok = False
        asyncio.run(pusher.tick())
        self.assertEqual(self._markers(), 0, "没发出去不能标记")
        self.ok = True
        asyncio.run(pusher.tick())
        self.assertEqual(len(self.sent), 1, "下一轮要重试")
        self.assertEqual(self._markers(), 1)

    def test_empty_targets_means_everyone(self):
        study = FakeStudy(targets=())
        asyncio.run(self._pusher(study).tick())
        self.assertEqual(self.sent, [("📚 今天的错题复习", None)])
        self.assertEqual(self._conn_count("study.push.2026-10-24.all"), 1)

    def test_before_push_time_does_nothing(self):
        study = FakeStudy(targets=(2,), time="23:59")
        asyncio.run(self._pusher(study).tick())
        self.assertEqual(self.sent, [])

    def test_audio_is_sent_after_text(self):
        groups = [
            {"artifact_id": 7, "label": "英语听写", "error": ""},
            {"artifact_id": 8, "label": "语文听写", "error": ""},
        ]
        study = FakeStudy(targets=(2,), groups=groups)
        asyncio.run(self._pusher(study).tick())
        self.assertEqual(len(self.files), 2)
        self.assertEqual(self.files[0][1], "英语听写.mp3")
        self.assertEqual(self.files[0][2], 2)

    def test_failed_target_does_not_block_the_other(self):
        study = FakeStudy(targets=(2, 3))
        calls = {"n": 0}

        async def flaky(text, member):
            calls["n"] += 1
            if member == 2:
                return False, "没有窗口"
            self.sent.append((text, member))
            return True, "ok"

        pusher = StudyPusher(
            send=flaky, send_file=self._send_file,
            service_factory=lambda: study,
            conn_factory=lambda: self._conn(), retry_s=0.0, now=lambda: NOW,
        )
        asyncio.run(pusher.tick())
        self.assertEqual(self.sent, [("📚 今天的错题复习", 3)])
        self.assertEqual(self._conn_count("study.push.2026-10-24."), 1)


class CooldownTest(_PusherBase):
    """失败后别再 30 秒一次地打:窗口关着的时候那是纯白打(2026-10-24)。"""

    def test_failure_cools_down_before_retry(self):
        study = FakeStudy(targets=(2,))
        pusher = StudyPusher(
            send=self._send, send_file=self._send_file,
            service_factory=lambda: study,
            conn_factory=lambda: self._conn(), retry_s=300.0, now=lambda: NOW,
        )
        self.ok = False
        asyncio.run(pusher.tick())
        self.ok = True
        asyncio.run(pusher.tick())
        self.assertEqual(self.sent, [], "冷却期内不该再试")
        # 冷却过了就重试(直接把冷却时钟拨到过去)
        pusher._retry_at = {}
        asyncio.run(pusher.tick())
        self.assertEqual(len(self.sent), 1)

    def test_failure_reason_is_stored_for_the_page(self):
        study = FakeStudy(targets=(2,))
        pusher = self._pusher(study)
        self.ok = False
        asyncio.run(pusher.tick())
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT value FROM sys_settings WHERE key = ?",
                ("study.push.err.2026-10-24.2",),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "页面要能说清为什么没收到")
        self.assertIn("没有 context_token", row["value"])

    def test_success_clears_the_error(self):
        study = FakeStudy(targets=(2,))
        pusher = self._pusher(study)
        self.ok = False
        asyncio.run(pusher.tick())
        self.ok = True
        asyncio.run(pusher.tick())
        self.assertEqual(self._conn_count("study.push.err."), 0)


if __name__ == "__main__":
    unittest.main()
