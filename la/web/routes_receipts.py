"""写入回执:列表 + 撤销。

从 create_app() 搬出来的第七块业务路由(2026-10-18 架构收口)。
两本回执账(a=assistant / m=memory)在这里合并;id 带来源前缀消歧。
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends

from ..domain.receipts import ReceiptError
from .deps import Services, get_services
from .visibility import receipt_visible


def register_receipt_routes(app) -> None:
    @app.get("/api/receipts")
    def api_receipts(
        limit: int = 20, services: Services = Depends(get_services)
    ) -> dict[str, Any]:
        """统一写入回执列表。纯读,不需要密钥,不经过模型。

        ⚠️ **两本账**:领域写入的回执在 assistant.db,事项层(待办/目标/提醒)与
        记忆改口的回执在 memory.db(回执必须与写入同库同事务)。
        所以这里要合起来读,并且给 id 带上来源前缀 —— 两张表的 id 各自从 1 开始,
        不区分的话「撤销 #3」会撤到另一条上。撤销时把前缀解开就行。
        """
        if services.receipts is None:
            return {"receipts": [], "open": 0}
        cap = max(1, min(limit, 100))
        merged: list[tuple[int, str, dict[str, Any]]] = []
        for prefix, log in (("a", services.receipts), ("m", services.memory_receipts)):
            if log is None:
                continue
            for item in log.recent(limit=cap):
                if not receipt_visible(services, item):
                    continue
                payload = item.as_dict()
                payload["id"] = prefix + ":" + str(item.id)
                payload["source"] = prefix
                merged.append((item.id, prefix, payload))
        merged.sort(key=lambda row: row[0], reverse=True)
        return {
            "receipts": [row[2] for row in merged[:cap]],
            "open": services.receipts.count_open()
            + (services.memory_receipts.count_open() if services.memory_receipts else 0),
        }

    @app.post("/api/receipts/undo")
    def api_receipts_undo(payload: dict[str, Any], services: Services = Depends(get_services)):
        """撤销一次写入。**同一个入口管五个域** —— 前端不需要知道领域。

        载荷二选一:
          {"receipt_id": 12}     撤这一条(id 可以带 "a:" / "m:" 前缀,见 /api/receipts)
          {"domain": "ledger"}   撤该域最近一条(不传 domain 就是全局最近一条)
        """
        if services.reverter is None:
            return {"ok": False, "error": "当前没有可撤销的写入通道"}
        try:
            if payload.get("receipt_id") is not None:
                raw_id = str(payload["receipt_id"])
                # "m:3" / "a:3" 的前缀是**消歧**用的:两张回执表 id 各自从 1 开始。
                # ⚠️ 以前前缀被剥掉就丢了,选路按"主本优先" —— 撤待办会撤到网页写入。
                source = ""
                if ":" in raw_id:
                    prefix, raw_id = raw_id.split(":", 1)
                    source = prefix if prefix in ("a", "m") else ""
                # 就算前端只给裸 id,也不能让“两本账同号”撤错:取可见的、
                # 且 ts 最新的那一条(与 _receipt_dict 的展示口径一致)。
                chosen: tuple[str, Any] | None = None
                for prefix, log in (("a", services.receipts), ("m", services.memory_receipts)):
                    if log is None or (source and prefix != source):
                        continue
                    try:
                        candidate = log.get(int(raw_id))
                    except Exception:
                        continue
                    if not receipt_visible(services, candidate):
                        continue
                    if chosen is None or str(candidate.ts) > str(chosen[1].ts):
                        chosen = (prefix, candidate)
                if chosen is None:
                    return {"ok": False, "error": "没有这条写入回执(或它不属于当前成员)"}
                source, _receipt = chosen
                result = services.reverter.undo(int(raw_id), source=source or None)
            else:
                result = services.reverter.undo_last(domain=payload.get("domain") or None)
        except ReceiptError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - 撤销失败要如实说,不能静默
            return {"ok": False, "error": type(exc).__name__ + ": " + str(exc)[:160]}
        return {"ok": True, "note": result["note"], "receipt": result["receipt"]}

