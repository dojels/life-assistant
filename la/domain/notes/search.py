"""笔记检索:中文滑窗关键词 + trigram 预筛 + 可解释打分。

⚠️ 为什么不是"一把 FTS 就完事":本项目的 SQLite 实测结论(见 docs/决策记录.md D10)——
   tokenize='unicode61' 对中文完全不可用(整句一个 token),trigram 能用但
   **两字查询召回为 0**。所以永远是双路:FTS 只用来**快速捞候选**,真正的匹配与打分在
   Python 里做(可解释、可校准、不受分词器脾气影响)。

⭐⭐ 2026-11-09:按【云朵辅助培训智能体】的 kb 检索重构了关键词这一路(只借方法,不借装备):
   * **滑窗**:一个中文段既出整段,也出全部 3 字窗、2 字窗 —— "动火作业的分级" 与
     "动火作业分级" 因此能互相命中(我们之前只做"前缀",命中不了段中的词);
   * **长度权重**:2 字 1 分、3 字 2 分、4 字 3 分、≥5 字 4 分(越长越具体);
   * **整串加分**:去掉标点后的完整查询原样出现 → +0.25(短语级精确匹配最可信);
   * **夹名入索引**:搜索 "网球" 能找到「网球」本子里的笔记(之前夹名只作过滤条件,
     文字里没写"网球"就一条都搜不到);
   * **分数归一化到 0~1**:hint 的阈值才有意义(原来是拍脑袋的 0.3)。

⚠️ 没有借它的 Chroma + torch:笔记是短文本、个人量级,暴力扫几百条足够;
   评估见 docs/笔记域设计.md §16。
"""

from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

FTS_TABLE = "app_notes_fts"
CAPABILITY_KEY = "notes_fts_tokenizer"
MODE_TRIGRAM = "trigram"
MODE_LIKE = "like"
MIN_TRIGRAM_CHARS = 3

#: 每条查询最多参与匹配的关键词数。滑窗会产出很多候选,但打分只看前 N 个
#: (长词优先);再多就是把"噪声命中"也算进覆盖率里。
MAX_KEYWORDS = 10
#: 候选集上限:先捞这么多 id,再在 Python 里精确打分。
CANDIDATE_CAP = 400

from ...memory.search import STOPWORDS, extract_terms

_PUNCT = re.compile(
    r"[\s\u3000-\u303F\uFF00-\uFFEF\u2000-\u206F!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_\x60{|}~]+"
)
_CN = re.compile(r"[\u4e00-\u9fff]")

#: 整串命中(短语级)的加分。要能把"原话照抄"的那条从"只命中几个词"的候选里顶上来,
#: 但不能盖过"多个长词全中"。
PHRASE_BONUS = 0.25
#: 命中标题的加分。标题是这条笔记的自我概括,命中它比命中正文更可信。
TITLE_BONUS = 0.10
#: 证据强度的饱和尺度:score = 1 - exp(-命中权重 / 4)。
#: ⭐ 为什么用饱和而不是"命中/总权重":长句会切出很多窗,分母很大,
#:    一句"我上次说反手拉不起来"只命中 4 个窗也会被算成 0.2 —— 但那是**好命中**。
#:    饱和曲线只问"证据有多强",与query 长短无关:1 分≈0.22、2 分≈0.39、
#:    4 分≈0.63、8 分≈0.86。阈值因此可以稳定(见 HINT_MIN_SCORE)。
SATURATION = 4.0
#: hint(每轮自动召回)的门槛。2026-11-09 用一份 10 条真实笔记 + 12 条真实问法校准:
#:   单个 2 字词、标题里也没有  ≈0.22  (噪音,不提示)
#:   单个 2 字词、标题里命中    ≈0.32  (提示:标题是这条笔记的自我概括)
#:   单个 3 字词                ≈0.39
#:   多个窗命中(一句自然语言)≈0.50~0.93
#:   整串原话命中              +0.25
#: 所以 0.30 正好把"只有一个 2 字词、还没进标题"的噪音挡在外面。
HINT_MIN_SCORE = 0.30
#: 冠军领先第二名这么多时,只提示它一条(云朵的"分差门控":第二名通常是噪声)。
HINT_GAP = 0.25


