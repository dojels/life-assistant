"""M12 联网开关:按钮、持久化、以及"点了立刻生效"。

⭐ 这一组测试守的是一条承诺:**按钮点了就生效,不需要重启**(常驻的微信桥接会话
   读的是同一份库里的状态)。所以除了接口本身,还要验"工具层当场改行为"。

运行:
    .venv/Scripts/python.exe -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from la import config
from la.base.phases import PHASE_ANSWER, CircuitBreaker
from la.base.tools import build_web_tools
from la.base.web import SearchHit
from la.ops import settings_store
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT
from la.tools.registry import ToolRegistry


class FakeProvider:
    name = "fake"

    async def search(self, query, *, limit):  # noqa: ANN001
        return [SearchHit(title="示例", url="https://example.com/")]


class ToggleFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.conn = db.connect(self.root / "assistant.db")
        db.migrate(self.conn, MIGRATIONS_ASSISTANT)

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
        self._tmp.cleanup()


class TestSettingsStore(ToggleFixture):
    def test_default_when_no_row(self) -> None:
        self.assertFalse(settings_store.web_enabled(self.conn))
        self.assertTrue(settings_store.web_enabled(self.conn, default=True))

    def test_set_then_read(self) -> None:
        settings_store.set_flag(self.conn, settings_store.WEB_ENABLED_KEY, True)
        self.assertTrue(settings_store.web_enabled(self.conn, default=False))
        settings_store.set_flag(self.conn, settings_store.WEB_ENABLED_KEY, False)
        self.assertFalse(settings_store.web_enabled(self.conn, default=True))

    def test_database_wins_over_env_default(self) -> None:
        """⭐ 用户点过按钮之后,他的点击就是最新意图;.env 只当出厂默认。"""
        settings_store.set_flag(self.conn, settings_store.WEB_ENABLED_KEY, False)
        self.assertFalse(settings_store.web_enabled(self.conn, default=True))

    def test_unknown_key_falls_back(self) -> None:
        self.assertIsNone(settings_store.get_raw(self.conn, "nope"))
        self.assertTrue(settings_store.get_flag(self.conn, "nope", default=True))

    def test_missing_connection_is_not_a_crash(self) -> None:
        self.assertFalse(settings_store.web_enabled(None, default=False))
        self.assertTrue(settings_store.web_enabled(None, default=True))


class TestLiveGate(ToggleFixture):
    """工具层每次调用**现读**开关 —— 这就是"点了立刻生效"的机制。"""

    def _call(self, tools, params: dict):
        registry = ToolRegistry()
        registry.register_all(tools)
        # ⭐ 批次 A(2026-10-15):web_search 已并入 web(action=search)。
        return asyncio.run(registry.execute(
            "web", {"action": "search", **params}, breaker=CircuitBreaker(phase=PHASE_ANSWER),
        ))

    def test_flip_takes_effect_without_rebuilding_tools(self) -> None:
        tools = build_web_tools(
            enabled=lambda: settings_store.web_enabled(self.conn, default=False),
            provider=FakeProvider(),
        )
        before = self._call(tools, {"queries": ["天气"]})
        self.assertFalse(before.ok)
        self.assertIn("联网是关着的", before.message)

        settings_store.set_flag(self.conn, settings_store.WEB_ENABLED_KEY, True)
        after = self._call(tools, {"queries": ["天气"]})
        self.assertTrue(after.ok, after.message)
        self.assertIn("https://example.com/", after.data)

        settings_store.set_flag(self.conn, settings_store.WEB_ENABLED_KEY, False)
        again = self._call(tools, {"queries": ["天气"]})
        self.assertFalse(again.ok)
        self.assertIn("联网是关着的", again.message)


class TestToggleEndpoint(unittest.TestCase):
    """⚠️ 路径常量必须**逐个**打补丁:config.ASSISTANT_DB 是 import 期算好的,
    只改 DATA_DIR 的话 Web 层仍然连真实的库 —— 第一版就是因此把开关写进了真库。"""

    PATHS = ("DATA_DIR", "ARCHIVE_DIR", "MEDIA_DIR", "ARTIFACTS_DIR",
             "LOGS_DIR", "ASSISTANT_DB", "MEMORY_DB", "RUNTIME_DIRS")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._patched = {key: getattr(config, key) for key in self.PATHS}
        config.DATA_DIR = self.tmp
        config.ARCHIVE_DIR = self.tmp / "archive"
        config.MEDIA_DIR = self.tmp / "media"
        config.ARTIFACTS_DIR = self.tmp / "artifacts"
        config.LOGS_DIR = self.tmp / "logs"
        config.ASSISTANT_DB = self.tmp / "assistant.db"
        config.MEMORY_DB = self.tmp / "memory.db"
        config.RUNTIME_DIRS = (self.tmp, config.ARCHIVE_DIR, config.MEDIA_DIR,
                               config.ARTIFACTS_DIR, config.LOGS_DIR)
        self._settings_cache = config._settings  # noqa: SLF001
        config._settings = None  # noqa: SLF001
        from fastapi.testclient import TestClient

        from la.web import deps as web_deps
        from la.web.app import create_app

        self._real_factory = web_deps.build_model_client
        web_deps.build_model_client = lambda settings: _StubClient()
        try:
            self.client = TestClient(create_app(), follow_redirects=False)
        finally:
            web_deps.build_model_client = self._real_factory

    def tearDown(self) -> None:
        for key, value in self._patched.items():
            setattr(config, key, value)
        config._settings = self._settings_cache  # noqa: SLF001
        self._tmp.cleanup()

    def test_status_defaults_to_off(self) -> None:
        payload = self.client.get("/api/web/status").json()
        self.assertFalse(payload["enabled"])
        self.assertIn("provider_ready", payload)

    def test_toggle_flips_and_persists(self) -> None:
        first = self.client.post("/web/toggle", data={"next": "/chat"})
        self.assertEqual(first.status_code, 303)
        self.assertTrue(self.client.get("/api/web/status").json()["enabled"])
        second = self.client.post("/web/toggle", data={"next": "/chat"})
        self.assertEqual(second.status_code, 303)
        self.assertFalse(self.client.get("/api/web/status").json()["enabled"])

    def test_chat_page_renders_the_button(self) -> None:
        page = self.client.get("/chat")
        self.assertEqual(page.status_code, 200)
        self.assertIn("联网 · 关", page.text)
        self.client.post("/web/toggle", data={"next": "/chat"})
        self.assertIn("联网 · 开", self.client.get("/chat").text)


class _StubClient:
    """只为让 create_app 装配得起来(这一组测试不碰模型)。"""

    async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        from la.llm.client import ChatResult

        return ChatResult(text="好的", model="stub")


if __name__ == "__main__":
    unittest.main()
