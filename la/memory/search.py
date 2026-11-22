"""摘要池检索:能力探测 + trigram/LIKE 双路降级。

⚠️ P0 实测结论(SQLite 3.50.4 / Python 3.14,见 docs/决策记录.md):
    * tokenize='trigram' 建表成功,三字查询命中;
    * 但**两字查询召回为 0** —— "会议" 查不到含 "会议" 的句子;
    * tokenize='unicode61' 对中文完全不可用(整句被当成一个 token)。

所以中文检索必须是双路的:
    查询词 >= 3 字 -> fts5 trigram
    查询词 <  3 字 -> LIKE 降级
并且**任何一次 fts 未命中都回退 LIKE**,避免索引过期导致的静默漏检。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any
from datetime import datetime, timezone

FTS_TABLE = "summary_pool_fts"
MIN_TRIGRAM_CHARS = 3
CAPABILITY_KEY = "fts_tokenizer"
MODE_TRIGRAM = "trigram"
MODE_LIKE = "like"

_PUNCT = re.compile(r"[^\w\u4e00-\u9fff]+")

# 停用词:只用于**把查询切成候选关键词**,不参与检索本身
STOPWORDS: tuple[str, ...] = (
    "我们", "你们", "他们", "它们", "咱们", "自己",
    "刚才", "刚刚", "之前", "上次", "那次", "这时", "那时", "后来",
    "那个", "这个", "那些", "这些", "哪个", "哪些",
    "什么", "怎么", "怎样", "为什么", "是不是", "有没有", "能不能",
    "还有", "就是", "然后", "因为", "所以", "但是", "不过", "如果", "虽然",
    "可以", "一下", "一点", "记得", "说过", "聊过", "聊的", "谈起", "提到",
    "告诉", "请问", "帮我", "知道", "觉得", "时候", "现在", "已经",
    "还是", "不是", "没有", "我的", "你的", "他的", "她的",
    "上午", "下午", "晚上", "早上", "中午", "时候",
    "多少", "多久", "哪儿", "哪里",
)
STOPWORDS_SORTED: tuple[str, ...] = tuple(sorted(set(STOPWORDS), key=len, reverse=True))

# 单字虚词:切完停用词后,再把段首段尾的这些字剥掉("说的装修预算是" -> "装修预算")
PARTICLES = "的了是在有和就都也还要会我你他她们吗呢吧啊呀嘛说叫个把给被与及或而但却并则之其此该些"

MAX_TERMS = 8


@dataclass
class SearchHit:
    summary_id: int
    score: float
    match_kinds: tuple[str, ...]
    #: FTS5 的 bm25(负值,越小越相关);只走 LIKE 降级时是 None
    bm25: float | None = None


def _strip_particles(segment: str) -> str:
    """剥掉段首段尾的单字虚词,但**绝不把词剥到 2 字以下**。

    ⚠️ 这个下界是必需的:"会议" 的 "会" 也在虚词表里,无脑 strip 会把它变成 "议"
    而被丢弃 —— 实测中这正是"两字词搜不到"的第二个坑(第一个坑是 trigram)。
    """
    start, end = 0, len(segment)
    changed = True
    while changed and end - start > 2:
        changed = False
        if segment[start] in PARTICLES:
            start += 1
            changed = True
        if end - start > 2 and segment[end - 1] in PARTICLES:
            end -= 1
            changed = True
    return segment[start:end]


def extract_terms(text: str, *, max_terms: int = MAX_TERMS) -> list[str]:
    """把一句自然语言查询切成候选关键词(中文按停用词切,英文按空白切)。"""
    if not text:
        return []
    segments: list[str] = []
    for chunk in _PUNCT.sub(" ", text).split():
        if re.fullmatch(r"[A-Za-z0-9_]+", chunk):
            if len(chunk) >= 2:
                segments.append(chunk.lower())
            continue
        parts = [chunk]
        for stopword in STOPWORDS_SORTED:
            expanded: list[str] = []
            for part in parts:
                expanded.extend(part.split(stopword))
            parts = expanded
        for part in parts:
            cleaned = _strip_particles(part)
            if len(cleaned) >= 2:
                segments.append(cleaned)

    seen: set[str] = set()
    ordered: list[str] = []
    for term in segments:
        if term in seen:
            continue
        seen.add(term)
        ordered.append(term)
        if len(ordered) >= max_terms:
            break
    return ordered


# --------------------------------------------------------------------------
# 能力探测
# --------------------------------------------------------------------------
def _probe_trigram(conn: sqlite3.Connection) -> bool:
    """真建一张临时虚表并试查一次 —— 比只看编译选项可靠。"""
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS temp.probe_trigram"
            " USING fts5(t, tokenize='trigram')"
        )
        conn.execute("DELETE FROM temp.probe_trigram")
        conn.execute(
            "INSERT INTO temp.probe_trigram(t) VALUES ('明天上午开项目会议讨论预算')"
        )
        row = conn.execute(
            "SELECT count(*) FROM temp.probe_trigram WHERE t MATCH ?", ('"项目会"',)
        ).fetchone()
        matched = int(row[0]) > 0
        conn.execute("DROP TABLE IF EXISTS temp.probe_trigram")
        return matched
    except sqlite3.Error:
        return False


def _read_capability(conn: sqlite3.Connection, name: str) -> str | None:
    try:
        row = conn.execute("SELECT value FROM sys_capabilities WHERE name = ?", (name,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return str(row[0]) if row else None


def _write_capability(conn: sqlite3.Connection, name: str, value: str) -> None:
    conn.execute(
        "INSERT INTO sys_capabilities(name, value, probed_at) VALUES (?, ?, ?)"
        " ON CONFLICT(name) DO UPDATE SET value = excluded.value, probed_at = excluded.probed_at",
        (name, value, datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )
    conn.commit()


FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS summary_pool_fts USING fts5(
    text, anchors, tags,
    content='summary_pool', content_rowid='id', tokenize='trigram'
)
"""

