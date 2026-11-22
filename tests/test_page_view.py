"""D140 验收:page_view —— 让智能体**看一眼自己写的网页**再说话。

这里测的不是"能不能跑",而是四件事:
  ① 渲染走的是**回环 HTTP + 与预览同一串 CSP**,不是 file://(那条是真实的泄露路径);
  ② Chrome 参数里**禁网但放行回环**、URL 永远在最后、超时按进程树杀;
  ③ 工具只认**自己生成过的网页产物**,别的一律拒绝,而且它是只读的;
  ④ 说出来的话必须**标明这是画面描述**,截不出来时不许说成"页面有问题"。

跑法:.venv/Scripts/python.exe -m unittest tests.test_page_view -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from la import config
from la.base import shot as shot_mod
from la.base.files import BaseToolError
from la.base.html_page import HTML_PREVIEW_CSP, build_html_tools
from la.domain.artifacts import ArtifactStore
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT

#: 一张真的 1×1 PNG(浏览器截图的最小替身:真的是 PNG,能被 png_size 读出宽高)
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)


class ConfigIsolated(unittest.TestCase):
    """把 data 目录换到临时目录 —— 截图落在 data/shots/,不能写进真实库。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self._saved = {
            key: getattr(config, key)
            for key in ("DATA_DIR", "ARTIFACTS_DIR", "MEDIA_DIR", "ASSISTANT_DB",
                        "MEMORY_DB", "SHOT_CHROME")
        }
        config.DATA_DIR = self.root
        config.ARTIFACTS_DIR = self.root / "artifacts"
        config.MEDIA_DIR = self.root / "media"
        config.ASSISTANT_DB = self.root / "assistant.db"
        config.MEMORY_DB = self.root / "memory.db"
        config.SHOT_CHROME = ""
        for name in ("artifacts", "media"):
            (self.root / name).mkdir(exist_ok=True)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            setattr(config, key, value)
        config._settings = None
        self.tmp.cleanup()


# ==========================================================================
# 1. Chrome 参数:禁网、放行回环、URL 在最后
# ==========================================================================
class ChromeArgsTestCase(unittest.TestCase):
    def test_args_have_the_four_hard_rules(self) -> None:
        args = shot_mod.build_args(
            "chrome.exe", "http://127.0.0.1:5/page?t=x", "out.png", "profile",
            width=800, height=600, virtual_time_ms=1234,
        )
        text = " ".join(args)
        self.assertIn("--headless=new", text)
        # ⭐ 禁网,但**回环必须 EXCLUDE** —— 只写 MAP * 会把我们自己的页面也映射掉,
        #    截出来是 Chrome 的错误页(云朵真机踩过)。
        self.assertIn("MAP * 0.0.0.0", text)
        self.assertIn("EXCLUDE 127.0.0.1", text)
        self.assertIn("--no-proxy-server", text)
        self.assertIn("--window-size=800,600", text)
        self.assertIn("--virtual-time-budget=1234", text)
        self.assertIn("--screenshot=out.png", text)
        # URL 必须最后一个:Chrome 只认末尾那个当目标
        self.assertEqual(args[-1], "http://127.0.0.1:5/page?t=x")




# ==========================================================================
# 2. 临时回环服务:只服务一次这一页,而且带着预览那串 CSP
# ==========================================================================
class PageServerTestCase(unittest.TestCase):
    def _get(self, url: str) -> tuple[int, bytes, dict]:
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, b"", dict(exc.headers or {})

    def test_serves_one_page_with_preview_csp(self) -> None:
        html = "<!doctype html><h1>你好</h1>".encode("utf-8")
        with shot_mod.page_server(html, csp=HTML_PREVIEW_CSP) as url:
            status, body, headers = self._get(url)
            self.assertEqual(status, 200)
            self.assertEqual(body, html)
            self.assertIn("text/html", headers.get("Content-Type", ""))
            csp = headers.get("Content-Security-Policy", "")
            self.assertIn("sandbox allow-scripts", csp)
            self.assertIn("connect-src 'none'", csp)
            self.assertIn("default-src 'none'", csp)
            self.assertNotIn("allow-same-origin", csp)
            self.assertIn("no-store", headers.get("Cache-Control", ""))
            # 同一个 URL 还能再取一次(Chrome 实测会稳定再发一次请求)
            self.assertEqual(self._get(url)[0], 200)

    def test_wrong_token_is_404(self) -> None:
        with shot_mod.page_server(b"<html></html>", csp=HTML_PREVIEW_CSP) as url:
            self.assertEqual(self._get(url.replace("t=", "t=x"))[0], 404)
            self.assertEqual(self._get(url.split("?")[0])[0], 404)

    def test_server_is_loopback_only(self) -> None:
        with shot_mod.page_server(b"<html></html>", csp=HTML_PREVIEW_CSP) as url:
            self.assertTrue(url.startswith("http://127.0.0.1:"), url)


