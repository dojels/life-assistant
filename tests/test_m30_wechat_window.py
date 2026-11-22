"""D163:微信 24 小时窗口 —— 过期就别再打了(2026-10-24 真机事故)。

妈妈那个号的 session.json 停在 10-18,里面还留着 context_token 和额度 10:
看着"有窗口",其实早作废。错题推送每 30 秒重试一次,整晚打了 500 多个
`ret=-2 prepare failed`,用户那边一个字没收到,页面上却写着"已推送"。
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from la import config
from la.bridge.adapters import weixin_store
from la.bridge.adapters.weixin_ilink import Conversation, ILinkAdapter, human_gap
from la.bridge.adapters.weixin_protocol import WINDOW_S


def _ms_ago(days: float) -> int:
    return int(time.time() * 1000) - int(days * 86400 * 1000)


class WindowAgeTest(unittest.TestCase):
    def test_fresh_window_is_open(self):
        conv = Conversation(user_id="u", context_token="t", last_seen_ms=_ms_ago(1 / 24))
        self.assertTrue(conv.window_open())

    def test_old_window_is_closed(self):
        conv = Conversation(user_id="u", context_token="t", last_seen_ms=_ms_ago(6))
        self.assertFalse(conv.window_open())
        self.assertIn("6 天", human_gap(conv.window_age_s() or 0.0))

    def test_missing_timestamp_is_permissive(self):
        """老 session.json 没记时间戳:宁可信其开,别把能发的判死。"""
        conv = Conversation(user_id="u", context_token="t", last_seen_ms=0)
        self.assertIsNone(conv.window_age_s())
        self.assertTrue(conv.window_open())

    def test_boundary_is_24_hours(self):
        conv = Conversation(
            user_id="u", last_seen_ms=_ms_ago(WINDOW_S / 86400 + 0.01)
        )
        self.assertFalse(conv.window_open())


class _Client:
    def __init__(self):
        self.calls = 0

    async def send_text(self, *_a, **_k):
        self.calls += 1
        return {"remaining": 9}


class PushRefusesClosedWindowTest(unittest.IsolatedAsyncioTestCase):
    def _adapter(self, conv: Conversation):
        adapter = ILinkAdapter.__new__(ILinkAdapter)
        adapter.client = _Client()
        adapter.transport = SimpleNamespace(
            conversations={conv.user_id: conv},
            conversation=lambda uid: conv,
            _only_user=lambda: conv.user_id,
        )
        adapter.last_error = ""
        return adapter

    async def test_push_text_does_not_call_the_api(self):
        conv = Conversation(user_id="u", context_token="t", last_seen_ms=_ms_ago(6))
        adapter = self._adapter(conv)
        ok, reason = await adapter.push_text("今天的错题", to_user_id="u")
        self.assertFalse(ok)
        self.assertIn("窗口已关", reason)
        self.assertEqual(adapter.client.calls, 0, "过期还去打 = 白打一晚上")

    async def test_member_reason_says_how_long(self):
        conv = Conversation(user_id="u", context_token="t", last_seen_ms=_ms_ago(6))
        adapter = self._adapter(conv)
        reason = adapter._why_no_window({"u"})
        self.assertIn("窗口已关", reason)
        self.assertIn("6 天", reason)

    def test_member_that_never_spoke(self):
        adapter = ILinkAdapter.__new__(ILinkAdapter)
        adapter.transport = SimpleNamespace(conversations={}, conversation=lambda uid: None)
        self.assertIn("还没有可推送", adapter._why_no_window({"u"}))


class StoreWindowsTest(unittest.TestCase):
    """网页进程拿不到内存里的 token,只能读盘 —— 这条通道必须准。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patched = config.DATA_DIR
        config.DATA_DIR = Path(self._tmp.name)

    def tearDown(self):
        config.DATA_DIR = self._patched
        self._tmp.cleanup()

    def test_reports_every_account(self):
        weixin_store.save_session({
            "user_id": "fresh@im.wechat",
            "context_token": "t",
            "last_seen_ms": _ms_ago(0.1),
            "remaining_quota": 10,
        }, account="")
        weixin_store.save_session({
            "user_id": "old@im.wechat",
            "context_token": "t",
            "last_seen_ms": _ms_ago(6),
            "remaining_quota": 10,
        }, account="mom")
        report = weixin_store.windows()
        self.assertTrue(report["fresh@im.wechat"]["open"])
        self.assertFalse(report["old@im.wechat"]["open"])
        self.assertEqual(report["old@im.wechat"]["account"], "mom")
        self.assertGreater(report["old@im.wechat"]["age_s"], 5 * 86400)

    def test_no_session_files_is_empty(self):
        self.assertEqual(weixin_store.windows(), {})


if __name__ == "__main__":
    unittest.main()