def _phrase(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def _weight(term: str) -> int:
    n = len(term)
    if n >= 5:
        return 4
    if n == 4:
        return 3
    if n == 3:
        return 2
    return 1


def extract_keywords(query: str, *, max_terms: int = MAX_KEYWORDS) -> list[str]:
    """中文友好的关键词:核心片段 + 3 字滑窗 + 2 字滑窗,长词优先。

    两步(缺一不可):
      1. **先按停用词/虚词把话切成核心片段**(用记忆层那套 extract_terms)——
         "我上次说反手拉不起来是怎么说的" 要先变成 "反手拉不起来",
         否则整句会占掉一个长词权重、还不匹配,把覆盖率稀释掉;
      2. **再对每个片段出滑窗**:整段 + 全部 3 字窗 + 全部 2 字窗 ——
         "动火作业的分级" 与 "动火作业分级" 因此能互相命中。

    ⚠️ 只对**含中文**的片段做滑窗:英文/型号整段更准("iPhone15" 切成 "iPh" 没有意义)。
    """
    text = str(query or "").strip()
    if not text:
        return []
    segments = extract_terms(text, max_terms=6)
    if not segments:
        # 全是停用词/标点时退化成原串(总比一条都不搜强)
        segments = [seg for seg in _PUNCT.split(text) if seg and len(seg.strip()) >= 2]
    long_cands: list[str] = []
    tri_cands: list[str] = []
    bi_cands: list[str] = []
    for seg in segments:
        seg = seg.strip()
        if not seg or seg in STOPWORDS:
            continue
        if _CN.search(seg):
            if len(seg) >= 2:
                long_cands.append(seg)
            if len(seg) >= 4:
                tri_cands.extend(seg[i:i + 3] for i in range(len(seg) - 2))
            if len(seg) >= 3:
                bi_cands.extend(seg[i:i + 2] for i in range(len(seg) - 1))
        else:
            long_cands.append(seg)

    out: list[str] = []
    seen: set[str] = set()
    for word in long_cands + tri_cands + bi_cands:
        word = word.strip()
        if len(word) < 2 or word in STOPWORDS:
            continue
        key = word.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(word)
        if len(out) >= max_terms:
            break
    return out


def _compact(query: str) -> str:
    """去掉标点/空白后的整串,用于"原话照抄"加分。"""
    return "".join(seg for seg in _PUNCT.split(str(query or "")) if seg)


@dataclass(frozen=True)
class NoteHit:
    note_id: int
    score: float          # 0~1
    match_kinds: tuple[str, ...]
    bm25: float | None = None
    #: 命中的关键词(调试与"为什么是它"的说明用)
    matched: tuple[str, ...] = ()


# --------------------------------------------------------------------------
# 能力探测(与记忆层同一套;虚表在运行时建,不写进迁移)
# --------------------------------------------------------------------------
def _probe_trigram(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS temp.probe_note_trigram"
            " USING fts5(t, tokenize='trigram')"
        )
        conn.execute("DELETE FROM temp.probe_note_trigram")
        conn.execute(
            "INSERT INTO temp.probe_note_trigram(t) VALUES ('明天上午开项目会议讨论预算')"
        )
        row = conn.execute(
            "SELECT count(*) FROM temp.probe_note_trigram WHERE t MATCH ?", ('"项目会"',)
        ).fetchone()
        matched = int(row[0]) > 0
        conn.execute("DROP TABLE IF EXISTS temp.probe_note_trigram")
        return matched
    except sqlite3.Error:
        return False


def _read_capability(conn: sqlite3.Connection) -> str | None:
    try:
        row = conn.execute(
            "SELECT value FROM sys_capabilities WHERE name = ?", (CAPABILITY_KEY,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return str(row[0]) if row else None


def _write_capability(conn: sqlite3.Connection, value: str) -> None:
    conn.execute(
        "INSERT INTO sys_capabilities(name, value, probed_at) VALUES (?, ?, ?)"
        " ON CONFLICT(name) DO UPDATE SET value = excluded.value, probed_at = excluded.probed_at",
        (CAPABILITY_KEY, value, datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )
    conn.commit()


FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS app_notes_fts USING fts5(
    title, body, tags,
    content='app_notes', content_rowid='id', tokenize='trigram'
)
"""

FTS_TRIGGERS: tuple[str, ...] = (
    """
    CREATE TRIGGER IF NOT EXISTS app_notes_fts_ai AFTER INSERT ON app_notes BEGIN
        INSERT INTO app_notes_fts(rowid, title, body, tags)
        VALUES (new.id, new.title, new.body, COALESCE(new.tags, ''));
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS app_notes_fts_ad AFTER DELETE ON app_notes BEGIN
        INSERT INTO app_notes_fts(app_notes_fts, rowid, title, body, tags)
        VALUES ('delete', old.id, old.title, old.body, COALESCE(old.tags, ''));
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS app_notes_fts_au AFTER UPDATE ON app_notes BEGIN
        INSERT INTO app_notes_fts(app_notes_fts, rowid, title, body, tags)
        VALUES ('delete', old.id, old.title, old.body, COALESCE(old.tags, ''));
        INSERT INTO app_notes_fts(rowid, title, body, tags)
        VALUES (new.id, new.title, new.body, COALESCE(new.tags, ''));
    END
    """,
)


def ensure_index(conn: sqlite3.Connection, *, force_probe: bool = False) -> str:
    """确保检索索引存在,返回当前模式。建好了就不再执行 DDL(与记忆层同一纪律)。"""
    cached = None if force_probe else _read_capability(conn)
    mode = cached
    if mode is None:
        mode = MODE_TRIGRAM if _probe_trigram(conn) else MODE_LIKE
        _write_capability(conn, mode)
    if mode == MODE_TRIGRAM:
        existed = (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (FTS_TABLE,)
            ).fetchone()
            is not None
        )
        if existed and cached is not None:
            return mode
        conn.execute(FTS_DDL)
        for trigger in FTS_TRIGGERS:
            conn.execute(trigger)
        if not existed:
            conn.execute("INSERT INTO " + FTS_TABLE + "(" + FTS_TABLE + ") VALUES('rebuild')")
        conn.commit()
    return mode


def get_mode(conn: sqlite3.Connection) -> str:
    return _read_capability(conn) or MODE_LIKE


# --------------------------------------------------------------------------
# 候选集:先粗筛(两条 SQL),再精排(Python)
# --------------------------------------------------------------------------
def _folder_clause(folder_path: str | None) -> tuple[str, list[Any]]:
    """夹过滤:自己 + 整棵子树(path 前缀)。"""
    if not folder_path:
        return "", []
    return (
        " AND n.folder_id IN (SELECT id FROM app_note_folders WHERE path = ?"
        " OR path LIKE ?)",
        [folder_path, folder_path + "/%"],
    )


def _fts_candidates(
    conn: sqlite3.Connection, terms: list[str], *, folder_path: str | None
) -> set[int]:
    if not terms:
        return set()
    clause, params = _folder_clause(folder_path)
    expr = " OR ".join(_phrase(term) for term in terms)
    sql = (
        "SELECT n.id FROM " + FTS_TABLE
        + " JOIN app_notes n ON n.id = " + FTS_TABLE + ".rowid"
        + " WHERE " + FTS_TABLE + " MATCH ? AND n.deleted_at IS NULL" + clause
        + " LIMIT ?"
    )
    try:
        rows = conn.execute(sql, [expr, *params, CANDIDATE_CAP]).fetchall()
    except sqlite3.OperationalError:
        return set()
    return {int(row[0]) for row in rows}


def _like_candidates(
    conn: sqlite3.Connection, terms: list[str], *, folder_path: str | None
) -> set[int]:
    """LIKE 一路:两字词、以及夹名(夹路径不进 FTS)。"""
    if not terms:
        return set()
    clause, params = _folder_clause(folder_path)
    conds: list[str] = []
    values: list[Any] = []
    for term in terms:
        conds.append(
            "(n.title LIKE ? OR n.body LIKE ? OR COALESCE(n.tags, '') LIKE ?"
            " OR COALESCE(f.path, '') LIKE ?)"
        )
        pattern = "%" + term + "%"
        values.extend([pattern, pattern, pattern, pattern])
    sql = (
        "SELECT n.id FROM app_notes n"
        " LEFT JOIN app_note_folders f ON f.id = n.folder_id"
        " WHERE n.deleted_at IS NULL AND (" + " OR ".join(conds) + ")" + clause
        + " LIMIT ?"
    )
    rows = conn.execute(sql, [*values, *params, CANDIDATE_CAP]).fetchall()
    return {int(row[0]) for row in rows}


def _load_rows(
    conn: sqlite3.Connection, ids: list[int], *, folder_path: str | None
) -> list[Any]:
    clause, params = _folder_clause(folder_path)
    placeholders = ",".join("?" for _ in ids)
    sql = (
        "SELECT n.id, n.title, n.body, COALESCE(n.tags, '') AS tags,"
        " COALESCE(f.path, '') AS folder_path, n.local_date"
        " FROM app_notes n LEFT JOIN app_note_folders f ON f.id = n.folder_id"
        " WHERE n.deleted_at IS NULL AND n.id IN (" + placeholders + ")" + clause
    )
    return list(conn.execute(sql, [*ids, *params]).fetchall())


def search_notes(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 10,
    folder_path: str | None = None,
    mode: str | None = None,
) -> dict[int, NoteHit]:
    """检索笔记,返回 {note_id: NoteHit}。排序(分数/日期)交给调用方。"""
    mode = mode or get_mode(conn)
    terms = extract_keywords(query)
    if not terms:
        return {}
    weights = {term: _weight(term) for term in terms}
    compact = _compact(query)

    ids: set[int] = set()
    if mode == MODE_TRIGRAM:
        ids |= _fts_candidates(
            conn, [t for t in terms if len(t) >= MIN_TRIGRAM_CHARS], folder_path=folder_path
        )
    # LIKE 一路永远要跑:两字词不在 trigram 里,夹名也不在 FTS 里。
    ids |= _like_candidates(conn, terms, folder_path=folder_path)
    if not ids:
        return {}

    rows = _load_rows(conn, sorted(ids), folder_path=folder_path)
    scored: dict[int, NoteHit] = {}
    for row in rows:
        note_id = int(row["id"])
        title = str(row["title"] or "")
        haystack = "\n".join(
            (title, str(row["body"] or ""), str(row["tags"] or ""),
             str(row["folder_path"] or ""))
        )
        matched = [term for term in terms if term in haystack]
        if not matched:
            continue
        hit_weight = sum(weights[term] for term in matched)
        #: 证据强度(饱和):与查询长短无关,只看"命中了多少有分量的词"
        score = 1.0 - math.exp(-hit_weight / SATURATION)
        kinds: list[str] = ["keyword"]
        if any(term in title for term in matched):
            score += TITLE_BONUS
            kinds.append("title")
        if len(compact) >= 2 and compact in haystack:
            score += PHRASE_BONUS
            kinds.append("phrase")
        scored[note_id] = NoteHit(
            note_id=note_id,
            score=min(1.0, round(score, 4)),
            match_kinds=tuple(kinds),
            bm25=None,
            matched=tuple(matched),
        )
    return scored


__all__ = [
    "NoteHit",
    "ensure_index",
    "get_mode",
    "search_notes",
    "extract_keywords",
    "MODE_TRIGRAM",
    "MODE_LIKE",
    "CAPABILITY_KEY",
]
