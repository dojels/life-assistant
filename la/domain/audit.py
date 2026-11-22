"""审计:无论谁写入,都记 ops_audit(谁写的:user | agent | import | system)。

审计表是**只追加**的(数据库触发器强制),所以这里只有写、没有改。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

ACTOR_USER = "user"
ACTOR_AGENT = "agent"
ACTOR_IMPORT = "import"
ACTOR_SYSTEM = "system"
ACTORS = (ACTOR_USER, ACTOR_AGENT, ACTOR_IMPORT, ACTOR_SYSTEM)


def record(
    conn: sqlite3.Connection,
    *,
    actor: str,
    action: str,
    target: str | None = None,
    detail: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO ops_audit(ts, actor, action, target, detail, session_id)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            actor if actor in ACTORS else ACTOR_SYSTEM,
            action,
            target,
            json.dumps(detail or {}, ensure_ascii=False, default=str),
            session_id,
        ),
    )


class AuditSink:
    """把审计做成可注入的回调,让 service 层不必直接依赖连接对象。"""

    def __init__(self, conn: sqlite3.Connection | None) -> None:
        self.conn = conn
        #: 写失败过几次(测试与自检可以读它 —— 静默降级不等于无声无息)。
        self.failures = 0

    def __call__(
        self,
        *,
        actor: str,
        action: str,
        target: str | None = None,
        detail: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> None:
        if self.conn is None:
            return
        try:
            record(
                self.conn,
                actor=actor,
                action=action,
                target=target,
                detail=detail,
                session_id=session_id,
            )
        except Exception as exc:  # noqa: BLE001 - 审计是附注,不是写入门票
            # ⚠️ 2026-10-17 审计:**审计排在 commit 之后,却能把"已写成功"变成一次异常** ——
            #    工具层把异常转成失败 Result,模型于是对用户说"没记上",而钱已经记了,
            #    用户会再记一遍。审计失败只该降级:业务数据是对的,少一条附注。
            self.failures += 1
            if self.failures <= 3:      # 只吵前三次,别把日志刷满
                import sys

                print("[audit] 审计写入失败(已忽略,业务数据不受影响):"
                      + type(exc).__name__ + ": " + str(exc), file=sys.stderr)
