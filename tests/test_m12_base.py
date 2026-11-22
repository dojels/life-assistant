"""M12 验收测试:基础能力层(读 / 看 / 找 / 写)。

三层都测:
  * **纯函数**(sniff / extract / read_window / list_directory / check_image)—— 不碰数据库;
  * **工具合同**(build_base_tools 生成的五个工具)—— 经 ToolRegistry 走完整管线;
  * **跨层**(artifact_write 的回执 + 撤销)—— 与 M7 的撤销机制接在一起。

运行:
    .venv/Scripts/python.exe -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import asyncio
import io
import os
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

from la import config
from la.base.phases import PHASE_ANSWER, CircuitBreaker
from la.base import extract, files, images, sniff
from la.base.files import BaseToolError
from la.base.tools import build_base_tools
from la.domain.artifacts import ArtifactError, ArtifactStore
from la.domain.materials import MaterialService
from la.domain.media import MediaStore
from la.domain.receipts import DOMAIN_ARTIFACT, ReceiptLog
from la.domain.reverters import build_reverter
from la.llm.client import ChatResult
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT
from la.tools.registry import ToolRegistry

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)

#: 一张 3×5 的 GIF(只有头部,尺寸解析用它)
GIF_3X5 = b"GIF89a" + struct.pack("<HH", 3, 5) + b"\x00" * 32

#: 一张 30×20 的 JPEG(APP0 + SOF0)
JPEG_30X20 = (
    b"\xff\xd8"
    + b"\xff\xe0" + struct.pack(">H", 4) + b"\x00\x00"
    + b"\xff\xc0" + struct.pack(">H", 11) + b"\x08" + struct.pack(">HH", 20, 30)
    + b"\x01\x01\x11\x00"
)

#: 一张 800×600 的 WebP(VP8X 容器头)
WEBP_800X600 = (
    b"RIFF" + struct.pack("<I", 30) + b"WEBP"
    + b"VP8X" + struct.pack("<I", 10) + b"\x00\x00\x00\x00"
    + (800 - 1).to_bytes(3, "little") + (600 - 1).to_bytes(3, "little")
)


def make_xlsx(rows: list[list[str]]) -> bytes:
    """造一份**最小可读**的 xlsx(标准库就能造,不需要 openpyxl)。"""
    shared: list[str] = []
    index_of: dict[str, int] = {}
    cells_xml: list[str] = []
    for row_index, row in enumerate(rows, 1):
        parts: list[str] = []
        for column, value in enumerate(row):
            ref = chr(ord("A") + column) + str(row_index)
            if value not in index_of:
                index_of[value] = len(shared)
                shared.append(value)
            parts.append('<c r="' + ref + '" t="s"><v>' + str(index_of[value]) + "</v></c>")
        cells_xml.append('<row r="' + str(row_index) + '">' + "".join(parts) + "</row>")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "xl/sharedStrings.xml",
            '<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            + "".join("<si><t>" + value + "</t></si>" for value in shared) + "</sst>",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<sheetData>" + "".join(cells_xml) + "</sheetData></worksheet>",
        )
    return buffer.getvalue()


def make_minimal_docx() -> bytes:
    """手搓一个最小 docx 包(python-docx 能解析的那种)。

    ⚠️ 刻意**不用 python-docx 生成** —— 那会让"读 docx"的测试依赖写 docx 的库,
    测出来的东西就不是同一回事了。
    """
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        "<w:p><w:r><w:t>周报标题</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>本周记了 3 笔账</w:t></w:r></w:p>"
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>周一</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>12.5</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        "</w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


class FakeVision:
    """假视觉模型:只回一句话,并记下它收到的消息。"""

    def __init__(self, reply: str = "这是一张收据,金额 12.5 元。") -> None:
        self.reply = reply
        self.calls = 0
        self.last_messages: list[dict] = []
        self.model = "deepseek-flash"

    async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        self.last_messages = messages
        return ChatResult(text=self.reply, model=self.model)


# ==========================================================================
# 1. 类型识别
# ==========================================================================
class TestSniff(unittest.TestCase):
    def test_magic_beats_suffix(self) -> None:
        self.assertEqual(sniff.sniff_media_type(PNG_1PX), "image/png")
        self.assertEqual(sniff.sniff_media_type(GIF_3X5), "image/gif")
        self.assertEqual(sniff.sniff_media_type(b"%PDF-1.7\n"), "application/pdf")
        self.assertEqual(sniff.sniff_media_type(b"\x00\x01"), "application/octet-stream")

    def test_office_flavor_reads_zip_layout(self) -> None:
        self.assertEqual(sniff.office_flavor(make_minimal_docx()), "docx")
        self.assertEqual(sniff.office_flavor(make_xlsx([["a"]])), "xlsx")
        self.assertIsNone(sniff.office_flavor(b"PK\x03\x04" + b"0" * 40))

    def test_detect_kind(self) -> None:
        self.assertEqual(sniff.detect_kind(make_minimal_docx()), "docx")
        self.assertEqual(sniff.detect_kind(make_xlsx([["a"]])), "xlsx")
        self.assertEqual(sniff.detect_kind("中文笔记".encode("utf-8")), "text")
        self.assertEqual(sniff.detect_kind(b"\x00\x01\x02\xff"), "binary")
        self.assertEqual(sniff.detect_kind(PNG_1PX), "image")

    def test_voice_containers_are_recognised(self) -> None:
        """微信语音是 AMR / SILK,**不是** MP3 —— 认不出来会被当成"二进制文件"(D139)。"""
        amr = b"#!AMR\n" + b"\x3c" * 32
        silk = b"\x02#!SILK_V3" + b"\x00" * 32
        self.assertEqual(sniff.sniff_media_type(amr), "audio/amr")
        self.assertEqual(sniff.sniff_media_type(silk), "audio/silk")
        self.assertEqual(sniff.detect_kind(amr), "audio")
        self.assertEqual(sniff.detect_kind(silk), "audio")

    def test_kind_label_is_human_readable(self) -> None:
        self.assertEqual(sniff.kind_label("docx"), "Word 文档")
        self.assertEqual(sniff.kind_label("unknown-thing"), "unknown-thing")


# ==========================================================================
# 2. 抽取层
# ==========================================================================
class TestExtract(unittest.TestCase):
    def test_plain_text_and_encoding(self) -> None:
        self.assertEqual(extract.extract_text("你好\n第二行".encode("utf-8")).text, "你好\n第二行")
        self.assertEqual(extract.extract_text("中文内容".encode("gbk")).text, "中文内容")

    def test_html_drops_script_and_style(self) -> None:
        html = "<html><head><style>p{color:red}</style></head><body><p>你好</p><script>alert(1)</script><div>第二段</div></body></html>"
        text = extract.extract_text(html.encode("utf-8"), name="a.html").text
        self.assertIn("你好", text)
        self.assertIn("第二段", text)
        self.assertNotIn("alert", text)
        self.assertNotIn("color:red", text)

    def test_csv_becomes_summary_table(self) -> None:
        rows = [["日期", "金额"]] + [[f"2026-10-{day:02d}", str(day)] for day in range(1, 26)]
        import csv as csv_module

        buffer = io.StringIO()
        csv_module.writer(buffer).writerows(rows)
        result = extract.extract_text(buffer.getvalue().encode("utf-8"), name="账.csv")
        self.assertTrue(result.ok)
        self.assertIn("26 行 × 2 列", result.text)
        self.assertIn("表头:日期 | 金额", result.text)
        self.assertIn("还有 6 行没显示", result.text)

    def test_xlsx_via_stdlib(self) -> None:
        data = make_xlsx([["日期", "金额"], ["2026-10-01", "12.5"], ["2026-10-02", "8"]])
        result = extract.extract_text(data, name="账单.xlsx")
        self.assertTrue(result.ok)
        self.assertIn("3 行 × 2 列", result.text)
        self.assertIn("2026-10-01 | 12.5", result.text)

    def test_xlsx_rows_reader_accepts_path_too(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "a.xlsx"
            path.write_bytes(make_xlsx([["x"], ["y"]]))
            self.assertEqual(extract.read_xlsx_rows(path)[1], ["y"])

    def test_docx_round_trip(self) -> None:
        result = extract.extract_text(make_minimal_docx(), name="周报.docx")
        self.assertTrue(result.ok, result.note)
        self.assertIn("周报标题", result.text)
        self.assertIn("本周记了 3 笔账", result.text)
        self.assertIn("周一 | 12.5", result.text)

    def test_image_and_binary_never_throw(self) -> None:
        image = extract.extract_text(PNG_1PX, name="a.png")
        self.assertFalse(image.ok)
        self.assertIn("image_view", image.note)
        blob = extract.extract_text(b"\x00\x01\x02\xff", name="a.bin")
        self.assertFalse(blob.ok)
        self.assertIn("读不出文本", blob.note)

    def test_audio_says_it_cannot_read_words(self) -> None:
        """语音读不出文字 —— 要**明说是音频**,别让它掉进"二进制文件"那句(D139)。"""
        amr = b"#!AMR\n" + b"\x3c" * 32
        result = extract.extract_text(amr, name="语音.amr")
        self.assertFalse(result.ok)
        self.assertEqual(result.kind, "audio")
        self.assertIn("音频", result.note)
        self.assertIn("转写", result.note)

    def test_broken_docx_reports_instead_of_raising(self) -> None:
        broken = io.BytesIO()
        with zipfile.ZipFile(broken, "w") as archive:
            archive.writestr("word/document.xml", "not xml at all <<<")
        result = extract.extract_text(broken.getvalue(), name="坏.docx")
        self.assertFalse(result.ok)
        self.assertTrue(result.note)

    def test_truncation_is_disclosed(self) -> None:
        result = extract.extract_text(("长" * 100 + "\n") .encode("utf-8") * 50, name="a.txt", limit=100)
        self.assertTrue(result.ok)
        self.assertIn("已截断", result.note)


# ==========================================================================
# 3. 读 / 列目录
# ==========================================================================
class TestFiles(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._original_data = config.DATA_DIR
        config.DATA_DIR = self.root

    def tearDown(self) -> None:
        config.DATA_DIR = self._original_data
        self._tmp.cleanup()

    def _write(self, name: str, text: str, encoding: str = "utf-8") -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode(encoding))
        return path

    def test_window_has_line_numbers_and_footer(self) -> None:
        path = self._write("note.txt", "\n".join(str(i) for i in range(1, 11)))
        text = files.render_read(files.read_text_window(path, offset=1, limit=3))
        self.assertIn("1| 1", text)
        self.assertIn("3| 3", text)
        self.assertIn("Showing lines 1-3 of 10. Use offset=4 to continue.", text)

    def test_end_of_file_footer(self) -> None:
        path = self._write("note.txt", "a\nb")
        text = files.render_read(files.read_text_window(path))
        self.assertIn("(End of file - total 2 lines)", text)

    def test_offset_past_end_is_an_error(self) -> None:
        path = self._write("note.txt", "a\nb")
        with self.assertRaises(BaseToolError) as ctx:
            files.read_text_window(path, offset=99)
        self.assertIn("超过文件总行数", str(ctx.exception))

    def test_long_line_is_truncated_and_flagged(self) -> None:
        path = self._write("long.txt", "x" * 3000)
        outcome = files.read_text_window(path, max_line_chars=100)
        self.assertTrue(outcome.truncated_line)
        self.assertIn("line truncated to 100 chars", files.render_read(outcome))

    def test_byte_cap_stops_early(self) -> None:
        path = self._write("big.txt", "\n".join("y" * 50 for _ in range(100)))
        outcome = files.read_text_window(path, max_bytes=200)
        self.assertTrue(outcome.truncated_bytes)
        self.assertIn("字节上限", files.render_read(outcome))

    def test_oversized_file_is_refused(self) -> None:
        path = self._write("huge.txt", "z")
        original = config.READ_MAX_FILE_BYTES
        try:
            config.READ_MAX_FILE_BYTES = 0
            with self.assertRaises(BaseToolError) as ctx:
                files.read_text_window(path)
            self.assertIn("文件太大", str(ctx.exception))
        finally:
            config.READ_MAX_FILE_BYTES = original

    def test_gbk_is_decoded_and_disclosed(self) -> None:
        path = self._write("gbk.txt", "中文一行", encoding="gbk")
        text = files.render_read(files.read_text_window(path))
        self.assertIn("中文一行", text)
        self.assertIn("编码 gbk", text)

    def test_listing_hides_dotfiles_and_symlinks(self) -> None:
        self._write("a.txt", "a")
        self._write(".env", "SECRET=1")
        (self.root / "sub").mkdir()
        rows, total, hidden = files.list_directory(self.root)
        self.assertIn("sub/", rows)
        self.assertIn("a.txt  1B", rows)
        self.assertEqual(total, 2)
        self.assertFalse(any(".env" in row for row in rows))

    def test_listing_limit_reports_hidden_count(self) -> None:
        for index in range(5):
            self._write(f"f{index}.txt", "x")
        rows, total, hidden = files.list_directory(self.root, limit=2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(total, 5)
        self.assertEqual(hidden, 3)

    def test_recursive_listing_uses_prefix(self) -> None:
        self._write("sub/inner.txt", "x")
        rows, _total, _hidden = files.list_directory(self.root, recursive=True)
        self.assertIn("sub/inner.txt  1B", rows)


# ==========================================================================
# 4. 图片校验
# ==========================================================================
class TestImages(unittest.TestCase):
    def test_dimensions_from_headers(self) -> None:
        self.assertEqual(images.image_dimensions(PNG_1PX, "image/png"), (1, 1))
        self.assertEqual(images.image_dimensions(GIF_3X5, "image/gif"), (3, 5))
        self.assertEqual(images.image_dimensions(JPEG_30X20, "image/jpeg"), (30, 20))
        self.assertEqual(images.image_dimensions(WEBP_800X600, "image/webp"), (800, 600))

    def test_ok_image_passes(self) -> None:
        media_type, refusal = images.check_image(PNG_1PX, name="a.png")
        self.assertEqual(media_type, "image/png")
        self.assertEqual(refusal, "")

    def test_non_image_is_refused(self) -> None:
        _media, refusal = images.check_image(b"%PDF-1.7", name="a.png")
        self.assertIn("不是", refusal)

    def test_too_many_bytes_is_refused_with_numbers(self) -> None:
        original = config.IMAGE_MAX_BYTES
        try:
            config.IMAGE_MAX_BYTES = 4
            _media, refusal = images.check_image(PNG_1PX)
            self.assertIn("图太大", refusal)
        finally:
            config.IMAGE_MAX_BYTES = original

    def test_too_large_dimensions_refused(self) -> None:
        original = config.IMAGE_MAX_DIMENSION
        try:
            config.IMAGE_MAX_DIMENSION = 100
            _media, refusal = images.check_image(WEBP_800X600)
            self.assertIn("单边上限", refusal)
        finally:
            config.IMAGE_MAX_DIMENSION = original

    def test_unparsable_header_is_not_a_refusal(self) -> None:
        _media, refusal = images.check_image(b"\x89PNG\r\n\x1a\n" + b"0" * 10)
        self.assertEqual(refusal, "")


# ==========================================================================
#: ⭐ 批次 A(2026-10-15):file = file_read+file_list,web = web_search+web_fetch。
#: 这里保留旧名当**别名**,只是为了让"测试要断的是能力,不是名字"这句话成立 ——
#: 新写的测试请直接用新名 + action。
_ALIASES: dict[str, tuple[str, dict]] = {
    "file_read": ("file", {"action": "read"}),
    "file_list": ("file", {"action": "list"}),
    "web_search": ("web", {"action": "search"}),
    "web_fetch": ("web", {"action": "fetch"}),
}


def _modern(name: str, params: dict) -> tuple[str, dict]:
    target, forced = _ALIASES.get(name, (name, {}))
    merged = dict(forced)
    merged.update(params or {})
    return target, merged


# 5. 工具合同(经 ToolRegistry 走完整管线)
# ==========================================================================
class TestBaseTools(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._original = (config.DATA_DIR, config.ARTIFACTS_DIR)
        config.DATA_DIR = self.root
        config.ARTIFACTS_DIR = self.root / "artifacts"
        self.conn = db.connect(self.root / "assistant.db")
        db.migrate(self.conn, MIGRATIONS_ASSISTANT)
        self.artifacts = ArtifactStore(self.conn, root=config.ARTIFACTS_DIR)
        self.receipts = ReceiptLog(self.conn)
        self.media = MediaStore(self.conn, root=self.root / "media")
        self.materials = MaterialService(self.conn, media=self.media)
        self.vision = FakeVision()
        self.tools = build_base_tools(
            materials=self.materials,
            artifacts=self.artifacts,
            receipts=self.receipts,
            receipt_domain=DOMAIN_ARTIFACT,
            vision_factory=lambda: self.vision,
            vision_model="deepseek-flash",
            session_id="test-session",
        )
        self.registry = ToolRegistry()
        self.registry.register_all(self.tools)

    def tearDown(self) -> None:
        self.conn.close()
        config.DATA_DIR, config.ARTIFACTS_DIR = self._original
        self._tmp.cleanup()

    def call(self, name: str, params: dict, *, read_only: bool = False):
        registry = ToolRegistry(read_only=True) if read_only else self.registry
        if read_only:
            registry.register_all(self.tools)
        breaker = CircuitBreaker(phase=PHASE_ANSWER)
        return asyncio.run(
            registry.execute(*_modern(name, params), breaker=breaker, session_id="test-session")
        )

    def _write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_file_read_returns_numbered_window(self) -> None:
        self._write("note.txt", "第一行\n第二行")
        result = self.call("file", {"path": str(self.root / "note.txt")})
        self.assertTrue(result.ok, result.message)
        self.assertIn("1| 第一行", result.data)
        self.assertIn("End of file - total 2 lines", result.data)

    def test_file_read_on_directory_lists_it(self) -> None:
        self._write("docs/a.md", "x")
        result = self.call("file", {"path": str(self.root / "docs")})
        self.assertTrue(result.ok)
        self.assertIn("a.md", result.data)

    def test_file_read_refuses_outside_whitelist(self) -> None:
        # 路径要按平台挑:Linux 上 "C:/..." 只是一个**相对**文件名,会先撞"文件不存在",
        # 于是这条测的就不是白名单了(2026-10-25 云端实测:失败原因正是这个)。
        outside = (
            "C:/Windows/System32/drivers/etc/hosts" if os.name == "nt" else "/etc/hosts"
        )
        result = self.call("file", {"path": outside})
        self.assertFalse(result.ok)
        self.assertIn("白名单", result.message)

    def test_file_read_refuses_secret_files(self) -> None:
        (config.BASE_DIR / ".env").exists()  # 真实项目里就有
        result = self.call("file", {"path": str(config.BASE_DIR / ".env")})
        self.assertFalse(result.ok)
        self.assertIn("不允许", result.message)

    def test_file_read_extracts_docx(self) -> None:
        path = self.root / "周报.docx"
        path.write_bytes(make_minimal_docx())
        result = self.call("file", {"path": str(path)})
        self.assertTrue(result.ok, result.message)
        self.assertIn("周报标题", result.data)
        self.assertIn(config.EXTERNAL_CONTENT_NOTICE, result.data)

    def test_file_read_image_points_at_image_view(self) -> None:
        path = self.root / "a.png"
        path.write_bytes(PNG_1PX)
        result = self.call("file", {"path": str(path)})
        self.assertTrue(result.ok)
        self.assertIn("image_view", result.data)

    def test_file_list_shows_project_root_by_default(self) -> None:
        result = self.call("file", {"path": str(self.root), "action": "list"})
        self.assertTrue(result.ok)
        self.assertIn(str(self.root), result.data)

    def test_image_view_uses_vision_model(self) -> None:
        path = self.root / "receipt.png"
        path.write_bytes(PNG_1PX)
        result = self.call("image_view", {"path": str(path), "question": "多少钱?"})
        self.assertTrue(result.ok, result.message)
        self.assertIn("收据", result.data)
        self.assertEqual(self.vision.calls, 1)
        content = self.vision.last_messages[-1]["content"]
        self.assertEqual(self.vision.last_messages[-1]["role"], "user")
        self.assertTrue(any(block.get("type") == "image_url" for block in content))
        self.assertTrue(any("多少钱?" in block.get("text", "") for block in content))

    def test_image_view_rejects_non_image(self) -> None:
        path = self.root / "a.txt"
        path.write_text("hello", encoding="utf-8")
        result = self.call("image_view", {"path": str(path)})
        self.assertFalse(result.ok)
        self.assertIn("不是图片", result.message)

    def test_image_view_without_vision_says_so(self) -> None:
        registry = ToolRegistry()
        registry.register_all(build_base_tools(
            materials=self.materials, artifacts=self.artifacts, receipts=self.receipts,
            vision_factory=None, vision_model="deepseek-flash",
        ))
        path = self.root / "a.png"
        path.write_bytes(PNG_1PX)
        result = asyncio.run(registry.execute(
            "image_view", {"path": str(path)}, breaker=CircuitBreaker(phase=PHASE_ANSWER),
        ))
        self.assertFalse(result.ok)
        self.assertIn("没有可用的视觉模型", result.message)

    def test_image_view_gate_refuses_undeclared_model(self) -> None:
        registry = ToolRegistry()
        registry.register_all(build_base_tools(
            materials=self.materials, artifacts=self.artifacts, receipts=self.receipts,
            vision_factory=lambda: self.vision, vision_model="some-text-only-model",
            session_id="t",
        ))
        path = self.root / "a.png"
        path.write_bytes(PNG_1PX)
        result = asyncio.run(registry.execute(
            "image_view", {"path": str(path)}, breaker=CircuitBreaker(phase=PHASE_ANSWER),
        ))
        self.assertFalse(result.ok)
        self.assertIn("没有声明支持图片输入", result.message)
        self.assertEqual(self.vision.calls, 0)

    def test_material_list_and_read(self) -> None:
        self.materials.ingest_bytes(
            "报销单\n金额 88 元".encode("utf-8"), channel="weixin", external_id="m1",
            original_name="报销.txt", kind="text",
        )
        listed = self.call("material_list", {})
        self.assertTrue(listed.ok)
        self.assertIn("报销.txt", listed.data)
        read = self.call("file", {"material_id": 1})
        self.assertTrue(read.ok, read.message)
        self.assertIn("金额 88 元", read.data)

    def test_material_image_goes_through_image_view(self) -> None:
        self.materials.ingest_bytes(
            PNG_1PX, channel="weixin", external_id="m2", original_name="图.png", kind="image",
        )
        result = self.call("image_view", {"material_id": 1})
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.vision.calls, 1)

    def test_material_has_no_write_path(self) -> None:
        """⭐ 素材只读:工具的签名里就没有任何改素材的参数。"""
        names = {spec.name for spec in self.tools}
        self.assertNotIn("material_write", names)
        self.assertNotIn("material_delete", names)
        for spec in self.tools:
            if spec.name.startswith("material") or spec.name in ("file", "image_view"):
                properties = spec.parameters_json_schema().get("properties", {})
                self.assertFalse({"delete", "write", "content"} & set(properties))

    def test_artifact_write_registers_and_records_receipt(self) -> None:
        result = self.call("artifact_write", {"name": "购物清单.md", "content": "# 清单\n- 牛奶"})
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.data["name"], "购物清单.md")
        self.assertTrue(self.artifacts.exists(result.data["artifact_id"]))
        receipts = self.receipts.recent(limit=5)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0].kind, "artifact.file")
        self.assertEqual(receipts[0].domain, DOMAIN_ARTIFACT)

    def test_artifact_write_rejects_unknown_extension(self) -> None:
        result = self.call("artifact_write", {"name": "a.exe", "content": "x"})
        self.assertFalse(result.ok)
        self.assertIn("只能写", result.message)

    def test_artifact_write_rejects_oversized_content(self) -> None:
        original = config.FILE_WRITE_MAX_BYTES
        try:
            config.FILE_WRITE_MAX_BYTES = 10
            result = self.call("artifact_write", {"name": "a.md", "content": "x" * 100})
            self.assertFalse(result.ok)
            self.assertIn("内容太大", result.message)
        finally:
            config.FILE_WRITE_MAX_BYTES = original

    def test_artifact_write_is_blocked_in_read_only_mode(self) -> None:
        result = self.call("artifact_write", {"name": "a.md", "content": "x"}, read_only=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "READ_ONLY")

    def test_artifact_undo_deletes_file_and_registration(self) -> None:
        written = self.call("artifact_write", {"name": "临时.md", "content": "x"})
        artifact_id = written.data["artifact_id"]
        receipt_id = written.data["receipt_id"]
        reverter = build_reverter(self.receipts, artifacts=self.artifacts)
        outcome = reverter.undo(receipt_id, actor="user")
        self.assertIn("已删除", str(outcome))
        self.assertFalse(self.artifacts.exists(artifact_id))
        # 登记也没了:get 对不存在的产物**抛错**而不是返回 None(下载入口靠这条判 404)
        with self.assertRaises(ArtifactError):
            self.artifacts.get(artifact_id)


if __name__ == "__main__":
    unittest.main()
