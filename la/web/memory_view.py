"""记忆页:把**每一层记忆**摊开给人看。

为什么要专门做这个页面:
  * 记忆系统是本项目最难"信任"的部分 —— 用户看不到它记了什么,就只能靠猜;
  * 而"看不到"恰恰是最危险的:一条错的永久事实会**污染之后每一轮对话**,
    却不会有任何报错,只会让回答越来越离谱;
  * 三层数据分离(归档 / 镜像 / 派生)在设计里写得很清楚,
    但**分离本身不产生信任,可核对才产生信任**(同 D59)。

⭐ 这个页面**完全不需要模型**:读的全是本地表和本地文件。
   关掉 API 也照样能看、能核对。

层次按**数据流向**排列 —— 从最原始、最不可重建的一层,到模型这一轮真正看到的东西:

    L0  归档 JSONL   事实源,删了就没了
    L1  事件镜像     events 表,可从 L0 重放重建
    L1b 事件类型分布 一眼看出"记了什么、没记什么"
    L2  摘要池       summary_pool,派生索引
    L3  永久事实     facts,矛盾时旧条目置 active=0 而不是删除
    L3c 事实候选     fact_candidates,入库前的闸门:谁提的、裁成什么、为什么
    L4  热摘要注入   按热度渲染出的 <memory> 块
    L5  上下文预算与压缩  五个段各占多少 token、什么时候压、压完留多少
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from ..memory.projector import build_memory_block
from ..memory.store import FactRow, MemoryStore, SummaryRow

# 中文引号。直接写字面量的话,和 Python 的双引号字符串会打架。
LQ = chr(0x201C)
RQ = chr(0x201D)
Q = chr(0x22)   # 半角双引号,给命令示例用


@dataclass
class Layer:
    """一层记忆。"""

    code: str                 # L0 … L5
    name: str                 # 人看的名字
    source: str               # 住在哪(文件路径 / 表名 / 内存)
    rebuildable: bool         # 丢了能不能从下层重建
    note: str                 # 这一层是什么、为什么这样设计
    stats: list[tuple[str, str]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    raw: str = ""             # 原文块(L4 用)
    empty_hint: str = ""      # 这一层空的时候,告诉用户怎么让它有内容
    #: 结构化条目(目前只有 L2 摘要用):表格塞不下的东西按"标题 + 正文"给出来。
    #: 模板有 items 就渲染成块,没有就照旧渲染表格 —— 别的层一个字不用改。
    items: list[dict[str, object]] = field(default_factory=list)
    #: 一句话结论(目前只有 L5 用:什么时候压、压完留多少)。模板把它渲染成一条醒目横条。
    headline: str = ""
    #: 与 rows **一一对应**的每行动作(目前只有 L3c 用):"review" = 这一行在等人放行,
    #: 模板据此渲染「放行 / 拒收」按钮;空串 = 这一行没有人工动作。
    #: ⚠️ 为什么单独一列而不是塞进 rows:rows 是**给人看的文本单元格**,
    #:    往里塞状态会让"表格某一列突然变成按钮"这种耦合藏进字符串比较里。
    row_actions: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.rows and not self.raw


@dataclass
class MemoryView:
    layers: list[Layer]
    generated_at: str = ""
    total_events: int = 0
    total_active_facts: int = 0
    total_summaries: int = 0


def _fmt_bytes(size: int) -> str:
    if size < 1024:
        return str(size) + " B"
    if size < 1024 * 1024:
        return str(round(size / 1024, 1)) + " KB"
    return str(round(size / 1024 / 1024, 2)) + " MB"


def _clip(text: object, limit: int = 160) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# ==========================================================================
# L2 摘要:把结构化正文拆成"标题行 + 内容行 + 展开后的分栏"
# ==========================================================================
# ⚠️ 摘要正文长这样(一行一个字段),**不是**一段散文:
#       <compacted-summary>
#       - 时段:2026-10-12T20:17:52+08:00 — 2026-10-12T20:51:38+08:00
#       - 事项:…;…;…
#       - 事实:(none)
#       ...
#       </compacted-summary>
#   以前整块塞进表格最后一格,七列一挤就成了"信息缩在一起" —— 可读性差是必然的。
#: ⚠️ 早期摘要写的是「事件」,后来的写「事项」—— 两个都收,统一显示成「事项」。
_SUMMARY_FIELDS = ("事项", "事件", "事实", "偏好", "待办", "锚点", "其他")
_SUMMARY_ALIAS = {"事件": "事项"}


_TIME_ONLY = re.compile(r"^[-—\s]*时段\s*[:：]")


def strip_summary_tags(text: object) -> str:
    """洗掉摘要正文里的 <compacted-summary> 壳。

    ⚠️ 有几条旧摘要把一段**没洗完的**原文嵌进了「事实」字段里(memory 里的 #17),
       壳会原样显示在预览上。这里只去掉标签本身,不动正文。
    """
    return (
        str(text or "")
        .replace("<compacted-summary>", " ")
        .replace("</compacted-summary>", " ")
        .strip()
    )


def parse_summary_fields(text: object) -> dict[str, str]:
    """把 <compacted-summary> 那块拆成 {字段: 值}。认不出的行直接忽略。"""
    fields: dict[str, str] = {}
    for line in strip_summary_tags(text).splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("- "):
            line = line[2:].strip()
        for sep in (":", "："):
            if sep in line:
                key, _, value = line.partition(sep)
                fields.setdefault(key.strip(), value.strip())
                break
    return fields


def _split_summary_value(value: object) -> list[str]:
    parts = [part.strip() for part in re.split(r"[;；]", str(value or ""))]
    # 时段已经在标题行里了;嵌在字段里的"时段:…"片段是旧数据带的噪声,不当正文显示。
    return [
        part for part in parts
        if part and part != "(none)" and not _TIME_ONLY.match(part)
    ]


def _dedupe(items: list[str]) -> list[str]:
    """同一条摘要里重复的条目只留一次。

    ⚠️ 摘要里常见「[发来 1 个附件]」连着出现两次(那是两件事被压进同一桶),
       原样铺开会让人以为附件真的发了两次。
    """
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def _summary_when(start: object, end: object) -> str:
    """2026-10-12T20:17:52+08:00 → 10-12 20:17(跨天时才再报一次日期)。"""
    first_raw, last_raw = str(start or ""), str(end or "")
    if len(first_raw) < 16:
        return first_raw
    first = first_raw[5:10] + " " + first_raw[11:16]
    if len(last_raw) < 16:
        return first
    if last_raw[5:10] == first_raw[5:10]:
        return first + " → " + last_raw[11:16]
    return first + " → " + last_raw[5:10] + " " + last_raw[11:16]


def _summary_item(row: sqlite3.Row) -> dict[str, object]:
    """一条摘要 → 给模板用的标题行 / 内容行 / 分栏正文。"""
    fields = parse_summary_fields(row["text"])
    # 同名(含旧名映射后同名)的字段合并,免得「事项」与「事件」在两行里各出现一次。
    merged: dict[str, list[str]] = {}
    for key in _SUMMARY_FIELDS:
        value = fields.get(key, "")
        if not value or value.strip() == "(none)":
            continue
        if key == "锚点":
            entries = [
                part.strip()
                for part in re.split(r"[,，]", value.strip().strip("[]"))
                if part.strip()
            ]
        elif key == "其他":
            entries = [value.strip()]
        else:
            entries = _split_summary_value(value)
        if entries:
            merged.setdefault(_SUMMARY_ALIAS.get(key, key), []).extend(
                entries if key == "锚点" else _dedupe(entries)
            )
    sections: list[dict[str, object]] = [
        {"key": key, "entries": entries} for key, entries in merged.items()
    ]
    matters = next((item["entries"] for item in sections if item["key"] == "事项"), [])
    # 预览优先取"事项"(那才是这一桶里真正发生了什么);
    # 没有就退到第一个非空字段;再没有才回正文 —— 但要把 <compacted-summary> 外壳洗掉。
    preview = " · ".join(str(x) for x in _dedupe(matters))
    if not preview and sections:
        preview = " · ".join(str(x) for x in sections[0]["entries"])
    if not preview:
        preview = _clip(strip_summary_tags(row["text"]), 110)
    heat = row["heat_cache"]
    return {
        "id": "#" + str(row["id"]),
        "when": _summary_when(row["start_time"], row["end_time"]),
        "range": str(row["start_seq"]) + "–" + str(row["end_seq"]),
        "heat": ("%.2f" % float(heat)) if heat is not None else "—",
        "visits": str(row["visits"]),
        "category": str(row["category"]),
        "preview": preview,
        "sections": sections,
    }


def _fact_rows(rows) -> list[FactRow]:
    """⭐ 不能把 sqlite3.Row 直接喂给 build_memory_block ——
    它用的是 row.content / row.text 属性访问,而 Row 只支持下标。"""
    out: list[FactRow] = []
    for row in rows:
        try:
            keywords = json.loads(row["keywords"] or "[]")
        except (json.JSONDecodeError, TypeError):
            keywords = []
        out.append(FactRow(
            id=int(row["id"]), type=str(row["type"]), category=str(row["category"]),
            content=str(row["content"]), keywords=keywords,
            active=bool(row["active"]), created_at=str(row["created_at"]),
            confidential=bool(row["confidential"]),
        ))
    return out


def layer_archive(archive_dir: Path | None = None) -> Layer:
    """L0:归档 JSONL —— 唯一的事实源。"""
    root = Path(archive_dir or config.ARCHIVE_DIR)
    layer = Layer(
        code="L0",
        name="归档 JSONL(事实源)",
        source=str(root),
        rebuildable=False,
        note="这里删了就真没了。memory.db 里的一切都能从这里重放重建,反过来不行。",
        columns=["文件", "事件数", "最大序号", "大小", "最后写入"],
        empty_hint="还没发生过任何对话。跑一句 "
                   + "`python -m la --once " + Q + "你好" + Q + "` 就会有第一条。",
    )
    files = sorted(root.glob("*.jsonl")) if root.exists() else []
    total = 0
    for item in files:
        count = 0
        last_seq = 0
        last_time = ""
        try:
            with item.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    count += 1
                    last_seq = max(last_seq, int(payload.get("seq") or 0))
                    last_time = str(payload.get("time") or last_time)
        except OSError:
            continue
        total += count
        layer.rows.append([
            item.name, str(count), str(last_seq),
            _fmt_bytes(item.stat().st_size), last_time[:19] or "—",
        ])
    layer.stats = [("文件", str(len(files))), ("事件总数", str(total))]
    return layer


def layer_events(conn: sqlite3.Connection) -> Layer:
    """L1:事件镜像 —— 可从归档重放。"""
    layer = Layer(
        code="L1",
        name="事件镜像(events 表)",
        source="memory.db · events",
        rebuildable=True,
        note="只是索引,不是事实源。DROP 掉之后从 L0 重放,能一模一样地长回来。",
        columns=["会话", "事件数", "进上下文", "不进上下文", "时间跨度"],
        empty_hint="events 表还是空的。",
    )
    rows = conn.execute(
        "SELECT session_id, COUNT(*) AS n, SUM(surface) AS surfaced,"
        " MIN(time) AS first_time, MAX(time) AS last_time"
        " FROM events GROUP BY session_id ORDER BY n DESC"
    ).fetchall()
    total = 0
    for row in rows:
        count = int(row["n"])
        total += count
        surfaced = int(row["surfaced"] or 0)
        layer.rows.append([
            str(row["session_id"]), str(count), str(surfaced), str(count - surfaced),
            str(row["first_time"])[:10] + " → " + str(row["last_time"])[:10],
        ])
    layer.stats = [("会话", str(len(rows))), ("事件总数", str(total))]
    return layer


def layer_event_types(conn: sqlite3.Connection) -> Layer:
    """L1b:事件按类型分布。"""
    layer = Layer(
        code="L1b",
        name="事件类型分布",
        source="memory.db · events",
        rebuildable=True,
        note="如果某一类一直是 0,说明那条路径从没被走过 —— 这比报错更难发现。",
        columns=["事件类型", "条数", "最新一条"],
        empty_hint="还没有任何事件。",
    )
    for row in conn.execute(
        "SELECT type, COUNT(*) AS n, MAX(time) AS last_time FROM events"
        " GROUP BY type ORDER BY n DESC"
    ).fetchall():
        layer.rows.append([
            str(row["type"]), str(row["n"]), str(row["last_time"])[:19] or "—",
        ])
    return layer


def layer_summaries(conn: sqlite3.Connection) -> Layer:
    """L2:摘要池。"""
    layer = Layer(
        code="L2",
        name="摘要池(summary_pool)",
        source="memory.db · summary_pool",
        # ⚠️ 2026-10-12 一致性审核:它**不可重建** —— compaction/summary 事件里只有桶与 id、
        #    没有摘要正文,归档里重放不出摘要。丢了只能从备份恢复。见 D93。
        rebuildable=False,
        note="巩固出来的长时记忆。heat 决定它在预算紧张时会不会被淘汰 —— 越常用的越不容易丢。"
             "⚠️ 这一层**不能从归档重放**(归档里只有'压过哪一段',没有摘要正文),"
             "所以它和归档一样要靠备份保住。",
        columns=["#", "区间", "时间", "热度", "看过", "类别", "内容"],
        empty_hint="摘要池不会凭空长出来:对话攒够轮次后由巩固把一段事件压成摘要。"
                   "想立刻看效果,跑 " + "`python -m la --compact`。",
    )
    rows = conn.execute(
        "SELECT * FROM summary_pool ORDER BY COALESCE(heat_cache, 0) DESC, id DESC"
    ).fetchall()
    for row in rows:
        heat = row["heat_cache"]
        layer.rows.append([
            "#" + str(row["id"]),
            str(row["start_seq"]) + "–" + str(row["end_seq"]),
            str(row["start_time"])[:10] + " → " + str(row["end_time"])[:10],
            ("%.2f" % float(heat)) if heat is not None else "—",
            str(row["visits"]),
            str(row["category"]),
            _clip(row["text"]),
        ])
    # 表格那份留着(折线/接口兼容),另外给一份**可读的**结构化条目 ——
    # 模板优先用 items,渲染成"标题 + 内容 + 展开分栏"。
    layer.items = [_summary_item(row) for row in rows]
    layer.stats = [("摘要", str(len(rows)))]
    return layer


def injected_fact_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """**这一轮真正会进上下文的**那几条事实。

    与 `MemoryStore.active_facts(limit=FACT_MAX_ACTIVE)` 是同一条 SQL —— 刻意重复一遍
    而不是 import 它,是因为导入 MemoryStore 会连带建 FTS 索引(写操作),
    而这一页必须严格只读(见 `test_view_does_not_write_anything`)。
    """
    return conn.execute(
        "SELECT * FROM facts WHERE active = 1 ORDER BY id DESC LIMIT ?",
        (config.FACT_MAX_ACTIVE,),
    ).fetchall()


def facts_injection_tokens(conn: sqlite3.Connection) -> int:
    """L3 每轮固定吃掉多少 token(与 projector 里的渲染口径一致:敏感只算占位符)。"""
    text = "<facts>\n" + "\n".join(
        "- " + (config.SECRET_PLACEHOLDER + "(#" + str(row["id"]) + " 已标记为敏感)"
               if row["confidential"] else str(row["content"]))
        for row in injected_fact_rows(conn)
    ) + "\n</facts>"
    return config.estimate_tokens(text)


def layer_facts(conn: sqlite3.Connection) -> Layer:
    """L3:永久事实。"""
    layer = Layer(
        code="L3",
        name="永久事实(facts)",
        source="memory.db · facts",
        # ⚠️ 事件里只有 content(类别/敏感标记/失效状态会丢),重放只能得到近似结果 ——
        #    所以按"不可重建"标注,不给一个说不清的结果(同 D64 的取向)。
        rebuildable=False,
        note="每条都**每一轮**进上下文,所以一条错的事实会持续污染之后所有对话。"
             "矛盾时两条都留着、旧的那条置为已取代 —— 不删除,是为了还能追查"
             + LQ + "它当时为什么这么答" + RQ + "。"
             "⚠️ 这一层同样**不能从归档精确重建**(事件里只有 content,"
             "类别/敏感标记/失效状态会丢),所以它也要靠备份保住。"
             + LQ + "本轮注入" + RQ + "列是**模型这一轮到底读到了哪几条** —— "
             "超上限时最旧的会被挤出去,而以前页面上看不出这件事。",
        columns=["#", "类型", "状态", "敏感", "本轮注入", "内容", "写入时间"],
        empty_hint="还没有长期事实。在聊天里说「记住:我不吃香菜」就会写进来一条(它会先过 L3c 的闸)。",
    )
    rows = conn.execute(
        "SELECT * FROM facts ORDER BY active DESC, id DESC"
    ).fetchall()
    # ⭐ 注入名单取的是**投影真正会用的那一份**(同一个查询),不另算一遍:
    #    两份算法迟早会漂移,而漂移的表现正是"页面说有、模型没看到"。
    injected = {int(row["id"]) for row in injected_fact_rows(conn)}
    active = 0
    confidential = 0
    for row in rows:
        is_active = bool(row["active"])
        active += 1 if is_active else 0
        confidential += 1 if row["confidential"] else 0
        if is_active:
            state = "生效中"
        elif row["superseded_by"]:
            state = "已被 #" + str(row["superseded_by"]) + " 取代"
        else:
            state = "已停用"
        if not is_active:
            injected_cell = "—"
        elif int(row["id"]) in injected:
            injected_cell = "是"
        else:
            injected_cell = "✗ 被挤掉"
        layer.rows.append([
            "#" + str(row["id"]), str(row["type"]), state,
            "是" if row["confidential"] else "",
            injected_cell,
            _clip(row["content"], 200),
            str(row["created_at"])[:19],
        ])
    capacity = len(injected)
    evicted = max(active - capacity, 0)
    layer.stats = [
        ("事实总数", str(len(rows))),
        ("生效中", str(active)),
        ("已取代/失效", str(len(rows) - active)),
        ("标记为敏感", str(confidential)),
        # ⭐ 这一条是新增的:以前只看得到"有多少条",看不到"模型这一轮读到了几条"。
        ("本轮注入", str(capacity) + " / 上限 " + str(config.FACT_MAX_ACTIVE)),
        ("每轮成本", str(facts_injection_tokens(conn)) + " token"),
    ]
    if evicted:
        layer.note += (
            "⚠️ 有 " + str(evicted) + " 条生效事实**没进这一轮的上下文**"
            "(注入只取最新的 " + str(config.FACT_MAX_ACTIVE) + " 条)。"
            "页面上标着「被挤掉」的那些,模型读不到。"
        )
    return layer


def layer_fact_candidates(conn: sqlite3.Connection) -> Layer:
    """L3c:事实候选 —— 入库前的闸门。

    ⭐ 这一层存在的理由与 L1b 一样:一条路径**从来没被走过**时,不会有任何报错。
       候选表里如果长期只有 rejected 没有 admitted,或者 verdict 全是 review,
       那说明裁判(模型)或闸门本身坏了 —— 这只有摊开才看得出来。
    """
    layer = Layer(
        code="L3c",
        name="事实候选(fact_candidates,入库前的闸门)",
        source="memory.db · fact_candidates",
        rebuildable=False,
        note="规则与模型**只能提议**,写进 facts 必须过这道闸:先做确定性检查"
             "(空/残句/疑问句/超长/未来动作),再由模型判关系"
             "(新 / 重复 / 细化 / 矛盾),判不出来就留在这里待人放行 —— "
             "「放行」直接写进 facts(留事件 + 可撤销回执),「拒收」则不再入库。"
             "在微信端也可以放行:跟助手说「通过吧」,它调 memory_review 走同一条路。"
             "⚠️ 候选**不进上下文** —— 只有 admitted 的那些会成为事实。",
        columns=["#", "来路", "状态", "候选内容", "裁决", "说明", "时间"],
        empty_hint="还没有任何候选。对助说说一句「记住:我不吃香菜」就会有一条。",
    )
    rows = conn.execute(
        "SELECT * FROM fact_candidates ORDER BY id DESC LIMIT 60"
    ).fetchall()
    counts = {"pending": 0, "admitted": 0, "rejected": 0, "review": 0}
    state_label = {
        "pending": "待裁决", "admitted": "已入库", "duplicate": "已合并",
        "rejected": "已拒收", "review": "待人工放行",
    }
    source_label = {"rule": "规则", "memory_note": "模型", "human": "人工"}
    for row in rows:
        status = str(row["status"])
        counts[status] = counts.get(status, 0) + 1
        try:
            verdict = json.loads(row["verdict"] or "{}")
        except (json.JSONDecodeError, TypeError):
            verdict = {}
        rel = str(verdict.get("relationship") or verdict.get("guard") or "")
        if rel == "guard":
            rel = str(verdict.get("guard") or "")
        layer.rows.append([
            "#" + str(row["id"]),
            source_label.get(str(row["source"]), str(row["source"])),
            state_label.get(status, status),
            _clip(row["content"], 90),
            rel or "—",
            _clip(row["reason"], 90),
            str(row["created_at"])[:19],
        ])
        # ⭐ 2026-10-25:待人工放行的那几行要有**按钮** —— 用户原话是
        #    "图2是网页端的显示,用户也没有审核放行的按钮"。在此之前这一层只有读。
        layer.row_actions.append("review" if status == "review" else "")
    layer.stats = [
        ("候选总数", str(len(rows))),
        ("待裁决", str(counts.get("pending", 0))),
        ("已入库", str(counts.get("admitted", 0))),
        ("已合并", str(counts.get("duplicate", 0))),
        ("已拒收", str(counts.get("rejected", 0))),
        ("待人工放行", str(counts.get("review", 0))),
    ]
    return layer


def layer_hot_block(conn: sqlite3.Connection, now_text: str = "") -> Layer:
    """L4:按热度渲染出的 <memory> 块。"""
    layer = Layer(
        code="L4",
        name="热摘要注入块(注入用的 memory 段)",
        source="内存中拼装,每轮重建",
        rebuildable=True,
        note="⭐ 这才是模型真正读到的那段记忆。真实对话时由检索决定选哪几条;"
             "这里展示的是" + LQ + "没有任何检索线索时" + RQ + "按热度取前 N 条的结果,",
        empty_hint="没有可注入的记忆(事实与摘要都还是空的)。",
    )
    fact_rows = conn.execute(
        "SELECT * FROM facts WHERE active = 1 ORDER BY id DESC LIMIT ?",
        (config.HOT_SUMMARY_MAX_ITEMS,),
    ).fetchall()
    summary_rows = conn.execute(
        "SELECT * FROM summary_pool ORDER BY COALESCE(heat_cache, 0) DESC LIMIT ?",
        (config.HOT_SUMMARY_MAX_ITEMS,),
    ).fetchall()
    facts = _fact_rows(fact_rows)
    summaries = [SummaryRow.from_row(row) for row in summary_rows]
    status = "hit" if (facts or summaries) else "miss"
    layer.raw = build_memory_block(
        facts=facts, summaries=summaries, status=status, now_text=now_text,
    )
    layer.stats = [
        ("状态", "命中" if status == "hit" else "未命中"),
        ("事实", str(len(facts))),
        ("摘要", str(len(summaries))),
        ("估算 token", str(config.estimate_tokens(layer.raw))),
        ("预算", str(config.BUDGET_HOT_SUMMARY)),
    ]
    return layer


def layer_projection(conn: sqlite3.Connection | None = None) -> Layer:
    """L5:上下文预算与压缩 —— 各段多少、什么时候压。

    ⭐ 用户 2026-10-17:*"我只要控什么时候压缩就行了,硬上限、总锁这些东西到底有什么用……
       不要污染页面。"* 所以这一层只留两件事:
         * 模型这一轮的上下文由**哪几段**组成(各段上限);
         * **什么时候压缩**(触发线 + 压完保留比例),一句话摆在最上面。

    ⚠️ 单轮上限(窗口 × 30%)、硬上限、总锁**不再搬到这一页**:
        * 总锁已经不参与任何判定(D134),已从代码与页面删除;
        * 硬上限是装配时的兜底(对话段预算取三者最小),它属于
          「轨迹 → 规则」那份装配说明,不是这一页要控的旋钮。
    """
    trigger = config.BUDGET_COMPACT_TRIGGER
    retain = str(int(config.BUDGET_COMPACT_RETAIN_RATIO * 100)) + "%"
    layer = Layer(
        code="L5",
        name="上下文预算与压缩",
        source="config.BUDGET_* · config.BUDGET_COMPACT_TRIGGER",
        rebuildable=True,
        note="这一层只回答两件事:模型这一轮看到的上下文由哪几段组成,以及什么时候压缩。"
             "**触发线是写死的数**(D134):历史对话越过它就压,不由窗口或任何别的预算推出来。"
             "⭐ **手动压缩不受这条线限制**:在聊天里说「压缩上下文」或点本页按钮,"
             "确认后立即压一次(仍保留最近约 20%,原始事件在归档里)。"
             "⚠️ 本页是**静态预算表**:只有 facts 子段能在本地实测;其余段真正占多少,"
             "要等一次对话后到「轨迹」页看那一轮发出去的上下文。"
             "各段怎么裁、谁先被砍,也在「轨迹 → 规则」那一页。",
        # ⚠️ 第三列叫「实测占用」而不是「这一轮实际」:这一页是静态预算表,
        #    只有 facts 子段能在本地测出真实占用量;其余段真正占多少要等一次对话后
        #    到「轨迹」页看那一轮发出去的上下文。
        columns=["段", "预算(token)", "实测占用", "说明"],
        headline="历史对话到 " + format(trigger, ",") + " token 就压缩,压完保留 "
                 + retain + "。",
    )
    # 实际占用:memory 段与 facts 子段都**实测**(不估算成"上限"),否则这一页还是纸面数字。
    actual: dict[str, str] = {}
    if conn is not None:
        try:
            actual["facts"] = (
                str(facts_injection_tokens(conn))
                + "(子预算 " + str(config.FACT_BUDGET_TOKENS) + ")"
            )
        except Exception:  # noqa: BLE001 - 这一页坏一个数字不该整页打不开
            actual = {}
    for name, tokens, why in (
        ("system", config.BUDGET_SYSTEM, "人设、协议、技能目录(顺序稳定,为了 KV 缓存命中)"),
        ("checkpoint", config.BUDGET_CHECKPOINT,
         "压缩检查点:被压掉的历史在这里留一行索引(压过哪段、摘要 #几、讲了什么)。"
         "它排在历史之前,所以整段能命中前缀缓存(D142)"),
        ("runtime", config.BUDGET_RUNTIME, "当前时间、会话、环境"),
        ("memory", config.BUDGET_HOT_SUMMARY, "L4 那个块就放在这一段"),
        ("evidence", config.BUDGET_EVIDENCE, "检索命中时注入的原文片段"),
        ("conversation", config.BUDGET_CONVERSATION,
         "最近若干轮对话;触发线 " + format(config.BUDGET_COMPACT_TRIGGER, ",")
         + " 比它低 —— 先压,再谈装不下"),
        # ⚠️ 工具 schema 由 API 的 tools 参数单独发出,**不进 messages 那本账**:
        #    每轮完全一样、压缩也动不了它。实测值在 --dump / 聊天页的执行反馈里。
        ("tools(工具 schema)", config.BUDGET_TOOLS,
         "单列、不进 messages 那本账(所以也不参与压缩触发线的判定);这一格是预留额(实测值),"
         "会话每轮上报真实占用(见 D95 / D130)"),
    ):
        layer.rows.append([name, str(tokens), actual.get(name, "—"), why])
    # facts 是 memory 段里的一个子段,但它**每轮都在**、且条数会涨 —— 单独摊开一行
    layer.rows.append([
        "├ facts(永久事实)", str(config.FACT_BUDGET_TOKENS),
        actual.get("facts", "—"),
        "L3 那层每轮固定吃掉的量(它是 L4 里唯一无条件注入的部分)",
    ])
    return layer


def build_memory_view(
    conn: sqlite3.Connection, *, now_text: str = "", archive_dir: Path | None = None,
    store: MemoryStore | None = None,
) -> MemoryView:
    """按数据流向拼出全部层次。**纯读** —— 不改任何东西,也不建索引。"""
    # ⚠️ 没传 store 时**不要**现建一个:MemoryStore.__init__ 会探测/建 FTS 索引,
    #    那是写操作,而这一页的验收条件之一正是"看一眼不改变任何东西"
    #    (tests/test_memory_view.py::test_view_does_not_write_anything 钉着这条)。
    #    所以注入名单与 token 成本都用一段裸 SQL 直接算。
    layers = [
        layer_archive(archive_dir),
        layer_events(conn),
        layer_event_types(conn),
        layer_summaries(conn),
        layer_facts(conn),
        layer_fact_candidates(conn),
        layer_hot_block(conn, now_text),
        layer_projection(conn),
    ]
    return MemoryView(
        layers=layers,
        generated_at=now_text,
        total_events=int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]),
        total_active_facts=int(
            conn.execute("SELECT COUNT(*) FROM facts WHERE active = 1").fetchone()[0]
        ),
        total_summaries=int(conn.execute("SELECT COUNT(*) FROM summary_pool").fetchone()[0]),
    )


__all__ = ["Layer", "MemoryView", "build_memory_view"]