"""撤销(M6.3):把"刚才那步"退回去。

为什么单独一层,而不是在每个服务里各写各的:
  * **撤销是横切的** —— 账本、日历、错题都会需要,逻辑不能复制三份;
  * **撤销必须留下痕迹** —— ops_undo_log 和审计一样只追加(触发器强制),
    所以"我撤回过"这件事本身也是不可否认的;
  * **撤销只能撤一次** —— 触发器限制 used_at 只能从 NULL 变成非 NULL。

与审计(ops_audit)的分工:
    ops_audit   = 谁在什么时候做了什么(不可变的行为记录)
    ops_undo_log= 怎么退回去(一次性、会失效的反向指令)
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from ..memory.archive import now_iso


class UndoError(Exception):
    """撤销不可执行(已用过 / 没有可撤销的操作 / 目标已不在)。"""


@dataclass
class UndoEntry:
    id: int
    ts: str
    actor: str
    kind: str
    label: str
    table_name: str
    row_id: int
    before: dict[str, Any]
    after: dict[str, Any] | None
    used_at: str | None

    @property
    def used(self) -> bool:
        return self.used_at is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "ts": self.ts, "kind": self.kind, "label": self.label,
            "table": self.table_name, "row_id": self.row_id,
            "used": self.used, "used_at": self.used_at, "before": self.before,
        }


class UndoLog:
    """撤销日志。它**只记账**,不执行 —— 真正的反向操作由调用方给的回调做。

    这样设计的原因:只有领域服务知道"撤销这次删除"意味着什么
    (要改哪张表、要不要发发件箱事件)。UndoLog 只保证"只记一次、只用一次"。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def record(
        self, *, kind: str, label: str, table_name: str, row_id: int,
        before: dict[str, Any], after: dict[str, Any] | None = None,
        actor: str = "user", session_id: str | None = None,
    ) -> int:
        cursor = self.conn.execute(
            "INSERT INTO ops_undo_log(ts, actor, kind, label, table_name, row_id,"
            " before_json, after_json, session_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                now_iso(), actor, kind, label, table_name, int(row_id),
                json.dumps(before, ensure_ascii=False, default=str),
                json.dumps(after, ensure_ascii=False, default=str) if after is not None else None,
                session_id,
            ),
        )
        return int(cursor.lastrowid or 0)

    def recent(self, *, limit: int = 10, unused_only: bool = True) -> list[UndoEntry]:
        clause = " WHERE used_at IS NULL" if unused_only else ""
        rows = self.conn.execute(
            "SELECT * FROM ops_undo_log" + clause + " ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def get(self, entry_id: int) -> UndoEntry:
        row = self.conn.execute(
            "SELECT * FROM ops_undo_log WHERE id = ?", (entry_id,)
        ).fetchone()
        if row is None:
            raise UndoError("没有这条撤销记录: " + str(entry_id))
        return self._row_to_entry(row)

    def mark_used(self, entry_id: int) -> None:
        cursor = self.conn.execute(
            "UPDATE ops_undo_log SET used_at = ? WHERE id = ? AND used_at IS NULL",
            (now_iso(), entry_id),
        )
        if cursor.rowcount == 0:
            raise UndoError("这条撤销记录已经用过了: " + str(entry_id))

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> UndoEntry:
        return UndoEntry(
            id=int(row["id"]), ts=row["ts"], actor=row["actor"], kind=row["kind"],
            label=row["label"], table_name=row["table_name"], row_id=int(row["row_id"]),
            before=json.loads(row["before_json"]),
            after=json.loads(row["after_json"]) if row["after_json"] else None,
            used_at=row["used_at"],
        )


__all__ = ["UndoError", "UndoEntry", "UndoLog"]
