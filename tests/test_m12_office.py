"""M12 正式文档:Markdown → Word / Excel(P1-4)。

守三件事:
  1. **排版真的落进去了**(中文字体、首行缩进、页眉页脚页码、表格底纹、冻结首行);
  2. **写完回读自检** —— 自检不过就不登记(宁可不给,也不给缺斤少两的文件);
  3. **没装依赖时只降级不崩**,并给一句能照做的安装指引。

运行:
    .venv/Scripts/python.exe -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import asyncio
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from la import config
from la.base.phases import PHASE_ANSWER, CircuitBreaker
from la.base import markdown_lite
from la.base.docx_writer import (
    DocxFacts,
    OfficeWriteError,
    build_docx,
    plan_total,
    verify_docx,
)
from la.base.tools import build_base_tools
from la.base.xlsx_writer import build_xlsx, verify_xlsx
from la.domain.artifacts import ArtifactStore
from la.domain.materials import MaterialService
from la.domain.media import MediaStore
from la.domain.receipts import DOMAIN_ARTIFACT, ReceiptLog
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT
from la.tools.registry import ToolRegistry

PAGEBREAK = chr(92) + "newpage"

SAMPLE = """---
title: 周报:10 月第二周
subtitle: 家庭事务
author: 生活助手
header: 周报 2026-W41
---
# 一、这周花了多少
餐饮 860 元,交通 210 元,**比上周少 12%**。

## 二、明细
| 分类 | 金额 | 笔数 |
|---|---|---|
| 餐饮 | 860 | 12 |
| 交通 | 210 | 5 |

""" + PAGEBREAK + """
# 三、下周计划
- 交物业费
1. 预约体检
"""


class TestMarkdownLite(unittest.TestCase):
    def test_frontmatter_is_parsed_and_stripped(self) -> None:
        document = markdown_lite.parse(SAMPLE)
        self.assertEqual(document.meta["title"], "周报:10 月第二周")
        self.assertEqual(document.meta["author"], "生活助手")
        self.assertEqual(document.title, "周报:10 月第二周")

    def test_blocks(self) -> None:
        document = markdown_lite.parse(SAMPLE)
        kinds = [block.kind for block in document.blocks]
        self.assertIn("heading", kinds)
        self.assertIn("table", kinds)
        self.assertIn("bullet", kinds)
        self.assertIn("number", kinds)
        self.assertIn("pagebreak", kinds)
        self.assertEqual(len(document.tables), 1)
        self.assertEqual(document.tables[0][0], ["分类", "金额", "笔数"])

    def test_title_falls_back_to_first_heading(self) -> None:
        document = markdown_lite.parse("""# 只有标题
