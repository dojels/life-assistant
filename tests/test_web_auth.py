"""I1 验收:首次安装、登录、登出、路由保护。

运行:
    .venv/Scripts/python.exe -m unittest tests.test_web_auth -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from la import config
from la.family import members
from la.memory.store import MemoryStore
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT, MIGRATIONS_MEMORY
from la.web.app import create_app


class WebAuthTest(unittest.TestCase):
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

    def _setup(self, pin: str = "123456") -> None:
        response = self.client.post("/setup", data={"pin": pin, "pin2": pin})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("恢复码", response.text)

    def test_setup_then_chat_requires_login(self) -> None:
        # 没有 PIN 时保持旧行为:页面照常可开,并且页面上要看得见“从哪开始”
        first = self.client.get("/chat")
        self.assertEqual(first.status_code, 200)
        self.assertIn("设置 PIN", first.text, "首次安装入口不能只藏在地址栏里")
        self.assertIn("/setup", first.text)
        self.assertEqual(self.client.get("/setup").status_code, 200)

        self._setup()
        self.assertEqual(self.client.get("/chat").status_code, 200)
        self.assertEqual(self.client.get("/setup").headers["location"], "/chat")

        self.assertEqual(self.client.post("/logout").status_code, 303)
        response = self.client.get("/chat")
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].startswith("/login"))

    def test_login_with_wrong_and_right_pin(self) -> None:
        self._setup()
        self.client.post("/logout")

        page = self.client.get("/login")
        self.assertEqual(page.status_code, 200)
        self.assertIn("管理员", page.text)

        bad = self.client.post("/login", data={"member_id": "1", "pin": "000000"})
        self.assertEqual(bad.status_code, 400)
        self.assertIn("不对", bad.text)

        good = self.client.post(
            "/login", data={"member_id": "1", "pin": "123456", "next": "/memory"}
        )
        self.assertEqual(good.status_code, 303)
        self.assertEqual(good.headers["location"], "/memory")
        self.assertEqual(self.client.get("/memory").status_code, 200)

    def test_login_throttle_after_five_failures(self) -> None:
        self._setup()
        self.client.post("/logout")
        codes = []
        for _ in range(6):
            codes.append(
                self.client.post(
                    "/login", data={"member_id": "1", "pin": "000000"}
                ).status_code
            )
        self.assertEqual(codes[:5], [400] * 5)
        self.assertEqual(codes[5], 429)

    def test_open_redirect_is_rejected(self) -> None:
        self._setup()
        self.client.post("/logout")
        response = self.client.post(
            "/login",
            data={"member_id": "1", "pin": "123456", "next": "//evil.example/x"},
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/chat")

    def test_member_with_pin_can_login(self) -> None:
        self._setup()
        mom = members.create_member(self.conn, "妈妈", role="parent", pin="654321")
        response = self.client.post(
            "/login", data={"member_id": str(mom.id), "pin": "654321"}
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/chat")

    def test_each_member_sees_their_own_chat_history(self) -> None:
        self._setup()
        mom = members.create_member(self.conn, "妈妈", role="parent", pin="654321")

        memory = db.connect_memory()
        try:
            db.migrate(memory, MIGRATIONS_MEMORY)
            store = MemoryStore(memory, archive_dir=config.ARCHIVE_DIR)
            store.append("member:1", "user/message", {"text": "管理员的秘密"})
            store.append("member:2", "user/message", {"text": "妈妈的秘密"})
        finally:
            memory.close()

        self.client.post("/logout")
        login = self.client.post(
            "/login", data={"member_id": str(mom.id), "pin": "654321"}
        )
        self.assertEqual(login.status_code, 303)
        page = self.client.get("/chat")
        self.assertEqual(page.status_code, 200)
        self.assertIn("妈妈的秘密", page.text)
        self.assertNotIn("管理员的秘密", page.text)


if __name__ == "__main__":
    unittest.main()