FTS_TRIGGERS: tuple[str, ...] = (
    """
    CREATE TRIGGER IF NOT EXISTS summary_pool_fts_ai AFTER INSERT ON summary_pool BEGIN
        INSERT INTO summary_pool_fts(rowid, text, anchors, tags)
        VALUES (new.id, new.text, new.anchors, new.tags);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS summary_pool_fts_ad AFTER DELETE ON summary_pool BEGIN
        INSERT INTO summary_pool_fts(summary_pool_fts, rowid, text, anchors, tags)
        VALUES ('delete', old.id, old.text, old.anchors, old.tags);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS summary_pool_fts_au AFTER UPDATE ON summary_pool BEGIN
        INSERT INTO summary_pool_fts(summary_pool_fts, rowid, text, anchors, tags)
        VALUES ('delete', old.id, old.text, old.anchors, old.tags);
        INSERT INTO summary_pool_fts(rowid, text, anchors, tags)
        VALUES (new.id, new.text, new.anchors, new.tags);
    END
    """,
)


def ensure_search_index(conn: sqlite3.Connection, *, force_probe: bool = False) -> str:
    """确保检索索引存在,返回当前模式('trigram' 或 'like')。

    ⚠️ **建好了就不要再执行 DDL**。Web 层是"每请求一个连接",而
    CREATE VIRTUAL TABLE / CREATE TRIGGER 都是**写语句**(要拿写锁)——
    只读请求因此会去抢写锁,和正在跑的那一轮对话撞上就是 database is locked。
    所以:能力已经记在 sys_capabilities 里 + 虚表确实存在 => 直接返回。
    """
    cached = None if force_probe else _read_capability(conn, CAPABILITY_KEY)
    mode = cached
    if mode is None:
        mode = MODE_TRIGRAM if _probe_trigram(conn) else MODE_LIKE
        _write_capability(conn, CAPABILITY_KEY, mode)
    if mode == MODE_TRIGRAM:
        existed = (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (FTS_TABLE,)
            ).fetchone()
            is not None
        )
        if existed and cached is not None:
            return mode      # 已经建好了:这条连接一行都不写
        conn.execute(FTS_DDL)
        for trigger in FTS_TRIGGERS:
            conn.execute(trigger)
        # 外内容表:只有**第一次建表**时才需要 rebuild。
        # ⚠️ 每次开连接都 rebuild 会让 Web 的"每请求一个连接"变成灾难(全表重扫)。
        if not existed:
            conn.execute("INSERT INTO " + FTS_TABLE + "(" + FTS_TABLE + ") VALUES('rebuild')")
        conn.commit()
    return mode


