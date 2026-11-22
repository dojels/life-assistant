"""M12 端到端验收:基础能力层(读 / 看 / 找 / 写 / 联网)。

    .venv/Scripts/python.exe scripts/verify_m12.py

全程**不需要密钥、不联网**:视觉那一环用假客户端,搜索用假后端。
唯一没法在这里验的是"真机微信发一张图"与"真的联网搜一次" —— 那两步在最后打印出来。

四条总验收(见 docs/基础能力层执行清单.md §0):
  V1 发来的图片助手看得见   V2 发来的文档读得出内容
  V3 能写产物且可撤销       V4 联网返回带链接的答案
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import zipfile
import io
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# ⚠️ 2026-10-15 的"批次 A"把工具合并过(file_read+file_list → file,web_search+web_fetch → web)。
#    这个脚本当时没跟着改,于是从那天起**一直是红的**——而没人跑它,所以没人知道。
#    把"旧名 -> 新名 + action"收成一张表:以后再有合并,只改这一处。
_RENAMED: dict[str, tuple[str, str]] = {
    "file_read": ("file", "read"),
    "file_list": ("file", "list"),
    "web_fetch": ("web", "fetch"),
    "web_search": ("web", "search"),
}


def _modern(name: str, params: dict) -> tuple[str, dict]:
    """把"停用的旧工具名"翻译成现名 + action。未登记的名字原样返回。"""
    renamed = _RENAMED.get(name)
    if renamed is None:
        return name, params
    return renamed[0], {"action": renamed[1], **params}
DATA_DIR = tempfile.mkdtemp(prefix="la-verify-m12-")

os.environ["LA_DATA_DIR"] = DATA_DIR
os.environ["LA_LLM_PROVIDER"] = "fake"
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.path.insert(0, str(BASE_DIR))

from la import config  # noqa: E402
from la.base.phases import PHASE_ANSWER, CircuitBreaker  # noqa: E402
from la.base import images, web  # noqa: E402
from la.base.tools import build_base_tools, build_web_tools  # noqa: E402
from la.domain.artifacts import ArtifactError, ArtifactStore  # noqa: E402
from la.domain.materials import MaterialService  # noqa: E402
from la.domain.media import MediaStore  # noqa: E402
from la.domain.receipts import DOMAIN_ARTIFACT, ReceiptLog  # noqa: E402
from la.domain.reverters import build_reverter  # noqa: E402
from la.llm.client import ChatResult  # noqa: E402
from la.storage import db  # noqa: E402
from la.storage.migrations import MIGRATIONS_ASSISTANT  # noqa: E402
from la.tools.registry import ToolRegistry  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    # ⚠️ 只用 ASCII 标记:Windows 控制台默认是 GBK,打 emoji 会直接 UnicodeEncodeError
    #    (与 verify_m11.py 的 PASS/FAIL 同一口径)。
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   " + detail) if detail and not ok else ""))


class FakeVision:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        return ChatResult(text="这是一张收据,金额 12.5 元,商户是便利店。", model="deepseek-flash")


class FakeProvider:
    name = "fake"

    async def search(self, query, *, limit):  # noqa: ANN001
        return [web.SearchHit(title="示例文档", url="https://example.com/doc", page_age="1 天前")]


def make_docx() -> bytes:
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        "<w:p><w:r><w:t>本月小结</w:t></w:r></w:p>"
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>餐饮</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>860</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
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


def make_xlsx() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "xl/sharedStrings.xml",
            '<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<si><t>日期</t></si><si><t>金额</t></si><si><t>2026-10-01</t></si></sst>",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
            '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>12.5</v></c></row></sheetData></worksheet>',
        )
    return buffer.getvalue()


def main() -> int:
    print("M12 基础能力层验收(离线,不需要密钥)\n")
    root = Path(DATA_DIR)
    config.DATA_DIR = root
    config.ARTIFACTS_DIR = root / "artifacts"
    conn = db.connect(root / "assistant.db")
    db.migrate(conn, MIGRATIONS_ASSISTANT)
    artifacts = ArtifactStore(conn, root=config.ARTIFACTS_DIR)
    receipts = ReceiptLog(conn)
    media = MediaStore(conn, root=root / "media")
    materials = MaterialService(conn, media=media)
    vision = FakeVision()

    tools = build_base_tools(
        materials=materials, artifacts=artifacts, receipts=receipts,
        receipt_domain=DOMAIN_ARTIFACT, vision_factory=lambda: vision,
        vision_model="deepseek-flash", session_id="verify-m12",
    ) + build_web_tools(enabled=True, provider=FakeProvider())
    registry = ToolRegistry()
    registry.register_all(tools)
    read_only_registry = ToolRegistry(read_only=True)
    read_only_registry.register_all(tools)

    def call(name: str, params: dict, *, ro: bool = False):
        target = read_only_registry if ro else registry
        name, params = _modern(name, params)
        return asyncio.run(target.execute(
            name, params, breaker=CircuitBreaker(phase=PHASE_ANSWER), session_id="verify-m12",
        ))

    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "note.md").write_text("# 购物\n- 牛奶\n- 鸡蛋\n- 面包", encoding="utf-8")
    (workspace / "table.xlsx").write_bytes(make_xlsx())
    (workspace / "report.docx").write_bytes(make_docx())
    (workspace / "receipt.png").write_bytes(PNG_1PX)
    (workspace / ".env").write_text("SECRET=1", encoding="utf-8")
    (workspace / "sub").mkdir(exist_ok=True)
    (workspace / "sub" / "inner.txt").write_text("内层", encoding="utf-8")

    print("一、读(文本 / 文档 / 目录)")
    read = call("file_read", {"path": str(workspace / "note.md")})
    check("读文本给出行号", read.ok and "1| # 购物" in read.data, read.message)
    check("结尾给出总行数", "(End of file - total 4 lines)" in read.data)
    window = call("file_read", {"path": str(workspace / "note.md"), "offset": 2, "limit": 2})
    check("offset/limit 能翻页", window.ok and "Showing lines 2-3 of 4" in window.data, window.message)
    beyond = call("file_read", {"path": str(workspace / "note.md"), "offset": 99})
    check("offset 越界明确报错", (not beyond.ok) and "超过文件总行数" in beyond.message)

    print("\n二、V2 文档读得出内容")
    docx = call("file_read", {"path": str(workspace / "report.docx")})
    check("docx 抽出段落", docx.ok and "本月小结" in docx.data, docx.message)
    check("docx 抽出表格", "餐饮 | 860" in docx.data, docx.data[:60])
    xlsx = call("file_read", {"path": str(workspace / "table.xlsx")})
    check("xlsx 给出形状与表头", xlsx.ok and "2 行 × 2 列" in xlsx.data and "表头:日期 | 金额" in xlsx.data, xlsx.message)
    check("xlsx 给出数据行", "2026-10-01 | 12.5" in xlsx.data)
    listing = call("file_list", {"path": str(workspace)})
    check("列目录列出文件", listing.ok and "note.md" in listing.data, listing.message)
    check("列目录不显示 .env", ".env" not in listing.data)
    recursive = call("file_list", {"path": str(workspace), "recursive": True})
    check("递归列目录带前缀", "sub/inner.txt" in recursive.data, recursive.data[:80])

    print("\n三、安全闸")
    denied = call("file_read", {"path": "C:/Windows/System32/drivers/etc/hosts"})
    check("白名单外一律拒绝", (not denied.ok) and "白名单" in denied.message, denied.message)
    secret = call("file_read", {"path": str(workspace / ".env")})
    check(".env 这类文件点名拒绝", (not secret.ok) and "不允许" in secret.message, secret.message)
    ssrf = call("web_fetch", {"url": "http://127.0.0.1:8765/"})
    check("SSRF:本机地址被拒", (not ssrf.ok) and "不允许抓取" in ssrf.message, ssrf.message)

    print("\n四、V1 看图片")
    viewed = call("image_view", {"path": str(workspace / "receipt.png"), "question": "多少钱?"})
    check("看图走视觉模型", viewed.ok and "收据" in viewed.data, viewed.message)
    check("图片只进 user 消息", vision.calls == 1)
    check("返回带外部内容提示", config.EXTERNAL_CONTENT_NOTICE in viewed.data)
    metrics = images.image_dimensions(PNG_1PX, "image/png")
    check("图片尺寸按头部解析", metrics == (1, 1), str(metrics))
    not_image = call("image_view", {"path": str(workspace / "note.md")})
    check("非图片明确拒绝", (not not_image.ok) and "不是图片" in not_image.message, not_image.message)

    print("\n五、素材层:发来的东西不再石沉大海")
    materials.ingest_bytes("报销单\n金额 88 元".encode("utf-8"), channel="weixin",
                           external_id="v-1", original_name="报销.txt", kind="text")
    materials.ingest_bytes(PNG_1PX, channel="weixin", external_id="v-2",
                           original_name="收据.png", kind="image")
    listed = call("material_list", {})
    check("看得到收到过什么", listed.ok and "报销.txt" in listed.data and "收据.png" in listed.data, listed.message)
    material_read = call("file_read", {"material_id": 1})
    check("按素材编号读内容", material_read.ok and "金额 88 元" in material_read.data, material_read.message)
    material_view = call("image_view", {"material_id": 2})
    check("图片素材能看", material_view.ok and vision.calls == 2, material_view.message)

    print("\n六、V3 写产物 + 可撤销")
    written = call("artifact_write", {"name": "购物清单.md", "content": "# 清单\n- 牛奶"})
    check("写产物成功", written.ok, written.message)
    artifact_id = written.data.get("artifact_id") if written.ok else ""
    check("产物已登记且文件在", bool(artifact_id) and artifacts.exists(artifact_id))
    check("带一条可撤销回执", bool(written.data.get("receipt_id")) if written.ok else False)
    blocked = call("artifact_write", {"name": "x.md", "content": "x"}, ro=True)
    check("只读模式下写被拒", (not blocked.ok) and blocked.error_code == "READ_ONLY", blocked.message)
    bad_ext = call("artifact_write", {"name": "x.exe", "content": "x"})
    check("不认识的扩展名被拒", (not bad_ext.ok) and "只能写" in bad_ext.message, bad_ext.message)
    reverter = build_reverter(receipts, artifacts=artifacts)
    undo = reverter.undo(int(written.data["receipt_id"]), actor="user")
    check("撤销后文件与登记都没了", (not artifacts.exists(artifact_id))
          and "已删除" in str(undo), str(undo)[:80])
    try:
        artifacts.get(artifact_id)
        gone = False
    except ArtifactError:
        gone = True
    check("撤销后查询明确报不存在", gone)

    print("\n七、V4 联网")
    disabled = build_web_tools(enabled=False, provider=None)
    disabled_registry = ToolRegistry()
    disabled_registry.register_all(disabled)
    off = asyncio.run(disabled_registry.execute(
        *_modern("web_search", {"queries": ["天气"]}),
        breaker=CircuitBreaker(phase=PHASE_ANSWER),
    ))
    check("默认关:没打开就明确拒绝", (not off.ok) and "联网是关着的" in off.message, off.message)
    searched = call("web_search", {"queries": ["示例"]})
    check("搜索返回 markdown 链接", searched.ok and "[示例文档](https://example.com/doc)" in searched.data, searched.message)
    check("搜索返回带外部内容提示", config.EXTERNAL_CONTENT_NOTICE in searched.data)

    print("\n八、正式文档:Word / Excel(P1-4)")
    import zipfile as _zipfile

    from openpyxl import load_workbook as _load_workbook

    report_md = """---
