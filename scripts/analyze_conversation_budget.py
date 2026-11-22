"""对话窗口预算分析(**只读**):对话段预算到底买住了多少,以及把它放大要连带改什么。

⚠️ 起点是用户 2026-10-14 那句"历史对话限 4000 太离谱了";当时对话段是 4000,重定标后是 24000
   (**D121**,见 docs/历史窗口重定标.md)。脚本里的数字全部现读 config,不写死 ——
   所以它既能复现"旧的一组数为什么错",也能给新的一组数记分。

为什么要单独一个脚本(2026-10-14 用户一句「历史对话限 4000 太离谱了」):

  * 「4000 是大还是小」不能靠感觉 —— 它是**估算口径**的 token(chars/4),
    而对话段里**还塞着思维链**(实测思维链字符是正文的 2.3 倍),
    所以「4000」和「模型能看到多少历史」是两件事;
  * 更关键:**放大它不会自动放大可见历史**。三个机制一起决定可见窗口 ——
    ① Projector._fit_conversation 的上限;② needs_compaction 的触发线;③
    session.pick_compaction_range 的桶(上限 x 0.5,保留最近 6 条)。
    只动 ①,②③ 会把它吃掉(而且从"回合结束后偶发"变成"每轮一次")。
  * ⭐ 所以第 3 节**把整套机制重放一遍**,并用**真实库里的压缩次数**校准重放是否可信。

它只读 data/memory.db 与 data/trace/context.jsonl,一个字节都不写。

运行:
    .venv/Scripts/python.exe scripts/analyze_conversation_budget.py
    .venv/Scripts/python.exe scripts/analyze_conversation_budget.py --limits 4000,8000,16000,32000
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.dont_write_bytecode = True

from la import config  # noqa: E402
from la.ui.cli import configure_stdio  # noqa: E402

configure_stdio()

#: 压缩时保留几条最近的可见事件(照 session.RECENT_EVENTS_KEPT)。
RECENT_EVENTS_KEPT = 6
#: ⚠️ 2026-10-17:D134 之后压缩不再是"以对话预算的 50% 为目标"。
#:    触发线是写死的 config.BUDGET_COMPACT_TRIGGER,压缩后保留 BUDGET_COMPACT_RETAIN_RATIO。
#:    这里改成读那份唯一口径,免得照着一个已经不存在的规则调参。
COMPACTION_TARGET_RATIO = config.BUDGET_COMPACT_RETAIN_RATIO
#: 重放时固定段(system + runtime + memory)的近似大小 —— 真实值每轮 500~900。
DEFAULT_FIXED = 700
#: 每个会话最多重放多少轮(很长的会话不至于拖慢脚本)。
MAX_TURNS_PER_SESSION = 400


def open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect("file:" + str(path).replace("?", "%3f") + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def parse_time(text: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None


def message_tokens(message: dict) -> int:
    """与 config.estimate_messages_tokens 同口径(正文 + 思维链 + 固定开销)。"""
    return (
        config.estimate_tokens(str(message.get("content") or ""))
        + config.estimate_tokens(str(message.get("reasoning_content") or ""))
        + 8
    )


def total_tokens(messages: list[dict]) -> int:
    return sum(message_tokens(m) for m in messages)


def fit(messages: list[dict], limit: int) -> tuple[list[dict], int]:
    """照抄 Projector._fit_conversation:成对丢最旧的,返回 (留下的, 丢了几条)。"""
    kept = list(messages)
    dropped = 0
    while kept and total_tokens(kept) > limit:
        if len(kept) >= 2:
            del kept[:2]
            dropped += 2
        else:
            kept.clear()
            dropped += 1
    return kept, dropped


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(int(len(ordered) * ratio), len(ordered) - 1)
    return ordered[index]


def human_span(seconds: float) -> str:
    if seconds < 90:
        return "%.0f 秒" % seconds
    if seconds < 5400:
        return "%.0f 分钟" % (seconds / 60.0)
    if seconds < 86400 * 2:
        return "%.1f 小时" % (seconds / 3600.0)
    return "%.1f 天" % (seconds / 86400.0)


def load_sessions(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """每个会话的事件流:消息事件带正文与思维链,另外记住 surface 与原始 JSON 体积。"""
    per: dict[str, list[dict]] = defaultdict(list)
    for row in conn.execute(
        "SELECT session_id, seq, type, time, surface, data FROM events"
        " WHERE type IN ('user/message', 'assistant/message') ORDER BY session_id, seq"
    ):
        raw = str(row["data"] or "{}")
        payload = json.loads(raw or "{}")
        per[row["session_id"]].append({
            "seq": int(row["seq"]),
            "type": row["type"],
            "time": row["time"],
            "surface": bool(row["surface"]),
            "raw_tokens": config.estimate_tokens(raw),
            "content": str(payload.get("text") or ""),
            "reasoning_content": str(payload.get("reasoning") or ""),
        })
    return dict(per)


def in_ranges(seq: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= seq <= end for start, end in ranges)


def real_compactions(conn: sqlite3.Connection) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM events WHERE type = 'compaction/start'").fetchone()[0])


def section_scale(conn: sqlite3.Connection) -> None:
    print("== 0. 现在这套预算(config 现读,不写死)==")
    # ⚠️ 2026-10-17:总锁已删除(它不参与任何判定),这里只报真正在用的数。
    print("  对话段 %d(≈真实 %d)· 触发线 %d · 硬上限 %d(≈真实 %d)"
          % (config.BUDGET_CONVERSATION,
             int(config.BUDGET_CONVERSATION * config.TOKEN_ESTIMATE_RATIO),
             config.BUDGET_COMPACT_TRIGGER,
             config.BUDGET_HARD_MAX,
             int(config.BUDGET_HARD_MAX * config.TOKEN_ESTIMATE_RATIO)))
    print("  单轮真实上限 %d(= 窗口 %d × %d%%)· 换算比 %.1f"
          % (config.BUDGET_REQUEST_REAL, config.CONTEXT_WINDOW_TOKENS,
             int(config.SINGLE_TURN_INPUT_SHARE * 100), config.TOKEN_ESTIMATE_RATIO))
    print()
    print("== 1. 现在的账:会话、对话段、思维链 ==")
    sessions = load_sessions(conn)
    ranges: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for row in conn.execute("SELECT session_id, start_seq, end_seq FROM summary_pool"):
        ranges[row["session_id"]].append((int(row["start_seq"]), int(row["end_seq"])))
    for session_id, messages in sorted(sessions.items(), key=lambda kv: -len(kv[1])):
        live = [m for m in messages if not in_ranges(m["seq"], ranges.get(session_id, []))]
        body = sum(len(str(m["content"])) for m in live)
        chain = sum(len(str(m["reasoning_content"])) for m in live)
        span = ""
        if live:
            first, last = parse_time(live[0]["time"]), parse_time(live[-1]["time"])
            if first and last:
                span = human_span((last - first).total_seconds())
        print("  %-22s 消息 %4d | 现在还在投影里 %3d 条 / %5d 估算 / 覆盖 %-8s | 正文 %5d 字符 思维链 %5d 字符" % (
            session_id[:22], len(messages), len(live), total_tokens(live), span, body, chain))
    body_all = sum(len(str(m["content"])) for ms in sessions.values() for m in ms)
    chain_all = sum(len(str(m["reasoning_content"])) for ms in sessions.values() for m in ms)
    print("  全部会话:正文 %d 字符,思维链 %d 字符 —— 思维链是正文的 %.0f%%" % (
        body_all, chain_all, 100.0 * chain_all / max(body_all, 1)))
    print("  ⭐ 对话段买的不只是「聊过的话」:大头是思维链(它也算预算,见 config.estimate_messages_tokens)。")
    print()


def replay(messages: list[dict], limit: int, *, fixed: int, trigger: int,
           no_chain: bool) -> tuple[list[dict], int]:
    """把一个会话重放一遍,返回 (每一轮投影那一刻的可见窗口, 压缩次数)。

    顺序:user/message 到达 -> 投影 -> assistant/message -> 回合结束后按需压缩。
    """
    rows: list[dict] = []
    ranges: list[tuple[int, int]] = []
    pending = False
    compactions = 0
    seen: list[dict] = []
    for event in messages:
        if event["type"] == "user/message":
            seen.append(event)
            history = [m for m in seen if not in_ranges(m["seq"], ranges)]
            if no_chain:
                history = [dict(m, reasoning_content="") for m in history]
            kept, dropped = fit(history, limit)
            kept_tokens = total_tokens(kept)
            pending = dropped > 0 or (fixed + kept_tokens) >= trigger
            span = 0.0
            if kept:
                first, last = parse_time(kept[0]["time"]), parse_time(kept[-1]["time"])
                if first and last:
                    span = (last - first).total_seconds()
            rows.append({
                "kept": len(kept), "kept_tokens": kept_tokens, "span": span,
                "dropped": dropped,
                "dropped_chars": sum(
                    len(str(m["content"])) + len(str(m["reasoning_content"]))
                    for m in history[:dropped]),
                "kept_chars": sum(
                    len(str(m["content"])) + len(str(m["reasoning_content"])) for m in kept),
            })
            if len(rows) >= MAX_TURNS_PER_SESSION:
                break
        else:
            seen.append(event)
            if not pending:
                continue
            pending = False
            last_end = max((end for _, end in ranges), default=0)
            surface = [e for e in seen if e["seq"] > last_end and e["surface"]]
            if len(surface) <= RECENT_EVENTS_KEPT:
                continue
            candidates = surface[:-RECENT_EVENTS_KEPT]
            budget = int(limit * COMPACTION_TARGET_RATIO)
            used = 0
            end_index: int | None = None
            for index, item in enumerate(candidates):
                used += item["raw_tokens"]
                if item["type"] == "assistant/message":
                    end_index = index
                if used >= budget:
                    break
            if end_index is None or end_index < 1:
                continue
            ranges.append((candidates[0]["seq"], candidates[end_index]["seq"]))
            compactions += 1
    return rows, compactions


def section_replay(sessions: dict[str, list[dict]], limits: list[int], *,
                   fixed: int, trigger: int, no_chain: bool, real_count: int) -> None:
    heading = ("3. 重放:各上限下「模型能看到多少历史」"
               if no_chain else
               "3b. 对照:假如历史**也**回传思维链(DSH 口径;我们是照云朵不回传,见 D123)")
    print("== " + heading + " ==")
    print("  %-9s %14s %11s %9s %13s %9s %12s" % (
        "对话段上限", "可见条数 p50/p90", "可见估算 p50/p90", "覆盖率", "压缩次数", "每轮压率", "峰值可见估算"))
    for limit in limits:
        kepts: list[float] = []
        kept_tokens_list: list[float] = []
        spans: list[float] = []
        touched = 0
        turns = 0
        compactions = 0
        for messages in sessions.values():
            if len(messages) < 4:
                continue
            rows, count = replay(messages, limit, fixed=fixed, trigger=trigger, no_chain=no_chain)
            compactions += count
            for row in rows:
                turns += 1
                kepts.append(row["kept"])
                kept_tokens_list.append(row["kept_tokens"])
                spans.append(row["span"])
                if row["dropped"]:
                    touched += 1
        if not turns:
            continue
        print("  %-9d %6.0f / %-6.0f %5.0f / %-5.0f %8.0f%% %9d %8.0f%% %12.0f" % (
            limit, percentile(kepts, 0.5), percentile(kepts, 0.9),
            percentile(kept_tokens_list, 0.5), percentile(kept_tokens_list, 0.9),
            100.0 * touched / turns, compactions, 100.0 * compactions / max(turns, 1),
            max(kept_tokens_list)))
        if limit == limits[0] and not no_chain:
            print("  校准:同口径下真实库里的 compaction/start = %d 次,重放得 %d 次"
                  " —— 偏差来自重放里固定段/裁剪时机的近似。" % (real_count, compactions))
    print("  口径:照 Projector._fit_conversation 成对丢最旧 + 回合结束后 session.pick_compaction_range"
          "(桶 = 上限 x 0.5,保留最近 %d 条可见事件);固定段按 %d 估算。" % (
              RECENT_EVENTS_KEPT, fixed))
    print("        覆盖率 = 有历史被 _fit_conversation 丢掉的轮次占比;中位跨度 %.1f 小时量级见上表。" % (
        sum(spans) / max(len(spans), 1) / 3600.0))
    print()


def section_coupling(limits: list[int], fixed: int, trigger: int) -> None:
    print("== 4. 耦合:放大对话段,压缩就会每轮触发 ==")
    print("  触发条件 total >= %d,而 total = 固定段 + 对话段;" % trigger)
    print("  固定段(system + runtime + memory)实测约 %d —— 于是对话段只要大于 %d 就会**每轮都触发**。" % (
        fixed, trigger - fixed))
    print("  (加 --trigger 可以模拟「连触发线一起改」的方案。)")
    print("  ⚠️ 下表按「每段都顶满上限」的**最坏情况**算;现实里到不到,要第 3 节的重放才看得出。")
    print("  %-14s %12s %16s %s" % ("对话段上限", "合计 total", "越过触发线?", "后果"))
    for limit in limits:
        total = fixed + limit
        over = total >= trigger
        print("  %-14d %12d %16s %s" % (
            limit, total, "是" if over else "否",
            "每轮压缩:每轮多一次摘要调用 + 一个摘要桶" if over else "只在历史真溢出时压"))
    print("  ⭐ 结论:4000 这**一个**数同时当着「历史窗口」和「压缩节流阀」。只调大它 = 每轮压缩;")
    print("     要调大就得连同触发口径一起改(见 docs/决策记录.md D120/D121)。")
    print()


def section_meter(conn: sqlite3.Connection, trace_file: Path) -> None:
    print("== 5. 估算口径校准(chars/4 到底高估还是低估)==")
    if not trace_file.exists():
        print("  (没有轨迹文件)")
        return
    lines = [line for line in trace_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        print("  (轨迹文件是空的)")
        return
    record = json.loads(lines[-1])
    session_id = str(record.get("session") or "")
    print("  最近一条轨迹:%s 会话 %s 估算合计 %s(含工具 schema %s)" % (
        record.get("at"), session_id, record.get("grand_total"), record.get("tools")))
    row = conn.execute(
        "SELECT time, data FROM events WHERE session_id = ? AND type = 'turn/end'"
        " ORDER BY time DESC LIMIT 1", (session_id,)
    ).fetchone()
    if row is None:
        print("  (这个会话没有 turn/end 实测 usage)")
        return
    usage = json.loads(row["data"] or "{}").get("usage") or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    estimated = int(record.get("grand_total") or 0)
    if prompt and estimated:
        print("  同一轮 API 实测 prompt_tokens: %d(时间 %s)" % (prompt, row["time"]))
        print("  ⭐ 实测 / 估算 = %.2f 倍 —— 对中文这是**低估**,不是 config.estimate_tokens" % (
            prompt / estimated))
        print("     注释里写的「刻意保守(高估)」。")
    print("  注意:实测值里还含这一轮自己产生的工具结果,所以这个比值是**下限**。")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="对话窗口预算分析(只读)")
    parser.add_argument("--db", default=str(BASE_DIR / "data" / "memory.db"))
    parser.add_argument("--trace", default=str(BASE_DIR / "data" / "trace" / "context.jsonl"))
    parser.add_argument("--limits", default="4000,24000,128000")
    parser.add_argument("--fixed", type=int, default=DEFAULT_FIXED)
    parser.add_argument("--trigger", type=int, default=0,
                        help="压缩触发线;0 = 用 config 里那一套(总锁 x 比例)")
    args = parser.parse_args()

    db = Path(args.db)
    if not db.exists():
        print("找不到记忆库: " + str(db))
        return 1
    limits = [int(item) for item in str(args.limits).split(",") if item.strip()]
    trigger = int(args.trigger) or int(
        config.BUDGET_COMPACT_TRIGGER)
    conn = open_ro(db)
    try:
        section_scale(conn)
        sessions = load_sessions(conn)
        real_count = real_compactions(conn)
        # ⭐ 生产口径在前(历史**不回传**思维链,见 D123);带思维链的当对照组放后面
        section_replay(sessions, limits, fixed=args.fixed, trigger=trigger,
                       no_chain=True, real_count=real_count)
        section_replay(sessions, limits, fixed=args.fixed, trigger=trigger,
                       no_chain=False, real_count=real_count)
        section_coupling(limits, args.fixed, trigger)
        section_meter(conn, Path(args.trace))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
