"""M5 验收测试:安全渲染、五个页面、写操作、fail-closed 聊天。

⭐ 核心 DoD:**关掉模型后,页面仍能正常查看与手工编辑数据**。

运行:
    .venv/Scripts/python.exe -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from la import config
from la.storage import db
from la.web.app import create_app
from la.web.markdown import (
    StreamingMarkdown,
    is_safe_url,
    render_markdown,
    safe_prefix_length,
)

FENCE = "```"


class MarkdownTestCase(unittest.TestCase):
    """四层 XSS 防护。"""

    def test_raw_html_is_escaped(self):
        html = render_markdown("<script>alert(1)</script>").html
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_img_onerror_is_escaped(self):
        html = render_markdown("<img src=x onerror=alert(1)>").html
        self.assertNotIn("<img", html)

    def test_javascript_url_is_downgraded(self):
        html = render_markdown("[点我](javascript:alert(1))").html
        self.assertNotIn("href", html)
        self.assertIn("点我", html)

    def test_data_url_is_downgraded(self):
        html = render_markdown("[x](data:text/html;base64,PHNjcmlwdD4=)").html
        self.assertNotIn("href", html)

    def test_safe_links_get_rel_noopener(self):
        html = render_markdown("[官网](https://example.com)").html
        self.assertIn('href="https://example.com"', html)
        self.assertIn('rel="noopener noreferrer"', html)

    def test_quote_in_url_cannot_break_out_of_attribute(self):
        """URL 里带引号时,引号必须被转义 —— 否则就能逃出 href 属性注入事件处理器。"""
        html = render_markdown('[x](https://a.b/"onmouseover="alert(1))').html
        self.assertNotIn('"onmouseover="', html, "裸引号绝不能出现在输出里")
        self.assertIn("&quot;", html, "引号应当被转义成实体")

    def test_scheme_whitelist(self):
        self.assertTrue(is_safe_url("https://a.b"))
        self.assertTrue(is_safe_url("/ledger"))
        self.assertTrue(is_safe_url("#top"))
        self.assertFalse(is_safe_url("javascript:x"))
        self.assertFalse(is_safe_url("vbscript:x"))
        self.assertFalse(is_safe_url("data:text/html,x"))
        self.assertFalse(is_safe_url(""))

    def test_basic_markup(self):
        html = render_markdown("# 标题\n\n**粗体** 与 单行代码\n\n- a\n- b").html
        self.assertIn("<h2>标题</h2>", html)
        self.assertIn("<strong>粗体</strong>", html)
        self.assertIn("<li>a</li>", html)

    def test_code_fence_content_is_escaped(self):
        html = render_markdown(FENCE + "\n<script>x</script>\n" + FENCE).html
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_unclosed_fence_degrades_to_code_block(self):
        """⭐ 未闭合退化:围栏没闭合时按代码块显示,而不是当普通文本。"""
        rendered = render_markdown(FENCE + "python\nprint(1)")
        self.assertTrue(rendered.html.startswith("<pre><code"))
        self.assertIn("print(1)", rendered.html)

    def test_streaming_only_renders_complete_lines(self):
        stream = StreamingMarkdown()
        first = stream.feed("**未完成")
        self.assertIn("pending", first, "半截的粗体不该被渲染")
        second = stream.feed("的粗体**\n下一行")
        self.assertIn("<strong>未完成的粗体</strong>", second)

    def test_safe_prefix_does_not_split_a_fence(self):
        text = "普通\n" + FENCE + "python\nprint(1)\n"
        self.assertEqual(safe_prefix_length(text), len("普通\n"),
                         "围栏未闭合时不能把围栏内容当普通文本渲染")

    def test_table_rendering(self):
        html = render_markdown("| a | b |\n| --- | --- |\n| 1 | 2 |").html
        self.assertIn("<table>", html)
        self.assertIn("<th>a</th>", html)
        self.assertIn("<td>1</td>", html)


class PagesWithoutModelTestCase(unittest.TestCase):
    """⭐ M5 的核心 DoD:界面的读取路径里绝不能有 LLM。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        # 这些常量都是**调用时**读取的,所以打补丁有效
        self._patched = {
            key: getattr(config, key)
            for key in ("DATA_DIR", "ARCHIVE_DIR", "MEDIA_DIR", "ASSISTANT_DB", "MEMORY_DB")
        }
        config.DATA_DIR = self.tmp
        config.ARCHIVE_DIR = self.tmp / "archive"
        config.MEDIA_DIR = self.tmp / "media"
        config.ASSISTANT_DB = self.tmp / "assistant.db"
        config.MEMORY_DB = self.tmp / "memory.db"
        # ⚠️ 必须**改环境变量**,不能只改内存里的 settings:
        #    load_dotenv 用的是 setdefault,只有环境变量已存在时才不会被 .env 覆盖。
        #    (只改 config._settings 的话,build_services 一 reload 就把密钥读回来了。)
        self._env_backup = {
            key: os.environ.get(key) for key in ("DEEPSEEK_API_KEY", "LA_LLM_PROVIDER")
        }
        os.environ["DEEPSEEK_API_KEY"] = ""
        os.environ["LA_LLM_PROVIDER"] = "deepseek"
        config._settings = None
        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        for key, value in self._patched.items():
            setattr(config, key, value)
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config._settings = None
        self._tmp.cleanup()

    def _use_fake_provider(self) -> None:
        os.environ["LA_LLM_PROVIDER"] = "fake"
        config._settings = None

    def test_all_read_pages_render_without_a_model(self):
        for path in ("/ledger", "/calendar", "/health", "/study", "/chat"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn("Family Agent", response.text, path)

    def test_healthz_reports_no_key(self):
        payload = self.client.get("/healthz").json()
        self.assertEqual(payload["status"], "ok")
        self.assertFalse(payload["model_configured"])

    def test_ledger_can_be_read_and_edited_without_a_model(self):
        """⭐ 手工记一笔 -> 确认 -> 页面上能看到,全程不碰模型。"""
        from la.domain.audit import AuditSink
        from la.domain.services.ledger_service import LedgerService

        conn = db.connect_assistant()
        ledger = LedgerService(conn, audit=AuditSink(conn))
        record = ledger.record_expense(
            {"amount_cents": 3800, "payee": "老张牛肉面", "category": "餐饮"}, actor="user"
        )
        conn.close()

        page = self.client.get("/ledger")
        self.assertIn("老张牛肉面", page.text)
        self.assertIn("待确认", page.text)

        response = self.client.post("/ledger/confirm/" + str(record.id), follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        conn = db.connect_assistant()
        status = conn.execute(
            "SELECT status FROM app_txns WHERE id = ?", (record.id,)
        ).fetchone()[0]
        conn.close()
        self.assertEqual(status, "confirmed")

    def test_health_can_be_logged_without_a_model(self):
        response = self.client.post(
            "/health/weight", data={"value": "72.5"}, follow_redirects=False
        )
        self.assertEqual(response.status_code, 303)
        conn = db.connect_assistant()
        row = conn.execute(
            "SELECT value FROM app_measurements WHERE metric_code = 'weight'"
        ).fetchone()
        conn.close()
        self.assertAlmostEqual(float(row[0]), 72.5, places=3)
        self.assertIn("72.5", self.client.get("/health").text)

    def test_study_can_be_edited_without_a_model(self):
        response = self.client.post(
            "/study/problem",
            data={"subject": "数学", "stem": "3/4 + 1/6 = ?", "answer": "11/12",
                  "kp": "异分母分数加减,通分"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        # ⭐ D111:错题页拆成页签,录进来的这道题在**错题本**那一页 —— 而默认的
        #    "今日复习"是孩子视角,按规矩不显示知识点。
        self.assertIn("异分母分数加减", self.client.get("/study?tab=problems").text)
        self.assertIn("错题本", self.client.get("/study").text, "页签要在")

    def test_calendar_page_lists_events(self):
        """⚠️ 时刻原来是写死的 "2026-10-14T15:00:00+08:00" —— 过了那天它就落到窗口外了。

        「日程」页的窗口从**今天 0 点**开始(见 _calendar_window:从"现在"开始的话,
        今天 15:00 那件事在 19:53 打开页面时就消失了),所以演示事件必须跟着 now 走,
        否则这条测试会自己过期。
        """
        from datetime import timedelta

        from la.domain.audit import AuditSink
        from la.domain.services.calendar_service import CalendarService

        start = (config.get_settings().now() + timedelta(days=3)).replace(
            hour=15, minute=0, second=0, microsecond=0
        )
        conn = db.connect_assistant()
        service = CalendarService(conn, audit=AuditSink(conn))
        service.create_event({"title": "和医生复诊", "start": start.isoformat(timespec="seconds")})
        conn.close()
        self.assertIn("和医生复诊", self.client.get("/calendar?days=400").text)

    def test_chat_fails_closed_without_a_model(self):
        """⭐ 模型不可用时聊天必须优雅失败,而不是白屏或 500。"""
        with self.client.stream("POST", "/chat/stream", data={"text": "你好"}) as response:
            self.assertEqual(response.status_code, 200)
            body = "".join(chunk for chunk in response.iter_text())
        self.assertIn("event: error", body)
        self.assertIn("LLMAuthError", body)

    def test_chat_streams_with_the_fake_provider(self):
        self._use_fake_provider()
        with self.client.stream("POST", "/chat/stream", data={"text": "你好"}) as response:
            body = "".join(chunk for chunk in response.iter_text())
        self.assertIn("event: html", body)
        self.assertIn("event: done", body)


if __name__ == "__main__":
    unittest.main()
