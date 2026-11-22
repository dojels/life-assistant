"""记忆库入口:事件落盘 + 镜像索引 + 摘要池 + 事实表。

不变量(设计深化 §1.1):
  * JSONL 事件日志是事实源;memory.db 里的 events 表是**镜像**,可重放重建;
  * 摘要池是派生索引,允许 DROP 后全量重放重建;
  * facts 的矛盾消解用 active=0 + superseded_by,不删除旧行。
"""

from __future__ import annotations
from ..base.clock import utc_now_iso as _utc_now

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .. import config
from . import anchors as anchors_mod
from .archive import Event, EventLog
from .decay import DEFAULT_CATEGORY, days_since, heat
from .search import ensure_search_index




def _fact_fingerprint(text: str) -> str:
    """事实的去重指纹:忽略空白与标点,也忽略"用户"这个前缀。

    这样 "我最喜欢的咖啡是冰美式" 与 "用户最喜欢 的咖啡是冰美式"
    会被认为是同一件事,不会各写一条。

    ⚠️ 2026-10-13 修一个**汉字级**的漏洞:原字符类里没有全角逗号(它们才是
    全角逗号(它们才是中文里最常用的)。后果是这两条的指纹**不相等**:
        "用户喜欢 打游戏，以前喜欢画画，喜欢看网络小说，然"     (规则抽的截断版)
        "用户喜欢打游戏、看网络小说，以前喜欢画画，也喜欢滑板"   (模型写的完整版)
    于是同一条事实写了两行,两行都每轮进上下文。口径统一到 guard.normalize。
    """
    from .guard import normalize

    return normalize(text)


@dataclass
class SummaryRow:
    id: int
    session_id: str
    start_seq: int
    end_seq: int
    start_time: str
    end_time: str
    text: str
    anchors: list[str]
    tags: list[str]
    category: str
    base_strength: float
    visits: int
    last_access: str | None
    heat_cache: float | None
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "SummaryRow":
        return cls(
            id=int(row["id"]),
            session_id=row["session_id"],
            start_seq=int(row["start_seq"]),
            end_seq=int(row["end_seq"]),
            start_time=row["start_time"],
            end_time=row["end_time"],
            text=row["text"],
            anchors=anchors_mod.as_anchor_list(row["anchors"]),
            tags=anchors_mod.as_anchor_list(row["tags"]),
            category=row["category"],
            base_strength=float(row["base_strength"]),
            visits=int(row["visits"]),
            last_access=row["last_access"],
            heat_cache=row["heat_cache"],
            created_at=row["created_at"],
        )


@dataclass
class FactRow:
    id: int
    type: str
    category: str
    content: str
    keywords: list[str]
    active: bool
    created_at: str
    #: 标记为敏感的事实(设计深化 §3.5)。⚠️ 注入块与工具返回**必须**据此脱敏:
    #: 早先这个标记只躺在库里,FactRow 根本不带它 —— 于是"标了敏感"等于没标。
    confidential: bool = False


def _row_to_fact(row: sqlite3.Row) -> FactRow:
    return FactRow(
        id=int(row["id"]),
        type=row["type"],
        category=row["category"],
        content=row["content"],
        keywords=anchors_mod.as_anchor_list(row["keywords"]),
        active=bool(row["active"]),
        created_at=row["created_at"],
        confidential=bool(row["confidential"]) if "confidential" in row.keys() else False,
    )


