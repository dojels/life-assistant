"""M13 验收测试:网页能力(html_page —— 生成自包含网页 + 沙箱预览 + 最小改动改版)。

四层都测:
  * **纯函数**(ensure_document / lint / patch_text / 折叠还原 / 图片内联)—— 不碰数据库;
  * **工具合同**(build_html_tools 生成的那一个工具)—— 经 ToolRegistry 走完整管线;
  * **注册**(build_base_tools 真的把它挂上去了)—— 工具清单是"唯一执行入口"的那张表;
  * **网页路由**(/generated/<产物id> 的沙箱 CSP)—— FastAPI TestClient 起真应用。

依据(`参考实现`):D:\云朵辅助培训智能体\skills\html-page +
app/inline_assets.py + app/main.py::_HTML_PREVIEW_CSP。

运行:
    .venv/Scripts/python.exe -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import asyncio
import re
import tempfile
import unittest
from pathlib import Path

from la import config
from la.base.phases import PHASE_ANSWER, CircuitBreaker
from la.base import html_page as hp
from la.base.tools import build_base_tools
from la.domain.artifacts import KIND_HTML, ArtifactStore, kind_for
from la.domain.receipts import DOMAIN_ARTIFACT, ReceiptLog
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT
from la.tools.registry import ToolRegistry

BASE_DIR = Path(__file__).resolve().parent.parent

#: 1×1 的 PNG(标准库就能造,不需要 Pillow)
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)

PAGE = (
    '<!doctype html><html><head><title>周报网页</title></head><body>'
    '<h1 id="t">周报</h1><img src="shot.png"><p>正文</p></body></html>'
)


# ==========================================================================
# 1. 纯函数
# ==========================================================================
class TestHtmlHelpers(unittest.TestCase):
    def test_ensure_document_wraps_a_fragment(self) -> None:
        text = hp.ensure_document("<h1>你好</h1>", "测试页")
        self.assertTrue(text.startswith("<!doctype html>"))
        self.assertIn('<meta charset="utf-8">', text)
        # ⭐ 手机优先:viewport 是这个项目额外要求的(云朵那边没有,它在电脑上看)
        self.assertIn("viewport", text)
        self.assertIn("<title>测试页</title>", text)
        self.assertIn("<h1>你好</h1>", text)

    def test_ensure_document_fills_missing_head_bits(self) -> None:
        text = hp.ensure_document("<!doctype html><html><head><title>x</title></head><body>ok</body></html>", "x")
        self.assertIn('<meta charset="utf-8">', text)
        self.assertIn("viewport", text)
        self.assertEqual(text.count("<head>"), 1)

    def test_lint_reports_the_four_sandbox_traps(self) -> None:
        warnings = hp.lint(
            '<link rel="stylesheet" href="https://cdn.example.com/a.css">'
            "<script>localStorage.setItem('a', 1); fetch('/x'); window.parent.postMessage('x','*')</script>"
        )
        joined = "\n".join(warnings)
        self.assertIn("外部资源", joined)
        self.assertIn("localStorage", joined)
        self.assertIn("网络", joined)
        self.assertIn("父页面", joined)
        self.assertEqual(len(warnings), 4, warnings)

    def test_lint_clean_page_is_silent(self) -> None:
        self.assertEqual(hp.lint("<p>hello</p><img src=\"data:image/png;base64,AAA\">"), [])

    def test_clean_stem_strips_prefix_timestamp_and_version(self) -> None:
        self.assertEqual(hp.clean_stem("20261012-190500-page_周报-v3.html"), "周报")
        self.assertEqual(hp.clean_stem("page_周报"), "周报")
        self.assertEqual(hp.clean_stem("周报.html"), "周报")
        self.assertEqual(hp.clean_stem(""), "")

    def test_patch_text_only_touches_the_hit(self) -> None:
        text = "<p>a</p><p>b</p>"
        new, error = hp.patch_text(text, "<p>a</p>", "<p>A</p>")
        self.assertEqual(error, "")
        self.assertEqual(new, "<p>A</p><p>b</p>")

    def test_patch_text_refuses_missing_and_ambiguous(self) -> None:
        _, error = hp.patch_text("<p>a</p>", "<h1>zzz</h1>", "x")
        self.assertIn("action=source", error)
        _, error = hp.patch_text("<p>a</p><p>a</p>", "<p>a</p>", "<p>b</p>")
        self.assertIn("唯一命中", error)
        new, error = hp.patch_text("<p>a</p><p>a</p>", "<p>a</p>", "<p>b</p>", replace_all=True)
        self.assertEqual(error, "")
        self.assertEqual(new, "<p>b</p><p>b</p>")

    def test_fold_and_restore_roundtrip(self) -> None:
        original = '<img src="data:image/png;base64,AAAABBBBCCCC">'
        folded, table = hp.fold_data_uris(original)
        self.assertNotIn("base64,AAAA", folded)
        self.assertEqual(len(table), 1)
        back, error = hp.restore_placeholders(folded, table)
        self.assertEqual(error, "")
        self.assertEqual(back, original)

    def test_restore_refuses_an_unknown_placeholder(self) -> None:
        """占位符是**记号**,不是正文:抄错一个就必须拒绝写入(云朵 source_view 的纪律)。"""
        _, error = hp.restore_placeholders("ELIDED_BASE64_9_of_5_chars", {})
        self.assertIn("对不上", error)

    def test_looks_local_knows_what_not_to_touch(self) -> None:
        for ref in ("data:image/png;base64,AA", "https://x/y.png", "//cdn/x.png", "file:///C:/x.png"):
            self.assertFalse(hp.looks_local(ref), ref)
        for ref in ("shot.png", "data/media/materials/x.jpg", "/artifacts/fabc123456", r"C:\pic\x.png"):
            self.assertTrue(hp.looks_local(ref), ref)

    def test_inline_local_images_rewrites_only_local_ones(self) -> None:
        payload = PNG_1PX

        def resolve(ref: str) -> tuple[bytes, str]:
            if ref == "shot.png":
                return payload, "image/png"
            raise AssertionError("不该去解析:" + ref)

        html = '<img src="shot.png"><img src="https://cdn.example.com/x.png">'
        text, notes, warns = hp.inline_local_images(html, resolve)
        self.assertIn("data:image/png;base64,", text)
        self.assertIn("https://cdn.example.com/x.png", text)   # 外链一律不碰
        self.assertEqual(len(notes), 1)
        self.assertEqual(warns, [])

    def test_inline_local_images_reports_instead_of_dropping(self) -> None:
        """找不到 / 太大 / 不是图片:一律保持原样并**如实回报**,绝不静默丢。"""
        def resolve(ref: str) -> tuple[bytes, str]:
            if ref == "big.png":
                return b"x" * (config.HTML_INLINE_MAX_IMAGE_BYTES + 1), "image/png"
            if ref == "note.txt.png":
                return b"plain text", "text/plain"
            raise hp.BaseToolError("在素材区/产物区里找不到这张图:missing.png")

        html = '<img src="big.png"><img src="note.txt.png"><img src="missing.png">'
        text, notes, warns = hp.inline_local_images(html, resolve)
        self.assertEqual(notes, [])
        self.assertNotIn("data:image", text)
        joined = "\n".join(warns)
        self.assertIn("图太大", joined)
        self.assertIn("不是能内联的图片", joined)
        self.assertIn("找不到", joined)


# ==========================================================================
# 2. 工具合同(经 ToolRegistry 走完整管线)
# ==========================================================================
class TestHtmlPageTool(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._original = (config.DATA_DIR, config.MEDIA_DIR, config.ARTIFACTS_DIR)
        config.DATA_DIR = self.root
        config.MEDIA_DIR = self.root / "media"
        config.ARTIFACTS_DIR = self.root / "artifacts"
        # ⚠️ 一个个建:config.RUNTIME_DIRS 是 import 期算好的元组,改了属性它不会跟着变
        for directory in (self.root, config.MEDIA_DIR, config.ARTIFACTS_DIR):
            directory.mkdir(parents=True, exist_ok=True)
        self.conn = db.connect(self.root / "assistant.db")
        db.migrate(self.conn, MIGRATIONS_ASSISTANT)
        self.artifacts = ArtifactStore(self.conn)
        self.receipts = ReceiptLog(self.conn)
        self.tools = hp.build_html_tools(
            artifacts=self.artifacts, receipts=self.receipts,
            receipt_domain=DOMAIN_ARTIFACT, session_id="s1",
        )
        self.registry = ToolRegistry()
        self.registry.register_all(self.tools)
        (config.MEDIA_DIR / "shot.png").write_bytes(PNG_1PX)

    def tearDown(self) -> None:
        self.conn.close()
        config.DATA_DIR, config.MEDIA_DIR, config.ARTIFACTS_DIR = self._original
        self._tmp.cleanup()

    def call(self, params: dict, *, read_only: bool = False):
        registry = self.registry
        if read_only:
            registry = ToolRegistry(read_only=True)
            registry.register_all(self.tools)
        return asyncio.run(
            registry.execute("html_page", params, breaker=CircuitBreaker(phase=PHASE_ANSWER),
                             session_id="s1")
        )

    def create(self, **extra):
        params = {"title": "周报网页", "html": PAGE}
        params.update(extra)
        return self.call(params)

    # -- create -----------------------------------------------------------
    def test_create_registers_a_previewable_html_artifact(self) -> None:
        result = self.create()
        self.assertTrue(result.ok, result.message)
        data = result.data
        artifact = self.artifacts.get(data["artifact_id"])
        self.assertEqual(artifact.kind, "html")
        self.assertEqual(artifact.media_type, "text/html; charset=utf-8")
        self.assertEqual(artifact.label, "周报网页")
        self.assertEqual(data["preview_url"], "/generated/" + artifact.id)
        self.assertEqual(data["download_url"], "/artifacts/" + artifact.id)
        self.assertTrue(Path(data["file_path"]).exists())
        self.assertGreater(data["receipt_id"], 0)

    def test_create_inlines_media_store_assets(self) -> None:
        """⭐ **素材层**的图片(/assets/<id>)也要内联 —— 错题的照片与图形就在那里。

        D110 实测:模型把句柄写对了(photo_handles / figure_handle),但内联器只认
        /artifacts 与 /materials,于是发到微信的页面里**一张图都没有**。
        """
        from la.domain.media import MediaStore

        media = MediaStore(self.conn, root=config.MEDIA_DIR)
        asset = media.store_bytes(PNG_1PX, media_type="image/png", subdir="problems")
        tools = hp.build_html_tools(
            artifacts=self.artifacts, receipts=self.receipts,
            receipt_domain=DOMAIN_ARTIFACT, session_id="s1", media=media,
        )
        registry = ToolRegistry()
        registry.register_all(tools)
        result = asyncio.run(registry.execute(
            "html_page",
            {"title": "错题页", "html": '<html><body><img src="/assets/'
             + str(asset.id) + '" alt="题图"></body></html>'},
            breaker=CircuitBreaker(phase=PHASE_ANSWER), session_id="s1",
        ))
        self.assertTrue(result.ok, result.message)
        page = self.artifacts.read_bytes(self.artifacts.get(result.data["artifact_id"])).decode("utf-8")
        self.assertIn("data:image/png;base64,", page)
        self.assertNotIn("/assets/" + str(asset.id), page, "句柄必须已经换成字节")

    def test_create_inlines_local_images_and_reports_lint(self) -> None:
        result = self.create(html=PAGE.replace("<p>正文</p>", '<p>正文</p><script>fetch("/x")</script>'))
        self.assertTrue(result.ok, result.message)
        body = Path(result.data["file_path"]).read_text(encoding="utf-8")
        self.assertIn("data:image/png;base64,", body)       # 图片由服务端内联
        self.assertIn("charset=\"utf-8\"", body)            # 文档头补齐
        self.assertTrue(result.data["lint"], "自检应该报出 fetch")
        self.assertIn("自检提示", result.data["note"])

    def test_create_refuses_a_second_page_with_the_same_title(self) -> None:
        """整页重画是**最贵**的那条路(云朵实测 2.9 倍/3.2 倍),拒绝文案必须给出 patch 的路。"""
        first = self.create()
        self.assertTrue(first.ok, first.message)
        again = self.create()
        self.assertFalse(again.ok)
        self.assertIn("不要用 create 重画", again.message)
        self.assertIn("action=source", again.message)
        # ⚠️ 出路必须活下来:registry 会把失败文案截到 200 字符(见 registry.py),
        #    所以拒绝文案里给的是**产物 id**(13 字符),不是那种带时间戳的长文件名。
        self.assertIn(first.data["artifact_id"], again.message)
        self.assertLess(len(again.message), 200 + len("BaseToolError: "))

    def test_create_refuses_an_empty_or_oversized_source(self) -> None:
        self.assertIn("html", self.call({"title": "x"}).message)
        big = "<p>" + "x" * (config.HTML_PAGE_MAX_BYTES + 10) + "</p>"
        self.assertIn("太大", self.call({"title": "x", "html": big}).message)

    def test_create_reports_a_missing_page_layer(self) -> None:
        registry = ToolRegistry()
        registry.register_all(hp.build_html_tools(artifacts=None, session_id="s1"))
        result = asyncio.run(registry.execute(
            "html_page", {"title": "x", "html": "<p>x</p>"},
            breaker=CircuitBreaker(phase=PHASE_ANSWER),
        ))
        self.assertFalse(result.ok)
        self.assertIn("产物层", result.message)

    # -- source / patch ---------------------------------------------------
    def test_source_returns_the_current_source_with_folded_images(self) -> None:
        created = self.create()
        result = self.call({"action": "source", "filename": created.data["name"]})
        self.assertTrue(result.ok, result.message)
        self.assertIn("ELIDED_BASE64_", result.data["source"])   # 内联图片被折叠成记号
        self.assertNotIn("base64,iVBOR", result.data["source"])
        self.assertEqual(result.data["folded_images"], 1)
        self.assertEqual(result.data["name"], created.data["name"])

    def test_patch_changes_only_the_hit_and_keeps_the_old_version(self) -> None:
        created = self.create()
        old_path = Path(created.data["file_path"])
        patched = self.call({
            "action": "patch", "filename": created.data["name"],
            "old_string": '<h1 id="t">周报</h1>',
            "new_string": '<h1 id="t" style="font-size:40px">周报(改)</h1>',
        })
        self.assertTrue(patched.ok, patched.message)
        self.assertEqual(patched.data["version"], 2)
        new_body = Path(patched.data["file_path"]).read_text(encoding="utf-8")
        self.assertIn("font-size:40px", new_body)
        self.assertEqual(new_body.count("data:image/png;base64,"), 1)  # 图片原样还在
        self.assertIn("正文", new_body)
        self.assertTrue(old_path.exists(), "旧版要留着给用户对照回退")
        self.assertNotEqual(created.data["artifact_id"], patched.data["artifact_id"])

    def test_patch_can_target_by_artifact_id_and_paste_a_placeholder(self) -> None:
        created = self.create()
        source = self.call({"action": "source", "filename": created.data["artifact_id"]})
        marker = re.search(r"ELIDED_BASE64_\d+_of_\d+_chars", source.data["source"]).group(0)
        result = self.call({
            "action": "patch", "filename": created.data["artifact_id"],
            "old_string": 'src="data:image/png;base64,' + marker + '"',
            "new_string": 'alt="错题" src="data:image/png;base64,' + marker + '"',
        })
        self.assertTrue(result.ok, result.message)
        self.assertIn('alt="错题"', Path(result.data["file_path"]).read_text(encoding="utf-8"))

    def test_patch_refuses_missing_old_string_and_points_at_source(self) -> None:
        created = self.create()
        result = self.call({
            "action": "patch", "filename": created.data["name"],
            "old_string": "<h1>凭印象写的一段</h1>", "new_string": "x",
        })
        self.assertFalse(result.ok)
        self.assertIn("action=source", result.message)
        self.assertIn("没找到", result.message)

    def test_patch_without_filename_says_it_fell_back(self) -> None:
        created = self.create()
        result = self.call({"action": "patch", "old_string": "<h1>不存在</h1>", "new_string": "x"})
        self.assertFalse(result.ok)
        self.assertIn("兜底", result.message)
        self.assertIn(created.data["name"], result.message)

    def test_patch_requires_old_string(self) -> None:
        self.create()
        result = self.call({"action": "patch", "new_string": "x"})
        self.assertFalse(result.ok)
        self.assertIn("old_string", result.message)

    def test_patch_on_a_missing_page_says_what_exists(self) -> None:
        result = self.call({"action": "patch", "filename": "page_不存在.html",
                            "old_string": "x", "new_string": "y"})
        self.assertFalse(result.ok)
        self.assertIn("没有找到那一页", result.message)

    def test_read_only_mode_blocks_the_whole_tool(self) -> None:
        result = self.call({"title": "x", "html": "<p>x</p>"}, read_only=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "READ_ONLY")


# ==========================================================================
# 3. 注册 / 技能契约(防"实现了却没挂上去")
# ==========================================================================
class TestWiring(unittest.TestCase):
    def test_html_page_is_registered_by_build_base_tools(self) -> None:
        names = [spec.name for spec in build_base_tools()]
        self.assertIn("html_page", names)
        spec = next(spec for spec in build_base_tools() if spec.name == "html_page")
        self.assertTrue(spec.writes, "网页是要落盘的,必须算写工具(只读模式要拦它)")
        self.assertEqual(spec.phase, "any")

    def test_kind_for_html_matches_the_base_layer_constant(self) -> None:
        """基础层不 import 域,所以 kind 是两处各写一遍的字符串 —— 这条测试盯着它们别漂。"""
        self.assertEqual(kind_for("x.html"), KIND_HTML)
        self.assertEqual(kind_for("x.htm"), KIND_HTML)
        self.assertEqual(hp.PAGE_KIND, KIND_HTML)

    def test_skill_file_teaches_the_patch_rule(self) -> None:
        skill = (BASE_DIR / "skills" / "html-page" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("action=patch", skill)
        self.assertIn("source", skill)
        self.assertIn("沙箱", skill)

    def test_skill_library_loads_it(self) -> None:
        from la.skills import SkillLibrary

        library = SkillLibrary(BASE_DIR / "skills")
        self.assertIn("html-page", library.names())
        loaded = library.load("html-page")
        self.assertTrue(loaded.body)
        self.assertIn("html_page", loaded.body)
        self.assertIn("html-page", library.catalog_text())


# ==========================================================================
# 4. 网页路由:沙箱预览
# ==========================================================================
class TestPreviewRoute(unittest.TestCase):
    """⚠️ 路径常量必须**逐个**打补丁:config.ASSISTANT_DB 是 import 期算好的。"""

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
        for directory in (self.tmp, config.MEDIA_DIR, config.ARTIFACTS_DIR, config.LOGS_DIR):
            directory.mkdir(parents=True, exist_ok=True)

        conn = db.connect(config.ASSISTANT_DB)
        db.migrate(conn, MIGRATIONS_ASSISTANT)
        store = ArtifactStore(conn)
        self.page = store.register_text(
            '<!doctype html><html><head><meta charset="utf-8"><title>t</title></head>'
            "<body><h1>网页</h1></body></html>",
            filename="page_演示-v1.html", label="演示", kind=KIND_HTML, session_id="s1",
        )
        self.plain = store.register_text("就是一段文本", filename="清单.md", label="清单")
        log = ReceiptLog(conn)
        log.record(kind="artifact.file", domain="artifact", label="网页:演示",
                   detail={"artifact_id": self.page.id, "name": "演示.html",
                           "preview_url": "/generated/" + self.page.id},
                   undo_plan={"type": "artifact", "artifact_id": self.page.id})
        conn.commit()
        conn.close()

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

    def test_preview_serves_the_page_inside_a_sandbox(self) -> None:
        response = self.client.get("/generated/" + self.page.id)
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        csp = response.headers["content-security-policy"]
        self.assertIn("sandbox allow-scripts", csp)
        self.assertNotIn("allow-same-origin", csp)      # ⭐ 这条一破,页面就能拿到我们的源
        self.assertIn("connect-src 'none'", csp)
        self.assertIn("default-src 'none'", csp)
        self.assertIn("网页", response.text)

    def test_preview_refuses_non_html_artifacts(self) -> None:
        """预览不是第二个下载入口:/generated 只服务 .html/.htm。"""
        self.assertEqual(self.client.get("/generated/" + self.plain.id).status_code, 400)

    def test_preview_of_an_unknown_id_is_404(self) -> None:
        self.assertEqual(self.client.get("/generated/f000000000000").status_code, 404)

    def test_download_still_works_for_the_page(self) -> None:
        response = self.client.get("/artifacts/" + self.page.id)
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["content-disposition"])

    def test_chat_receipt_offers_preview_and_download(self) -> None:
        page = self.client.get("/chat")
        self.assertEqual(page.status_code, 200)
        self.assertIn('class="receipt-preview"', page.text)
        self.assertIn('href="/generated/' + self.page.id + '"', page.text)
        self.assertIn('class="receipt-download"', page.text)

    def test_materials_page_links_the_preview(self) -> None:
        page = self.client.get("/materials")
        self.assertEqual(page.status_code, 200)
        self.assertIn('href="/generated/' + self.page.id + '"', page.text)

    def test_js_uses_the_same_preview_rule(self) -> None:
        """流式那一路是 JS 现拼的卡片 —— 两条路必须同一条规则,否则刷新前后不一致。"""
        script = (BASE_DIR / "la" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("detail.preview_url", script)
        self.assertIn("preview.className = 'receipt-preview'", script)


class _StubClient:
    async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        from la.llm.client import ChatResult

        return ChatResult(text="好的", model="stub")


if __name__ == "__main__":
    unittest.main()
