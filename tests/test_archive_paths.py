"""P0 验收:档案文件名必须跨平台安全,微信 ADS 档案要能迁成普通文件。

运行:
    .venv/Scripts/python.exe -m unittest tests.test_archive_paths -v
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from la.memory.archive import Event, EventLog, archive_filename
from la.tools.archive_tools import _render_events


class ArchiveFilenameTest(unittest.TestCase):
    def test_plain_sessions_keep_their_name(self):
        self.assertEqual(archive_filename("web"), "web.jsonl")
        self.assertEqual(archive_filename("default"), "default.jsonl")
        self.assertEqual(archive_filename("persona-edit"), "persona-edit.jsonl")

    def test_colon_is_percent_encoded(self):
        self.assertEqual(archive_filename("member:1"), "member%3A1.jsonl")
        self.assertEqual(archive_filename("bridge:a@im.wechat"), "bridge%3Aa@im.wechat.jsonl")
        self.assertNotIn(":", archive_filename("bridge:a@im.wechat"))

    def test_locator_uses_the_safe_name(self):
        event = Event(
            seq=1,
            type="user/message",
            time="2026-10-18T10:00:00+08:00",
            data={"text": "hello"},
            surface=True,
            session_id="bridge:a@im.wechat",
        )
        rendered, truncated, locator = _render_events([event], max_chars=2000)
        self.assertEqual(len(rendered), 1)
        self.assertFalse(truncated)
        self.assertEqual(locator, "data/archive/bridge%3Aa@im.wechat.jsonl#seq=1")


class EventLogPathTest(unittest.TestCase):
    def test_colon_session_writes_a_normal_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = EventLog("member:1", root)
            log.append("session/start", {"who": "mom"})
            new = root / "member%3A1.jsonl"
            self.assertTrue(new.exists())
            self.assertFalse((root / "member").exists())
            self.assertEqual(len(new.read_text(encoding="utf-8").splitlines()), 1)
            self.assertEqual(log.last_seq, 1)

    def test_missing_legacy_stream_does_not_create_an_ads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bridge").write_bytes(b"")
            log = EventLog("bridge:missing", root)
            self.assertFalse(log.migrated_from_legacy)
            log.append("session/start", {})
            new = root / "bridge%3Amissing.jsonl"
            self.assertTrue(new.exists())
            self.assertGreater(new.stat().st_size, 0)
            # 0 字节的 bridge 容器没有被写进任何东西
            self.assertEqual((root / "bridge").stat().st_size, 0)
            self.assertEqual(len(new.read_text(encoding="utf-8").splitlines()), 1)

    @unittest.skipUnless(os.name == "nt", "NTFS ADS 只在 Windows 上")
    def test_legacy_ads_is_copied_and_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "bridge"
            base.write_bytes(b"")
            legacy = root / "bridge:o9cq@im.wechat.jsonl"
            original = (
                Event(seq=1, type="user/message", time="2026-10-18T10:00:00+08:00",
                      data={"text": "first"}, surface=True).to_line() + "\n"
                + Event(seq=2, type="assistant/message", time="2026-10-18T10:00:01+08:00",
                        data={"text": "second"}, surface=True).to_line() + "\n"
            ).encode("utf-8")
            with open(str(legacy), "w", encoding="utf-8", newline="") as handle:
                handle.write(original.decode("utf-8"))

            log = EventLog("bridge:o9cq@im.wechat", root)
            self.assertTrue(log.migrated_from_legacy)
            new = root / "bridge%3Ao9cq@im.wechat.jsonl"
            self.assertTrue(new.exists())
            self.assertEqual(new.read_bytes(), original)
            self.assertEqual([event.seq for event in log.read()], [1, 2])

            # 旧 ADS 不删不改(归档只增不改)
            self.assertTrue(legacy.exists())
            self.assertEqual(legacy.read_bytes(), original)
            self.assertEqual(base.stat().st_size, 0)

            # 迁移后继续追加,序号接着走
            log.append("assistant/message", {"text": "third"})
            self.assertEqual(log.last_seq, 3)
            self.assertEqual(len(new.read_text(encoding="utf-8").splitlines()), 3)

            # 目录里现在能看到普通 .jsonl;0 字节的 bridge 容器不算
            names = sorted(p.name for p in root.glob("*.jsonl"))
            self.assertEqual(names, ["bridge%3Ao9cq@im.wechat.jsonl"])


if __name__ == "__main__":
    unittest.main()
