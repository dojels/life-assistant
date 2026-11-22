"""一次性重生成指定摘要(默认 dry-run,不写库)。

用途:2026-11-21 之前 LLM 摘要因缺少 "- " 前缀被校验器全部拒掉,历史摘要
(#22/#23)是抽取式兜底,长且锚点过多。修复后可以只重生成这些有原始事件的摘要。

    python scripts/regenerate_summaries.py --session-id member:1 --id 22 --id 23
    python scripts/regenerate_summaries.py --session-id member:1 --id 22 --id 23 --apply

安全边界:
  * 默认 dry-run,只打印旧/新长度、来源、锚点数;
  * --apply 先备份整个 memory.db 到 data/backups/,再更新 summary_pool;
  * 只接受 source=llm 且 valid=true 的结果;LLM 失败/超长时跳过,不覆盖旧摘要;
  * 每次更新追加 summary/regenerated 事件(不 surface),可追查。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from la import config
from la.agent.session import COMPACTION_EVENT_TYPES
from la.llm.factory import build_client
from la.memory.store import MemoryStore
from la.memory.summarize import build_summarizer
from la.storage import db


def _backup_db(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect("file:" + str(source) + "?mode=ro", uri=True)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()


async def main() -> int:
    parser = argparse.ArgumentParser(description="重生成指定摘要(默认 dry-run)")
    parser.add_argument("--session-id", default="member:1", help="会话 id,默认 member:1")
    parser.add_argument("--id", type=int, action="append", required=True, help="summary_pool id,可重复")
    parser.add_argument("--apply", action="store_true", help="真的写库(默认只 dry-run)")
    args = parser.parse_args()

    settings = config.get_settings(reload=True)
    conn = db.connect(config.MEMORY_DB)
    store = MemoryStore(conn, archive_dir=config.ARCHIVE_DIR)
    summarizer = build_summarizer(build_client(settings), settings)

    rows = list(
        conn.execute(
            "SELECT * FROM summary_pool WHERE session_id = ? AND id IN ("
            + ",".join("?" for _ in args.id)
            + ") ORDER BY id",
            (args.session_id, *args.id),
        )
    )
    print("db", config.MEMORY_DB)
    print("provider", settings.provider, "model", settings.model)
    print("summaries", [int(row["id"]) for row in rows], "apply", bool(args.apply))

    backup: Path | None = None
    audit: Path | None = None
    if args.apply:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = config.DATA_DIR / "backups" / ("memory-before-summary-regen-" + stamp + ".db")
        _backup_db(Path(config.MEMORY_DB), backup)
        audit = backup.with_suffix(".json")
        print("backup", backup)

    results: list[dict] = []
    for row in rows:
        summary_id = int(row["id"])
        events = [
            event
            for event in store.events(
                args.session_id, int(row["start_seq"]), int(row["end_seq"])
            )
            if event.type in COMPACTION_EVENT_TYPES
        ]
        if not events:
            print("skip", summary_id, "区间里没有可摘要事件")
            continue
        draft = await summarizer.summarize(
            events, start_time=row["start_time"], end_time=row["end_time"]
        )
        old_text = str(row["text"] or "")
        can_apply = draft.source == "llm" and draft.valid
        item = {
            "summary_id": summary_id,
            "start_seq": int(row["start_seq"]),
            "end_seq": int(row["end_seq"]),
            "old_chars": len(old_text),
            "old_tokens": config.estimate_tokens(old_text),
            "new_chars": len(draft.text),
            "new_tokens": config.estimate_tokens(draft.text),
            "anchors": len(draft.anchors),
            "source": draft.source,
            "valid": bool(draft.valid),
            "problems": list(draft.problems),
            "can_apply": can_apply,
        }
        results.append(item)
        print(
            "summary", summary_id,
            "old", item["old_chars"], "/", item["old_tokens"],
            "new", item["new_chars"], "/", item["new_tokens"],
            "anchors", item["anchors"],
            "source", item["source"],
            "valid", item["valid"],
            "problems", item["problems"],
        )
        if args.apply and can_apply:
            conn.execute(
                "UPDATE summary_pool SET text = ?, anchors = ?, heat_cache = NULL WHERE id = ?",
                (draft.text, json.dumps(draft.anchors, ensure_ascii=False), summary_id),
            )
            conn.commit()
            store.append(
                args.session_id,
                "summary/regenerated",
                {
                    "summary_id": summary_id,
                    "start_seq": int(row["start_seq"]),
                    "end_seq": int(row["end_seq"]),
                    "old_chars": item["old_chars"],
                    "new_chars": item["new_chars"],
                    "source": draft.source,
                    "valid": bool(draft.valid),
                    "problems": list(draft.problems),
                    "at": config.format_local(settings.now()),
                },
            )
            print("applied", summary_id)
        elif args.apply:
            print("skip", summary_id, "结果不是 valid LLM 摘要,不覆盖旧摘要")

    if args.apply and audit is not None:
        audit.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print("audit", audit)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
