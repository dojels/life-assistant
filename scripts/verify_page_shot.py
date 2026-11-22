"""D140 真机验收:page_view —— 它能不能**真的自己看一眼**自己写的网页。

⚠️ 这个脚本测的不是"能不能跑",而是四句真话:
   ① 本机找得到浏览器(找不到就该说清怎么装,而不是静默失败);
   ② 渲染走的是**回环 HTTP + 预览那串 CSP**,不是 file://;
   ③ 真 Chrome 真渲染:产出的 PNG 尺寸就是设的视口,**不是一张空图**;
   ④ 工具那一层:能在真实产物库里找到页面、真的把画面描述出来、并且如实标明
      「这是画面描述,不是源码级结论」。

跑法:
    .venv/Scripts/python.exe scripts/verify_page_shot.py            # 离线(不花钱)
    .venv/Scripts/python.exe scripts/verify_page_shot.py --live     # 再真打一次视觉模型

⚠️ --live 会打真实模型(一次读图,约 1~2k token)。没有凭据就自动跳过,不算失败。
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.dont_write_bytecode = True

from la import config  # noqa: E402
from la.base import shot as shot_mod  # noqa: E402
from la.base.html_page import HTML_PREVIEW_CSP, build_html_tools  # noqa: E402
from la.domain.artifacts import ArtifactStore  # noqa: E402
from la.ui.cli import configure_stdio  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        PASSED.append(label)
        print("  PASS  " + label)
    else:
        FAILED.append(label + (("  <- " + detail) if detail else ""))
        print("  FAIL  " + label + (("  <- " + detail) if detail else ""))


def latest_page() -> tuple[ArtifactStore, sqlite3.Connection, object]:
    """从**真实产物库**里取最近一页网页(只读打开,绝不迁移、绝不写)。"""
    uri = "file:" + str(Path(config.ASSISTANT_DB).as_posix()) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    store = ArtifactStore(conn, root=config.ARTIFACTS_DIR)
    pages = [item for item in store.list(limit=200)
             if str(item.name).lower().endswith((".html", ".htm"))]
    if not pages:
        raise SystemExit("真实产物库里还没有网页产物 —— 先生成一个再来验收")
    return store, conn, pages[0]


class _StubVision:
    """离线替身:把"看到了什么"固定住,专门测工具那一层的管道与措辞。"""

    def __init__(self) -> None:
        self.calls = 0
        self.question = ""

    async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        self.question = str(messages[0]["content"][0]["text"])
        from la.llm.client import ChatResult

        return ChatResult(
            text="一句话:这是一页浅底深字的网页。\n可见文字:周报 / 数学 / 语文。\n"
                 "版面:标题居中;右侧卡片下沿比左侧低约 20px,看着没对齐。",
            model="stub",
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="再真打一次视觉模型(花钱)")
    parser.add_argument("--report", default="", help="把结果另存一份 UTF-8 报告")
    args = parser.parse_args()

    print("[1/5] 浏览器")
    try:
        chrome = shot_mod.find_chrome()
        check("找得到浏览器", bool(chrome), chrome)
    except shot_mod.ShotError as exc:
        check("找得到浏览器", False, exc.message)
        return 1

    print("[2/5] 渲染服务:回环 + 预览同一串 CSP")
    html = "<!doctype html><html><body><h1>验收</h1></body></html>".encode("utf-8")
    with shot_mod.page_server(html, csp=HTML_PREVIEW_CSP) as url:
        check("地址是回环", url.startswith("http://127.0.0.1:"), url)
        with urllib.request.urlopen(url, timeout=10) as response:
            body = response.read()
            csp = response.headers.get("Content-Security-Policy", "")
        check("取回来的是同一份 HTML", body == html)
        check("带 sandbox allow-scripts", "sandbox allow-scripts" in csp, csp[:80])
        check("带 connect-src 'none'", "connect-src 'none'" in csp)
        check("没有 allow-same-origin", "allow-same-origin" not in csp)

    print("[3/5] 真 Chrome 真渲染(真实产物库里最近一页网页)")
    store, conn, page = latest_page()
    try:
        page_html = store.path_of(page).read_text(encoding="utf-8")
        result = shot_mod.render_html(page_html)
        check("产出的是 PNG", result.data.startswith(b"\x89PNG"))
        check("尺寸等于设的视口(" + str(config.SHOT_WIDTH) + "×"
              + str(config.SHOT_HEIGHT) + ")",
              (result.width, result.height) == (config.SHOT_WIDTH, config.SHOT_HEIGHT),
              str(result.width) + "×" + str(result.height))
        check("不是空图(>5KB)", len(result.data) > 5 * 1024, str(result.kb) + "KB")
        check("截图文件留在 data/shots/", result.path.is_file(), str(result.path))
        check("耗时可接受(<25 秒)", result.elapsed < 25, str(result.elapsed) + "s")
        print("        (" + str(page.name) + " → " + str(result.path.name) + ", "
              + str(result.width) + "×" + str(result.height) + ", "
              + str(result.kb) + "KB, " + str(result.elapsed) + "s)")

        # ⭐ 分屏:长页面靠"把页面上移"拍第二屏(滚动在无头截图里是死路,实测整张全白)
        second = shot_mod.render_html(page_html, offset_y=config.SHOT_HEIGHT)
        check("第二屏拍得到", bool(second.data), "")
        check("第二屏与第一屏不同(真的动了)", second.data != result.data)
    finally:
        conn.close()

    print("[4/5] 工具层:找得到页面、说得清画面")
    store, conn, page = latest_page()
    stub = _StubVision()
    try:
        tools = build_html_tools(
            artifacts=store, vision_factory=lambda: stub, vision_model="deepseek-flash",
        )
        spec = next(item for item in tools if item.name == "page_view")
        payload = asyncio.run(spec.handler({}))
        check("工具是只读的", not spec.writes)
        check("看到的是最近那一页", payload.get("artifact_id") == str(page.id),
              str(payload.get("artifact_id")))
        note = str(payload.get("note") or "")
        check("写明是网页截图", "【网页截图" in note)
        check("写明是画面描述、不是源码结论", "不是源码级结论" in note)
        check("问的是版面", "版面" in stub.question, stub.question[:60])
        check("真调了一次视觉模型", stub.calls == 1, str(stub.calls))
        check("描述了画面", "没对齐" in note)
        check("没把「看不到下半截」当成缺陷", "被截断" not in note)
        print("        " + note.splitlines()[0])
    finally:
        conn.close()

    print("[5/5] 真机视觉模型" + ("(--live)" if args.live else "(跳过,加 --live 才跑)"))
    if args.live:
        import os

        from la.llm.client import DeepSeekClient
        from la.llm.vision import describe_image

        settings = config.get_settings()
        if settings.provider == "fake" or not settings.api_key:
            print("        没有真实凭据,跳过(不算失败)")
        else:
            client = DeepSeekClient(
                api_key=settings.api_key, base_url=settings.base_url, model=settings.model,
            )
            description = asyncio.run(describe_image(
                client, result.data, "image/png",
                question="这是网页截图。版面有没有明显问题(错位/溢出/被截断/白屏)?只说看得见的。",
                model=settings.model or None,
            ))
            check("真模型读出了画面", len(description) > 20, description[:80])
            print("        真模型说:" + description.replace("\n", " ")[:200])
            os.environ.setdefault("LA_VERIFIED_LIVE", "1")
    else:
        print("        (跳过:--live 才会花一次读图的钱)")

    total = len(PASSED) + len(FAILED)
    print("")
    print("通过 " + str(len(PASSED)) + "/" + str(total))
    if FAILED:
        print("失败:")
        for line in FAILED:
            print("  - " + line)
    lines = ["通过 " + str(len(PASSED)) + "/" + str(total)] + ["PASS " + x for x in PASSED]
    lines += ["FAIL " + x for x in FAILED]
    if args.report:
        Path(args.report).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 1 if FAILED else 0


if __name__ == "__main__":
    configure_stdio()
    sys.exit(main())
