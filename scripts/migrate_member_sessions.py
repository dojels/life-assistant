"""一次性迁移:把旧 Web / 微信会话合并成成员的 `member:<id>` 时间线。

它只做三件事,顺序不能换:

1. **合并原文**:从旧 archive 读事件,按时间排序,重新编号 1..N,写
   `member%3A<id>.jsonl`;旧 archive 原样保留。
2. **改归属**:facts / fact_candidates 的 session_id 改成 member;旧摘要行
   改成 member 会话,并把 start_seq/end_seq 改成负数(= 原 id),避免和
   新时间线的正 seq 区间撞 UNIQUE;文本/时间/热度原样保留。
3. **建绑定**:把微信联系人绑到成员(供 Bridge.thread 解析)。

⚠️ 不碰 ops_receipts / ops_questions:它们有"只追加/只能答一次"的数据库
   触发器,改 session_id 会被数据库直接拒绝;它们属于历史审计,不是记忆源。

用法:
    .venv/Scripts/python.exe scripts/migrate_member_sessions.py            # 预演
    .venv/Scripts/python.exe scripts/migrate_member_sessions.py --apply    # 真正写
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from la import config  # noqa: E402
from la.family import identity  # noqa: E402
from la.memory.archive import Event, EventLog, archive_filename  # noqa: E402
from la.memory.store import MemoryStore  # noqa: E402
from la.storage import db  # noqa: E402
from la.storage.migrations import MIGRATIONS_ASSISTANT  # noqa: E402


def _pick_sessions(conn: sqlite3.Connection, explicit: list[str], contact: str) -> list[str]:
    rows = [
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT session_id FROM events WHERE session_id IS NOT NULL"
        )
    ]
    if explicit:
        return [item for item in explicit if item]
    selected: list[str] = []
    if "web" in rows:
        selected.append("web")
    if contact:
        selected.append("bridge:" + contact)
    else:
        bridges = [row for row in rows if row.startswith("bridge:") and row.count(":") == 1]
        if bridges:
            counts = {
                row: conn.execute(
                    "SELECT COUNT(*) FROM events WHERE session_id = ?", (row,)
                ).fetchone()[0]
                for row in bridges
            }
            selected.append(max(counts, key=counts.get))
    return selected


def _read_events(archive_dir: Path, session_id: str) -> list[Event]:
    try:
        return EventLog(session_id, archive_dir).read()
    except OSError as exc:
        print("!! 读不了档案:", session_id, type(exc).__name__, exc)
        return []


def _merge(archive_dir: Path, sessions: list[str]) -> list[Event]:
    rows: list[tuple[str, int, int, Event]] = []
    for order, session_id in enumerate(sessions):
        events = _read_events(archive_dir, session_id)
        print("  读", session_id, len(events), "条")
        for event in events:
            rows.append((str(event.time), order, int(event.seq), event))
    rows.sort(key=lambda item: (item[0], item[1], item[2]))
    merged: list[Event] = []
    for seq, (_time, _order, _old_seq, event) in enumerate(rows, 1):
        merged.append(
            Event(
                seq=seq,
                type=event.type,
                time=event.time,
                data=event.data,
                surface=event.surface,
            )
        )
    return merged


def _write_archive(archive_dir: Path, target: Path, events: list[Event], *, force: bool) -> None:
    if target.exists() and not force:
        raise SystemExit("目标档案已存在:" + str(target) + "  (加 --force 才覆盖)")
    archive_dir.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup = target.with_name(target.name + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(target, backup)
        print("  旧目标另存:", backup.name)
    tmp = target.with_name(target.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        for event in events:
            handle.write(event.to_line() + "\n")
    os.replace(tmp, target)


def _update_memory(
    conn: sqlite3.Connection,
    *,
    member_session: str,
    sessions: list[str],
    events: list[Event],
    force: bool,
) -> dict[str, int]:
    placeholders = ",".join("?" for _ in sessions)
    old_facts = conn.execute(
        "SELECT COUNT(*) FROM facts WHERE session_id IN (" + placeholders + ")", sessions
    ).fetchone()[0]
    old_summaries = conn.execute(
        "SELECT COUNT(*) FROM summary_pool WHERE session_id IN (" + placeholders + ")", sessions
    ).fetchone()[0]
    before_events = conn.execute(
        "SELECT COUNT(*) FROM events WHERE session_id = ?", (member_session,)
    ).fetchone()[0]
    if before_events and not force:
        raise SystemExit(
            "目标会话已有 " + str(before_events) + " 条事件;加 --force 才重建"
        )

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM events WHERE session_id = ?", (member_session,))
        conn.execute("DELETE FROM events WHERE session_id IN (" + placeholders + ")", sessions)
        conn.executemany(
            "INSERT OR REPLACE INTO events(session_id, seq, type, time, surface, data)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    member_session,
                    event.seq,
                    event.type,
                    event.time,
                    1 if event.surface else 0,
                    json.dumps(event.data, ensure_ascii=False),
                )
                for event in events
            ],
        )
        conn.execute(
            "UPDATE facts SET session_id = ? WHERE session_id IN (" + placeholders + ")",
            [member_session, *sessions],
        )
        try:
            conn.execute(
                "UPDATE fact_candidates SET session_id = ?"
                " WHERE session_id IN (" + placeholders + ")",
                [member_session, *sessions],
            )
        except sqlite3.Error:
            pass
        # 旧摘要:改成 member 会话 + 负 seq(每行唯一),文本/时间/热度保留。
        rows = conn.execute(
            "SELECT id FROM summary_pool WHERE session_id IN (" + placeholders + ")",
            sessions,
        ).fetchall()
        for (summary_id,) in rows:
            conn.execute(
                "UPDATE summary_pool SET session_id = ?, start_seq = ?, end_seq = ?"
                " WHERE id = ?",
                (member_session, -int(summary_id), -int(summary_id), int(summary_id)),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "facts": old_facts,
        "summaries": old_summaries,
        "events": len(events),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--member", type=int, default=identity.DEFAULT_MEMBER_ID)
    parser.add_argument("--sessions", default="", help="逗号分隔;留空自动挑 web + 唯一的微信联系人")
    parser.add_argument("--weixin-contact", default="", help="微信 external_id(不带 bridge: 前缀)")
    parser.add_argument("--memory-db", default=str(config.MEMORY_DB))
    parser.add_argument("--assistant-db", default=str(config.ASSISTANT_DB))
    parser.add_argument("--archive-dir", default=str(config.ARCHIVE_DIR))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--force", action="store_true", help="目标会话已有数据时重建")
    args = parser.parse_args()

    memory_db = Path(args.memory_db)
    assistant_db = Path(args.assistant_db)
    archive_dir = Path(args.archive_dir)
    if not memory_db.exists():
        print("找不到 memory.db:", memory_db)
        return 2
    if not archive_dir.exists():
        print("找不到 archive 目录:", archive_dir)
        return 2

    member_session = identity.session_id_for_member(args.member)
    mconn = sqlite3.connect(str(memory_db))
    mconn.row_factory = sqlite3.Row
    try:
        explicit = [item.strip() for item in args.sessions.split(",") if item.strip()]
        sessions = _pick_sessions(mconn, explicit, args.weixin_contact.strip())
        if not sessions:
            print("没有可合并的旧会话;用 --sessions 或 --weixin-contact 指定")
            return 2
        print("合并到:", member_session)
        print("来源:", ", ".join(sessions))
        events = _merge(archive_dir, sessions)
        print("合并后事件:", len(events))
        if args.apply:
            # 先写档案,再改库:档案是事实源;档案写失败时库还没动。
            target = archive_dir / archive_filename(member_session)
            _write_archive(archive_dir, target, events, force=args.force)
            stats = _update_memory(
                mconn,
                member_session=member_session,
                sessions=sessions,
                events=events,
                force=args.force,
            )
            MemoryStore(mconn, archive_dir=archive_dir).ensure_session(
                member_session, title="家庭"
            )
            print("迁移统计:", stats)
        else:
            # 预演也要报摘要数,但不写
            placeholders = ",".join("?" for _ in sessions)
            print(
                "预演: facts=",
                mconn.execute(
                    "SELECT COUNT(*) FROM facts WHERE session_id IN (" + placeholders + ")",
                    sessions,
                ).fetchone()[0],
                " summaries=",
                mconn.execute(
                    "SELECT COUNT(*) FROM summary_pool WHERE session_id IN (" + placeholders + ")",
                    sessions,
                ).fetchone()[0],
            )
    finally:
        mconn.close()

    if args.apply:
        conn = db.connect(assistant_db)
        try:
            db.migrate(conn, MIGRATIONS_ASSISTANT)
            identity.ensure_default_member(conn)
            if args.weixin_contact.strip():
                identity.bind_member(
                    conn, "weixin", args.weixin_contact.strip(), args.member
                )
                print("绑定: weixin/" + args.weixin_contact.strip() + " -> " + member_session)
        finally:
            conn.close()
        print("完成:", target.name, len(events), "条")
    else:
        print("预演完成;加 --apply 才写档案/改库/建绑定。旧档案不会删除。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