正文""")
        self.assertEqual(document.title, "只有标题")

    def test_bold_splitting(self) -> None:
        self.assertEqual(
            markdown_lite.inline_bold("普通 **重点** 尾巴"),
            [("普通 ", False), ("重点", True), (" 尾巴", False)],
        )

    def test_unknown_syntax_is_a_paragraph_not_an_error(self) -> None:
        document = markdown_lite.parse("""```
code fence
```""")
        self.assertTrue(all(block.kind == "paragraph" for block in document.blocks))


class TestDocx(unittest.TestCase):
    def test_build_and_verify(self) -> None:
        data, facts = build_docx(SAMPLE, fallback_title="周报", today="2026 年 10 月 13 日")
        self.assertGreater(len(data), 5000)
        self.assertEqual(facts.title, "周报:10 月第二周")
        self.assertEqual(facts.tables, 1)
        self.assertEqual(verify_docx(data, facts), [])

    def test_formal_layout_is_really_there(self) -> None:
        """排版不能只是"看着像":直接查 OOXML 里的关键设置。"""
        data, _facts = build_docx(SAMPLE, fallback_title="周报", today="2026 年 10 月 13 日")
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            document = archive.read("word/document.xml").decode("utf-8")
            header = archive.read("word/header1.xml").decode("utf-8")
            footer = archive.read("word/footer1.xml").decode("utf-8")
        self.assertIn("word/header1.xml", names)
        self.assertIn("word/footer1.xml", names)
        self.assertIn("周报 2026-W41", header)
        self.assertIn("PAGE", footer)
        # ⚠️ 注意别在 needle 前面多写一个引号:XML 里属性前是空格,不是引号
        # 默认**公文风**:正文仿宋、标题黑体(报告风才用宋体)
        self.assertIn("w:eastAsia=" + chr(34) + "仿宋" + chr(34), document)
        self.assertIn("w:eastAsia=" + chr(34) + "黑体" + chr(34), document)
        self.assertIn("w:eastAsia=" + chr(34) + "楷体" + chr(34), document)   # 二级标题
        self.assertIn("w:firstLineChars=" + chr(34) + "200" + chr(34), document)
        self.assertIn("w:type=" + chr(34) + "page" + chr(34), document)
        self.assertIn("w:fill=" + chr(34) + "D9D9D9" + chr(34), document)   # 表头
        self.assertIn("w:fill=" + chr(34) + "F5F5F5" + chr(34), document)   # 斑马纹/合计行
        self.assertIn("合计", document)

    def test_verify_detects_missing_content(self) -> None:
        data, facts = build_docx(SAMPLE, fallback_title="周报", today="2026 年 10 月 13 日")
        facts.title = "这个名字根本不在文档里"
        self.assertTrue(verify_docx(data, facts))

    def test_broken_bytes_report_instead_of_raising(self) -> None:
        self.assertTrue(verify_docx(b"not a docx at all", DocxFacts(title="x")))

    def test_empty_content_is_refused(self) -> None:
        with self.assertRaises(OfficeWriteError):
            build_docx("  " + chr(10) * 2, fallback_title="空")

    def test_missing_library_degrades_with_hint(self) -> None:
        from la.base import docx_writer

        original = docx_writer.DOCX_AVAILABLE
        docx_writer.DOCX_AVAILABLE = False
        try:
            with self.assertRaises(OfficeWriteError) as ctx:
                build_docx(SAMPLE, fallback_title="周报")
            self.assertIn("pip install python-docx", str(ctx.exception))
        finally:
            docx_writer.DOCX_AVAILABLE = original


class TestTotalRowRules(unittest.TestCase):
    """合计行的规则要**可预测**:求错了比不求和更糟。"""

    def test_numeric_columns_are_summed(self) -> None:
        rows = [["分类", "金额", "笔数"], ["餐饮", "860", "12"], ["交通", "210", "5"]]
        self.assertEqual(plan_total(rows), [1, 2])

    def test_per_unit_and_date_columns_are_skipped(self) -> None:
        rows = [["路线", "人均", "日期"], ["A", "35", "2026-10-01"], ["B", "45", "2026-10-02"]]
        self.assertEqual(plan_total(rows), [])

    def test_needs_at_least_two_data_rows(self) -> None:
        self.assertEqual(plan_total([["分类", "金额"], ["餐饮", "860"]]), [])

    def test_mixed_column_is_skipped(self) -> None:
        rows = [["项目", "值"], ["a", "10"], ["b", "很多"]]
        self.assertEqual(plan_total(rows), [])

    def test_total_row_values(self) -> None:
        from la.base.docx_writer import _format_number

        self.assertEqual(_format_number(1070.0), "1070")
        self.assertEqual(_format_number(12.5), "12.5")


class TestDocxTotalAndZebra(unittest.TestCase):
    def test_total_row_is_added_with_label(self) -> None:
        from docx import Document as DocxDocument

        data, _facts = build_docx(SAMPLE, fallback_title="周报", today="2026 年 10 月 13 日")
        table = DocxDocument(io.BytesIO(data)).tables[0]
        last = [cell.text.strip() for cell in table.rows[-1].cells]
        self.assertEqual(last[0], "合计")
        self.assertEqual(last[1], "1070")     # 860 + 210
        self.assertEqual(last[2], "17")       # 12 + 5

    def test_total_can_be_turned_off_from_frontmatter(self) -> None:
        from docx import Document as DocxDocument

        text = SAMPLE.replace("---\n# 一、这周花了多少", "total: off\n---\n# 一、这周花了多少")
        data, _facts = build_docx(text, fallback_title="周报", today="2026 年 10 月 13 日")
        table = DocxDocument(io.BytesIO(data)).tables[0]
        self.assertNotEqual(table.rows[-1].cells[0].text.strip(), "合计")

    def test_report_style_switches_fonts_back(self) -> None:
        # ⚠️ 替换串里必须把开头那个 --- 补回来,否则 frontmatter 的起始围栏就没了
        text = SAMPLE.replace("---\ntitle:", "---\nstyle: 报告\ntitle:")
        data, _facts = build_docx(text, fallback_title="周报", today="2026 年 10 月 13 日")
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            document = archive.read("word/document.xml").decode("utf-8")
        self.assertIn("w:eastAsia=" + chr(34) + "宋体" + chr(34), document)
        self.assertNotIn("w:eastAsia=" + chr(34) + "仿宋" + chr(34), document)


class TestXlsx(unittest.TestCase):
    def test_tables_become_sheets(self) -> None:
        data, facts = build_xlsx(SAMPLE, fallback_name="周报")
        self.assertEqual(facts.sheets, ["二、明细"])
        self.assertEqual(verify_xlsx(data, facts), [])

    def test_numbers_are_numbers_and_header_is_frozen(self) -> None:
        from openpyxl import load_workbook

        data, facts = build_xlsx(SAMPLE, fallback_name="周报")
        sheet = load_workbook(io.BytesIO(data))[facts.sheets[0]]
        self.assertTrue(sheet.cell(row=1, column=1).font.bold)
        self.assertEqual(sheet.freeze_panes, "A2")
        self.assertEqual(sheet.cell(row=2, column=2).value, 860)
        self.assertIsInstance(sheet.cell(row=2, column=2).value, int)

    def test_total_row_uses_a_formula(self) -> None:
        from openpyxl import load_workbook

        data, facts = build_xlsx(SAMPLE, fallback_name="周报")
        self.assertTrue(facts.has_total)
        sheet = load_workbook(io.BytesIO(data))[facts.sheets[0]]
        self.assertEqual(sheet.cell(row=4, column=1).value, "合计")
        self.assertEqual(sheet.cell(row=4, column=2).value, "=SUM(B2:B3)")
        self.assertEqual(verify_xlsx(data, facts), [])

    def test_data_rows_have_zebra_fill(self) -> None:
        from openpyxl import load_workbook

        data, facts = build_xlsx(SAMPLE, fallback_name="周报")
        sheet = load_workbook(io.BytesIO(data))[facts.sheets[0]]
        self.assertTrue(sheet.cell(row=2, column=1).fill.fgColor.rgb.endswith("F5F5F5"))

    def test_without_tables_everything_goes_into_one_column(self) -> None:
        data, facts = build_xlsx("""# 标题
