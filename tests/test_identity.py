"""P3 验收:成员身份、渠道绑定、Web/微信落到同一个 member:<id> 会话。

运行:
    .venv/Scripts/python.exe -m unittest tests.test_identity -v
"""

from __future__ import annotations

import dataclasses
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from la import config
import la.bridge.bridge as bridge_core
from la.bridge import Bridge
from la.family import identity
from la.llm.fake import FakeClient
from la.storage import db


class StubTransport:
    name = "stub"

    async def send(self, action) -> None:
        return None

    async def typing(self, thread_id: str) -> None:
        return None


class IdentityFixture(unittest.TestCase):
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
        self.assistant = db.connect_assistant()

    def tearDown(self) -> None:
        try:
            self.assistant.close()
        except Exception:
            pass
        for key, value in self._patched.items():
            setattr(config, key, value)
        self._tmp.cleanup()


class MigrationTest(IdentityFixture):
    def test_migration_seeds_one_admin_and_the_binding_table(self):
        rows = self.assistant.execute(
            "SELECT id, name, role, active FROM app_members ORDER BY id"
        ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [(1, "管理员", "admin", 1)])
        tables = {
            row[0]
            for row in self.assistant.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertIn("app_member_bindings", tables)


class SessionIdTest(IdentityFixture):
    def test_session_id_round_trip(self):
        self.assertEqual(identity.session_id_for_member(1), "member:1")
        self.assertEqual(identity.member_id_from_session("member:7"), 7)
        self.assertIsNone(identity.member_id_from_session("web"))
        self.assertIsNone(identity.member_id_from_session("member:x"))

    def test_web_defaults_to_the_first_member(self):
        self.assertEqual(identity.web_session_id(self.assistant), "member:1")
        with mock.patch.dict(os.environ, {"LA_MEMBER_ID": "2"}):
            self.assertEqual(identity.web_session_id(self.assistant), "member:2")
        with mock.patch.dict(os.environ, {"LA_MEMBER_ID": "not-a-number"}):
            self.assertEqual(identity.web_session_id(self.assistant), "member:1")

    def test_web_and_wechat_can_land_on_the_same_session(self):
        identity.bind_member(self.assistant, "weixin", "wxid_mom", 1)
        web = identity.web_session_id(self.assistant)
        member_id = identity.member_for_binding(self.assistant, "weixin", "wxid_mom")
        wechat = identity.session_id_for_member(member_id)
        self.assertEqual(web, wechat)
        self.assertEqual(web, "member:1")


class BindingTest(IdentityFixture):
    def test_bind_rebind_and_unbind(self):
        identity.bind_member(self.assistant, "weixin", "wxid_mom", 1)
        self.assertEqual(identity.member_for_binding(self.assistant, "weixin", "wxid_mom"), 1)

        self.assistant.execute(
            "INSERT INTO app_members(id, name, role, active, created_at)"
            " VALUES (2, '妈妈', 'parent', 1, '2026-10-18T00:00:00Z')"
        )
        self.assistant.commit()
        identity.bind_member(self.assistant, "weixin", "wxid_mom", 2)
        self.assertEqual(identity.member_for_binding(self.assistant, "weixin", "wxid_mom"), 2)

        self.assertTrue(identity.unbind_member(self.assistant, "weixin", "wxid_mom"))
        self.assertIsNone(identity.member_for_binding(self.assistant, "weixin", "wxid_mom"))
        with self.assertRaises(ValueError):
            identity.bind_member(self.assistant, "weixin", "wxid_x", 99)


class BridgeBindingTest(IdentityFixture):
    def _bridge(self) -> Bridge:
        settings = dataclasses.replace(
            config.get_settings(reload=True), provider="fake"
        )
        return Bridge(settings=settings, transport=StubTransport())

    def test_bound_contact_gets_member_session(self):
        identity.bind_member(self.assistant, "weixin", "wxid_mom", 1)
        bridge = self._bridge()
        try:
            with mock.patch.object(bridge_core, "build_client", lambda settings: FakeClient()):
                thread = bridge.thread("wxid_mom")
            self.assertEqual(thread.session_id, "member:1")
        finally:
            bridge.close()

    def test_binding_change_is_picked_up_without_restarting_the_bridge(self):
        """网页端刚绑定后,桥接下一条消息就进 member 会话(不能等重启)。"""
        bridge = self._bridge()
        try:
            with mock.patch.object(bridge_core, "build_client", lambda settings: FakeClient()):
                before = bridge.thread("wxid_new")
                self.assertEqual(before.session_id, "bridge:wxid_new")
                identity.bind_member(self.assistant, "weixin", "wxid_new", 1)
                after = bridge.thread("wxid_new")
            self.assertIsNot(after, before)
            self.assertEqual(after.session_id, "member:1")
        finally:
            bridge.close()

    def test_unbound_contact_keeps_the_old_shard(self):
        bridge = self._bridge()
        try:
            with mock.patch.object(bridge_core, "build_client", lambda settings: FakeClient()):
                thread = bridge.thread("wxid_unknown")
            self.assertEqual(thread.session_id, "bridge:wxid_unknown")
        finally:
            bridge.close()


if __name__ == "__main__":
    unittest.main()
