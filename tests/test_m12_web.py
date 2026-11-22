"""M12 联网验收测试:SSRF 闸 / 抓取 / 搜索解析 / 工具合同。

⚠️ 为什么本地测试服务器要先"开闸":真实的 check_public_url **就应该**拒绝
127.0.0.1 —— 那是它的本职工作。所以本地回环的抓取测试必须显式打补丁,
同时**另有一组测试专门验证"没打补丁时它确实拒绝"**。

运行:
    .venv/Scripts/python.exe -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import asyncio
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from la import config
from la.base.phases import PHASE_ANSWER, CircuitBreaker
from la.base import web
from la.base.tools import build_web_tools
from la.tools.registry import ToolRegistry


class _Handler(BaseHTTPRequestHandler):
    body = "<html><head><style>p{color:red}</style></head><body><p>你好</p><script>x()</script></body></html>"
    location = ""
    status = 200

    def log_message(self, *args):  # noqa: ANN002 - 静音
        return

    def do_GET(self):  # noqa: N802
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", type(self).location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = type(self).body.encode("utf-8")
        self.send_response(type(self).status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class LocalServer:
    def __init__(self) -> None:
        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return "http://" + str(host) + ":" + str(port)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class FakeProvider:
    name = "fake"

    def __init__(self, hits=None, error: str = "") -> None:
        self.hits = hits if hits is not None else [
            web.SearchHit(title="DeepSeek 文档", url="https://api-docs.deepseek.com/", page_age="3 天前"),
            web.SearchHit(title="重复条目", url="https://api-docs.deepseek.com/"),
        ]
        self.error = error
        self.queries: list[str] = []

    async def search(self, query, *, limit):  # noqa: ANN001
        self.queries.append(query)
        if self.error:
            raise web.WebError(self.error)
        return self.hits[:limit]


# ==========================================================================
# 1. SSRF 闸(**默认拒绝**)
# ==========================================================================
class TestUrlGate(unittest.TestCase):
    def test_loopback_and_private_are_refused(self) -> None:
        for url in ("http://127.0.0.1:8765/", "http://10.0.0.5/", "http://192.168.1.1/",
                    "http://169.254.169.254/latest/meta-data/", "http://localhost:8080/",
                    "http://[::1]/"):
            with self.subTest(url=url):
                with self.assertRaises(web.WebError):
                    web.check_public_url(url)

    def test_other_schemes_are_refused(self) -> None:
        for url in ("file:///C:/Windows/win.ini", "ftp://example.com/a", "gopher://x/", ""):
            with self.subTest(url=url):
                with self.assertRaises(web.WebError):
                    web.check_public_url(url)

    def test_too_long_url_refused(self) -> None:
        with self.assertRaises(web.WebError):
            web.check_public_url("http://93.184.216.34/" + "a" * (config.URL_MAX_CHARS + 1))

    def test_public_address_passes(self) -> None:
        self.assertEqual(web.check_public_url("http://93.184.216.34/x"), "http://93.184.216.34/x")


# ==========================================================================
# 2. 抓取(本地服务器 + 显式开闸)
# ==========================================================================
class TestFetch(unittest.TestCase):
    def setUp(self) -> None:
        self.server = LocalServer()
        self._real_gate = web.check_public_url
        # ⚠️ 只在本类里开闸:真实闸门的行为由 TestUrlGate 与
        #    test_gate_blocks_local_server_without_patch 守着。
        web.check_public_url = lambda url: str(url)  # type: ignore[assignment]

    def tearDown(self) -> None:
        web.check_public_url = self._real_gate  # type: ignore[assignment]
        self.server.close()

    def test_gate_blocks_local_server_without_patch(self) -> None:
        web.check_public_url = self._real_gate  # type: ignore[assignment]
        with self.assertRaises(web.WebError):
            asyncio.run(web.fetch_page(self.server.url + "/"))

    def test_html_becomes_text(self) -> None:
        page = asyncio.run(web.fetch_page(self.server.url + "/"))
        self.assertEqual(page.status, 200)
        self.assertIn("你好", page.text)
        self.assertNotIn("color:red", page.text)
        self.assertNotIn("x()", page.text)

    def test_same_origin_redirect_is_followed(self) -> None:
        _Handler.location = self.server.url + "/after"
        try:
            page = asyncio.run(web.fetch_page(self.server.url + "/redirect"))
        finally:
            _Handler.location = ""
        self.assertTrue(page.url.endswith("/after"))

    def test_cross_origin_redirect_is_refused(self) -> None:
        other = LocalServer()
        try:
            _Handler.location = other.url + "/elsewhere"
            with self.assertRaises(web.WebError) as ctx:
                asyncio.run(web.fetch_page(self.server.url + "/redirect"))
            self.assertIn("别的站点", str(ctx.exception))
        finally:
            _Handler.location = ""
            other.close()

    def test_output_is_truncated_and_flagged(self) -> None:
        _Handler.body = "<p>" + "长" * 500 + "</p>"
        try:
            page = asyncio.run(web.fetch_page(self.server.url + "/", max_chars=100))
        finally:
            _Handler.body = _Handler.__dict__.get("body") or ""
        self.assertTrue(page.truncated)
        self.assertLessEqual(len(page.text), 100)


# ==========================================================================
# 3. 官方 Search / Fetch(D148):形状、计费判定、错误映射
# ==========================================================================
RESULTS = [
    {
        "rerank_score": 2.4, "title": "DeepSeek V4 发布", "url": "https://a.example/v4",
        "snippet": "短摘要", "summary": "长摘要:发布了 V4。",
        "site_name": "Example", "date": "2026-06-01T00:00:00+00:00", "authority": 3,
    },
    {"title": "重复", "url": "https://a.example/v4", "summary": "重复条目"},
    {"title": "B", "url": "https://b.example/", "summary": "另一条", "authority": -1},
]


class TestOfficialSearch(unittest.TestCase):
    def _make(self, results=None, status=200, message=""):
        calls: list[dict] = []

        async def poster(url, *, headers, payload, timeout_ms):  # noqa: ANN001
            calls.append({"url": url, "headers": headers, "payload": payload})
            if status != 200:
                return status, {"error": {"message": message, "param": None}}, message
            return status, {"results": results if results is not None else RESULTS}, ""

        return web.DeepSeekWeb(api_key="sk-test", base_url="https://api.deepseek.com",
                               poster=poster), calls

    def test_search_hits_the_official_endpoint_with_bearer_auth(self) -> None:
        client, calls = self._make()
        hits = asyncio.run(client.search("DeepSeek V4", limit=5))
        self.assertEqual(calls[0]["url"], "https://api.deepseek.com/v1/tools/search")
        self.assertEqual(calls[0]["headers"]["Authorization"], "Bearer sk-test")
        self.assertEqual(calls[0]["payload"]["query"], "DeepSeek V4")
        self.assertEqual(calls[0]["payload"]["count"], 5)
        self.assertFalse(calls[0]["payload"]["need_content"], "别把整页正文拉回来占上下文")
        self.assertEqual([hit.url for hit in hits], ["https://a.example/v4", "https://b.example/"])

    def test_hits_keep_summary_date_and_authority(self) -> None:
        client, _calls = self._make()
        hit = asyncio.run(client.search("x", limit=3))[0]
        self.assertEqual(hit.summary, "长摘要:发布了 V4。")
        self.assertEqual(hit.snippet, "短摘要")
        self.assertEqual(hit.site_name, "Example")
        self.assertEqual(hit.authority, 3)
        line = hit.as_line()
        self.assertIn("https://a.example/v4", line)
        self.assertIn("长摘要", line, "摘要要进给模型看的那一行")
        self.assertIn("权威 3/4", line)

    def test_summary_is_clipped_to_the_render_budget(self) -> None:
        long_summary = "字" * (config.WEB_SUMMARY_RENDER_CHARS + 50)
        client, _calls = self._make(results=[{"title": "T", "url": "https://x/", "summary": long_summary}])
        hit = asyncio.run(client.search("x", limit=1))[0]
        self.assertLessEqual(len(hit.as_line()), config.WEB_SUMMARY_RENDER_CHARS + 200)

    def test_limit_is_respected(self) -> None:
        client, _calls = self._make()
        self.assertEqual(len(asyncio.run(client.search("x", limit=1))), 1)

    def test_empty_results_are_not_an_error(self) -> None:
        client, _calls = self._make(results=[])
        self.assertEqual(asyncio.run(client.search("没有这个", limit=5)), [])

    def test_error_status_mapping_is_actionable(self) -> None:
        for status, words in ((401, "API key"), (402, "余额"), (429, "频繁"), (500, "服务端")):
            with self.subTest(status=status):
                client, _calls = self._make(status=status, message="boom")
                with self.assertRaises(web.WebError) as ctx:
                    asyncio.run(client.search("x", limit=1))
                self.assertIn(words, str(ctx.exception))

    def test_fetch_returns_markdown_and_title(self) -> None:
        async def poster(url, *, headers, payload, timeout_ms):  # noqa: ANN001
            self.assertEqual(url, "https://api.deepseek.com/v1/tools/fetch")
            self.assertEqual(payload["url"], "https://example.com/a")
            return 200, {"title": "示例", "content": "# 标题\n\n正文"}, ""

        client = web.DeepSeekWeb(api_key="k", base_url="https://api.deepseek.com", poster=poster)
        page = asyncio.run(client.fetch("https://example.com/a"))
        self.assertEqual(page.provider, "deepseek")
        self.assertEqual(page.title, "示例")
        self.assertEqual(page.media_type, "text/markdown")
        self.assertIn("正文", page.text)

    def test_fetch_404_says_why(self) -> None:
        async def poster(url, *, headers, payload, timeout_ms):  # noqa: ANN001
            return 404, {"error": {"message": "not found"}}, ""

        client = web.DeepSeekWeb(api_key="k", base_url="https://api.deepseek.com", poster=poster)
        with self.assertRaises(web.WebError) as ctx:
            asyncio.run(client.fetch("https://example.com/gone"))
        self.assertIn("抓不到", str(ctx.exception))

    def test_fetch_with_empty_content_is_refused(self) -> None:
        async def poster(url, *, headers, payload, timeout_ms):  # noqa: ANN001
            return 200, {"title": "空", "content": "   "}, ""

        client = web.DeepSeekWeb(api_key="k", base_url="https://api.deepseek.com", poster=poster)
        with self.assertRaises(web.WebError) as ctx:
            asyncio.run(client.fetch("https://example.com/empty"))
        self.assertIn("没有正文", str(ctx.exception))

    def test_fetch_url_gate_keeps_private_addresses_at_home(self) -> None:
        """⚠️ 官方抓取由**对方的服务器**去访问:内网地址它到不了,这里挡的是"别白送出去"。"""
        for url in ("http://127.0.0.1:8765/", "http://10.0.0.5/", "http://192.168.1.1/",
                    "http://localhost/", "http://printer.local/", "http://[::1]/",
                    "file:///C:/Windows/win.ini", "https://user:pw@example.com/a"):
            with self.subTest(url=url):
                with self.assertRaises(web.WebError):
                    web.check_fetch_url(url)
        self.assertEqual(web.check_fetch_url("https://example.com/a"), "https://example.com/a")

    def test_backend_selection(self) -> None:
        class FakeSettings:
            web_search_provider = "deepseek"
            web_fetch_provider = "deepseek"
            api_key = "sk-test"
            base_url = "https://api.deepseek.com"

        backend = web.build_web_backend(FakeSettings())
        self.assertIsInstance(backend.search, web.DeepSeekWeb)
        self.assertIsInstance(backend.fetch, web.DeepSeekWeb)
        self.assertEqual(backend.fetch_name, "deepseek")

        FakeSettings.web_fetch_provider = "local"
        backend = web.build_web_backend(FakeSettings())
        self.assertIsNone(backend.fetch)
        self.assertEqual(backend.fetch_name, "local")

        FakeSettings.web_fetch_provider = "deepseek"
        FakeSettings.api_key = ""
        backend = web.build_web_backend(FakeSettings())
        self.assertIsNone(backend.search, "没 key 就别假装能搜")
        self.assertIn("no key", backend.fetch_name, "配了官方却没 key 时要如实说降级了")

        FakeSettings.api_key = "sk-test"
        FakeSettings.web_search_provider = "none"
        self.assertIsNone(web.build_web_backend(FakeSettings()).search)


# ==========================================================================
# 4. 工具合同:默认关 / 没后端 / 正常返回
# ==========================================================================
class TestWebTools(unittest.TestCase):
    def _call(self, tools, name: str, params: dict):
        registry = ToolRegistry()
        registry.register_all(tools)
        # ⭐ 批次 A(2026-10-15):web_search / web_fetch 已合成一个 web(action=…)。
        #    这里保留旧名写法,由 _modern 翻成新合同 —— 测试要断的是**行为**,不是工具名。
        params = dict(params)
        if name == "web_search":
            name, params = "web", {"action": "search", **params}
        elif name == "web_fetch":
            name, params = "web", {"action": "fetch", **params}
        return asyncio.run(registry.execute(
            name, params, breaker=CircuitBreaker(phase=PHASE_ANSWER),
        ))

    def test_disabled_by_default(self) -> None:
        tools = build_web_tools(enabled=False, provider=FakeProvider())
        result = self._call(tools, "web_search", {"queries": ["天气"]})
        self.assertFalse(result.ok)
        self.assertIn("联网是关着的", result.message)
        result = self._call(tools, "web_fetch", {"url": "https://example.com/"})
        self.assertFalse(result.ok)
        self.assertIn("联网是关着的", result.message)

    def test_missing_provider_is_honest(self) -> None:
        tools = build_web_tools(enabled=True, provider=None)
        result = self._call(tools, "web_search", {"queries": ["天气"]})
        self.assertFalse(result.ok)
        self.assertIn("没有配置搜索后端", result.message)

    def test_search_returns_markdown_links_with_notice(self) -> None:
        provider = FakeProvider()
        tools = build_web_tools(enabled=True, provider=provider)
        result = self._call(tools, "web_search", {"queries": ["deepseek 文档"]})
        self.assertTrue(result.ok, result.message)
        self.assertIn("https://api-docs.deepseek.com/", result.data)
        self.assertIn("(3 天前)", result.data)
        self.assertIn(config.EXTERNAL_CONTENT_NOTICE, result.data)
        self.assertEqual(provider.queries, ["deepseek 文档"])

    def test_search_reports_provider_failure_per_query(self) -> None:
        tools = build_web_tools(enabled=True, provider=FakeProvider(error="额度用完"))
        result = self._call(tools, "web_search", {"queries": ["a", "b"]})
        self.assertTrue(result.ok)
        self.assertIn("额度用完", result.data)
        self.assertIn("检索「b」失败", result.data)

    def test_search_output_stays_within_the_render_budget(self) -> None:
        """⭐ 3 条 × 8 条 × 400 字摘要会撑爆工具预算 —— 必须**自己按预算加**,并说清漏了几条。"""
        hits = [
            web.SearchHit(title="第%d条" % index, url="https://x.example/%d" % index,
                          summary="字" * 400)
            for index in range(8)
        ]
        provider = FakeProvider(hits=hits)
        tools = build_web_tools(enabled=True, provider=provider)
        result = self._call(tools, "web_search", {"queries": ["a", "b", "c"]})
        self.assertTrue(result.ok, result.message)
        self.assertLessEqual(len(result.data), config.WEB_SEARCH_RENDER_MAX_CHARS + 600,
                             "总长要贴着预算,不能被注册表从中间截断")
        self.assertIn("没显示", result.data)

    def test_more_than_three_queries_is_capped(self) -> None:
        provider = FakeProvider()
        tools = build_web_tools(enabled=True, provider=provider)
        self._call(tools, "web_search", {"queries": ["a", "b", "c", "d", "e"]})
        self.assertEqual(provider.queries, ["a", "b", "c"])

    # -- D148:联网的账(官方按"成功请求"计费,失败不计费)
    def test_search_records_a_billable_usage(self) -> None:
        seen: list[dict] = []
        tools = build_web_tools(
            enabled=True, provider=FakeProvider(), provider_name="deepseek",
            on_usage=seen.append,
        )
        result = self._call(tools, "web_search", {"queries": ["deepseek 文档"]})
        self.assertTrue(result.ok, result.message)
        self.assertEqual(seen[0]["kind"], "search")
        self.assertTrue(seen[0]["billable"])
        self.assertEqual(seen[0]["requests"], 1)
        self.assertEqual(seen[0]["results"], 2)
        self.assertEqual(seen[0]["provider"], "deepseek")

    def test_search_without_hits_is_not_billable(self) -> None:
        seen: list[dict] = []
        tools = build_web_tools(enabled=True, provider=FakeProvider(hits=[]), on_usage=seen.append)
        result = self._call(tools, "web_search", {"queries": ["没有这个"]})
        self.assertTrue(result.ok, result.message)
        self.assertFalse(seen[0]["billable"], "没结果不计费(官方口径)")
        self.assertEqual(seen[0]["requests"], 0)

    def test_search_failure_is_recorded_but_not_billable(self) -> None:
        seen: list[dict] = []
        tools = build_web_tools(
            enabled=True, provider=FakeProvider(error="额度用完"), on_usage=seen.append,
        )
        self._call(tools, "web_search", {"queries": ["a"]})
        self.assertFalse(seen[0]["billable"])
        self.assertIn("额度用完", seen[0]["error"])

    def test_injected_fetcher_is_used_and_billed(self) -> None:
        class FakeFetcher:
            def __init__(self) -> None:
                self.urls: list[str] = []

            async def fetch(self, url):  # noqa: ANN001
                self.urls.append(url)
                return web.FetchedPage(
                    url=url, status=200, media_type="text/markdown",
                    text="# 标题\n\n正文", title="示例", provider="deepseek",
                )

        fetcher = FakeFetcher()
        seen: list[dict] = []
        tools = build_web_tools(
            enabled=True, provider=FakeProvider(), fetcher=fetcher,
            fetch_name="deepseek", on_usage=seen.append,
        )
        result = self._call(tools, "web_fetch", {"url": "https://example.com/a"})
        self.assertTrue(result.ok, result.message)
        self.assertIn("正文", result.data)
        self.assertIn("示例", result.data)
        self.assertEqual(fetcher.urls, ["https://example.com/a"])
        self.assertEqual(seen[0]["kind"], "fetch")
        self.assertTrue(seen[0]["billable"])
        self.assertEqual(seen[0]["host"], "example.com")

    def test_fetch_gate_message_is_actionable(self) -> None:
        tools = build_web_tools(enabled=True, provider=None)
        result = self._call(tools, "web_fetch", {"url": "http://127.0.0.1:8765/"})
        self.assertFalse(result.ok)
        self.assertIn("不允许抓取", result.message)


if __name__ == "__main__":
    unittest.main()