只有正文,没有表格""", fallback_name="备注")
        self.assertEqual(len(facts.sheets), 1)
        self.assertEqual(verify_xlsx(data, facts), [])

    def test_verify_detects_wrong_sheet_name(self) -> None:
        data, facts = build_xlsx(SAMPLE, fallback_name="周报")
        facts.sheets = ["不存在的表"]
        self.assertTrue(verify_xlsx(data, facts))

    def test_missing_library_degrades_with_hint(self) -> None:
        from la.base import xlsx_writer

        original = xlsx_writer.XLSX_AVAILABLE
        xlsx_writer.XLSX_AVAILABLE = False
        try:
            with self.assertRaises(OfficeWriteError) as ctx:
                build_xlsx(SAMPLE, fallback_name="周报")
            self.assertIn("pip install openpyxl", str(ctx.exception))
        finally:
            xlsx_writer.XLSX_AVAILABLE = original


class TestArtifactWriteOffice(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._patched = {key: getattr(config, key) for key in ("DATA_DIR", "ARTIFACTS_DIR")}
        config.DATA_DIR = self.root
        config.ARTIFACTS_DIR = self.root / "artifacts"
        self.conn = db.connect(self.root / "assistant.db")
        db.migrate(self.conn, MIGRATIONS_ASSISTANT)
        self.artifacts = ArtifactStore(self.conn, root=config.ARTIFACTS_DIR)
        self.receipts = ReceiptLog(self.conn)
        self.materials = MaterialService(self.conn, media=MediaStore(self.conn, root=self.root / "media"))
        self.registry = ToolRegistry()
        self.registry.register_all(build_base_tools(
            materials=self.materials, artifacts=self.artifacts, receipts=self.receipts,
            receipt_domain=DOMAIN_ARTIFACT, session_id="office-test",
        ))

    def tearDown(self) -> None:
        self.conn.close()
        for key, value in self._patched.items():
            setattr(config, key, value)
        self._tmp.cleanup()

    def call(self, params: dict):
        return asyncio.run(self.registry.execute(
            "artifact_write", params, breaker=CircuitBreaker(phase=PHASE_ANSWER),
        ))

    def test_write_docx(self) -> None:
        result = self.call({"name": "周报.docx", "content": SAMPLE})
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.data["name"], "周报.docx")
        payload = self.artifacts.read_bytes(result.data["artifact_id"])
        self.assertTrue(payload.startswith(b"PK"))
        self.assertEqual(verify_docx(payload, build_docx(SAMPLE, fallback_title="周报")[1]), [])
        self.assertIn("正式文档", result.data["note"])

    def test_write_xlsx(self) -> None:
        result = self.call({"name": "台账.xlsx", "content": SAMPLE})
        self.assertTrue(result.ok, result.message)
        payload = self.artifacts.read_bytes(result.data["artifact_id"])
        self.assertTrue(payload.startswith(b"PK"))
        self.assertIn("工作表", result.data["note"])

    def test_office_files_get_downloadable_media_types(self) -> None:
        from la.domain.artifacts import media_type_for

        self.assertIn("wordprocessingml", media_type_for("a.docx"))
        self.assertIn("spreadsheetml", media_type_for("a.xlsx"))

    def test_unknown_extension_still_refused(self) -> None:
        result = self.call({"name": "a.exe", "content": "x"})
        self.assertFalse(result.ok)
        self.assertIn("只能写", result.message)

    def test_receipt_is_recorded_for_office_too(self) -> None:
        self.call({"name": "周报.docx", "content": SAMPLE})
        receipts = self.receipts.recent(limit=3)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0].domain, DOMAIN_ARTIFACT)


if __name__ == "__main__":
    unittest.main()