def get_mode(conn: sqlite3.Connection) -> str:
    return _read_capability(conn, CAPABILITY_KEY) or MODE_LIKE


def _fts_phrase(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def _match_fts(
    conn: sqlite3.Connection, term: str, limit: int, session_id: str | None = None
) -> list[tuple[int, float]]:
    """返回 [(rowid, bm25)];bm25 是负值,**越小越相关**。

    ⚠️ 会话过滤必须进 SQL(2026-10-18 审计):旧实现先全局取 limit*3
    再在 Python 里按 session 过滤;别的会话占满候选窗口时,本会话明明有
    摘要也会被漏掉(表现为假 miss)。
    """
    sql = (
        "SELECT rowid, bm25(" + FTS_TABLE + ") AS rank FROM " + FTS_TABLE
        + " WHERE " + FTS_TABLE + " MATCH ?"
    )
    params: list[Any] = [_fts_phrase(term)]
    if session_id:
        sql += " AND rowid IN (SELECT id FROM summary_pool WHERE session_id = ?)"
        params.append(session_id)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(int(row[0]), float(row[1])) for row in rows]


def _match_like(
    conn: sqlite3.Connection, term: str, limit: int, session_id: str | None = None
) -> list[int]:
    pattern = "%" + term + "%"
    sql = (
        "SELECT id FROM summary_pool"
        " WHERE (text LIKE ? OR anchors LIKE ? OR tags LIKE ?)"
    )
    params: list[Any] = [pattern, pattern, pattern]
    if session_id:
        sql += " AND session_id = ?"
        params.append(session_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [int(row[0]) for row in rows]


def search_summaries(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 20,
    mode: str | None = None,
    session_id: str | None = None,
) -> dict[int, SearchHit]:
    """按关键词检索摘要池,返回 {summary_id: SearchHit}。

    这不是最终排序 —— 排序(锚点 > 时间 > 相关度 > 热度)在 retrieval.py 里做。
    """
    mode = mode or get_mode(conn)
    terms = extract_terms(query)
    if not terms:
        return {}

    scores: dict[int, float] = {}
    kinds: dict[int, set[str]] = {}
    ranks: dict[int, float] = {}
    for term in terms:
        matched: list[tuple[int, float]] = []
        if mode == MODE_TRIGRAM and len(term) >= MIN_TRIGRAM_CHARS:
            matched = _match_fts(conn, term, limit * 3, session_id=session_id)
            if matched:
                for rowid, rank in matched:
                    scores[rowid] = scores.get(rowid, 0.0) + 1.0
                    kinds.setdefault(rowid, set()).add("fts")
                    # 同一行被多个词命中时,保留最相关(最小)的那次
                    best = ranks.get(rowid)
                    ranks[rowid] = rank if best is None else min(best, rank)
        if not matched:
            # 两字词,或 fts 未命中 -> LIKE 降级(实测必需)
            for rowid in _match_like(conn, term, limit * 3, session_id=session_id):
                scores[rowid] = scores.get(rowid, 0.0) + 0.8
                kinds.setdefault(rowid, set()).add("like")

    # 会话过滤已经下推到 FTS/LIKE 的 SQL 里,这里不再二次截断候选。

    ordered = sorted(scores.items(), key=lambda item: -item[1])[:limit]
    return {
        rowid: SearchHit(
            summary_id=rowid,
            score=score,
            match_kinds=tuple(sorted(kinds.get(rowid, set()))),
            bm25=ranks.get(rowid),
        )
        for rowid, score in ordered
    }
