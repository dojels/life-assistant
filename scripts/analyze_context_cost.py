"""上下文成本分析(**只读**):缓存命中率、压缩次数、每一轮请求都由什么组成。

为什么要单独一个脚本:
  * 2026-10-14 用户问"5950 就压缩,是为了省钱还是质量?这么频繁压缩真的能省钱吗?"
    这类问题**不能靠感觉回答** —— 我们的 `turn/end` 事件里就存着每一轮的实测 usage
    (含 prompt_cache_hit/miss),摘要池与压缩事件也在同一个库里;
  * 改完装配顺序(例如把时间戳移出系统提示)之后,要能**用同一个脚本量出差别**。

它只做三件事,全是只读:
  1. 逐轮 usage:prompt / 缓存命中 / 未命中 / 输出,以及总命中率;
  2. 按"这一轮有几个工具结果"分组看命中率 —— ⭐ 这一张表是判定"跨轮缓存有没有吃到"的关键:
     一轮里只有一次模型请求的那些轮,命中率极低 = 时间戳在提示词开头把前缀打散了;
  3. 压缩账:compaction/start、compaction/summary、summary_pool 的条数与体积;
     若存在轨迹文件,再顺带把**最近一轮的投影**拆开(system / runtime / memory / conversation / tools)。

运行:
    .venv/Scripts/python.exe scripts/analyze_context_cost.py
    .venv/Scripts/python.exe scripts/analyze_context_cost.py --db data/memory.db
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.dont_write_bytecode = True

from la.ui.cli import configure_stdio  # noqa: E402  (Windows 控制台默认 GBK:⭐ 与中文会把它打崩)

configure_stdio()


def open_ro(path: Path) -> sqlite3.Connection:
    """只读打开 —— 这个脚本**一个字节都不许写**(与记忆页同一条纪律)。"""
    conn = sqlite3.connect("file:" + str(path).replace("?", "%3f") + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _usage(data: object) -> dict:
    payload = json.loads(data) if isinstance(data, str) else (data or {})
    return payload.get("usage") or {}


def scan_turns(conn: sqlite3.Connection) -> list[dict]:
    """逐轮:这一轮有几个工具结果 + 实测 usage(按会话顺序)。"""
    per_session: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for row in conn.execute("SELECT session_id, type, data FROM events ORDER BY session_id, seq"):
        per_session[row["session_id"]].append((row["type"], row["data"]))

    turns: list[dict] = []
    for session_id, items in per_session.items():
        tool_results = 0
        for kind, data in items:
            if kind == "tool/result":
                tool_results += 1
            elif kind == "turn/end":
                usage = _usage(data)
                if usage:
                    turns.append({
                        "session": session_id,
                        "tool_results": tool_results,
                        "prompt": int(usage.get("prompt_tokens") or 0),
                        "hit": int(usage.get("cache_hit_tokens") or 0),
                        "miss": int(usage.get("cache_miss_tokens") or 0),
                        "out": int(usage.get("completion_tokens") or 0),
                    })
                tool_results = 0
    return turns


def hit_rate(rows: list[dict]) -> float:
    hit = sum(row["hit"] for row in rows)
    miss = sum(row["miss"] for row in rows)
    return (100.0 * hit / (hit + miss)) if (hit + miss) else 0.0


def _avg(rows: list[dict], key: str) -> float:
    return (sum(row[key] for row in rows) / len(rows)) if rows else 0.0


def print_usage(turns: list[dict]) -> None:
    print("== 逐轮 usage(实测,来自 turn/end 事件)==")
    print("轮数: %d" % len(turns))
    print("prompt 合计:   %d" % sum(row["prompt"] for row in turns))
    print("缓存命中:      %d(%.1f%%)" % (
        sum(row["hit"] for row in turns), hit_rate(turns)))
    print("缓存未命中:    %d" % sum(row["miss"] for row in turns))
    print("输出:          %d" % sum(row["out"] for row in turns))
    print("平均每轮 prompt: %.0f" % _avg(turns, "prompt"))
    print()
    print("== 按'这一轮有几个工具结果'分组(1 个 ≈ 一轮只调一次模型)==")
    buckets: dict[int, list[dict]] = defaultdict(list)
    for row in turns:
        buckets[min(row["tool_results"], 5)].append(row)
    print("  %-10s %6s %12s %14s" % ("工具结果", "轮数", "缓存命中率", "平均 prompt"))
    for key in sorted(buckets):
        label = ("%d 个" % key) + ("及以上" if key == 5 else "")
        rows = buckets[key]
        print("  %-10s %6d %11.1f%% %14.0f" % (label, len(rows), hit_rate(rows), _avg(rows, "prompt")))
    if buckets.get(1):
        print()
        print("  ⭐ 1 个工具结果那组 = 一轮只有一次模型请求。命中率低 → 跨轮缓存没吃到;")
        print("     高 → 提示词前缀稳定(时间戳之类的易变信息没在开头)。")


def print_compaction(conn: sqlite3.Connection) -> None:
    print()
    print("== 压缩账 ==")
    for kind in ("compaction/start", "compaction/summary", "compaction/skipped"):
        count = conn.execute("SELECT COUNT(*) FROM events WHERE type = ?", (kind,)).fetchone()[0]
        print("  %-22s %d" % (kind, count))
    try:
        rows = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(text)), 0) AS chars"
                            " FROM summary_pool").fetchone()
        print("  summary_pool           %d 条 / %d 字符" % (int(rows["n"]), int(rows["chars"])))
    except sqlite3.Error:
        pass


def print_last_projection(trace_file: Path) -> None:
    """最近一轮的投影拆开看 —— 顺带回答"这 17k 里到底是什么占的"。"""
    print()
    print("== 最近一轮的投影组成 ==")
    if not trace_file.exists():
        print("  (还没有轨迹文件: %s)" % trace_file)
        return
    lines = [line for line in trace_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        print("  (轨迹文件是空的)")
        return
    record = json.loads(lines[-1])
    segments = record.get("segments") or {}
    print("  时间 %s | 会话 %s | 第 %s 轮" % (
        record.get("at"), record.get("session"), record.get("turn")))
    for key in ("system", "checkpoint", "runtime", "memory", "conversation", "evidence"):
        print("    %-14s %d" % (key, int(segments.get(key) or 0)))
    print("    %-14s %d   ← 不进 messages,但每轮都付" % ("tools(schema)", int(segments.get("tools") or 0)))
    print("  投影合计: %s + 工具 schema %s = %s" % (
        record.get("total"), record.get("tools"), record.get("grand_total")))


def main() -> int:
    parser = argparse.ArgumentParser(description="上下文成本分析(只读)")
    parser.add_argument("--db", default=str(BASE_DIR / "data" / "memory.db"))
    parser.add_argument("--trace", default=str(BASE_DIR / "data" / "trace" / "context.jsonl"))
    args = parser.parse_args()

    db = Path(args.db)
    if not db.exists():
        print("找不到记忆库: " + str(db))
        return 1
    conn = open_ro(db)
    try:
        turns = scan_turns(conn)
        print_usage(turns)
        print_compaction(conn)
    finally:
        conn.close()
    print_last_projection(Path(args.trace))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