# ==========================================================================
# 3. 渲染:失败要说人话,超时要杀树
# ==========================================================================
class RenderTestCase(ConfigIsolated):
    def _runner_writing(self, data: bytes = PNG_1PX):
        def runner(args, timeout):
            out = next(item for item in args if item.startswith("--screenshot="))
            Path(out.split("=", 1)[1]).write_bytes(data)
        return runner

    def test_renders_and_keeps_the_png(self) -> None:
        result = shot_mod.render_html("<html>ok</html>", runner=self._runner_writing(),
                                      csp=HTML_PREVIEW_CSP)
        self.assertTrue(result.data.startswith(b"\x89PNG"))
        self.assertEqual((result.width, result.height), (1, 1))
        self.assertTrue(result.path.is_file())
        self.assertEqual(result.path.parent, shot_mod.shot_dir())
        self.assertGreaterEqual(result.elapsed, 0.0)
        # 工作目录(浏览器 profile / 中间产物)必须被清掉
        leftovers = [p.name for p in shot_mod.shot_dir().iterdir() if p.name.startswith("work-")]
        self.assertEqual(leftovers, [])

    def test_renders_over_loopback_never_file_url(self) -> None:
        """⭐ 真渲染时喂给浏览器的 URL 必须是**回环 HTTP**,不能是 file://。

        file:// 没有响应头 CSP:页面里一句 <img src="file:///C:/..."> 就能把本机任意
        图片画进画面,再被视觉模型读走(云朵 shots.py 记的就是这条路)。
        """
        seen: list[str] = []

        def runner(args, timeout):  # noqa: ANN001
            seen.append(args[-1])
            out = next(item for item in args if item.startswith("--screenshot="))
            Path(out.split("=", 1)[1]).write_bytes(PNG_1PX)

        shot_mod.render_html("<html>ok</html>", runner=runner, csp=HTML_PREVIEW_CSP)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].startswith("http://127.0.0.1:"), seen[0])
        self.assertNotIn("file:", seen[0])

    def test_runner_writing_nothing_says_so(self) -> None:
        with self.assertRaises(shot_mod.ShotError) as ctx:
            shot_mod.render_html("<html>ok</html>", runner=lambda args, timeout: None,
                                 csp=HTML_PREVIEW_CSP)
        self.assertEqual(ctx.exception.code, "NO_OUTPUT")
        self.assertIn("没有输出截图", ctx.exception.message)

    def test_empty_html_is_refused(self) -> None:
        with self.assertRaises(shot_mod.ShotError) as ctx:
            shot_mod.render_html("", runner=self._runner_writing(), csp=HTML_PREVIEW_CSP)
        self.assertEqual(ctx.exception.code, "EMPTY")

    def test_not_a_png_is_refused(self) -> None:
        with self.assertRaises(shot_mod.ShotError) as ctx:
            shot_mod.render_html("<html>ok</html>",
                                 runner=self._runner_writing(b"not a png"),
                                 csp=HTML_PREVIEW_CSP)
        self.assertEqual(ctx.exception.code, "BAD_OUTPUT")

    def test_timeout_kills_the_process_tree(self) -> None:
        """真起一个"不会结束"的进程当浏览器:超时必须报 TIMEOUT,而且不许留活口。"""
        started = time.time()
        with self.assertRaises(shot_mod.ShotError) as ctx:
            shot_mod.run_chrome(
                [sys.executable, "-c", "import time; time.sleep(30)"], 0.6
            )
        self.assertEqual(ctx.exception.code, "TIMEOUT")
        self.assertLess(time.time() - started, 20, "超时之后必须马上回来,不能干等")

    def test_missing_browser_says_how_to_fix(self) -> None:
        with mock.patch.object(shot_mod, "CHROME_CANDIDATES", ()), \
                mock.patch.object(shot_mod.shutil, "which", return_value=None):
            with self.assertRaises(shot_mod.ShotError) as ctx:
                shot_mod.find_chrome()
        self.assertEqual(ctx.exception.code, "NO_BROWSER")
        self.assertIn("SHOT_CHROME", ctx.exception.message)

    def test_offset_y_shifts_the_page_without_touching_the_file(self) -> None:
        """⭐ 分屏看长页面:靠"把页面上移"而不是 window.scrollTo。

        实测(2026-10-17):无头截图里 window.scrollTo 之后截出来**整张全白**
        (Chrome 的 --screenshot 拍的是布局视口顶部);改成 transform 上移,
        三屏分别拍到了红/蓝/绿三段,完全正确。
        """
        html = b"<html><body><p>x</p></body></html>"
        self.assertEqual(shot_mod.with_offset(html, 0), html)
        shifted = shot_mod.with_offset(html, 900)
        self.assertIn(b"translateY(-900px)", shifted)
        self.assertIsInstance(shifted, bytes)
        self.assertNotIn(b"scrollTo", shifted, "滚动这条路在无头截图里是死的,不许退回去")
        # 没有 </body> 的片段也要能插(不能把内容截丢)
        self.assertIn(b"translateY(-300px)", shot_mod.with_offset(b"<p>x</p>", 300))

    def test_cleanup_removes_only_expired_shots(self) -> None:
        folder = shot_mod.shot_dir()
        folder.mkdir(parents=True, exist_ok=True)
        old = folder / "old.png"
        old.write_bytes(PNG_1PX)
        fresh = folder / "fresh.png"
        fresh.write_bytes(PNG_1PX)
        stamp = time.time() - 48 * 3600
        os.utime(old, (stamp, stamp))
        self.assertEqual(shot_mod.cleanup(ttl_hours=24), 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())