title: 本周小结
subtitle: 2026 年第 41 周
author: 生活助手
---
# 一、花了多少
餐饮 860 元,交通 210 元。

## 二、明细
| 分类 | 金额 |
|---|---|
| 餐饮 | 860 |
| 交通 | 210 |
"""
    docx_written = call("artifact_write", {"name": "本周小结.docx", "content": report_md})
    check("能写 .docx", docx_written.ok, docx_written.message)
    if docx_written.ok:
        raw = artifacts.read_bytes(docx_written.data["artifact_id"])
        with _zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = archive.namelist()
            document_xml = archive.read("word/document.xml").decode("utf-8")
            footer_xml = archive.read("word/footer1.xml").decode("utf-8")
        check("docx 有封面与页眉页脚部件",
              "word/header1.xml" in names and "word/footer1.xml" in names)
        check("docx 的页码是域不是死数字", "PAGE" in footer_xml)
        check("docx 是公文风(正文仿宋 + 标题黑体)",
              'w:eastAsia="仿宋"' in document_xml and 'w:eastAsia="黑体"' in document_xml)
        check("docx 正文首行缩进 2 字符", 'w:firstLineChars="200"' in document_xml)
        check("docx 表格有表头底纹", 'w:fill="D9D9D9"' in document_xml)
        check("docx 表格有斑马纹", 'w:fill="F5F5F5"' in document_xml)
        check("docx 表格自动加了合计行", "合计" in document_xml)
        check("产物回执照样记了", bool(docx_written.data.get("receipt_id")))
        sample_docx = Path(DATA_DIR) / "本周小结.docx"
        sample_docx.write_bytes(raw)
    xlsx_written = call("artifact_write", {"name": "明细.xlsx", "content": report_md})
    check("能写 .xlsx", xlsx_written.ok, xlsx_written.message)
    if xlsx_written.ok:
        raw = artifacts.read_bytes(xlsx_written.data["artifact_id"])
        sheet = _load_workbook(io.BytesIO(raw))["二、明细"]
        check("xlsx 表头加粗且冻结首行", sheet.cell(row=1, column=1).font.bold
              and sheet.freeze_panes == "A2")
        check("xlsx 里的数字是数字(能求和)", isinstance(sheet.cell(row=2, column=2).value, int))
        check("xlsx 追加了合计行公式",
              str(sheet.cell(row=4, column=1).value) == "合计"
              and str(sheet.cell(row=4, column=2).value).startswith("=SUM("))
        check("xlsx 数据行有斑马纹", sheet.cell(row=2, column=1).fill.fgColor.rgb.endswith("F5F5F5"))
        sample_xlsx = Path(DATA_DIR) / "明细.xlsx"
        sample_xlsx.write_bytes(raw)
    bad = call("artifact_write", {"name": "x.exe", "content": "x"})
    check("不支持的扩展名仍然拒绝", (not bad.ok) and "只能写" in bad.message, bad.message)

    print("\n九、联网开关按钮(点了立刻生效,不用重启)")
    from la.ops import settings_store

    live_tools = build_web_tools(
        enabled=lambda: settings_store.web_enabled(conn, default=False),
        provider=FakeProvider(),
    )
    live_registry = ToolRegistry()
    live_registry.register_all(live_tools)

    def live_search():
        return asyncio.run(live_registry.execute(
            *_modern("web_search", {"queries": ["示例"]}),
            breaker=CircuitBreaker(phase=PHASE_ANSWER),
        ))

    check("默认关:按钮没开时拒绝", not live_search().ok)
    settings_store.set_flag(conn, settings_store.WEB_ENABLED_KEY, True)
    check("点开之后立刻可用(同一个工具对象,没重建)", live_search().ok)
    settings_store.set_flag(conn, settings_store.WEB_ENABLED_KEY, False)
    check("点关之后立刻拒绝", not live_search().ok)
    chat_template = (BASE_DIR / "la" / "web" / "templates" / "chat.html").read_text(encoding="utf-8")
    check("聊天页有「联网」按钮", "/web/toggle" in chat_template and "联网 ·" in chat_template)

    print("\n十、空承诺已修")
    source = (BASE_DIR / "la" / "bridge" / "bridge.py").read_text(encoding="utf-8")
    check("桥接层不再说「想用它的时候说一声就行」", "想用它的时候说一声就行" not in source)
    check("桥接层改说「可以直接让我读它的内容」", "可以直接让我读它的内容" in source)

    conn.close()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n结果: " + str(passed) + "/" + str(len(RESULTS)) + " 项通过")
    for name, ok, _ in RESULTS:
        if not ok:
            print("  未通过: " + name)
    print("\n生成的样例(可以直接打开看观感):")
    for name in ("本周小结.docx", "明细.xlsx"):
        candidate = Path(DATA_DIR) / name
        if candidate.exists():
            print("  " + str(candidate))
    print("\n最后两步只能你来走(需要真环境):")
    print("  1) 打开联网:在 .env 里设 LA_WEB_ENABLED=1,再跑")
    print("     python -m la --once \"帮我查一下 DeepSeek 官方文档的域名\"")
    print("     它应该先 web_search 再 web_fetch,并带上链接。")
    print("  2) 微信里发一张照片 + 一句「这是什么」,它应该能说出图里有什么。")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
