"""文件 → 可读文本 —— 基础层的抽取层(M12)。

形状照抄云朵的 app/agent/doc_text.py(那条路已经在真机上跑过),三条纪律逐条照搬:
  1. **截断必须中性告知**:"原文共 N 字,上面是前 8000 字" —— 不静默丢内容;
  2. **只读不写、绝不抛异常**:失败返回空串 + 原因(文件可能正被 Word/Excel 占用);
  3. **重库延迟导入**:python-docx / python-pptx / pypdf 都在函数里 import,
     没装时给一句"请 pip install …",整个应用照常跑(与 D82 引 pycryptodome 同一条纪律)。

格式矩阵(读):
  txt/md/csv/tsv/json/log/xml/yaml  → 直读(utf-8 → utf-8-sig → gbk)
  html/htm                          → 去 script/style、块级标签当换行、剥标签
  csv/tsv                           → 解析成表,给"形状 + 表头 + 前 N 行"
  xlsx/xlsm                         → **标准库** zipfile + ElementTree(零依赖)
  docx                              → python-docx:段落 + 表格
  pptx                              → python-pptx:逐页标题/正文/表格/备注
  pdf                               → pypdf 逐页;**先探测文字层**,扫描件如实说读不出
  图片                              → **不抽取**,提示改用 image_view

⚠️ xlsx 的读取**只有一份实现**(在本文件):账单导入 la/domain/importers/statement.py
   也调这里,而不是各写一遍。
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Any, Callable
from xml.etree import ElementTree

from .. import config
from .sniff import detect_kind, kind_label

_HTML_HINT = re.compile(r"<\s*(html|body|div|p|table|head)\b", re.I)
_HTML_DROP = re.compile(r"<(script|style|noscript|template)\b[^>]*>.*?</\1\s*>", re.S | re.I)
_HTML_BLOCK = re.compile(r"</?(p|div|br|li|tr|td|th|h[1-6]|section|article|header|footer|table)\b[^>]*>", re.I)
_HTML_TAG = re.compile(r"<[^>]+>")
_HTML_BLANK = re.compile(r"\n{3,}")

TEXT_SUFFIXES = (".txt", ".md", ".json", ".log", ".xml", ".yaml", ".yml", ".ics",
                 ".py", ".js", ".css", ".ini", ".toml", ".cfg")
TABLE_SUFFIXES = (".csv", ".tsv")

#: 需要哪个包才能读哪种格式(错误信息里直接用)
LIBRARIES = {"docx": ("docx", "python-docx"), "pptx": ("pptx", "python-pptx"), "pdf": ("pypdf", "pypdf")}


@dataclass(frozen=True)
class Extracted:
    """抽取结果。text 为空时,note 必须说清**为什么**读不出来。"""

    kind: str
    text: str = ""
    note: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.text)


class MissingLibrary(RuntimeError):
    """缺一个可选依赖。消息里直接给安装指引。"""


def clip_text(text: str, limit: int) -> tuple[str, bool, int]:
    """(截断后的文本, 是否截断, 原文长度)。"""
    raw = str(text or "").strip()
    if limit and len(raw) > limit:
        return raw[:limit], True, len(raw)
    return raw, False, len(raw)


def cut_note(cut: bool, total: int, limit: int) -> str:
    if not cut:
        return ""
    return "(已截断:原文共 " + str(total) + " 字,上面只给了前 " + str(limit) + " 字)"


def html_to_text(html: str) -> str:
    """网页 → 纯文本。⚠️ 先整块丢掉 script/style,再剥标签;顺序反了会把 JS 当正文。"""
    text = _HTML_DROP.sub(" ", html or "")
    text = _HTML_BLOCK.sub("\n", text)
    text = _HTML_TAG.sub("", text)
    text = unescape(text)
    lines = [line.strip() for line in text.splitlines()]
    return _HTML_BLANK.sub("\n\n", "\n".join(line for line in lines if line)).strip()


# -- 表格(xlsx / csv)------------------------------------------------------
def read_xlsx_rows(source: bytes | str | Path) -> list[list[str]]:
    """最小 XLSX 读取器:**只用标准库**。微信/支付宝导出的就是 xlsx,不值得为它引依赖。

    ⚠️ 只读第一个工作表 —— 账单导出永远只有一张;要看别的表另说。
    """
    handle: Any = io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else Path(source)
    with zipfile.ZipFile(handle) as archive:
        names = archive.namelist()
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root:
                shared.append("".join(node.text or "" for node in item.iter() if node.tag.endswith("}t")))
        sheet_name = next((name for name in names if name.startswith("xl/worksheets/sheet")), None)
        if sheet_name is None:
            return []
        root = ElementTree.fromstring(archive.read(sheet_name))

    rows: list[list[str]] = []
    for row_node in root.iter():
        if not row_node.tag.endswith("}row"):
            continue
        cells: dict[int, str] = {}
        for cell in row_node:
            if not cell.tag.endswith("}c"):
                continue
            index = _column_index(cell.attrib.get("r") or "")
            kind = cell.attrib.get("t")
            value = ""
            for child in cell:
                if child.tag.endswith("}v"):
                    value = child.text or ""
                elif child.tag.endswith("}is"):
                    value = "".join(node.text or "" for node in child.iter() if node.tag.endswith("}t"))
            if kind == "s" and value.isdigit():
                position = int(value)
                value = shared[position] if position < len(shared) else ""
            cells[index] = value.strip()
        if cells:
            width = max(cells) + 1
            rows.append([cells.get(i, "") for i in range(width)])
    return rows


def read_csv_rows(data: bytes) -> list[list[str]]:
    """CSV/TSV → 二维表。分隔符靠**样本里谁多**来判(支付宝/微信的导出两种都出现过)。"""
    text = ""
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        text = data.decode("utf-8", "replace")
    sample = text[:4096]
    delimiter = "\t" if sample.count("\t") > sample.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    return [[cell.strip() for cell in row] for row in reader if any(cell.strip() for cell in row)]


def _column_index(ref: str) -> int:
    letters = "".join(ch for ch in ref if ch.isalpha())
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch.upper()) - ord("A") + 1)
    return max(index - 1, 0)


def rows_to_text(rows: list[list[str]], *, mode: str = "summary", head_rows: int = 20) -> str:
    """二维表 → 文本。summary 只给"形状 + 表头 + 前若干行"。

    ⭐ 为什么默认摘要而不是全表:一张 5000 行的账单塞进上下文既贵又没用 ——
    模型要的是"这是什么表、有哪些列、长什么样",细节按需再要。
    """
    if not rows:
        return "(这张表是空的)"
    width = max(len(row) for row in rows)
    head = "表格:" + str(len(rows)) + " 行 × " + str(width) + " 列"
    head += "\n表头:" + " | ".join(rows[0])
    body_rows = rows[:head_rows] if mode == "summary" else rows
    body = "\n".join(str(index + 1).rjust(3) + "| " + " | ".join(row) for index, row in enumerate(body_rows))
    tail = ""
    if mode == "summary" and len(rows) > head_rows:
        tail = "\n…(还有 " + str(len(rows) - head_rows) + " 行没显示)"
    return head + "\n" + body + tail


# -- 各格式 ---------------------------------------------------------------
def _docx_text(data: bytes) -> str:
    import docx  # python-docx

    document = docx.Document(io.BytesIO(data))
    parts: list[str] = []
    for para in document.paragraphs:
        text = str(para.text or "").strip()
        if text:
            parts.append(text)
    for index, table in enumerate(document.tables, 1):
        parts.append("【表格 " + str(index) + "】")
        for row in table.rows:
            parts.append(" | ".join(str(cell.text or "").strip() for cell in row.cells))
    return "\n".join(parts)


def _pptx_text(data: bytes) -> str:
    from pptx import Presentation  # python-pptx

    deck = Presentation(io.BytesIO(data))
    parts: list[str] = []
    for index, slide in enumerate(deck.slides, 1):
        parts.append("## 第 " + str(index) + " 页")
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                text = str(shape.text_frame.text or "").strip()
                if text:
                    parts.append(text)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    parts.append(" | ".join(str(cell.text or "").strip() for cell in row.cells))
        try:
            if slide.has_notes_slide:
                note = str(slide.notes_slide.notes_text_frame.text or "").strip()
                if note:
                    parts.append("(备注)" + note)
        except Exception:  # noqa: BLE001 - 备注是可选的,读不到就算了
            pass
    return "\n".join(parts)


def _pdf_text(data: bytes) -> tuple[str, str]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages: list[str] = []
    empty = 0
    for index, page in enumerate(reader.pages, 1):
        text = str(page.extract_text() or "").strip()
        if text:
            pages.append("## 第 " + str(index) + " 页\n" + text)
        else:
            empty += 1
    if not pages:
        return "", "这份 PDF 没有文字层(多半是扫描件/图片版),抽不出文字"
    if empty:
        return "\n\n".join(pages), "有 " + str(empty) + " 页没有文字层(扫描页),这些页读不出内容"
    return "\n\n".join(pages), ""


def _guard(kind: str) -> None:
    module, package = LIBRARIES[kind]
    try:
        __import__(module)
    except ImportError as exc:
        raise MissingLibrary(
            "读" + kind_label(kind) + "需要 " + package + ",当前环境里没装。"
            "请在项目虚拟环境里执行 pip install " + package + "(依赖清单见 pyproject.toml)"
        ) from exc


def _decode_any(data: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def extract_text(
    data: bytes, *, name: str = "", limit: int = config.EXTRACT_TEXT_LIMIT, mode: str = "summary",
) -> Extracted:
    """把一份文件变成可读文本。**绝不抛异常** —— 失败也要给一句人话。"""
    kind = detect_kind(data)
    suffix = Path(name).suffix.lower() if name else ""
    try:
        if kind == "text":
            raw = _decode_any(data)
            if suffix in (".html", ".htm") or (not suffix and _HTML_HINT.search(raw[:2048])):
                text, cut, total = clip_text(html_to_text(raw), limit)
                return Extracted(kind="html", text=text, note=cut_note(cut, total, limit))
            if suffix in TABLE_SUFFIXES:
                rows = read_csv_rows(data)
                text, cut, total = clip_text(rows_to_text(rows, mode=mode), limit)
                return Extracted(kind="table", text=text, note=cut_note(cut, total, limit))
            text, cut, total = clip_text(raw, limit)
            return Extracted(kind="text", text=text, note=cut_note(cut, total, limit))
        if kind == "xlsx" or suffix in (".xlsx", ".xlsm"):
            rows = read_xlsx_rows(data)
            text, cut, total = clip_text(rows_to_text(rows, mode=mode), limit)
            return Extracted(kind="xlsx", text=text, note=cut_note(cut, total, limit))
        if kind in LIBRARIES:
            _guard(kind)
            body = _pdf_text(data) if kind == "pdf" else (_docx_text(data) if kind == "docx" else _pptx_text(data))
            if isinstance(body, tuple):
                raw, extra = body
            else:
                raw, extra = body, ""
            text, cut, total = clip_text(raw, limit)
            return Extracted(kind=kind, text=text, note=extra or cut_note(cut, total, limit))
        if kind == "image":
            return Extracted(kind="image", note="这是图片 —— 请用 image_view 看它(抽取层不处理图片)")
        if kind == "audio":
            # ⭐ 说清楚"是音频、读不出文字",而不是让它掉到最后那句"二进制文件"
            #    (2026-10-17:模型把语音说成"文件读不出内容",又在回话里绕了半天,D139)
            return Extracted(
                kind="audio",
                note="这是音频(语音),抽取层读不出里面的文字;语音的文字要靠发消息那边转写",
            )
        return Extracted(kind=kind, note="这是" + kind_label(kind) + ",读不出文本;可以下载原件来看")
    except MissingLibrary as exc:
        return Extracted(kind=kind, note=str(exc))
    except Exception as exc:  # noqa: BLE001 - 抽取失败不该让整轮对话崩掉
        return Extracted(
            kind=kind,
            note="读这个" + kind_label(kind) + "时出错(" + type(exc).__name__ + "):" + str(exc)[:120],
        )


__all__ = ["Extracted", "MissingLibrary", "extract_text", "html_to_text", "rows_to_text",
           "read_xlsx_rows", "read_csv_rows", "clip_text", "cut_note", "LIBRARIES",
           "TEXT_SUFFIXES", "TABLE_SUFFIXES"]
