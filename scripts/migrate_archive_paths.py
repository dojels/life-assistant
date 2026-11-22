"""一次性迁移:把带冒号的会话档案从 NTFS ADS 复制到普通 JSONL。

背景
----
旧版 EventLog 直接用 `session_id + ".jsonl"` 拼路径。微信的会话 id 是
`bridge:o9cq…@im.wechat`,在 Windows 上冒号会被解释成 NTFS 备用数据流(ADS):
落盘是一个 0 字节的 `bridge` 文件 + 若干不可见的流。资源管理器、zip、robocopy
都看不到这些流,备份会静默漏掉整个微信档案。

本脚本只做**复制**:旧文件/流保留不动(归档只增不改),新文件用
`la.memory.archive.archive_filename()` 生成的 percent-encoded 名字。
重复运行是安全的:新文件已存在就跳过。

用法
----
    .venv/Scripts/python.exe scripts/migrate_archive_paths.py          # 预演
    .venv/Scripts/python.exe scripts/migrate_archive_paths.py --apply  # 真正复制
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from la import config  # noqa: E402
from la.memory.archive import EventLog, archive_filename  # noqa: E402


def _sessions(conn: sqlite3.Connection) -> list[str]:
    rows: set[str] = set()
    queries = (
        "SELECT DISTINCT session_id FROM events WHERE session_id IS NOT NULL",
        "SELECT DISTINCT session_id FROM summary_pool WHERE session_id IS NOT NULL",
        "SELECT id FROM sessions",
    )
    for sql in queries:
        try:
            for (sid,) in conn.execute(sql):
                if sid:
                    rows.add(str(sid))
        except sqlite3.Error:
            continue
    return sorted(rows)


def _line_count(data: bytes) -> int:
    return sum(1 for line in data.splitlines() if line.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(config.MEMORY_DB), help="memory.db 路径")
    parser.add_argument("--archive-dir", default=str(config.ARCHIVE_DIR), help="archive 目录")
    parser.add_argument("--apply", action="store_true", help="真正复制;不加只预演")
    args = parser.parse_args()

    db_path = Path(args.db)
    archive_dir = Path(args.archive_dir)
    if not db_path.exists():
        print("找不到 memory.db:", db_path)
        return 2
    if not archive_dir.exists():
        print("找不到 archive 目录:", archive_dir)
        return 2

    conn = sqlite3.connect(db_path)
    try:
        sessions = _sessions(conn)
    finally:
        conn.close()

    migrated = 0
    skipped = 0
    for sid in sessions:
        old = archive_dir / (sid + ".jsonl")
        new = archive_dir / archive_filename(sid)
        if old == new:
            continue
        if not old.exists():
            print("skip(无旧档案):", sid)
            skipped += 1
            continue
        try:
            data = old.read_bytes()
        except OSError as exc:
            print("skip(读不了):", sid, type(exc).__name__, exc)
            skipped += 1
            continue
        if not data:
            print("skip(空文件):", sid)
            skipped += 1
            continue
        print(
            "迁移:" + sid + " -> " + new.name
            + "  " + str(len(data)) + " bytes / " + str(_line_count(data)) + " lines"
        )
        if not args.apply:
            continue
        log = EventLog(sid, archive_dir)
        if not new.exists():
            print("  !! 新文件没有生成:", new)
            skipped += 1
            continue
        if new.read_bytes() != data:
            print("  !! 新文件内容与旧档案不一致:", new)
            skipped += 1
            continue
        print("  ok  migrated_from_legacy=" + str(log.migrated_from_legacy))
        migrated += 1

    print("---")
    print("apply=" + str(args.apply) + " migrated=" + str(migrated) + " skipped=" + str(skipped))
    if not args.apply:
        print("预演完成;加 --apply 才会复制。旧文件/ADS 不会被删除。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
