"""聊天页的纯支撑:SSE 帧、回执水位线、步骤账本适配。

从 create_app()/app.py 搬出来(2026-10-18 架构收口)。不碰 app、模板或请求状态。
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..agent.step_ledger import KIND_TOOL, TOOL_KINDS, ExecutionLedger
from .deps import Services, build_model_client
from .visibility import receipt_visible


def _now_ms() -> int:
    return int(time.time() * 1000)


def _sse(event: str, data: Any) -> str:
    """一帧 SSE。data 一律是合法 JSON(裸字符串事件也要 json.dumps 一次)。"""
    return "event: " + event + "\ndata: " + json.dumps(data, ensure_ascii=False) + "\n\n"


def _receipt_dict(services: Services, receipt_id: int, source: str = "") -> dict[str, Any]:
    """取一条回执给前端。取不到就返回最小形状 —— 让前端能显示,而不是空白。

    ⚠️ 两本回执账的 id 各自从 1 自增(2026-10-18 审计修):同号时以前"谁先有就用谁",
       于是「待办 #5」的回执卡会显示成「网页 #5」。现在同号取**最新写的**那条
       (本轮刚写的那条 ts 最大),并在显式给了 source(a/m)时只认那一本。
    """
    candidates: list[tuple[str, Any]] = []
    for prefix, log in (("a", services.receipts), ("m", services.memory_receipts)):
        if log is None or (source and prefix != source):
            continue
        try:
            candidates.append((prefix, log.get(int(receipt_id))))
        except Exception:
            continue
    if not candidates:
        return {"id": int(receipt_id), "label": "已写入", "undoable": False}
    prefix, newest = max(candidates, key=lambda pair: (str(pair[1].ts), int(pair[1].id)))
    # ⚠️ 必须带 source(2026-10-18 审计):两张回执表的 id 各自从 1 自增,
    #    前端只回传裸 id 时,撤销会按“主本优先”撤到另一条写入。
    newest.source = prefix
    return newest.as_dict()


def _receipt_marks(services: Services, session_id: str) -> dict[str, int]:
    """两本回执账各记各的水位线(不能混用同一个数,见 MemorySession.receipt_marks)。"""
    marks: dict[str, int] = {}
    for key, log in (("a", services.receipts), ("m", services.memory_receipts)):
        if log is None:
            continue
        try:
            marks[key] = int(log.last_id(session_id=session_id) or 0)
        except Exception:
            marks[key] = 0
    return marks


def _receipts_since(services: Services, marks: dict[str, int], session_id: str) -> list[Any]:
    found: list[Any] = []
    for key, log in (("a", services.receipts), ("m", services.memory_receipts)):
        if log is None:
            continue
        try:
            fresh = log.since(int(marks.get(key, 0)), session_id=session_id)
        except Exception:
            continue
        for item in fresh:
            item.source = key
        found.extend(fresh)
    return sorted(found, key=lambda item: (str(item.ts), int(item.id)))


# 首屏最多渲染多少条历史消息(只影响显示,不影响模型看到的内容)
HISTORY_LIMIT = 20

def _build_client(settings: config.Settings):
    """聊天用的客户端。口径与 CLI / 视觉识别**同一份**(见 deps.build_model_client)。"""
    from .deps import build_model_client

    return build_model_client(settings)


class _TraceStep:
    """把 MemorySession 的 `on_step` 回调接到 ExecutionLedger 上。

    这一层只做两件事:字号/字段名对齐,以及**忽略没开过的 end**(防御)。
    """

    def __init__(self, ledger: Any, queue: "asyncio.Queue") -> None:
        self.ledger = ledger
        self.queue = queue

    def __call__(self, kind: str, data: dict[str, Any]) -> None:
        if kind != "step":
            return
        key = str(data.get("key") or "")
        status = str(data.get("status") or "running")
        tool_kind, domain = _lookup_tool(key)
        # ⚠️ 优先用事件里带的(注册表给的)领域/kind:前端不再自己维护一份映射。
        tool_kind = str(data.get("kind") or tool_kind)
        domain = str(data.get("domain") or domain)
        if status == "running":
            row = self.ledger.begin(
                str(data.get("title") or ""), kind=tool_kind, key=key, domain=domain
            )
            step = row.as_dict()
        else:
            if key not in self.ledger._index:  # noqa: SLF001 - 同模块内的防御性检查
                return
            self.ledger.end(
                key,
                str(data.get("title") or ""),
                ok=status != "failed",
                detail=str(data.get("detail") or ""),
            )
            step = self.ledger._index[key].as_dict()  # noqa: SLF001
        payload: dict[str, Any] = {"step": step}
        receipt_id = data.get("receipt_id")
        if receipt_id:
            payload["receipt_id"] = int(receipt_id)
        self.queue.put_nowait(("step", payload))


def _lookup_tool(key: str) -> tuple[str, str]:
    """tool:<名字> -> (kind, 领域)。认不出来就当成普通工具。"""
    from ..agent.step_ledger import KIND_TOOL, TOOL_KINDS

    if not key.startswith("tool:"):
        return "node", ""
    found = TOOL_KINDS.get(key[5:])
    return found if found is not None else (KIND_TOOL, "")


