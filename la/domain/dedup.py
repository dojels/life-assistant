"""三级去重键(能力域 §5.1)。

单靠订单号不够 —— 它可能缺失,退款单号也不同,而且重复导出的时间戳可能差几秒。所以:

  一级:(source, 交易订单号)                       最可靠,优先用
  二级:sha256(时间 + 收支 + 金额 + 对方 + 商品 + 方式)  订单号缺失时的兜底
  三级:(日期, 金额, 对方) 在 ±N 天窗口内模糊匹配      命中后**要求人工确认**

外加一层:文件级 file_sha256 —— 同一个文件重复上传直接短路。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta

FUZZY_WINDOW_DAYS = 3


def level1_key(source: str, order_id: str | None) -> str | None:
    order = (order_id or "").strip()
    if not order:
        return None
    return "L1:" + source + ":" + order


def level2_key(
    *,
    source: str,
    ts: str,
    direction: str,
    amount_cents: int,
    peer: str | None,
    item: str | None,
    method: str | None,
) -> str:
    parts = [source, _date_part(ts), direction, str(amount_cents), peer or "", item or "", method or ""]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]
    return "L2:" + digest


def _date_part(ts: str) -> str:
    """只取到日 —— 导出格式的时间常有几秒差异,秒级参与哈希会导致漏判重。"""
    return (ts or "")[:10]


@dataclass
class DedupDecision:
    level: str = "new"          # L1 | L2 | L3 | new
    key: str | None = None
    duplicate_txn_id: int | None = None
    fuzzy_candidates: list[int] = field(default_factory=list)
    needs_confirmation: bool = False

    def as_dict(self) -> dict:
        return {
            "level": self.level,
            "key": self.key,
            "duplicate_txn_id": self.duplicate_txn_id,
            "fuzzy_candidates": list(self.fuzzy_candidates),
            "needs_confirmation": self.needs_confirmation,
        }


def find_duplicate(
    conn,
    *,
    source: str,
    ts: str,
    direction: str,
    amount_cents: int,
    peer: str | None,
    item: str | None,
    method: str | None,
    order_id: str | None,
    fuzzy_window_days: int = FUZZY_WINDOW_DAYS,
) -> DedupDecision:
    """按三级顺序判断这笔是不是已经记过了。"""
    key1 = level1_key(source, order_id)
    if key1 is not None:
        row = conn.execute(
            "SELECT id FROM app_txns_live WHERE dedup_key = ? OR idempotency_key = ? LIMIT 1",
            (key1, key1),
        ).fetchone()
        if row is not None:
            return DedupDecision(level="L1", key=key1, duplicate_txn_id=int(row["id"]))

    key2 = level2_key(
        source=source, ts=ts, direction=direction, amount_cents=amount_cents,
        peer=peer, item=item, method=method,
    )
    row = conn.execute("SELECT id FROM app_txns_live WHERE dedup_key = ? LIMIT 1", (key2,)).fetchone()
    if row is not None:
        return DedupDecision(level="L2", key=key2, duplicate_txn_id=int(row["id"]))

    # 三级:同日/邻近日期、同金额、同对方 —— 只报候选,绝不自动合并
    day = _date_part(ts)
    try:
        center = datetime.fromisoformat(day)
    except ValueError:
        return DedupDecision(level="new", key=key2)
    start = (center - timedelta(days=fuzzy_window_days)).date().isoformat()
    end = (center + timedelta(days=fuzzy_window_days + 1)).date().isoformat()
    rows = conn.execute(
        "SELECT id, ts, payee FROM app_txns_live"
        " WHERE substr(ts, 1, 10) >= ? AND substr(ts, 1, 10) < ?"
        " AND payee IS ? AND id IN ("
        "   SELECT txn_id FROM app_postings WHERE amount_cents = ?"
        " )",
        (start, end, peer, amount_cents),
    ).fetchall()
    candidates = [int(row["id"]) for row in rows]
    if candidates:
        return DedupDecision(
            level="L3", key=key2, fuzzy_candidates=candidates, needs_confirmation=True
        )
    return DedupDecision(level="new", key=key2)


def file_fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
