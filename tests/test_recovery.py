"""R0 验收:恢复码换 PIN,旧 PIN/旧票据/旧恢复码全部失效。

运行:
    .venv/Scripts/python.exe -m unittest tests.test_recovery -v
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from fastapi.testclient import TestClient

from la import config
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT
from la.web.app import create_app

CODE_RE = re.compile(r"[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}")


class RecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self._patched = {
            key: getattr(config, key)
            for key in ("DATA_DIR", "ARCHIVE_DIR", "ARTIFACTS_DIR", "ASSISTANT_DB",
                        "MEMORY_DB", "LOGS_DIR")
        }
        config.DATA_DIR = self.tmp
        config.ARCHIVE_DIR = self.tmp / "archive"
        config.ARTIFACTS_DIR = self.tmp / "artifacts"
        config.ASSISTANT_DB = self.tmp / "assistant.db"
        config.MEMORY_DB = self.tmp / "memory.db"
        config.LOGS_DIR = self.tmp / "logs"
        config.ensure_runtime_dirs()
        self.conn = db.connect_assistant()
        db.migrate(self.conn, MIGRATIONS_ASSISTANT)
        self.client = TestClient(create_app(), follow_redirects=False)

    def tearDown(self) -> None:
        self.client.close()
        self.conn.close()
        for key, value in self._patched.items():
            setattr(config, key, value)
        self._tmp.cleanup()

    def test_remote_setup_requires_bootstrap_token_and_recover_is_reachable(self) -> None:
        remote = TestClient(
            create_app(), client=("203.0.113.9", 50000), follow_redirects=False
        )
        self.assertEqual(remote.get("/setup").status_code, 403, "没有口令不能云上安装")
        with mock.patch.dict(os.environ, {"LA_BOOTSTRAP_TOKEN": "cloud-secret"}):
            config.get_settings(reload=True)
            try:
                remote = TestClient(
                    create_app(), client=("203.0.113.9", 50000), follow_redirects=False
                )
                self.assertEqual(remote.get("/setup?token=wrong").status_code, 403)
                self.assertEqual(remote.get("/setup?token=cloud-secret").status_code, 200)
                response = remote.post("/setup", data={
                    "pin": "123456", "pin2": "123456", "token": "cloud-secret",
                })
                self.assertEqual(response.status_code, 200, response.text)
                anonymous = TestClient(
                    create_app(), client=("203.0.113.9", 50000), follow_redirects=False
                )
                self.assertEqual(anonymous.get("/recover").status_code, 200,
                                 "云端没有本机浏览器,恢复页必须可达")
            finally:
                config.get_settings(reload=True)

    def _setup(self) -> str:
        response = self.client.post(
            "/setup", data={"pin": "123456", "pin2": "123456"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        found = CODE_RE.search(response.text)
        self.assertIsNotNone(found, "首次安装必须显示恢复码")
        return found.group(0)

    def test_recover_replaces_pin_and_rotates_code(self) -> None:
        code = self._setup()
        self.client.post("/logout")

        page = self.client.get("/recover")
        self.assertEqual(page.status_code, 200)
        self.assertIn("恢复 PIN", page.text)

        wrong = self.client.post(
            "/recover",
            data={"member_id": "1", "code": "AAAA-BBBB-CCCC", "pin": "222222", "pin2": "222222"},
        )
        self.assertEqual(wrong.status_code, 400)
        self.assertIn("恢复码不对", wrong.text)

        done = self.client.post(
            "/recover",
            data={"member_id": "1", "code": code, "pin": "222222", "pin2": "222222"},
        )
        self.assertEqual(done.status_code, 200, done.text)
        new_code = CODE_RE.search(done.text).group(0)
        self.assertNotEqual(new_code, code)

        self.client.post("/logout")
        old = self.client.post("/login", data={"member_id": "1", "pin": "123456"})
        self.assertEqual(old.status_code, 400)
        new = self.client.post("/login", data={"member_id": "1", "pin": "222222"})
        self.assertEqual(new.status_code, 303)

        # 旧恢复码不能再换一次
        self.client.post("/logout")
        reused = self.client.post(
            "/recover",
            data={"member_id": "1", "code": code, "pin": "333333", "pin2": "333333"},
        )
        self.assertEqual(reused.status_code, 400)

        # 旧 PIN 已失效,新 PIN 还在
        self.client.post("/logout")
        self.assertEqual(
            self.client.post("/login", data={"member_id": "1", "pin": "222222"}).status_code,
            303,
        )


if __name__ == "__main__":
    unittest.main()