class MemoryStore:
    """记忆侧的唯一入口。"""

    def __init__(self, conn: sqlite3.Connection, *, archive_dir: Path | None = None) -> None:
        self.conn = conn
        self.archive_dir = archive_dir
        self.search_mode = ensure_search_index(conn)
        self._logs: dict[str, EventLog] = {}

    # -- 会话与事件 -------------------------------------------------------
    def log_for(self, session_id: str) -> EventLog:
        log = self._logs.get(session_id)
        if log is None:
            log = EventLog(session_id, self.archive_dir)
            self._logs[session_id] = log
        return log

    def ensure_session(self, session_id: str, *, title: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO sessions(id, created_at, title, last_active_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET last_active_at = excluded.last_active_at",
            (session_id, _utc_now(), title, _utc_now()),
        )
        self.conn.commit()

    def append(
        self,
        session_id: str,
        event_type: str,
        data: dict[str, Any] | None = None,
        *,
        surface: bool | None = None,
        time: str | None = None,
    ) -> Event:
        """唯一写入路径:先落 JSONL(事实源),再更新镜像表。"""
        log = self.log_for(session_id)
        event = log.append(event_type, data, surface=surface, time=time)
        self.conn.execute(
            "INSERT OR REPLACE INTO events(session_id, seq, type, time, surface, data)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                session_id,
                event.seq,
                event.type,
                event.time,
                1 if event.surface else 0,
                json.dumps(event.data, ensure_ascii=False),
            ),
        )
        self.conn.commit()
        return event

    def events(
        self, session_id: str, start_seq: int = 1, end_seq: int | None = None
    ) -> list[Event]:
        sql = "SELECT seq, type, time, surface, data FROM events WHERE session_id = ? AND seq >= ?"
        params: list[Any] = [session_id, start_seq]
        if end_seq is not None:
            sql += " AND seq <= ?"
            params.append(end_seq)
        sql += " ORDER BY seq"
        rows = self.conn.execute(sql, params).fetchall()
        return [
            Event(
                seq=int(row["seq"]),
                type=row["type"],
                time=row["time"],
                data=json.loads(row["data"] or "{}"),
                surface=bool(row["surface"]),
                session_id=session_id,
            )
            for row in rows
        ]

    def query_range(
        self,
        start: str,
        end: str,
        *,
        limit: int = 200,
        session_id: str | None = None,
    ) -> list[Event]:
        """按时间范围读取事件(左闭右开)。

        ⚠️ 时间比较是**字符串比较**,依赖"所有事件时间都带同一个时区偏移"这一不变量
        (archive.now_iso() 用配置时区写,保证一致)。换时区后老数据仍按原偏移排序。
        """
        sql = (
            "SELECT session_id, seq, type, time, surface, data FROM events"
            " WHERE time >= ? AND time < ? AND type IN ('user/message', 'assistant/message')"
        )
        params: list[Any] = [start, end]
        if session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        sql += " ORDER BY time, seq LIMIT ?"
        params.append(limit)
        return [self._row_to_event(row) for row in self.conn.execute(sql, params).fetchall()]

    def search_events(
        self, query: str, *, limit: int = 20, session_id: str | None = None
    ) -> list[Event]:
        """档案原文检索(慢路径)。v1 用 LIKE + 关键词切分,量级上去后再换 FTS 索引。"""
        from .anchors import extract_anchors
        from .search import extract_terms

        terms = [*extract_terms(query), *extract_anchors(query)]
        if not terms:
            return []
        seen: dict[tuple[str, int], Event] = {}
        for term in terms[:6]:
            pattern = "%" + term + "%"
            sql = (
                "SELECT session_id, seq, type, time, surface, data FROM events"
                " WHERE type IN ('user/message', 'assistant/message') AND data LIKE ?"
            )
            params: list[Any] = [pattern]
            if session_id:
                sql += " AND session_id = ?"
                params.append(session_id)
            sql += " ORDER BY time DESC LIMIT ?"
            params.append(limit)
            for row in self.conn.execute(sql, params).fetchall():
                event = self._row_to_event(row)
                seen[(event.session_id or "", event.seq)] = event
        ordered = sorted(seen.values(), key=lambda item: item.time)
        return ordered[:limit]

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> Event:
        return Event(
            seq=int(row["seq"]),
            type=row["type"],
            time=row["time"],
            data=json.loads(row["data"] or "{}"),
            surface=bool(row["surface"]),
            session_id=row["session_id"],
        )

    # -- 巩固幂等 ---------------------------------------------------------
    def consolidation_seen(
        self, session_id: str, start_seq: int, end_seq: int, rounds_after_reply: int
    ) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM consolidations WHERE session_id = ? AND start_seq = ?"
            " AND end_seq = ? AND rounds_after_reply = ?",
            (session_id, start_seq, end_seq, rounds_after_reply),
        ).fetchone()
        return row is not None

    def record_consolidation(
        self,
        *,
        session_id: str,
        start_seq: int,
        end_seq: int,
        rounds_after_reply: int,
        summary_id: int | None,
        evidence_count: int,
    ) -> bool:
        """记录一次巩固。返回 True 表示本次是首次(未被幂等键挡下)。"""
        try:
            self.conn.execute(
                "INSERT INTO consolidations(session_id, start_seq, end_seq, rounds_after_reply,"
                " summary_id, evidence_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    start_seq,
                    end_seq,
                    rounds_after_reply,
                    summary_id,
                    evidence_count,
                    _utc_now(),
                ),
            )
        except sqlite3.IntegrityError:
            self.conn.rollback()
            return False
        self.conn.commit()
        return True

    def last_seq(self, session_id: str) -> int:
        row = self.conn.execute(
            "SELECT MAX(seq) AS m FROM events WHERE session_id = ?", (session_id,)
        ).fetchone()
        return int(row["m"]) if row and row["m"] is not None else 0

    def rebuild_from_archive(self, session_id: str) -> int:
        """从 JSONL 重放,重建 events 镜像。

        ⚠️ **摘要池不在重建范围内**(2026-10-12 一致性审核确认):compaction/summary
        事件里只有桶与 id、没有摘要正文,所以 summary_pool 丢了只能从备份恢复 ——
        这也是记忆页把 L2 标成"不可重建"的原因。见 docs/决策记录.md D93。
        """
        self.conn.execute("DELETE FROM events WHERE session_id = ?", (session_id,))
        self.conn.commit()
        log = EventLog(session_id, self.archive_dir)
        count = 0
        for event in log.read():
            self.conn.execute(
                "INSERT OR REPLACE INTO events(session_id, seq, type, time, surface, data)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    event.seq,
                    event.type,
                    event.time,
                    1 if event.surface else 0,
                    json.dumps(event.data, ensure_ascii=False),
                ),
            )
            count += 1
        self.conn.commit()
        self._logs[session_id] = log
        return count

    # -- 摘要池 -----------------------------------------------------------
    def upsert_summary(
        self,
        *,
        session_id: str,
        start_seq: int,
        end_seq: int,
        start_time: str,
        end_time: str,
        text: str,
        anchors: Iterable[str] = (),
        tags: Iterable[str] = (),
        category: str = DEFAULT_CATEGORY,
        base_strength: float = 1.0,
    ) -> int:
        """同一个桶只保留一条("每桶一条"):重压时更新,不新增行。"""
        anchor_json = json.dumps(list(anchors), ensure_ascii=False)
        tag_json = json.dumps(list(tags), ensure_ascii=False)
        existing = self.conn.execute(
            "SELECT id FROM summary_pool WHERE session_id = ? AND start_seq = ? AND end_seq = ?",
            (session_id, start_seq, end_seq),
        ).fetchone()
        if existing:
            summary_id = int(existing["id"])
            self.conn.execute(
                "UPDATE summary_pool SET text = ?, anchors = ?, tags = ?, category = ?,"
                " heat_cache = NULL WHERE id = ?",
                (text, anchor_json, tag_json, category, summary_id),
            )
        else:
            cursor = self.conn.execute(
                "INSERT INTO summary_pool(session_id, start_seq, end_seq, start_time, end_time,"
                " text, anchors, tags, category, base_strength, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    start_seq,
                    end_seq,
                    start_time,
                    end_time,
                    text,
                    anchor_json,
                    tag_json,
                    category,
                    base_strength,
                    _utc_now(),
                ),
            )
            summary_id = int(cursor.lastrowid or 0)
        self.conn.commit()
        return summary_id

    def summaries(self, ids: Iterable[int]) -> dict[int, SummaryRow]:
        id_list = [int(i) for i in ids]
        if not id_list:
            return {}
        placeholders = ",".join("?" for _ in id_list)
        rows = self.conn.execute(
            "SELECT * FROM summary_pool WHERE id IN (" + placeholders + ")", id_list
        ).fetchall()
        return {int(row["id"]): SummaryRow.from_row(row) for row in rows}

    def all_summaries(self, session_id: str | None = None) -> list[SummaryRow]:
        if session_id:
            rows = self.conn.execute(
                "SELECT * FROM summary_pool WHERE session_id = ? ORDER BY id", (session_id,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM summary_pool ORDER BY id").fetchall()
        return [SummaryRow.from_row(row) for row in rows]

    def find_summary_for_seq(
        self, session_id: str, start_seq: int, end_seq: int
    ) -> SummaryRow | None:
        """找覆盖该序号区间的现有摘要桶(用于巩固时"合并进同一行"而不是新增)。"""
        row = self.conn.execute(
            "SELECT * FROM summary_pool WHERE session_id = ? AND start_seq <= ? AND end_seq >= ?"
            " ORDER BY (end_seq - start_seq) ASC LIMIT 1",
            (session_id, start_seq, end_seq),
        ).fetchone()
        return SummaryRow.from_row(row) if row else None

    def summaries_between(
        self, start: str, end: str, *, limit: int = 5, session_id: str | None = None
    ) -> list[SummaryRow]:
        """取时间窗内的摘要(每日播报要"昨日概要")。

        ⚠️ session_id 不是可选的“优化”(2026-10-18 审计):家庭版里它决定
           简报会不会把**别的成员**的摘要正文注入本会话。调用方必须给。
        """
        sql = (
            "SELECT * FROM summary_pool WHERE end_time >= ? AND start_time < ?"
        )
        params: list[Any] = [start, end]
        if session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        sql += " ORDER BY end_time DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [SummaryRow.from_row(row) for row in rows]

    def compacted_ranges(self, session_id: str) -> list[tuple[int, int]]:
        """真正被**压缩**过的 seq 区间(投影时跳过它们)。

        ⚠️ 不能把 summary_pool 的所有行都算进来(2026-10-18 审计):
           `consolidate()` 在找不到已压缩桶时,会为“考古证据”新建一条
           tags=["consolidated"] 的摘要;它只是**回填的检索材料**,不是
           “这段原文已经压成摘要了”。把它算成 compacted 会让尚未压缩的
           user/assistant 事件从后续投影里静默消失(还留下孤立 assistant)。
        """
        rows = self.conn.execute(
            "SELECT start_seq, end_seq FROM summary_pool WHERE session_id = ?"
            " AND tags NOT LIKE '%\"consolidated\"%'"
            " ORDER BY start_seq",
            (session_id,),
        ).fetchall()
        return [(int(row["start_seq"]), int(row["end_seq"])) for row in rows]

    def bump_visits(self, ids: Iterable[int], amount: int) -> int:
        """升温:每轮结束后由后台任务批量落库,不在 LLM 调用链里阻塞。"""
        id_list = [int(i) for i in ids]
        if not id_list or amount <= 0:
            return 0
        placeholders = ",".join("?" for _ in id_list)
        cursor = self.conn.execute(
            "UPDATE summary_pool SET visits = visits + ?, last_access = ?, heat_cache = NULL"
            " WHERE id IN (" + placeholders + ")",
            [amount, _utc_now(), *id_list],
        )
        self.conn.commit()
        return cursor.rowcount

    def pool_max_heat(self, session_id: str | None = None) -> float:
        """池内最大热度(设计深化 §3.1 的 heat_norm 分母)。

        ⚠️ 必须按会话取(2026-11-07 修):summary_pool 是**全成员共用的一张表**,
           不带 WHERE 的 MAX 会让 A 成员的高频主题抬高 B 成员的分母 —— B 的热度项
           被整体稀释(0.2 的权重实际只剩零头),排序被锚点/BM25 单方面接管。
           session_id=None 仍返回全库最大值,只给没有会话上下文的诊断/页面用。

        `heat_cache` 是每轮结束批量刷新的;从没刷过(NULL)时返回 0,
        由调用方退化成"用候选里的最大值",而不是拿 0 当分母。
        """
        if session_id:
            row = self.conn.execute(
                "SELECT MAX(COALESCE(heat_cache, 0)) AS m FROM summary_pool"
                " WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT MAX(COALESCE(heat_cache, 0)) AS m FROM summary_pool"
            ).fetchone()
        return float(row["m"] or 0.0) if row is not None else 0.0

    def bump_base_strength(self, ids: Iterable[int], amount: float = 1.0) -> int:
        """残余基底强度 +1(设计深化 §1.4:考古成功 → base_strength += 1,永久防归零)。

        与 visits 的区别:visits 会被时间衰减稀释,base_strength 是**加在根号里面**的
        永久项 —— 一旦被考古找回过,这条记忆再怎么衰减也不会归零。
        """
        id_list = [int(i) for i in ids]
        if not id_list or amount <= 0:
            return 0
        placeholders = ",".join("?" for _ in id_list)
        cursor = self.conn.execute(
            "UPDATE summary_pool SET base_strength = base_strength + ?, heat_cache = NULL"
            " WHERE id IN (" + placeholders + ")",
            [float(amount), *id_list],
        )
        self.conn.commit()
        return cursor.rowcount

    def refresh_heat_cache(
        self, now: datetime | None = None, *, session_id: str | None = None
    ) -> int:
        """按热度公式重算 heat_cache(纯函数,读取时算)。

        ⚠️ 带 session_id 时只刷这一会话(2026-11-07):每轮结束都要调它,而家庭版
           是全体成员共库 —— 一个人聊天没必要把所有人的桶都重算一遍。分母本身
           也按会话取(见 `pool_max_heat`),两边口径一致。
        """
        moment = now or datetime.now(config.get_settings().tz)
        updated = 0
        for row in self.all_summaries(session_id):
            value = heat(
                row.base_strength,
                row.visits,
                days_since(row.last_access or row.created_at, moment),
                category=row.category,
            )
            self.conn.execute(
                "UPDATE summary_pool SET heat_cache = ? WHERE id = ?", (value, row.id)
            )
            updated += 1
        self.conn.commit()
        return updated

    # -- 事实(偏好/身份/决定)--------------------------------------------
    def add_fact(
        self,
        content: str,
        *,
        session_id: str | None = None,
        fact_type: str = "other",
        category: str = "permanent",
        keywords: Iterable[str] = (),
        confidential: bool = False,
        supersedes: int | None = None,
    ) -> int:
        """写一条永久事实;若 supersedes 指定旧事实,旧事实置 active=0(不删除)。

        ⚠️ **这里不再做去重**(2026-10-13)。它以前拿字面指纹判"同一条",有两处硬伤:
          ① 指纹自己的字符类漏了全角逗号/分号/问号 —— 一个汉字级的漏洞让去重整条失效;
          ② 就算指纹对了,字面判等也判不了"喜欢 X"与"不喜欢 X"(那是矛盾,不是重复)。
        去重与矛盾判定现在**只有一处**:候选准入闸的语义裁决
        (`fact_gate.FactGate` / `fact_adjudicator`)。多一处判等,就多一处绕过裁决的暗门。
        """
        now = _utc_now()
        cursor = self.conn.execute(
            "INSERT INTO facts(session_id, type, category, content, keywords, confidential,"
            " active, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (
                session_id,
                fact_type,
                category,
                content,
                json.dumps(list(keywords), ensure_ascii=False),
                1 if confidential else 0,
                now,
                now,
            ),
        )
        fact_id = int(cursor.lastrowid or 0)
        if supersedes is not None:
            self.conn.execute(
                "UPDATE facts SET active = 0, superseded_by = ?, updated_at = ? WHERE id = ?",
                (fact_id, now, supersedes),
            )
        self.conn.commit()
        return fact_id

    def set_fact_type(self, fact_id: int, fact_type: str) -> bool:
        """只改一条事实的类别(identity / preference / decision / other)。

        ⚠️ 这是 memory_correct「只改类别」那条分支的实现。没有它的时候,工具会回
        「只改了类别」但库里一个字节都没动 —— 说了却没发生,比报错更糟。
        """
        cursor = self.conn.execute(
            "UPDATE facts SET type = ?, updated_at = ? WHERE id = ?",
            (str(fact_type or "other"), _utc_now(), int(fact_id)),
        )
        self.conn.commit()
        return bool(cursor.rowcount)

    def supersede(self, old_fact_id: int, new_fact_id: int) -> bool:
        """把旧事实置为失效并指回新事实。**只由准入闸在新说法真的入库之后调用。**

        ⚠️ 与 set_fact_active(False) 的区别:后者会清掉 superseded_by 指针,
        而「改口」要留下 旧 -> 新 的线索(排障时能回答「它为什么改口」)。
        """
        if int(old_fact_id) == int(new_fact_id):
            return False
        cursor = self.conn.execute(
            "UPDATE facts SET active = 0, superseded_by = ?, updated_at = ? WHERE id = ?",
            (int(new_fact_id), _utc_now(), int(old_fact_id)),
        )
        self.conn.commit()
        return bool(cursor.rowcount)

    def update_fact_content(
        self, fact_id: int, content: str, *, keywords: Iterable[str] = ()
    ) -> bool:
        """就地更新一条事实的说法(裁决判为 refines 时用)。

        ⚠️ 与"改口"的区别:改口是**新增一行 + 旧的置失效**(留下两句说法的演变史);
           refines 是同一件事说得更准,不留第二行 —— 否则"细化"会被记成"改口",
           一条事实会变成两条,而两条都进上下文。
        """
        body = str(content or "").strip()
        if not body:
            return False
        cursor = self.conn.execute(
            "UPDATE facts SET content = ?, keywords = ?, updated_at = ? WHERE id = ?",
            (
                body,
                json.dumps(list(keywords), ensure_ascii=False),
                _utc_now(),
                int(fact_id),
            ),
        )
        self.conn.commit()
        return bool(cursor.rowcount)

    def get_fact(self, fact_id: int, *, include_inactive: bool = True) -> FactRow | None:
        """按 id 取一条事实。撤销"改口"要用 include_inactive=True —— 失效的那条也得能读回来。"""
        sql = "SELECT * FROM facts WHERE id = ?"
        if not include_inactive:
            sql += " AND active = 1"
        row = self.conn.execute(sql, (int(fact_id),)).fetchone()
        return _row_to_fact(row) if row is not None else None

    def set_fact_active(self, fact_id: int, active: bool) -> bool:
        """把一条事实置为生效 / 失效(置失效时清掉 superseded_by 指针)。

        ⭐ **这是"改口"与"遗忘"的唯一实现,里面没有 DELETE**:
          事实表只追加不删除(模块头的不变量第 3 条),旧说法留在库里可查,
          但不再进 <facts> 注入块,也不再被 search_facts 命中。
        """
        row = self.conn.execute(
            "SELECT id FROM facts WHERE id = ?", (int(fact_id),)
        ).fetchone()
        if row is None:
            return False
        self.conn.execute(
            "UPDATE facts SET active = ?, superseded_by = NULL, updated_at = ? WHERE id = ?",
            (1 if active else 0, _utc_now(), int(fact_id)),
        )
        self.conn.commit()
        return True

    def active_facts(
        self, *, limit: int = 50, session_id: str | None = None
    ) -> list[FactRow]:
        """生效事实。session_id 给定时只取该会话(以及历史遗留的 NULL 全局事实)。"""
        rows = self.conn.execute(
            "SELECT id, type, category, content, keywords, active, confidential, created_at"
            " FROM facts WHERE active = 1"
            " AND (? IS NULL OR session_id = ? OR session_id IS NULL)"
            " ORDER BY id DESC LIMIT ?",
            (session_id, session_id, limit),
        ).fetchall()
        return [
            FactRow(
                id=int(row["id"]),
                type=row["type"],
                category=row["category"],
                content=row["content"],
                keywords=anchors_mod.as_anchor_list(row["keywords"]),
                active=bool(row["active"]),
                created_at=row["created_at"],
                confidential=bool(row["confidential"]),
            )
            for row in rows
        ]

    def fact_coverage(self) -> dict[str, Any]:
        """事实表的覆盖域:生效 / 失效各几条、最早最晚、各类别分布。

        ⭐ 给"我现在看得到哪些数据"用 —— 只读工具靠它回答"我到底记住了什么",
        而不是让模型凭印象说"我记得你有很多偏好"。
        """
        total = int(self.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0])
        active = int(
            self.conn.execute("SELECT COUNT(*) FROM facts WHERE active = 1").fetchone()[0]
        )
        bounds = self.conn.execute(
            "SELECT MIN(created_at), MAX(created_at) FROM facts"
        ).fetchone()
        by_type = {
            str(row["type"]): int(row["n"])
            for row in self.conn.execute(
                "SELECT type, COUNT(*) AS n FROM facts WHERE active = 1 GROUP BY type"
            ).fetchall()
        }
        return {
            "total": total,
            "active": active,
            "inactive": total - active,
            "earliest": bounds[0],
            "latest": bounds[1],
            "active_by_type": by_type,
        }

    # -- 事实准入:候选队列 -----------------------------------------------
    def add_candidate(
        self,
        content: str,
        *,
        source: str = "rule",
        session_id: str | None = None,
        fact_type: str = "other",
        keywords: Iterable[str] = (),
        confidential: bool = False,
        supersedes: int | None = None,
    ) -> int:
        """把一条**候选**事实放进队列(不放进 facts,也就不会进注入)。

        幂等:同一句(归一化后)在同一个状态下只留一条 —— 用户重复说三遍不会刷三条。
        返回 0 表示它已经排过队了(调用方不必处理)。
        """
        from .guard import normalize

        body = str(content or "").strip()
        if not body:
            return 0
        normalized = normalize(body)
        if not normalized:
            return 0
        existing = self.conn.execute(
            "SELECT id FROM fact_candidates WHERE normalized = ? AND status = 'pending'",
            (normalized,),
        ).fetchone()
        if existing is not None:
            return 0
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO fact_candidates(session_id, content, normalized, fact_type,"
            " keywords, confidential, source, status, created_at, supersedes)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (
                session_id,
                body,
                normalized,
                str(fact_type or "other"),
                json.dumps(list(keywords), ensure_ascii=False),
                1 if confidential else 0,
                str(source or "rule"),
                _utc_now(),
                int(supersedes) if supersedes else None,
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid or 0)

    def pending_candidates(
        self, *, limit: int = 20, session_id: str | None = None
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM fact_candidates WHERE status = 'pending'"
        params: list[Any] = []
        if session_id:
            # NULL = 升级前的遗留候选,按旧行为交给当前会话处理一次。
            sql += " AND (session_id = ? OR session_id IS NULL)"
            params.append(session_id)
        sql += " ORDER BY id LIMIT ?"
        params.append(max(int(limit), 1))
        return self.conn.execute(sql, params).fetchall()

    def review_candidates(
        self, *, limit: int = 10, session_id: str | None = None
    ) -> list[sqlite3.Row]:
        """等人放行的候选(status='review'),旧的在前。

        ⭐ 2026-10-25:它以前只出现在记忆页的一张只读表里 —— 微信端用户说"通过吧"
           之后**没有任何入口**能把这条放行(用户实际踩到)。
           现在网页、对话工具(memory_review)都从这里取同一份列表。
        """
        sql = "SELECT * FROM fact_candidates WHERE status = 'review'"
        params: list[Any] = []
        if session_id:
            # NULL = 升级前的遗留候选,与 pending_candidates 同一条口径
            sql += " AND (session_id = ? OR session_id IS NULL)"
            params.append(session_id)
        sql += " ORDER BY id LIMIT ?"
        params.append(max(int(limit), 1))
        return self.conn.execute(sql, params).fetchall()

    def review_count(self, *, session_id: str | None = None) -> int:
        """有多少条在等人放行(只数,不取正文 —— 每轮拼上下文时用)。"""
        try:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM fact_candidates WHERE status = 'review'"
                " AND (? IS NULL OR session_id = ? OR session_id IS NULL)",
                (session_id, session_id),
            ).fetchone()
        except sqlite3.Error:
            return 0
        return int(row[0] if row else 0)

    def candidates(self, *, status: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
        if status:
            return self.conn.execute(
                "SELECT * FROM fact_candidates WHERE status = ? ORDER BY id DESC LIMIT ?",
                (status, max(int(limit), 1)),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM fact_candidates ORDER BY id DESC LIMIT ?", (max(int(limit), 1),)
        ).fetchall()

    def candidate_counts(self) -> dict[str, int]:
        counts = {"pending": 0, "admitted": 0, "rejected": 0, "review": 0}
        try:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM fact_candidates GROUP BY status"
            ).fetchall()
        except sqlite3.Error:
            return counts  # 老库还没迁移:如实返回全 0,不假装有
        for row in rows:
            counts[str(row["status"])] = int(row["n"])
        return counts

    def set_candidate_status(
        self,
        candidate_id: int,
        status: str,
        *,
        reason: str = "",
        verdict: dict[str, Any] | None = None,
        target_fact_id: int | None = None,
        fact_id: int | None = None,
    ) -> bool:
        """候选出队。**判决原文与理由都落库** —— 排障时要能回答"它当初为什么被拒"。"""
        cursor = self.conn.execute(
            "UPDATE fact_candidates SET status = ?, reason = ?, verdict = ?,"
            " target_fact_id = ?, fact_id = ?, resolved_at = ? WHERE id = ?",
            (
                str(status),
                str(reason or "")[:400],
                json.dumps(verdict, ensure_ascii=False) if verdict else None,
                int(target_fact_id) if target_fact_id else None,
                int(fact_id) if fact_id else None,
                _utc_now(),
                int(candidate_id),
            ),
        )
        self.conn.commit()
        return bool(cursor.rowcount)

    def get_candidate(self, candidate_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM fact_candidates WHERE id = ?", (int(candidate_id),)
        ).fetchone()

    # -- 注入成本 ---------------------------------------------------------
    def injection_overview(self, *, session_id: str | None = None) -> dict[str, Any]:
        """这一层**真正进上下文**的是哪些、占多少 token。

        ⚠️ 它存在的理由:注入侧固定取 id 最大的 N 条(见 `active_facts`),超了就从
           最旧那端掉出去 —— 而记忆页以前无条件列出**全部**。于是"页面看得见的"
           与"模型读得到的"可以不一样,且页面上没有任何提示。这个洞现在由
           L3 的「本轮注入」列 + 这里的上报一起堵上。
        """
        total_active = int(
            self.conn.execute(
                "SELECT COUNT(*) FROM facts WHERE active = 1"
                " AND (? IS NULL OR session_id = ? OR session_id IS NULL)",
                (session_id, session_id),
            ).fetchone()[0]
        )
        injected = self.active_facts(
            limit=config.FACT_MAX_ACTIVE, session_id=session_id
        )
        return {
            "total_active": total_active,
            "injected_ids": [int(row.id) for row in injected],
            "injected_count": len(injected),
            "max_active": int(config.FACT_MAX_ACTIVE),
            "evicted_count": max(total_active - len(injected), 0),
            "budget_tokens": int(config.FACT_BUDGET_TOKENS),
        }

    def search_facts(
        self, query: str, *, limit: int = 10, session_id: str | None = None
    ) -> list[FactRow]:
        """事实检索:锚点 + 关键词 LIKE(事实量小,不需要 FTS)。"""
        terms = [*anchors_mod.extract_anchors(query)]
        from .search import extract_terms

        terms.extend(extract_terms(query))
        if not terms:
            return []
        seen: dict[int, FactRow] = {}
        for term in terms:
            pattern = "%" + term + "%"
            rows = self.conn.execute(
                "SELECT id, type, category, content, keywords, active, confidential, created_at"
                " FROM facts WHERE active = 1 AND (content LIKE ? OR keywords LIKE ?)"
                " AND (? IS NULL OR session_id = ? OR session_id IS NULL)"
                " ORDER BY id DESC LIMIT ?",
                (pattern, pattern, session_id, session_id, limit),
            ).fetchall()
            for row in rows:
                seen[int(row["id"])] = FactRow(
                    id=int(row["id"]),
                    type=row["type"],
                    category=row["category"],
                    content=row["content"],
                    keywords=anchors_mod.as_anchor_list(row["keywords"]),
                    active=bool(row["active"]),
                    created_at=row["created_at"],
                    confidential=bool(row["confidential"]),
                )
        return list(seen.values())[:limit]

    def facts_for_context(
        self,
        query: str,
        *,
        limit: int = 20,
        session_id: str | None = None,
        budget_tokens: int | None = None,
    ) -> list[FactRow]:
        """每轮注入 <facts> 的那一批:**相关的先来,再按新旧补足,最后按预算裁**。

        ⚠️ 旧口径是 active_facts(limit=20) —— 纯按 id 倒序取最近 20 条,与这一轮问
           什么无关(2026-11-07 修)。后果是事实一多,老但相关的偏好会被新但无关的
           事实挤掉;20 条不同领域的事实还会在 1500 token 的记忆块里把摘要挤没。
        ⭐ 预算用 config.FACT_BUDGET_TOKENS(与整块 BUDGET_HOT_SUMMARY 分开记账):
           事实最多吃 600 token,剩下的留给 hot-memories —— 这就是"共用一个 top-k"
           在**预算**上的修法。
        """
        budget = config.FACT_BUDGET_TOKENS if budget_tokens is None else int(budget_tokens)
        ordered: list[FactRow] = []
        seen: set[int] = set()

        def take(row: FactRow) -> None:
            if row.id in seen:
                return
            seen.add(row.id)
            ordered.append(row)

        # 相关优先(锚点 + 关键词),再补最近的 —— search_facts 已经按 id DESC 排。
        for row in self.search_facts(query, limit=max(limit, 1), session_id=session_id):
            take(row)
        for row in self.active_facts(limit=max(limit, 1), session_id=session_id):
            take(row)

        picked: list[FactRow] = []
        used = 0
        for row in ordered:
            if len(picked) >= max(limit, 1):
                break
            cost = config.estimate_tokens(row.content) + 8
            if used + cost > budget:
                break
            picked.append(row)
            used += cost
        return picked

