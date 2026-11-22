"""D158:微信二维码进设置页 —— 账号隔离、重连标志、竞态修复。

测的都是"会出事的形状":
  * 两个微信号共用一个目录(串号);
  * 网页扫完的凭据被后台进程的 SessionExpired 擦掉;
  * 桥接重启后没接上新凭据,用户以为扫码失败。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from la import config
from la.bridge.adapters import weixin_store
from la.bridge.adapters.weixin_login import QRSession, poll_once
from la.bridge.adapters.weixin_protocol import BotCredentials, SessionExpired

QR_URL = "https://liteapp.weixin.qq.com/q/AbCdEfG?qrcode=0123456789abcdef&bot_type=3"


class FakeClient:
    """够用的假网关:要码、查状态、断线。"""

    def __init__(self, status: str = "confirmed", url: str = QR_URL) -> None:
        self.token = ""
        self.base_url = "https://ilinkai.weixin.qq.com"
        self.status = status
        self.url = url
        self.polls = 0
        self.closed = False

    async def request_qrcode(self) -> dict:
        return {"qrcode": "abc123", "qrcode_img_content": self.url}

    async def poll_qrcode(self, qrcode: str, *, timeout_s: float = 40.0) -> dict:
        self.polls += 1
        return {
            "status": self.status,
            "ilink_bot_id": "bot-1",
            "bot_token": "TOKEN-NEW",
            "baseurl": "https://ilinkai.weixin.qq.com",
            "ilink_user_id": "u1",
        }

    async def aclose(self) -> None:
        self.closed = True


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._saved = {key: getattr(config, key) for key in ("DATA_DIR", "ASSISTANT_DB")}
        config.DATA_DIR = self.root
        config.ASSISTANT_DB = self.root / "assistant.db"
        weixin_store.use_account("")

    def tearDown(self) -> None:
        weixin_store.use_account("")
        for key, value in self._saved.items():
            setattr(config, key, value)
        self._tmp.cleanup()

    def creds(self, token: str = "TOKEN-A") -> BotCredentials:
        return BotCredentials(bot_id="bot-a", token=token, base_url="https://x", user_id="u")


class AccountIsolationTest(StoreCase):
    def test_accounts_have_separate_files(self) -> None:
        weixin_store.save(self.creds("TOKEN-MOM"), account="mom")
        mom = weixin_store.credentials_path("mom")
        self.assertTrue(mom.exists())
        self.assertIn("accounts", str(mom))
        self.assertIsNotNone(weixin_store.load("mom"))
        self.assertIsNone(weixin_store.load(""))

    def test_known_accounts_lists_default_and_named(self) -> None:
        weixin_store.save(self.creds("T1"), account="")
        weixin_store.save(self.creds("T2"), account="mom")
        self.assertEqual(weixin_store.known_accounts(), ["", "mom"])

    def test_clear_only_touches_one_account(self) -> None:
        weixin_store.save(self.creds("T1"), account="")
        weixin_store.save(self.creds("T2"), account="mom")
        weixin_store.clear("mom")
        self.assertIsNotNone(weixin_store.load(""))
        self.assertIsNone(weixin_store.load("mom"))


class ReloginFlagTest(StoreCase):
    def test_flag_is_one_shot_per_account(self) -> None:
        weixin_store.request_relogin("mom")
        self.assertTrue(weixin_store.relogin_path("mom").exists())
        self.assertTrue(weixin_store.consume_relogin("mom"))
        self.assertFalse(weixin_store.consume_relogin("mom"))
        self.assertFalse(weixin_store.relogin_path("").exists())


class PollTest(StoreCase):
    def test_confirmed_writes_credentials_flag_and_clears_cursor(self) -> None:
        weixin_store.save_cursor("old-cursor", account="mom")
        session = QRSession(qrcode="abc123", url=QR_URL)
        asyncio.run(poll_once(FakeClient(), session, account="mom", timeout_s=1.0))
        self.assertEqual(session.status, "confirmed")
        saved = weixin_store.load("mom")
        self.assertIsNotNone(saved)
        self.assertEqual(saved.token, "TOKEN-NEW")
        self.assertEqual(weixin_store.load_cursor("mom"), "")
        self.assertTrue(weixin_store.relogin_path("mom").exists())

    def test_scaned_does_not_write(self) -> None:
        session = QRSession(qrcode="abc123", url=QR_URL)
        asyncio.run(poll_once(FakeClient(status="scaned"), session, account="mom"))
        self.assertEqual(session.status, "scaned")
        self.assertIsNone(weixin_store.load("mom"))


class ManagerTest(StoreCase):
    def test_start_poll_confirms_without_leaking_client(self) -> None:
        from la.bridge.adapters.weixin_login_session import LoginSessionManager

        clients: list[FakeClient] = []

        def factory() -> FakeClient:
            client = FakeClient()
            clients.append(client)
            return client

        manager = LoginSessionManager(client_factory=factory)

        async def scenario():
            session = await manager.start("mom")
            self.assertEqual(session.status, "waiting")
            first = await manager.poll("mom")
            self.assertEqual(first.status, "confirmed")
            await manager.poll("mom")
            return clients[0].polls

        polls = asyncio.run(scenario())
        self.assertEqual(polls, 1)
        self.assertTrue(clients[0].closed)


class BridgeReloadTest(StoreCase):
    def _adapter(self):
        from la.bridge.adapters.weixin_ilink import ILinkAdapter

        return ILinkAdapter(bridge=None, log=lambda *a, **k: None, account="mom")

    def test_reload_applies_new_token_and_drops_conversations(self) -> None:
        adapter = self._adapter()
        adapter.client.token = "OLD"
        adapter.transport.conversations["u1"] = object()
        weixin_store.save(self.creds("NEW"), account="mom")
        weixin_store.save_cursor("old", account="mom")

        async def go():
            return await adapter.reload_credentials()

        self.assertTrue(asyncio.run(go()))
        self.assertEqual(adapter.client.token, "NEW")
        self.assertEqual(adapter.cursor, "old")
        self.assertEqual(adapter.transport.conversations, {})

    def test_session_expired_with_new_disk_credentials_does_not_clear(self) -> None:
        """回归:网页刚扫完 → 后台收到 -14 → **不能**把新凭据擦掉。"""
        adapter = self._adapter()
        adapter.client.token = "OLD"
        state = {"raised": False}

        async def get_updates(cursor: str = ""):
            if not state["raised"]:
                state["raised"] = True
                raise SessionExpired(ret=-14, errmsg="expired")
            return {"msgs": [], "get_updates_buf": ""}

        adapter.client.get_updates = get_updates
        weixin_store.save(self.creds("NEW"), account="mom")

        asyncio.run(adapter.run_forever(max_polls=1))
        self.assertEqual(adapter.client.token, "NEW")
        self.assertIsNotNone(weixin_store.load("mom"))


class WebTest(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        from fastapi.testclient import TestClient

        from la.family import members as member_store
        from la.storage import db
        from la.web import auth as web_auth
        from la.web.app import create_app

        self.conn = db.connect_assistant()
        member_store.set_pin(self.conn, 1, "123456")
        token = web_auth.create_session(self.conn, 1)
        self.client = TestClient(create_app(), follow_redirects=False)
        self.client.cookies.set(web_auth.SESSION_COOKIE, token)

    def tearDown(self) -> None:
        try:
            self.conn.close()
        finally:
            super().tearDown()

    def test_page_requires_admin_and_renders(self) -> None:
        page = self.client.get("/settings/wechat")
        self.assertEqual(page.status_code, 200)
        self.assertIn("微信登录", page.text)

    def test_state_without_session_is_idle(self) -> None:
        response = self.client.get("/settings/wechat/state")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "idle")

    def test_start_qr_renders_svg(self) -> None:
        from la.bridge.adapters.weixin_login_session import LoginSessionManager
        from la.web import routes_wechat

        original = routes_wechat.MANAGER
        clients: list[FakeClient] = []

        def factory() -> FakeClient:
            client = FakeClient()
            clients.append(client)
            return client

        routes_wechat.MANAGER = LoginSessionManager(client_factory=factory)
        try:
            started = self.client.post("/settings/wechat/qr", data={"account": ""})
            self.assertEqual(started.status_code, 303)
            page = self.client.get("/settings/wechat")
            self.assertIn("<svg", page.text)
        finally:
            routes_wechat.MANAGER = original

    def test_non_admin_is_rejected(self) -> None:
        from fastapi.testclient import TestClient

        from la.family import members as member_store
        from la.web import auth as web_auth

        parent = member_store.create_member(self.conn, "家长", role="parent", pin="111111")
        token = web_auth.create_session(self.conn, parent.id)
        client = TestClient(self.client.app, follow_redirects=False)
        client.cookies.set(web_auth.SESSION_COOKIE, token)
        self.assertEqual(client.get("/settings/wechat").status_code, 403)


if __name__ == "__main__":
    unittest.main()
