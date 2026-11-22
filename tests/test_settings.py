"""I2 验收:设置页(成员 + PIN + 微信绑定),管理员权限。

运行:
    .venv/Scripts/python.exe -m unittest tests.test_settings -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from la import config
from la.domain import ownership
from la.family import identity, members
from la.memory.store import MemoryStore
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT, MIGRATIONS_MEMORY
from la.web.app import create_app


class SettingsTest(unittest.TestCase):
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
        response = self.client.post("/setup", data={"pin": "123456", "pin2": "123456"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("恢复码", response.text)

    def tearDown(self) -> None:
        self.client.close()
        self.conn.close()
        for key, value in self._patched.items():
            setattr(config, key, value)
        self._tmp.cleanup()

    def test_settings_requires_admin(self) -> None:
        mom = members.create_member(self.conn, "妈妈", role="parent", pin="654321")
        self.client.post("/logout")
        login = self.client.post(
            "/login", data={"member_id": str(mom.id), "pin": "654321"}
        )
        self.assertEqual(login.status_code, 303)
        response = self.client.get("/settings")
        self.assertEqual(response.status_code, 403)

    def test_admin_can_add_member_and_bind_wechat(self) -> None:
        page = self.client.get("/settings")
        self.assertEqual(page.status_code, 200)
        self.assertIn("添加成员", page.text)

        added = self.client.post(
            "/settings/members/add",
            data={"name": "妈妈", "role": "parent", "pin": "654321"},
        )
        self.assertEqual(added.status_code, 303)
        row = self.conn.execute(
            "SELECT id FROM app_members WHERE name = '妈妈'"
        ).fetchone()
        self.assertIsNotNone(row)
        member_id = int(row[0])

        bound = self.client.post(
            "/settings/bind", data={"external_id": "wxid_mom", "member_id": str(member_id)}
        )
        self.assertEqual(bound.status_code, 303)
        self.assertEqual(
            identity.member_for_binding(self.conn, "weixin", "wxid_mom"), member_id
        )
        # 绑定列表搬到「微信」页签(D159),断言跟着到那一页
        page = self.client.get("/settings?tab=wechat")
        self.assertIn("wxid_mom", page.text)
        self.assertIn("妈妈", page.text)

        unbound = self.client.post("/settings/unbind", data={"external_id": "wxid_mom"})
        self.assertEqual(unbound.status_code, 303)
        self.assertIsNone(identity.member_for_binding(self.conn, "weixin", "wxid_mom"))

    def test_share_mode_can_be_toggled(self) -> None:
        self.assertEqual(ownership.get_share_mode(self.conn, "ledger"), "own")
        response = self.client.post(
            "/settings/share", data={"domain": "ledger", "mode": "family"}
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(ownership.get_share_mode(self.conn, "ledger"), "family")
        page = self.client.get("/settings")
        self.assertIn("数据归属", page.text)
        self.assertIn("始终各自", page.text)

        refused = self.client.post(
            "/settings/share", data={"domain": "calendar", "mode": "family"}
        )
        self.assertEqual(refused.status_code, 303)
        self.assertIn("err=", refused.headers["location"])
        self.assertEqual(ownership.get_share_mode(self.conn, "calendar"), "own")

    def test_pending_contact_appears_from_bridge_events(self) -> None:
        memory = db.connect_memory()
        try:
            db.migrate(memory, MIGRATIONS_MEMORY)
            store = MemoryStore(memory, archive_dir=config.ARCHIVE_DIR)
            store.append("bridge:wxid_new", "user/message", {"text": "在吗"})
        finally:
            memory.close()
        page = self.client.get("/settings?tab=wechat")
        self.assertEqual(page.status_code, 200)
        self.assertIn("wxid_new", page.text)

    def test_deactivate_revokes_the_members_login(self) -> None:
        mom = members.create_member(self.conn, "妈妈", role="parent", pin="654321")
        member_id = mom.id
        response = self.client.post(f"/settings/members/{member_id}/deactivate")
        self.assertEqual(response.status_code, 303)
        self.assertFalse(members.get_member(self.conn, member_id).active)
        self.client.post("/logout")
        login = self.client.post(
            "/login", data={"member_id": str(member_id), "pin": "654321"}
        )
        self.assertEqual(login.status_code, 400)

    def test_invalid_member_form_reports_error(self) -> None:
        before = len(members.list_members(self.conn))
        response = self.client.post(
            "/settings/members/add",
            data={"name": "孩子", "role": "child", "pin": "abc"},
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("err=", response.headers["location"])
        self.assertEqual(len(members.list_members(self.conn)), before)


if __name__ == "__main__":
    unittest.main()
