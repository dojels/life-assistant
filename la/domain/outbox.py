"""事件发件箱中继:领域写入 -> 记忆事件(单向流动,能力域 §3)。

铁律:
  * 领域数据 -> 记忆:✅ 自动(本模块)
  * 记忆 -> 领域数据:⚠️ 只能经用户确认
  * 记忆 -> 回答事实:❌ 禁止

事务性:领域写入与 outbox 写入**在同一个事务里**(见 ledger_service);
本模块只在事务提交之后搬运,所以失败可以重试,顺序也有保证。
"""

from __future__ import annotations
from ..base.clock import utc_now_iso as _utc_now

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable




@dataclass
class DrainReport:
    scanned: int = 0
    relayed: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "relayed": self.relayed,
            "skipped": self.skipped,
            "failed": self.failed,
            "errors": list(self.errors),
        }


class OutboxRelay:
    """把 ops_outbox 里未处理的事件搬进归档(记忆的事实源)。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        append_event: Callable[[str, dict[str, Any]], Any],
        seen: Callable[[str], bool] | None = None,
    ) -> None:
        """conn 是**领域库**(ops_outbox 在那里);seen 是往记忆库反查的回调。

        ⚠️ 这两者必须在不同的库上:events 表在 memory.db,ops_outbox 在 assistant.db。
        把反查也写到 conn 上会直接报 "no such table: events"。
        """
        self.conn = conn
        self._append = append_event
        self._seen = seen

    def pending(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, event_type, payload, idempotency_key, attempts FROM ops_outbox"
            " WHERE processed_at IS NULL ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()

    def drain(self, limit: int = 50) -> DrainReport:
        report = DrainReport()
        for row in self.pending(limit):
            report.scanned += 1
            key = str(row["idempotency_key"])
            try:
                payload = json.loads(row["payload"] or "{}")
            except json.JSONDecodeError:
                payload = {"raw": row["payload"]}
            payload = dict(payload)
            payload["outbox_key"] = key

            if self._already_relayed(key):
                report.skipped += 1
                self._mark(row["id"], row["attempts"], _utc_now())
                continue

            try:
                self._append(str(row["event_type"]), payload)
            except Exception as exc:  # 失败留在发件箱里等下次,不丢
                report.failed += 1
                report.errors.append(type(exc).__name__ + ": " + str(exc)[:120])
                self.conn.execute(
                    "UPDATE ops_outbox SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                    (str(exc)[:300], row["id"]),
                )
                self.conn.commit()
                continue

            report.relayed += 1
            self._mark(row["id"], row["attempts"], _utc_now())
        return report

    # -- 内部 -------------------------------------------------------------
    def _mark(self, outbox_id: int, attempts: int, processed_at: str | None) -> None:
        self.conn.execute(
            "UPDATE ops_outbox SET processed_at = COALESCE(?, processed_at), attempts = ?"
            " WHERE id = ?",
            (processed_at, attempts + 1, outbox_id),
        )
        self.conn.commit()

    def _already_relayed(self, key: str) -> bool:
        """幂等兜底:中继到一半崩了,靠事件里的 outbox_key 认出已经搬过的。

        归档是只追加的,不能靠"改标记"保证恰好一次,所以用内容反查。
        """
        if self._seen is None:
            return False
        try:
            return bool(self._seen(key))
        except Exception:
            return False  # 反查失败时宁可多搬一次,也不能卡住中继
