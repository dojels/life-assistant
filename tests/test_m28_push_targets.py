"""D161:错题推送可以选收件人。

设置里勾了谁就推给谁,不勾 = 所有已绑微信的成员。
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from la import config
from la.domain.schemas.study import StudyPushSettings
from la.domain.services.study_service import StudyService
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT
from la.web.app import create_app


class PushTargetsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = sqlite3.connect(str(Path(self._tmp.name) / "assistant.db"))
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn, MIGRATIONS_ASSISTANT)
        self.service = StudyService(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_default_is_empty_meaning_all(self) -> None:
        self.assertEqual(self.service.settings().push_targets, ())

    def test_targets_are_saved_and_read_back(self) -> None:
        self.service.update_settings(StudyPushSettings(targets=[2, 3]))
        self.assertEqual(self.service.settings().push_targets, (2, 3))

    def test_update_keeps_other_fields(self) -> None:
        self.service.update_settings(StudyPushSettings(push_time="20:15", targets=[2]))
        settings = self.service.settings()
        self.assertEqual(settings.push_time_local, "20:15")
        self.assertEqual(settings.push_targets, (2,))

    def test_empty_list_clears_back_to_all(self) -> None:
        self.service.update_settings(StudyPushSettings(targets=[2]))
        self.service.update_settings(StudyPushSettings(targets=[]))
        self.assertEqual(self.service.settings().push_targets, ())

    def test_duplicate_targets_are_collapsed(self) -> None:
        self.service.update_settings(StudyPushSettings(targets=[3, 3, 2]))
        self.assertEqual(self.service.settings().push_targets, (3, 2))

    def test_as_dict_exposes_list_for_ui(self) -> None:
        self.service.update_settings(StudyPushSettings(targets=[5]))
        self.assertEqual(self.service.settings().as_dict()["push_targets"], [5])

    def test_garbage_in_db_falls_back_to_empty(self) -> None:
        self.service.update_settings(StudyPushSettings(targets=[2]))
        self.conn.execute(
            "UPDATE app_study_settings SET push_targets = ? WHERE id = 1", ("not-json",)
        )
        self.conn.commit()
        self.assertEqual(self.service.settings().push_targets, ())




class StudySettingsPageTest(unittest.TestCase):
    """回归:收件人必须是**看得见的复选框**(2026-10-24 用户截图:只有名字没有框)。

    真因:.switch 把原生 input 设成 opacity:0,靠 .switch-track 画开关;
    收件人那几行用了 .switch 却没有 track —— 于是整颗复选框隐形。
    """

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        from fastapi.testclient import TestClient

        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self._patched = {
            key: getattr(config, key)
            for key in ("DATA_DIR", "ARCHIVE_DIR", "ARTIFACTS_DIR", "MEDIA_DIR",
                        "ASSISTANT_DB", "MEMORY_DB", "LOGS_DIR")
        }
        config.DATA_DIR = self.tmp
        config.ARCHIVE_DIR = self.tmp / "archive"
        config.ARTIFACTS_DIR = self.tmp / "artifacts"
        config.MEDIA_DIR = self.tmp / "media"
        config.ASSISTANT_DB = self.tmp / "assistant.db"
        config.MEMORY_DB = self.tmp / "memory.db"
        config.LOGS_DIR = self.tmp / "logs"
        config.ensure_runtime_dirs()
        self.conn = db.connect_assistant()
        db.migrate(self.conn, MIGRATIONS_ASSISTANT)
        self.client = TestClient(create_app(), follow_redirects=False)
        response = self.client.post("/setup", data={"pin": "123456", "pin2": "123456"})
        self.assertEqual(response.status_code, 200, response.text)

    def tearDown(self) -> None:
        self.client.close()
        self.conn.close()
        for key, value in self._patched.items():
            setattr(config, key, value)
        self._tmp.cleanup()

    def test_recipients_are_real_checkboxes(self) -> None:
        page = self.client.get("/study?tab=review")
        self.assertEqual(page.status_code, 200, page.text[:400])
        self.assertIn('name="targets"', page.text)
        self.assertIn("<span class=\"switch-track\"></span>", page.text)
        # ⚠️ 收件人不能再用 .switch 包着:那会让复选框隐形
        self.assertNotIn('class="switch"><input type="checkbox" name="targets"', page.text)

    def test_closed_window_is_shown_next_to_recipient(self) -> None:
        """2026-10-24:妈妈号六天没说话 → 页面必须**提前**说"窗口已关"。

        上一版页面只写"已推送",而那个人根本收不到:用户只能在晚上追问
        "为什么没收到"。窗口状态必须写在勾选框旁边。
        """
        import time

        from la.bridge.adapters import weixin_store
        from la.family import members as member_store

        member = member_store.create_member(self.conn, "无人知晓")
        self.conn.execute(
            "INSERT INTO app_member_bindings(channel, external_id, member_id, created_at)"
            " VALUES ('weixin', ?, ?, ?)",
            ("o-mom@im.wechat", member.id, "2026-10-18T00:00:00+00:00"),
        )
        self.conn.commit()
        weixin_store.save_session({
            "user_id": "o-mom@im.wechat",
            "context_token": "t",
            "last_seen_ms": int(time.time() * 1000) - 6 * 86400 * 1000,
            "remaining_quota": 10,
        }, account="mom")
        page = self.client.get("/study?tab=review")
        self.assertEqual(page.status_code, 200, page.text[:400])
        self.assertIn("窗口已关", page.text)
        self.assertIn("无人知晓", page.text)

if __name__ == "__main__":
    unittest.main()