"""P4 验收:微信多联系人时的提醒推送与追问路由。

运行:
    .venv/Scripts/python.exe -m unittest tests.test_wechat_multi -v
"""

from __future__ import annotations

import sqlite3
import unittest
from types import SimpleNamespace
from unittest import mock

from la.bridge.adapters.weixin_ilink import Conversation, ILinkAdapter


class PushAllCase(unittest.IsolatedAsyncioTestCase):
    def _adapter(self, conversations: dict[str, Conversation]):
        adapter = ILinkAdapter.__new__(ILinkAdapter)
        adapter.transport = SimpleNamespace(conversations=conversations)
        return adapter

    async def test_pushes_to_every_contact_with_a_window(self):
        conversations = {
            "mom": Conversation(user_id="mom", context_token="t1", remaining_quota=5),
            "dad": Conversation(user_id="dad", context_token="t2", remaining_quota=5),
            "no_token": Conversation(user_id="no_token", context_token="", remaining_quota=5),
        }
        adapter = self._adapter(conversations)
        calls: list[str] = []

        async def fake_push(text: str, *, to_user_id: str = ""):
            calls.append(to_user_id)
            return True, "ok"

        adapter.push_text = fake_push
        ok, reason = await adapter.push_text_all("到点提醒")
        self.assertTrue(ok)
        self.assertEqual(calls, ["mom", "dad"])
        self.assertIn("2", reason)

    async def test_one_failure_does_not_block_the_other(self):
        conversations = {
            "mom": Conversation(user_id="mom", context_token="t1", remaining_quota=5),
            "dad": Conversation(user_id="dad", context_token="t2", remaining_quota=5),
        }
        adapter = self._adapter(conversations)

        async def fake_push(text: str, *, to_user_id: str = ""):
            if to_user_id == "dad":
                return False, "额度用完了"
            return True, "ok"

        adapter.push_text = fake_push
        ok, reason = await adapter.push_text_all("到点提醒")
        self.assertTrue(ok)
        self.assertIn("1", reason)

    async def test_push_to_one_member_uses_only_their_contacts(self):
        conversations = {
            "mom": Conversation(user_id="mom", context_token="t1", remaining_quota=5),
            "dad": Conversation(user_id="dad", context_token="t2", remaining_quota=5),
        }
        adapter = self._adapter(conversations)
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE app_member_bindings (channel TEXT, external_id TEXT, member_id INTEGER)"
        )
        conn.execute(
            "INSERT INTO app_member_bindings VALUES ('weixin', 'mom', 2)"
        )
        conn.commit()
        calls: list[str] = []

        async def fake_push(text: str, *, to_user_id: str = ""):
            calls.append(to_user_id)
            return True, "ok"

        adapter.push_text = fake_push
        with mock.patch(
            "la.bridge.adapters.weixin_ilink.db.connect_assistant", return_value=conn
        ):
            ok, reason = await adapter.push_text_to_member("到点提醒", 2)
        self.assertTrue(ok, reason)
        self.assertEqual(calls, ["mom"])

    async def test_no_window_is_a_clear_failure(self):
        conversations = {
            "mom": Conversation(user_id="mom", context_token="", remaining_quota=5),
        }
        adapter = self._adapter(conversations)
        ok, reason = await adapter.push_text_all("到点提醒")
        self.assertFalse(ok)
        self.assertIn("窗口", reason)


if __name__ == "__main__":
    unittest.main()
