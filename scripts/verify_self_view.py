"""自我观察 v0(D138)的真机验收:它看到的那份账,必须与**库里的事实**对得上。

⚠️ 这个脚本测的不是"能不能跑",而是**那句话是不是真的**:
   ① 工具注册了、而且是只读的;
   ② digest 的长度与两段契约(开头/结尾)都在;
   ③ **独立复核**:digest 里的轮次与工具总数,用一条与实现无关的 SQL 再数一遍,必须一致;
   ④ 同一个 as_of 调两次,字节相同(它会以为自己变了);
   ⑤ **不写库**:调用前后 events 条数不变。

跑法:.venv/Scripts/python.exe scripts/verify_self_view.py
"""

from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from la import config  # noqa: E402
from la.agent.self_view import MAX_DIGEST_CHARS, build_behavior_digest  # noqa: E402
from la.tools.insight_tools import build_insight_tools  # noqa: E402
from la.tools.registry import READ_ONLY_TOOLS, ToolRegistry  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        PASSED.append(label)
        print("  PASS  " + label)
    else:
        FAILED.append(label + (("  <- " + detail) if detail else ""))
        print("  FAIL  " + label + (("  <- " + detail) if detail else ""))


def main() -> int:
    os.environ.setdefault("DEEPSEEK_API_KEY", "")
    db_path = Path(config.MEMORY_DB)
    if not db_path.exists():
        print("找不到记忆库: " + str(db_path))
        return 1
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    now = datetime.now().astimezone()

    print("[1/5] 工具:注册、只读、参数收敛")
    store = SimpleNamespace(conn=conn)
    specs = build_insight_tools(store, now=lambda: now)
    registry = ToolRegistry()
    registry.register_all(specs)
    spec = registry.get("self_inspect")
    check("工具已注册", spec is not None)
    check("self_inspect 在只读白名单里", "self_inspect" in READ_ONLY_TOOLS)
    check("注册后 writes=False", spec is not None and not spec.writes)
    check("scope 只有 behavior",
          spec is not None and spec.parameters_json_schema()
          and spec.parameters_json_schema()["properties"]["scope"].get("enum") == ["behavior"])

    print("[2/5] digest:长度、两段契约")
    before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    text = build_behavior_digest(conn, now=now, window_days=7)
    check("长度不超过 " + str(MAX_DIGEST_CHARS) + " 字符", len(text) <= MAX_DIGEST_CHARS,
          str(len(text)))
    check("开头写清窗口与范围", text.startswith("[自我观察 · 行为]"))
    check("结尾写明「事实不是结论」", "不是结论" in text and "不要把对自己的评价写进" in text)

    print("[3/5] 独立复核:digest 的数字 vs 直接 SQL 数的")
    first = (now.date() - timedelta(days=6)).isoformat()
    end = (now.date() + timedelta(days=1)).isoformat()
    asks = conn.execute(
        "SELECT COUNT(*) FROM events WHERE type = 'user/message'"
        " AND time >= ? AND time < ?", (first, end),
    ).fetchone()[0]
    model_tools = 0
    for row in conn.execute(
        "SELECT data FROM events WHERE type = 'tool/result' AND time >= ? AND time < ?",
        (first, end),
    ):
        import json as _json
        try:
            payload = _json.loads(str(row["data"] or "{}"))
        except ValueError:
            continue
        if str(payload.get("via") or "") == "model":
            model_tools += 1
    got_asks = re.search(r"轮次 (\d+)", text)
    got_tools = re.search(r"工具   共 (\d+) 次", text)
    check("轮次与 SQL 一致", got_asks is not None and int(got_asks.group(1)) == asks,
          "digest=" + (got_asks.group(1) if got_asks else "?") + " sql=" + str(asks))
    check("工具总数与 SQL 一致",
          got_tools is not None and int(got_tools.group(1)) == model_tools,
          "digest=" + (got_tools.group(1) if got_tools else "?") + " sql=" + str(model_tools))

    print("[4/5] 幂等:同一个 as_of 两次调用")
    again = build_behavior_digest(conn, now=now, window_days=7)
    check("两次输出字节相同", again == text)

    print("[5/5] 只读:调用不改库")
    after = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    check("events 条数不变", before == after, str(before) + " -> " + str(after))

    print("\n--- 这次它看到的内容(同时写进 _research/self_digest.txt)---")
    # ⚠️ 控制台是 GBK,digest 里有 ⚠️ 这类字符 —— 直接 print 会 UnicodeEncodeError。
    #    所以**写文件**看全文,控制台只打一行"已写出"。
    target = ROOT / "_research" / "self_digest.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    print("已写出 " + str(target) + "(" + str(len(text)) + " 字符)")
    print("-" * 66)
    conn.close()
    if FAILED:
        print("失败 %d/%d:" % (len(FAILED), len(FAILED) + len(PASSED)))
        for item in FAILED:
            print("  - " + item)
        return 1
    print("全部通过:%d 项" % len(PASSED))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
