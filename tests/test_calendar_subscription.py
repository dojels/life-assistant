"""日历订阅(ICS)与"待办进月历"的页面级验收。

用户 2026-10-22 的两条要求:
  ① 待办事项也应该显示在日历里;
  ② 订阅到手机的功能 —— 他是 iPhone,要能直接把只读日历加进系统日历。

这里钉住:
  * 月历格子里真的画出待办(不是只在「待办」页签里有);
  * 订阅 token 要显式生成,没 token / 错 token 一律 403;
  * 生成的 ICS 同时含日程与待办,并且带 iPhone 会看的刷新提示;
  * 重置后旧链接立即失效(否则"重置"只是换了个显示)。
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path

from la import config


class _FakeRequest:
    """只给 record_hit 用的最小请求对象(和 FastAPI 的那个同形)。"""

    def __init__(self, host: str, ua: str = "iOS/26.6.1 (23G83) dataaccessd/1.0") -> None:
        self.headers = {"host": host, "user-agent": ua}

        class _Client:
            host = "117.147.23.39"

        self.client = _Client()


class CalendarSubscriptionCase(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from la.domain.audit import AuditSink
        from la.domain.services.calendar_service import CalendarService
        from la.storage import db
        from la.tasks.todos import TodoStore
        from la.web import app as web_app

        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self._patched = {
            key: getattr(config, key)
            for key in ("DATA_DIR", "ARCHIVE_DIR", "MEDIA_DIR", "ASSISTANT_DB", "MEMORY_DB")
        }
        self._env = {
            key: os.environ.get(key)
            for key in ("DEEPSEEK_API_KEY", "LA_LLM_PROVIDER", "LA_TZ",
                        "LA_CALENDAR_PUBLIC_BASE")
        }
        os.environ["DEEPSEEK_API_KEY"] = ""
        os.environ["LA_LLM_PROVIDER"] = "fake"
        os.environ.pop("LA_CALENDAR_PUBLIC_BASE", None)
        config.DATA_DIR = self.tmp
        config.ARCHIVE_DIR = self.tmp / "archive"
        config.MEDIA_DIR = self.tmp / "media"
        config.ASSISTANT_DB = self.tmp / "assistant.db"
        config.MEMORY_DB = self.tmp / "memory.db"
        config._settings = None

        now = config.get_settings().now()
        self.today = now.date()
        self.soon = (self.today.replace(day=min(self.today.day, 20)))
        conn = db.connect_assistant()
        CalendarService(conn, audit=AuditSink(conn)).create_event({
            "title": "和医生复诊",
            "start": self.today.isoformat() + "T15:00:00+08:00",
        })
        conn.close()
        memory = db.connect_memory()
        TodoStore(memory).add(
            "交物业费", due_date_local=(self.today.replace(day=min(self.today.day, 19))).isoformat()
        )
        memory.close()
        self.client = TestClient(web_app.create_app())

    def tearDown(self) -> None:
        for key, value in self._patched.items():
            setattr(config, key, value)
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config._settings = None
        self._tmp.cleanup()

    def _rotate(self, token: str = "ics-1") -> None:
        response = self.client.post(
            "/calendar/subscribe/rotate", data={"_idem": token}, follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)

    def _sub_url(self, html: str) -> str:
        match = re.search(r'/calendar/ics\?token=([A-Za-z0-9_\-]+)', html)
        self.assertIsNotNone(match, "页面上应该出现订阅链接")
        return "/calendar/ics?token=" + match.group(1)

    def test_month_grid_draws_todo_chips(self) -> None:
        html = self.client.get("/calendar").text
        self.assertIn("cal-chip todo", html)
        self.assertIn("交物业费", html)
        self.assertIn("/calendar?tab=todos#todo-", html)

    def test_ics_requires_a_valid_token(self) -> None:
        self.assertEqual(self.client.get("/calendar/ics").status_code, 403)
        self.assertEqual(self.client.get("/calendar/ics?token=nope").status_code, 403)

    def test_generated_ics_contains_events_and_todos(self) -> None:
        self._rotate()
        page = self.client.get("/calendar").text
        self.assertIn("在 iPhone 上订阅", page)
        self.assertIn("订阅链接会自动刷新", page)
        self.assertIn("下载 .ics(一次性)", page)
        self.assertIn("不安全连接", page)
        url = self._sub_url(page)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/calendar", response.headers["content-type"])
        body = response.text
        self.assertIn("SUMMARY:和医生复诊", body)
        self.assertIn("SUMMARY:待办 · 交物业费", body)
        self.assertIn("REFRESH-INTERVAL", body)
        self.assertIn("X-WR-CALNAME", body)
        self.assertIn("CATEGORIES:待办", body)

    def test_rotate_revokes_the_old_link(self) -> None:
        self._rotate("ics-2")
        old = self._sub_url(self.client.get("/calendar").text)
        self._rotate("ics-3")
        self.assertEqual(self.client.get(old).status_code, 403)
        new = self._sub_url(self.client.get("/calendar").text)
        self.assertEqual(self.client.get(new).status_code, 200)

    def test_phone_url_uses_the_public_base_when_configured(self) -> None:
        """⭐ 2026-10-31 用户:"不想装 Tailscale"。

        公网只放行 /calendar/ics 一条只读入口 —— 页面"给手机的那一条"必须是这个公网地址,
        复制/订阅按钮也用它;当前这一页的地址(内网/尾网)只给电脑用。
        """
        os.environ["LA_CALENDAR_PUBLIC_BASE"] = "http://203.0.113.10:8765"
        config._settings = None
        self._rotate()
        page = self.client.get("/calendar").text
        self.assertIn("http://203.0.113.10:8765/calendar/ics?token=", page)
        self.assertIn("webcal://203.0.113.10:8765/calendar/ics?token=", page)
        self.assertIn("不用装任何 App", page)
        # 当前页地址仍要标出来,免得以后有人拿它当"手机也能用"
        self.assertIn("手机连不上", page)

    def test_calendar_page_shows_when_the_phone_last_fetched(self) -> None:
        """⭐ 2026-10-31 用户报"手机上订阅了日历,好几天没收到更新"。

        iPhone 每小时都在失败(旧地址的公网端口已关),但页面**什么都看不出来**。
        现在每来取一次就记一笔,页面直接写"上次手机来取:什么时候、用的哪个地址"。
        """
        self._rotate()
        url = self._sub_url(self.client.get("/calendar").text)
        self.client.get(url)                       # 模拟手机来取一次
        page = self.client.get("/calendar").text
        self.assertIn("上次手机来取", page)
        self.assertIn("testserver", page)          # 记下了它用的地址,便于和当前地址对比

    def test_a_stale_fetch_is_called_out(self) -> None:
        from datetime import datetime, timedelta, timezone

        from la.storage import db
        from la.web import calendar_subscribe

        self._rotate()
        conn = db.connect_assistant()
        try:
            calendar_subscribe.record_hit(
                conn, None, request=_FakeRequest("testserver"),
                when=datetime.now(timezone.utc) - timedelta(days=3, hours=2),
            )
        finally:
            conn.close()
        page = self.client.get("/calendar").text
        self.assertIn("已经 3 天没来取了", page)

    def test_a_different_host_is_called_out(self) -> None:
        """手机还挂着旧地址(公网 IP)时,页面要直接指出来 —— 这正是 10-31 的真因。"""
        from la.storage import db
        from la.web import calendar_subscribe

        self._rotate()
        conn = db.connect_assistant()
        try:
            calendar_subscribe.record_hit(
                conn, None, request=_FakeRequest("203.0.113.10:8765"),
            )
        finally:
            conn.close()
        page = self.client.get("/calendar").text
        self.assertIn("203.0.113.10:8765", page)
        self.assertIn("手机上还挂着旧订阅", page)

    def test_public_base_keeps_the_forwarded_port(self) -> None:
        """nginx 的 $host 会丢端口 —— 链接必须补回 :8765,否则手机连 80 会超时。"""
        from la.web.calendar_subscribe import _public_base

        class URL:
            scheme = "http"
            netloc = "127.0.0.1:8766"

        class Request:
            def __init__(self, headers):
                self.headers = headers
                self.url = URL()

        self.assertEqual(
            _public_base(Request({"host": "203.0.113.10", "x-forwarded-port": "8765"})),
            "http://203.0.113.10:8765",
        )
        self.assertEqual(
            _public_base(Request({"host": "203.0.113.10:8765", "x-forwarded-port": "8765"})),
            "http://203.0.113.10:8765",
            "已经带端口时不能补两遍",
        )
        self.assertEqual(
            _public_base(Request({
                "host": "cal.example.com", "x-forwarded-proto": "https",
                "x-forwarded-port": "443",
            })),
            "https://cal.example.com",
            "标准的 443 不写在 URL 里",
        )

    def test_download_disposition(self) -> None:
        self._rotate("ics-4")
        url = self._sub_url(self.client.get("/calendar").text) + "&download=1"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers.get("content-disposition", ""))


if __name__ == "__main__":
    unittest.main()
