"""自我观察 v0:**行为视角** —— 把事件日志压成一份「它自己的行为账」。

> 2026-10-16 用户拍板:*「先做行为吧」*。设计评审见 docs/决策记录.md D138。

为什么不是"让它读自己的代码":

  * 代码与文档说的是**应该怎么设计**(那是主人的信念),
    而行为数据说的是**实际发生了什么**(那是事实);
  * 它读文档再复述一遍,得到的不是"发现",是**回声** —— 比没有更糟;
  * 而行为数据(哪个工具失败、哪类问题被重问、压缩发生了几次)是它**没法从文档里抄到的**。

⭐ 三条纪律(每一条都对着一种真实的翻车方式):

  1. **只报事实,不报判断** —— 判断由模型在回复里下,并说明依据;
     对自己的评价**绝不写进 facts**(否则三个月后记忆里全是自我暗示,而那不是事实);
  2. **"被挡住" ≠ "坏了"** —— 用户拒绝确认(``NEEDS_CONFIRMATION``)与真正报错
     (``HANDLER_ERROR``)在 ``tool/result`` 里都是 ok=false。第一版评审时发现:
     若不分档,``expense_confirm`` 会被报成"2 次调用 2 次失败、成功率 0%",
     模型很可能得出"这个工具坏了"—— 实际上它在等主人确认。**误诊比不诊断更糟。**
  3. **每个计数都带分母** —— "失败 4 次"必须写成"4 / 22 次";
     没有分母的数字会被读成灾难。

⚠️ 为什么住在 ``la/agent/`` 而不是 ``la/memory/``(与最初计划不同):
    它要读三样东西 —— 事件日志(memory.db)、写入回执(assistant.db)、
    最近几轮的轨迹(``data/trace``,那是 agent 层的东西)。
    放进 memory 层就得反向 import agent,**分层会脏**;放这里,依赖方向是 agent → memory,与现状一致。

⚠️ 长度:返回给模型的 digest 控制在 ``MAX_DIGEST_CHARS`` 以内(默认 1500 字符)。
    注意 ``registry.clip_tokens`` 是按 **4 字符/token** 换算的英文口径,
    中文实际 ~1–1.5 字符/token —— 所以这里**按字符**卡,不指望 token 预算兜底。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence

from .. import config

#: digest 硬上限(字符)。超过就按"先砍列表、再砍次要段落"的固定顺序收。
MAX_DIGEST_CHARS = 1500

#: 错误码分档(2026-10-16 评审定的六类 → 五桶;2026-11-17 加"参数错")。
#: ⚠️ 未知码一律当"真故障"(宁可误报,不可漏报——漏报会让它以为一切正常)。
CONFIRM_CODES: tuple[str, ...] = ("NEEDS_CONFIRMATION",)
POLICY_CODES: tuple[str, ...] = ("FORBIDDEN", "READ_ONLY")
#: 参数写错 ≠ 工具坏了(2026-11-17 真机:模型把"今天"写成 2026-11-16,每天日报一次)。
BAD_PARAM_CODES: tuple[str, ...] = ("BAD_PARAMS",)
BUDGET_CODES: tuple[str, ...] = (
    "PHASE_BUDGET_EXCEEDED", "TOTAL_STEPS_EXCEEDED", "DUPLICATE_CALL",
)
FAULT_CODES: tuple[str, ...] = ("TIMEOUT", "HANDLER_ERROR")

#: 每个窗口里最多列几个"真故障工具"/"从未调用的工具"。
TOP_FAULTS = 3
NEVER_CALLED_SHOWN = 6


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _json(value: Any) -> dict[str, Any]:
    try:
        payload = json.loads(str(value or "{}"))
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _bucket(code: str) -> str:
    """错误码 -> 哪一个桶。空码 = 成功。"""
    text = str(code or "").strip().upper()
    if not text:
        return "ok"
    if text in CONFIRM_CODES:
        return "confirm"
    if text in POLICY_CODES:
        return "policy"
    if text in BUDGET_CODES:
        return "budget"
    if text in BAD_PARAM_CODES:
        return "bad_params"
    return "fault"


def _rate(hit: int, miss: int) -> float:
    total = hit + miss
    return (hit / total) if total else 0.0


def _thousands(value: int) -> str:
    return format(int(value), ",")


def build_behavior_digest(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    window_days: int = 7,
    session_id: str | None = None,
    catalog: Sequence[str] | None = None,
    trace_records: Iterable[dict[str, Any]] | None = None,
    assistant_conn: sqlite3.Connection | None = None,
) -> str:
    """把最近 ``window_days`` 天的行为压成一份给模型看的账。

    ⚠️ 同一个 ``now`` 调两次必须字节相同(``tests/test_self_view.py`` 钉着):
       否则模型两轮里看到两个不同的"现在的自己",会以为自己变了。
    """
    days = max(1, min(int(window_days or 7), 30))
    today = now.date()
    first = today - timedelta(days=days - 1)
    start_text = first.isoformat()
    end_text = (today + timedelta(days=1)).isoformat()
    scope_sql = ""
    params: list[Any] = [start_text, end_text]
    if session_id:
        scope_sql = " AND session_id = ?"
        params.append(str(session_id))

    # -- 1) 事件类型计数(一次扫完,后面各段都从这里取)
    counts: dict[str, int] = {}
    for row in conn.execute(
        "SELECT type, COUNT(*) AS n FROM events WHERE time >= ? AND time < ?"
        + scope_sql + " GROUP BY type",
        params,
    ):
        counts[str(row["type"])] = _int(row["n"])

    # -- 2) token 账(llm/usage:每次模型调用一条,D136 起)
    prompt_tokens = hit_tokens = miss_tokens = out_tokens = 0
    usage_calls = 0
    usage_first = ""
    for row in conn.execute(
        "SELECT time, data FROM events WHERE time >= ? AND time < ?"
        + scope_sql + " AND type = 'llm/usage' ORDER BY time",
        params,
    ):
        payload = _json(row["data"])
        usage_calls += 1
        prompt_tokens += _int(payload.get("prompt_tokens"))
        hit_tokens += _int(payload.get("cache_hit_tokens"))
        miss_tokens += _int(payload.get("cache_miss_tokens"))
        out_tokens += _int(payload.get("completion_tokens"))
        if not usage_first:
            usage_first = str(row["time"])[:16].replace("T", " ")

    # -- 3) 工具账(只算模型自己发起的 via=model)
    buckets = {
        "ok": 0, "confirm": 0, "policy": 0, "budget": 0, "bad_params": 0, "fault": 0,
    }
    per_tool: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT data FROM events WHERE time >= ? AND time < ?"
        + scope_sql + " AND type = 'tool/result'",
        params,
    ):
        payload = _json(row["data"])
        if str(payload.get("via") or "") != "model":
            continue
        name = str(payload.get("tool") or "?")
        bucket = _bucket(str(payload.get("error_code") or ""))
        buckets[bucket] += 1
        slot = per_tool.setdefault(name, {"calls": 0, "fault": 0})
        slot["calls"] += 1
        if bucket == "fault":
            slot["fault"] += 1

    tool_total = sum(buckets.values())
    faults = sorted(
        ((name, slot) for name, slot in per_tool.items() if slot["fault"] > 0),
        key=lambda item: (-item[1]["fault"], -item[1]["calls"], item[0]),
    )[:TOP_FAULTS]
    fault_text = ", ".join(
        name + " " + str(slot["fault"]) + "/" + str(slot["calls"]) for name, slot in faults
    ) or "无"

    never_called: list[str] = []
    for name in catalog or ():
        text = str(name)
        if text and per_tool.get(text, {}).get("calls", 0) == 0:
            never_called.append(text)
    never_called.sort()

    # -- 4) 上下文行为:压缩 / 轨迹里丢过消息吗
    compactions = counts.get("compaction/summary", 0)
    skipped = counts.get("compaction/skipped", 0)
    dropped = 0
    capped = 0
    for record in trace_records or ():
        dropped += _int(record.get("dropped_turns"))
        if bool(record.get("capped")):
            capped += 1

    # -- 5) 写入行为:回执 / 撤销 / 说了没写
    #    ⚠️ 没拿到领域库(assistant.db)就写"—",不写 0 ——
    #       0 会被读成"这段时间一次都没写过",而真实情况是"没读到"。
    receipts = undone = 0
    receipts_available = assistant_conn is not None
    if assistant_conn is not None:
        try:
            row = assistant_conn.execute(
                "SELECT COUNT(*) AS n, SUM(CASE WHEN undone_at IS NOT NULL"
                " THEN 1 ELSE 0 END) AS u FROM ops_receipts"
                " WHERE substr(ts, 1, 10) >= ? AND substr(ts, 1, 10) < ?",
                (start_text, end_text),
            ).fetchone()
            receipts = _int(row["n"]) if row is not None else 0
            undone = _int(row["u"]) if row is not None else 0
        except sqlite3.Error:
            receipts = undone = 0
    claimed = counts.get("turn/write-claim-without-write", 0)

    # -- 6) 拼装(先全写,再按预算收)
    scope_label = ("会话 " + session_id) if session_id else "全部会话"
    head = (
        "[自我观察 · 行为] 窗口 " + start_text + "~" + today.isoformat()
        + "(" + str(days) + " 天)· " + scope_label
        + " · as_of " + now.strftime("%Y-%m-%d %H:%M")
    )
    throughput = (
        "吞吐   轮次 " + str(counts.get("user/message", 0))
        + " · 模型调用 " + str(usage_calls)
        + " · 输出 token " + _thousands(out_tokens)
    )
    cache = (
        "缓存   命中 %.1f%%(%s/%s)"
        % (100.0 * _rate(hit_tokens, miss_tokens),
           _thousands(hit_tokens), _thousands(hit_tokens + miss_tokens))
    )
    if not usage_calls:
        cache += "  ⚠️ 这段没有 token 账(D136 起才有)"
    elif usage_first and usage_first[:10] > start_text:
        cache += "  ⚠️ token 账只从 " + usage_first + " 起有"
    tools_line = (
        "工具   共 " + str(tool_total) + " 次:正常 " + str(buckets["ok"])
        + " / 等确认 " + str(buckets["confirm"])
        + " / 被策略挡 " + str(buckets["policy"])
        + " / 预算拒 " + str(buckets["budget"])
        + " / 参数错 " + str(buckets["bad_params"])
        + " / 真故障 " + str(buckets["fault"])
    )
    fault_line = "       真故障最多: " + fault_text
    never_line = ""
    if never_called:
        shown = "、".join(never_called[:NEVER_CALLED_SHOWN])
        more = len(never_called) - NEVER_CALLED_SHOWN
        if more > 0:
            shown += " 等 " + str(len(never_called)) + " 个"
        never_line = ("       " + "从未调用: " + shown)
    context_line = (
        "上下文 压缩 " + str(compactions) + " 次"
        + (("、放弃 " + str(skipped) + " 次") if skipped else "")
        + " · 轨迹里丢消息 " + str(dropped) + " 条"
        + " · 硬上限收紧 " + str(capped) + " 轮"
    )
    if receipts_available:
        write_line = (
            "写入   回执 " + str(receipts) + " 条 · 撤销 " + str(undone) + " 条"
            + " · 写了却说没成功 " + str(claimed) + " 次"
        )
    else:
        write_line = ("写入   回执 —(没读到领域库)· 写了却说没成功 "
                      + str(claimed) + " 次")
    footer = (
        "⚠️ 以上是事件日志里的**事实**,不是结论。"
        "判断请写进回复(带依据);**不要把对自己的评价写进事实/记忆**。"
    )

    blocks = [head, throughput, cache, tools_line, fault_line]
    if never_line:
        blocks.append(never_line)
    blocks.extend([context_line, write_line, "", footer])
    text = "\n".join(blocks)

    # 超长时的收法:先砍"从未调用"的名单,再砍"上下文/写入"两行 —— 顺序固定,结果可复现。
    if len(text) > MAX_DIGEST_CHARS and never_line:
        blocks = [item for item in blocks if item != never_line]
        text = "\n".join(blocks)
    if len(text) > MAX_DIGEST_CHARS:
        blocks = [item for item in blocks if item not in (context_line, write_line)]
        text = "\n".join(blocks)
    if len(text) > MAX_DIGEST_CHARS:
        text = text[: MAX_DIGEST_CHARS - 20].rstrip() + chr(10) + "…(已截断)"
    return text


__all__ = [
    "MAX_DIGEST_CHARS",
    "build_behavior_digest",
]