# ==========================================================================
# 4. 工具:只认自己生成的网页;只读;描述要标明是"画面"
# ==========================================================================
class PageViewToolTestCase(ConfigIsolated):
    def setUp(self) -> None:
        super().setUp()
        self.conn = db.connect(self.root / "assistant.db")
        db.migrate(self.conn, MIGRATIONS_ASSISTANT)
        self.store = ArtifactStore(self.conn, root=config.ARTIFACTS_DIR)
        self.page = self.store.register_text(
            "<!doctype html><html><body><h1>周报</h1></body></html>",
            filename="page_周报-v1.html", label="周报", kind="html", session_id="s1",
        )
        self.shots: list[Any] = []

    def tearDown(self) -> None:
        self.conn.close()
        super().tearDown()

    def _tools(self, *, vision=True, artifacts: str = "store"):
        class StubClient:
            def __init__(self) -> None:
                self.calls = 0
                self.question = ""

            async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
                self.calls += 1
                from la.llm.client import ChatResult

                self.question = str(messages[0]["content"][0]["text"])
                return ChatResult(text="标题居中,下面的表格整体偏左,和标题没对齐。", model="stub")

        self.client = StubClient()
        return build_html_tools(
            artifacts=(self.store if artifacts == "store" else artifacts),
            session_id="s1",
            vision_factory=(lambda: self.client) if vision else None,
            vision_model="deepseek-flash",
        )

    def _fake_render(self, width: int = 1280, height: int = 900):
        def render(html, **kwargs):  # noqa: ANN001, ANN003
            self.shots.append({"html": html, **kwargs})
            return shot_mod.PageShot(
                data=PNG_1PX, width=width, height=height, elapsed=1.25,
                path=self.root / "shots" / "fake.png", url="http://127.0.0.1:1/page",
            )
        return render

    def _handler(self, tools):
        spec = next(item for item in tools if item.name == "page_view")
        return spec, spec.handler

    def test_registered_read_only_with_closed_schema(self) -> None:
        from la.tools.registry import READ_ONLY_TOOLS, ToolRegistry

        spec, _handler = self._handler(self._tools())
        self.assertFalse(spec.writes)
        self.assertIn("page_view", READ_ONLY_TOOLS)
        self.assertEqual(spec.required, ())
        registry = ToolRegistry()
        registry.register(spec)
        self.assertFalse(registry.get("page_view").writes)
        schema = spec.parameters_json_schema()
        self.assertEqual(
            set(schema["properties"]), {"filename", "question", "width", "height", "offset_y"}
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertIsInstance(spec.description, str)
        self.assertIn("截图", spec.description)

    def test_renders_the_most_recent_page_and_reports_the_picture(self) -> None:
        _spec, handler = self._handler(self._tools())
        with mock.patch.object(shot_mod, "render_html", self._fake_render()):
            payload = asyncio.run(handler({}))
        self.assertIn("【网页截图", payload["note"])
        self.assertIn("1280×900", payload["note"])
        self.assertIn("标题居中", payload["note"])
        self.assertIn("不是源码级结论", payload["note"])
        self.assertEqual(payload["artifact_id"], str(self.page.id))
        self.assertEqual(payload["width"], 1280)
        self.assertEqual(self.client.calls, 1, "要真的问一次视觉模型")
        # 默认问题必须盯着**版面**,不是"好不好看"
        self.assertIn("版面", self.client.question)

    def test_offset_y_is_passed_and_labelled(self) -> None:
        _spec, handler = self._handler(self._tools())
        render = self._fake_render()
        with mock.patch.object(shot_mod, "render_html", render):
            payload = asyncio.run(handler({"offset_y": 900}))
        self.assertEqual(self.shots[-1]["offset_y"], 900)
        self.assertIn("从 y=900 开始的一屏", payload["note"])
        self.assertIn("上移 900px", payload["note"])

    def test_question_goes_to_the_vision_model(self):
        _spec, handler = self._handler(self._tools())
        with mock.patch.object(shot_mod, "render_html", self._fake_render()):
            asyncio.run(handler({"question": "标题和表格有没有重叠"}))
        self.assertIn("标题和表格有没有重叠", self.client.question)

    def test_unknown_page_is_refused_with_a_way_out(self) -> None:
        _spec, handler = self._handler(self._tools())
        with self.assertRaises(BaseToolError) as ctx:
            asyncio.run(handler({"filename": "不存在.html"}))
        self.assertIn("没有找到那一页", str(ctx.exception))

    def test_no_artifact_layer_is_refused(self) -> None:
        _spec, handler = self._handler(self._tools(artifacts=None))
        with self.assertRaises(BaseToolError):
            asyncio.run(handler({}))

    def test_without_vision_it_says_it_could_not_see(self) -> None:
        """没有视觉模型 = **我没看到**,不是"页面没问题" —— 而且要把截图路径给出来。"""
        _spec, handler = self._handler(self._tools(vision=False))
        with mock.patch.object(shot_mod, "render_html", self._fake_render()):
            with self.assertRaises(BaseToolError) as ctx:
                asyncio.run(handler({}))
        message = str(ctx.exception)
        self.assertIn("没有可用的视觉模型", message)
        self.assertIn("fake.png", message)

    def test_render_failure_does_not_blame_the_page(self) -> None:
        _spec, handler = self._handler(self._tools())
        def boom(html, **kwargs):  # noqa: ANN001, ANN003
            raise shot_mod.ShotError("TIMEOUT", "页面渲染超过 25 秒")

        with mock.patch.object(shot_mod, "render_html", boom):
            with self.assertRaises(BaseToolError) as ctx:
                asyncio.run(handler({}))
        message = str(ctx.exception)
        self.assertIn("没截成图", message)
        self.assertIn("不代表页面有问题", message)

    def test_can_view_a_page_from_another_session(self) -> None:
        """看可以跨会话(错题卷挂在 study 下);**改**不行 —— 那是两件不同风险的事。"""
        other = self.store.register_text(
            "<!doctype html><html><body><h1>错题卷</h1></body></html>",
            filename="page_错题卷-v1.html", label="错题卷", kind="html", session_id="study",
        )
        tools = self._tools()          # 本测试的会话是 s1
        _spec, handler = self._handler(tools)
        with mock.patch.object(shot_mod, "render_html", self._fake_render()):
            payload = asyncio.run(handler({"filename": str(other.id)}))
        self.assertEqual(payload["artifact_id"], str(other.id))
        self.assertIn("study", payload["note"], "跨会话看的页面要标清它从哪来")

        source = next(item for item in tools if item.name == "html_page")
        with self.assertRaises(BaseToolError):
            asyncio.run(source.handler({"action": "source", "filename": str(other.id)}))

    def test_non_html_artifact_is_not_a_page(self) -> None:
        self.store.register_text("# 周报", filename="周报.md", label="周报", kind="text",
                                 session_id="s1")
        _spec, handler = self._handler(self._tools())
        with self.assertRaises(BaseToolError):
            asyncio.run(handler({"filename": "周报.md"}))


if __name__ == "__main__":
    unittest.main()
