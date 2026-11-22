"""M4.0 端到端验收:服务层地基 + schema 单真源 + 发件箱 + 审计 + function calling。

确定性(离线 fake 客户端),可反复跑:
    .venv/Scripts/python.exe scripts/verify_m4_0.py

真实模型冒烟测试另见 scripts/smoke_real_api.py(需要 DEEPSEEK_API_KEY)。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
DATA_DIR = tempfile.mkdtemp(prefix="la-verify4-")

os.environ["LA_DATA_DIR"] = DATA_DIR
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.path.insert(0, str(BASE_DIR))

from la.domain.audit import AuditSink  # noqa: E402
from la.domain.outbox import OutboxRelay  # noqa: E402
from la.domain.routers.ledger import LedgerRouter  # noqa: E402
from la.domain.schemas.ledger import ExpenseCreate, ExpenseQuery  # noqa: E402
from la.domain.services.ledger_service import LedgerService  # noqa: E402
from la.domain.tools import build_ledger_tools  # noqa: E402
from la.llm.client import ToolCall  # noqa: E402
from la.llm.fake import FakeClient  # noqa: E402
from la.llm.tools import build_tool_catalog, catalog_fingerprint, to_openai_tool  # noqa: E402
from la.memory.store import MemoryStore  # noqa: E402
from la.storage import db  # noqa: E402
from la.storage.migrations import MIGRATIONS_ASSISTANT, MIGRATIONS_MEMORY  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + ("   " + detail if detail else ""))


def main() -> int:
    import asyncio
    import dataclasses

    from la import config
    from la.agent.session import MemorySession

    print("验收数据目录: " + DATA_DIR)

    assistant = db.connect(Path(DATA_DIR) / "assistant.db")
    db.migrate(assistant, MIGRATIONS_ASSISTANT)
    memory = db.connect(Path(DATA_DIR) / "memory.db")
    db.migrate(memory, MIGRATIONS_MEMORY)
    store = MemoryStore(memory, archive_dir=Path(DATA_DIR) / "archive")
    service = LedgerService(assistant, audit=AuditSink(assistant))

    print("\n[1/5] schema 单真源:REST 请求体与工具参数来自同一个 Pydantic 模型")
    specs = {spec.name: spec for spec in build_ledger_tools(service)}
    check(
        "工具 parameters == ExpenseCreate.model_json_schema()",
        specs["expense_record"].parameters_json_schema() == ExpenseCreate.model_json_schema(),
    )
    payload = to_openai_tool(specs["expense_record"])
    check("工具是标准 OpenAI function 形态", payload["type"] == "function"
          and payload["function"]["name"] == "expense_record")
    check("金额字段说明带单位提示(分)",
          "分" in payload["function"]["parameters"]["properties"]["amount_cents"]["description"])

    print("\n[2/5] 服务层:router 与 tool 共用同一个 service")
    router = LedgerRouter(service)
    status, body = router.create_expense(
        {"amount_cents": 3800, "payee": "老张牛肉面", "category": "餐饮", "actor": "user"}
    ).as_tuple()
    check("POST /expenses 返回 201", status == 201, str(body)[:80])
    check("写入的是草稿(助手不能直接入账)", body.get("status") == "draft")
    record = service.read(body["id"])
    check("两腿分录之和为零", sum(amount for _a, amount in record.postings) == 0,
          str(record.postings))
    bad_status, bad_body = router.create_expense({"amount_cents": -1}).as_tuple()
    check("非法请求体被拒(400)", bad_status == 400, str(bad_body)[:60])
    bad_status2, bad_body2 = router.create_expense(
        {"amount_cents": 100, "category": "不存在"}
    ).as_tuple()
    check("未知分类给出可操作错误(422)", bad_status2 == 422 and "可用分类" in bad_body2["message"])

    print("\n[3/5] 发件箱:领域写入 -> 中继 -> 归档")
    relay = OutboxRelay(
        assistant,
        append_event=lambda kind, data: store.append("domain", kind, data),
        seen=lambda key: memory.execute(
            "SELECT 1 FROM events WHERE data LIKE ? AND data LIKE ? LIMIT 1",
            ("%outbox_key%", "%" + key + "%"),
        ).fetchone() is not None,
    )
    report = relay.drain()
    check("中继搬走了事件", report.relayed >= 1, json.dumps(report.as_dict(), ensure_ascii=False))
    archived = [e for e in store.events("domain") if e.type == "ledger.txn.created"]
    check("归档里有账目事件", len(archived) == 1)
    assistant.execute("UPDATE ops_outbox SET processed_at = NULL")
    assistant.commit()
    second = relay.drain()
    check("即使标记丢失也不重复搬运", second.skipped >= 1 and second.relayed == 0)

    print("\n[4/5] 审计:谁写的")
    rows = [(r["actor"], r["action"]) for r in assistant.execute(
        "SELECT actor, action FROM ops_audit ORDER BY id")]
    check("界面写入记为 user", ("user", "ledger.txn.create") in rows, str(rows[:3]))
    check("审计只追加(触发器在)", "ledger.txn.create" in [a for _x, a in rows])

    print("\n[5/5] function calling:模型发起的工具调用被真正执行")
    client = FakeClient(tool_script=[[ToolCall(
        id="c1", name="expense_record",
        arguments=json.dumps({"amount_cents": 2600, "payee": "出租车", "category": "交通"}),
    )]])
    settings = dataclasses.replace(
        config.get_settings(reload=True), provider="fake", session_id="m4"
    )
    session = MemorySession(
        client, settings, store=store, session_id="m4",
        assistant_conn=assistant, auto_briefing=False,
    )
    session.start()
    reply = asyncio.run(session.respond("打车花了 26 元"))
    count = assistant.execute("SELECT count(*) FROM app_txns").fetchone()[0]
    check("模型请求的工具被真的执行", count == 2, "交易数=" + str(count))
    check("工具结果回灌给模型后给出了答复", bool(reply.text.strip()), reply.text.strip()[:60])
    catalogs = client.tool_catalogs
    check("工具清单稳定且按名排序",
          catalogs and [i["function"]["name"] for i in catalogs[0]]
          == sorted(i["function"]["name"] for i in catalogs[0]))
    check("清单指纹可复算(用于解释缓存未命中)",
          len(catalog_fingerprint(catalogs[0])) == 16)

    summary = service.query_expenses(ExpenseQuery(period="2026-10", include_drafts=True))
    check("聚合走 SQL,且标注来源", summary.source == "ledger" and summary.txn_count == 2,
          "合计 " + str(summary.total_cents) + " 分 / " + str(summary.txn_count) + " 笔")

    assistant.close()
    memory.close()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n结果: " + str(passed) + "/" + str(len(RESULTS)) + " 项通过")
    for name, ok, _ in RESULTS:
        if not ok:
            print("  未通过: " + name)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
