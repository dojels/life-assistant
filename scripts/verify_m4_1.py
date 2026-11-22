"""M4.1 端到端验收:账单导入、三级去重、规则回写、月度聚合、Beancount 导出。

每一步都是**独立进程**(真重启),数据目录隔离:
    .venv/Scripts/python.exe scripts/verify_m4_1.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
DATA_DIR = tempfile.mkdtemp(prefix="la-verify41-")
BILL_DIR = tempfile.mkdtemp(prefix="la-bill-")

os.environ["LA_DATA_DIR"] = DATA_DIR
os.environ["LA_LLM_PROVIDER"] = "fake"
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.path.insert(0, str(BASE_DIR))

RESULTS: list[tuple[str, bool, str]] = []

ALIPAY = """支付宝交易记录明细查询
账号:[test@example.com]
---------------------------------交易记录明细列表------------------------------------
交易时间,交易分类,交易对方,对方账号,商品说明,收/支,金额,收/付款方式,交易状态,交易订单号,商家订单号,备注
2026-10-08 12:30:00,餐饮美食,老张牛肉面,zhang***,午饭,支出,38.00,余额宝,交易成功,A001,M1,
2026-10-08 18:10:00,交通出行,滴滴出行,didi***,打车回家,支出,26.50,花呗,交易成功,A002,M2,
2026-10-12 20:00:00,餐饮美食,海底捞,hai***,聚餐,支出,268.00,银行卡,交易成功,A003,M3,
2026-10-15 09:00:00,其他,某公司,,退款,收入,38.00,余额,交易成功,A004,M4,
------------------------------------------------------------------------------------
共 3 笔记录
已全部展示
"""


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + ("   " + detail if detail else ""))


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, "-m", "la", *args],
        cwd=str(BASE_DIR),
        env=dict(os.environ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )


def main() -> int:
    print("验收数据目录: " + DATA_DIR)
    bill = Path(BILL_DIR) / "alipay_2026_10.csv"
    bill.write_text(ALIPAY, encoding="utf-8")

    print("\n[1/5] 账单导入")
    proc = run("--import-bill", str(bill))
    out = proc.stdout
    check("导入进程正常退出", proc.returncode == 0, proc.stderr.strip()[:120])
    # ⚠️ 2026-10-17 审计:导入器现在**也导收入行**(账本支持 income),摘要变成
    #    "新增 4 笔(其中收入 1 笔)"。这条检查的本意是"收入行不会被算成支出",
    #    所以改成同时看两个数,而不是死认"新增 3 笔"这个字符串。
    check("导入了 3 笔支出 + 1 笔收入(收入不算进支出)",
          "新增 4 笔" in out and "其中收入 1 笔" in out, out.strip()[:100])

    proc = run("--ledger", "10")
    check("账本里能看到这 3 笔", proc.stdout.count("老张牛肉面") == 1 and "海底捞" in proc.stdout)
    check("导入的账直接是已确认状态", "[confirmed]" in proc.stdout)

    print("\n[2/5] 重复导入必须被挡住")
    proc = run("--import-bill", str(bill))
    check("同文件二次导入被 sha256 短路", "已导入过" in proc.stdout, proc.stdout.strip()[:100])
    renamed = Path(BILL_DIR) / "copy.csv"
    renamed.write_text(ALIPAY, encoding="utf-8")
    proc = run("--import-bill", str(renamed))
    check("改名后仍然认得出来(认内容不认文件名)", "已导入过" in proc.stdout)

    proc = run("--ledger", "10")
    # 3 笔支出 + 1 笔收入 = 4 行(见上一条:导入器也导收入行)。
    check("账本没有变多", proc.stdout.count("- #") == 4,
          "交易数=" + str(proc.stdout.count("- #")))

    print("\n[3/5] 月度聚合:数字全部来自 SQL")
    proc = run("--query-expense", "2026-10")
    out = proc.stdout
    check("合计正确(3800+2650+26800=33250 分)", "33250 分" in out, out.strip().splitlines()[1] if out else "")
    check("餐饮合计正确(3800+26800)", "餐饮: 30600 分" in out, out.strip()[:160])
    check("交通合计正确", "交通: 2650 分" in out)
    check("回复标注来源", "来源: ledger" in out)
    check("账单自带的'餐饮美食'被解析成'餐饮'", "餐饮美食" not in out)

    print("\n[4/5] 规则回写:纠正一次,下次自动分对")
    # 离线假客户端不会自发调工具,所以用**脚本化**的假客户端驱动"对话记账"这条链路;
    # 真实模型自发调工具的能力由 scripts/smoke_real_api.py 验证。
    import asyncio  # noqa: E402
    import dataclasses  # noqa: E402
    import json  # noqa: E402

    from la import config  # noqa: E402
    from la.agent.session import MemorySession  # noqa: E402
    from la.domain.audit import AuditSink  # noqa: E402
    from la.domain.rules import RuleEngine, Subject  # noqa: E402
    from la.domain.services.ledger_service import LedgerService  # noqa: E402
    from la.llm.client import ToolCall  # noqa: E402
    from la.llm.fake import FakeClient  # noqa: E402
    from la.memory.store import MemoryStore  # noqa: E402
    from la.storage import db  # noqa: E402
    from la.storage.migrations import MIGRATIONS_MEMORY  # noqa: E402

    conn = db.connect_assistant()
    memory_conn = db.connect_memory()
    db.migrate(memory_conn, MIGRATIONS_MEMORY)
    store = MemoryStore(memory_conn, archive_dir=Path(DATA_DIR) / "archive")
    service = LedgerService(conn, audit=AuditSink(conn))

    # ⚠️ 2026-10-17 审计:记账工具后来加了"**不许瞎猜分类**"这条合同 ——
    #    规则没命中时它返回 needs_category + 候选,由模型挑一个再记。
    #    旧脚本只喂一次不带分类的调用,于是"记上了"这条永远不成立(等于脚本失效)。
    #    现在两步走:先验证"不带分类时拒绝 + 候选",再带分类真记一笔。
    scripted = FakeClient(tool_script=[
        [
            ToolCall(id="c0", name="expense_record",
                     arguments=json.dumps({"amount_cents": 100, "payee": "无人店铺",
                                           "source": "chat"})),
        ],
        [
            ToolCall(id="c1", name="expense_record",
                     arguments=json.dumps({"amount_cents": 2800, "payee": "星巴克",
                                           "source": "chat", "narration": "拿铁",
                                           "category": "餐饮"})),
        ],
    ])
    settings = dataclasses.replace(
        config.get_settings(reload=True), provider="fake", session_id="corr"
    )
    # ⚠️ 2026-10-17 审计:这里原来没传 auto_approve —— 而 M7/D65 之后"助手写入"
    #    走的是"直写"(settings.direct_write,默认 True),不传就等于把门禁全关着,
    #    于是这个脚本从直写上线那天起就一直红着(模型发起的工具调用被当成"需要确认")。
    #    现在跟着真实配置走:LA_DIRECT_WRITE=0 时仍然验证老的门禁路径。
    session = MemorySession(
        scripted, settings, store=store, session_id="corr",
        assistant_conn=conn, auto_briefing=False,
        auto_approve=settings.direct_write,
    )
    session.start()
    asyncio.run(session.respond("记一笔:星巴克 28 元"))

    # 第一条(没有分类)必须**一个字都没写**:候选要由模型/用户来定,不许它自己编。
    uncategorized = conn.execute(
        "SELECT count(*) FROM app_txns WHERE payee = '无人店铺'").fetchone()[0]
    check("规则没命中时不许瞎猜分类", uncategorized == 0,
          "没有分类的那一笔被写进去了" if uncategorized else "拒绝并给出候选")

    row = conn.execute(
        "SELECT id, status FROM app_txns WHERE payee LIKE '%星巴克%' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    check("模型发起的工具调用写进了账本", row is not None, "交易 " + str(row["id"]) if row else "无")
    # M7(D65)之后默认是**直写**:助手记的账直接生效,安全网换成"回执 + 一键撤销"。
    # 想验证老的草稿路径,把 LA_DIRECT_WRITE 设为 0 再跑(那边的门禁代码原样保留)。
    check("助手写入直接入账(直写模式)", row is not None and row["status"] == "confirmed",
          str(row["status"]) if row else "")
    receipt = conn.execute(
        "SELECT id, domain, label FROM ops_receipts ORDER BY id DESC LIMIT 1"
    ).fetchone()
    check("⭐ 这一笔留下了写入回执(项目级机制)",
          receipt is not None and receipt["domain"] == "ledger",
          (receipt["label"] if receipt else "无回执"))

    before = conn.execute(
        "SELECT c.name FROM app_postings p LEFT JOIN app_categories c ON c.id = p.category_id"
        " WHERE p.txn_id = ? AND p.amount_cents > 0",
        (row["id"],),
    ).fetchone()
    # 分类来自**模型显式给出的**那一个(而不是工具替它猜的)。
    check("分类来自模型显式给出的值", before["name"] == "餐饮", str(before["name"]))

    service.correct_last(txn_id=row["id"], category="餐饮", actor="user")
    rules = RuleEngine(conn).list()
    check("纠正被回写成规则", any(r.origin == "learned" for r in rules),
          str([r.as_dict() for r in rules]))
    matched = RuleEngine(conn).match(Subject(peer="星巴克"))
    check("同商户下次能命中规则", matched is not None, str(matched.as_dict()) if matched else "无")

    # 闭环:再来一笔同商户的,应当**自动**分到餐饮(先规则、规则未命中再靠模型)
    second = service.record_expense(
        {"amount_cents": 3300, "payee": "星巴克", "source": "chat"}, actor="agent"
    )
    check("同商户的下一笔被规则自动分好类", second.category == "餐饮", str(second.category))
    hit = RuleEngine(conn).list()[0].hit_count
    check("规则命中计数在涨", hit >= 1, "命中 " + str(hit) + " 次")
    conn.close()
    memory_conn.close()

    print("\n[5/5] Beancount 导出与存疑行")
    proc = run("--export-beancount")
    text = proc.stdout
    check("导出含正确的账户名", "Expenses:餐饮" in text and "Assets:支付宝" in text)
    check("导出含金额", "38.00 CNY" in text)
    check("导出是只读快照", text.startswith("; 由家庭助手导出"))
    amounts = [line.strip().split()[-2] for line in text.splitlines()
               if line.startswith("  ") and "CNY" in line]
    total = sum(float(v) for v in amounts)
    check("每笔分录之和为零", abs(total) < 1e-6, "合计 " + str(total))
    conn.close()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n结果: " + str(passed) + "/" + str(len(RESULTS)) + " 项通过")
    for name, ok, _ in RESULTS:
        if not ok:
            print("  未通过: " + name)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())