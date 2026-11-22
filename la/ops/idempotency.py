"""幂等键语义(能力域 §6.2④)。

| 场景 | 行为 | HTTP |
| :--- | :--- | :--- |
| 首次(键 + 载荷指纹都没见过) | 正常处理 | 201 |
| 重复(**键与指纹都相同**,且已完成) | 返回先前结果 | 200 |
| 重复但**载荷不同** | 拒绝(键复用) | **422** |
| 并发重复(同一键正在处理中) | 拒绝 | **409** |
| 缺键 | 拒绝 | **400** |

⚠️ 设计文档提醒这条草案已经过期、从未成为 RFC,但 Stripe/Adyen 都在用 —— 值得照做。
"""

from __future__ import annotations
from ..base.clock import utc_now_iso as _utc_now

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

class IdempotencyConflict(RuntimeError):
    """同一个幂等键被并发/不同载荷复用。

    以前 `run_once` 把 conflict / in_progress 当成“已完成”返回 replay,
    结果是“没写、也不报错”(2026-10-18 审计)。现在由 Web 层转成 409。
    """

    def __init__(self, decision: "Decision") -> None:
        super().__init__(decision.kind)
        self.decision = decision


KIND_NEW = "new"
KIND_REPLAY = "replay"
KIND_CONFLICT = "conflict"
KIND_IN_PROGRESS = "in_progress"
KIND_MISSING_KEY = "missing_key"

HTTP_BY_KIND = {
    KIND_NEW: 201,
    KIND_REPLAY: 200,
    KIND_CONFLICT: 422,
    KIND_IN_PROGRESS: 409,
    KIND_MISSING_KEY: 400,
}


def fingerprint(payload: object) -> str:
    """载荷指纹:键排序后哈希,保证同样的内容得到同样的值。"""
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]




@dataclass
class Decision:
    kind: str
    key: str
    fingerprint: str
    response: str | None = None

    @property
    def http_status(self) -> int:
        return HTTP_BY_KIND.get(self.kind, 400)

    @property
    def should_process(self) -> bool:
        return self.kind == KIND_NEW

    def as_dict(self) -> dict:
        return {"kind": self.kind, "http_status": self.http_status, "key": self.key,
                "fingerprint": self.fingerprint, "response": self.response}


class IdempotencyStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def begin(self, key: str, payload: object) -> Decision:
        """请求开始时调用。返回的 Decision 决定"做不做、回什么"。"""
        if not key or not str(key).strip():
            return Decision(kind=KIND_MISSING_KEY, key="", fingerprint="")

        digest = fingerprint(payload)
        row = self.conn.execute(
            "SELECT fingerprint, status, response FROM sys_idempotency WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            try:
                self.conn.execute(
                    "INSERT INTO sys_idempotency(key, fingerprint, status, created_at, updated_at)"
                    " VALUES (?, ?, 'in_progress', ?, ?)",
                    (key, digest, _utc_now(), _utc_now()),
                )
                self.conn.commit()
                return Decision(kind=KIND_NEW, key=key, fingerprint=digest)
            except sqlite3.IntegrityError:
                # 并发同 key 的 SELECT→INSERT 竞态:对方先插了,重新读一次,
                # 按“已存在”的语义返回,而不是把 IntegrityError 抛成 500。
                self.conn.rollback()
                row = self.conn.execute(
                    "SELECT fingerprint, status, response FROM sys_idempotency WHERE key = ?",
                    (key,),
                ).fetchone()
                if row is None:
                    raise

        if row["fingerprint"] != digest:
            return Decision(kind=KIND_CONFLICT, key=key, fingerprint=digest,
                            response=row["response"])
        if row["status"] == "completed":
            return Decision(kind=KIND_REPLAY, key=key, fingerprint=digest,
                            response=row["response"])
        if row["status"] == "failed":
            # ⚠️ 上一次失败了 —— 同一个 key **必须**能重试。
            # 否则客户端一次 500 就被永久钉在 409 上,而它其实什么都没做成。
            self.conn.execute(
                "UPDATE sys_idempotency SET status = 'in_progress', response = NULL,"
                " updated_at = ? WHERE key = ?",
                (_utc_now(), key),
            )
            self.conn.commit()
            return Decision(kind=KIND_NEW, key=key, fingerprint=digest)
        return Decision(kind=KIND_IN_PROGRESS, key=key, fingerprint=digest)

    def complete(self, key: str, response: object) -> None:
        self.conn.execute(
            "UPDATE sys_idempotency SET status = 'completed', response = ?, updated_at = ?"
            " WHERE key = ?",
            (json.dumps(response, ensure_ascii=False, default=str), _utc_now(), key),
        )
        self.conn.commit()

    def fail(self, key: str, error: str = "") -> None:
        self.conn.execute(
            "UPDATE sys_idempotency SET status = 'failed', response = ?, updated_at = ?"
            " WHERE key = ?",
            (error[:500], _utc_now(), key),
        )
        self.conn.commit()

    def forget(self, key: str) -> None:
        """失败后允许重试时清掉记录。"""
        self.conn.execute("DELETE FROM sys_idempotency WHERE key = ?", (key,))
        self.conn.commit()
