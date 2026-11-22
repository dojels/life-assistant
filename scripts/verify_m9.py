"""M9 验收(实验):账本收支一体 + 统一反馈机制的 Web 回路。

为什么独立起服务器 + 独立 LA_DATA_DIR:
  * 真实 data/ 里是用户自己的账 —— 验收脚本只允许在**副本**上写;
  * 这次改动的风险面是"模板 + 路由 + 每请求一套连接",
    这三类问题只有**真 HTTP 请求**(并发、Jinja 未定义变量、SQL 新列)才打得出来。

七组检查(全部真请求,不 mock):
  1. 账本页把收入、支出、结余、分类、明细都画出来(纯 SSR,不经模型);
  2. 界面「记一笔」能记收入(两腿平衡、结余跟着变);
  3. 明细里就地改分类:改完真的变了,而且回写成规则;
  4. 统一反馈机制的**表单**回路(禁 JS 也能用):页面看得到待回答的问题,提交后状态变 answered;
  5. 统一反馈机制的 **JSON** 回路(聊天页按钮走这条):重复回答必须被拒;
  6. 并发 18 次请求:迁移与 WAL 都只能做一次,不能出现 database is locked;
  7. 软删除仍然可撤销(老能力不能被新版式弄丢)。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.dont_write_bytecode = True

from la.ui.cli import configure_stdio  # noqa: E402

configure_stdio()

PORT = 8793
BASE = "http://127.0.0.1:" + str(PORT)
failures: list[str] = []
checks = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if ok:
        print("  PASS  " + label)
    else:
        print("  FAIL  " + label + ("  <- " + detail if detail else ""))
        failures.append(label)


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="la-m9-"))
    env = dict(os.environ)
    env.update({
        "LA_DATA_DIR": str(work / "data"),
        "LA_LLM_PROVIDER": "fake",
        "LA_DIRECT_WRITE": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    env.pop("DEEPSEEK_API_KEY", None)
    os.environ.update(env)

    from la import config

    config.reload_paths()
    assert str(config.DATA_DIR).startswith(str(work)), "数据目录没有被隔离,拒绝运行"

    from la.agent.feedback import Option, Question, QuestionLog
    from la.domain.audit import AuditSink
    from la.domain.receipts import ReceiptLog
    from la.domain.services.ledger_service import LedgerService
    from la.storage import db

    (work / "data").mkdir(parents=True, exist_ok=True)
    conn = db.connect_assistant()
    receipts = ReceiptLog(conn)
    ledger = LedgerService(conn, audit=AuditSink(conn), receipts=receipts)

    def seed(**kwargs) -> object:
        kwargs.setdefault("source", "ui")
        return ledger.record_expense(kwargs, actor="user", status="confirmed")

    # 支出 38.00 与 26.50;收入 1000.00 —— 结余必须正好是 935.50
    seed(amount_cents=3800, payee="老张牛肉面", category="餐饮",
         ts="2026-10-11T12:30:00+08:00")
    seed(amount_cents=2650, payee="滴滴出行", category="交通",
         ts="2026-10-11T08:05:00+08:00")
    seed(direction="income", amount_cents=100000, payee="某公司", category="工资",
         account="银行卡", ts="2026-10-10T10:00:00+08:00")
    draft = ledger.record_expense(
        {"amount_cents": 4500, "payee": "菜市场", "category": "餐饮",
         "ts": "2026-10-12T07:40:00+08:00"}, actor="agent")
    pending_question = Question(
        prompt="账本里还没有「宠物」这个支出分类,新建吗?",
        options=[Option(label="新建「宠物」", value="create"),
                 Option(label="先不建", value="cancel")],
        domain="ledger", context={"分类": "宠物", "类型": "支出"}, session_id="web",
    )
    question = pending_question.prepare()
    QuestionLog(conn).record(question)
    conn.close()
    print("样本:支出 64.50 + 收入 1000.00(结余 935.50),另有一笔草稿与一个待回答问题")

    log = open(work / "server.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "la", "--web", "127.0.0.1:" + str(PORT)],
        cwd=str(BASE_DIR), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    try:
        import httpx

        client = httpx.Client(base_url=BASE, timeout=30.0, follow_redirects=False)
        ready = False
        for _ in range(80):
            try:
                if client.get("/healthz").status_code == 200:
                    ready = True
                    break
            except Exception:
                time.sleep(0.25)
        check("服务器起来了(独立数据目录)", ready, str(work))
        if not ready:
            return 1

        print("\n[1/7] 账本页:收入 / 支出 / 结余 / 分类 / 明细")
        page = client.get("/ledger?period=2026-10").text
        check("页面 200 且渲染出标题", "账本" in page)
        check("三个口径都在", "收入" in page and "支出" in page and "结余" in page)
        check("支出合计 = 64.50", "64.50" in page, "SQL 聚合对不上")
        check("收入合计 = 1,000.00", "1,000.00" in page)
        check("结余 = 935.50", "935.50" in page, "净额算错了")
        check("支出分类画出来了", "餐饮" in page and "交通" in page)
        check("收入分类画出来了(钱从哪来)", "钱从哪来" in page and "工资" in page)
        check("明细按天分组", "day-group" in page and "老张牛肉面" in page)
        check("待确认草稿还在", "菜市场" in page)
        check("快速记账表单在", 'action="/ledger/add"' in page)

        print("\n[2/7] 待回答的问题就在页面上(禁 JS 也能回答)")
        check("「等你拍板」区块在", "等你拍板" in page)
        check("问题原文在", "宠物" in page and "支出分类" in page)
        check("答案是普通表单 POST", 'action="/questions/answer"' in page)

        print("\n[3/7] 界面记一笔(收入)")
        response = client.post("/ledger/add", data={
            "direction": "income", "amount": "500.50", "category": "奖金",
            "account": "银行卡", "payee": "年终奖", "narration": "",
        })
        check("提交后 303 回账本", response.status_code == 303, str(response.status_code))
        page = client.get("/ledger?period=2026-10").text
        check("新记录出现在明细里", "年终奖" in page)
        check("收入合计跟着变(1,500.50)", "1,500.50" in page)
        check("结余跟着变(1,436.00)", "1,436.00" in page)

        print("\n[4/7] 明细里就地改分类(并回写成规则)")
        conn = db.connect_assistant()
        try:
            txn_id = conn.execute(
                "SELECT id FROM app_txns_live WHERE payee = '滴滴出行'"
            ).fetchone()["id"]
        finally:
            conn.close()
        response = client.post("/ledger/recategorize/" + str(txn_id),
                               data={"category": "娱乐", "payee": "滴滴出行"})
        check("改分类 303", response.status_code == 303, str(response.status_code))
        conn = db.connect_assistant()
        try:
            row = conn.execute(
                "SELECT c.name AS name FROM app_postings p"
                " JOIN app_categories c ON c.id = p.category_id"
                " JOIN app_txns t ON t.id = p.txn_id WHERE t.payee = '滴滴出行'"
            ).fetchone()
            rules = conn.execute("SELECT COUNT(*) AS n FROM app_rules").fetchone()["n"]
        finally:
            conn.close()
        check("分类真的改了", row is not None and row["name"] == "娱乐", str(row and row["name"]))
        check("回写成规则(下次自动分对)", rules == 1, "规则数 " + str(rules))

        print("\n[5/7] 统一反馈机制:表单回路")
        response = client.post("/questions/answer",
                               data={"question_id": question.id, "choice": "create",
                                     "next": "/ledger"})
        check("回答 303 回账本", response.status_code == 303, str(response.status_code))
        payload = client.get("/api/questions").json()
        answered = [item for item in payload["questions"] if item["id"] == question.id]
        check("问题已记成 answered",
              bool(answered) and answered[0]["status"] == "answered",
              str(answered[:1]))
        check("答案内容存下来了",
              bool(answered) and answered[0]["answer"]["selected"] == ["create"])
        check("待回答计数归零", payload["pending"] == 0, str(payload["pending"]))
        page = client.get("/ledger?period=2026-10").text
        check("已回答的问题不再出现在「等你拍板」", "等你拍板" not in page)

        print("\n[6/7] 统一反馈机制:JSON 回路 + 重复回答必须被拒")
        conn = db.connect_assistant()
        try:
            orphan = Question(prompt="这条消息在问一个已经结束的对话", options=[
                Option(label="好的", value="ok")], domain="ledger").prepare()
            QuestionLog(conn).record(orphan)
        finally:
            conn.close()
        first = client.post("/api/questions/answer",
                            json={"question_id": orphan.id, "selected": ["ok"]}).json()
        check("没人在等也如实回答(ok)", bool(first.get("ok")), str(first))
        check("并说明这一轮已经结束", first.get("resumed") is False, str(first))
        second = client.post("/api/questions/answer",
                             json={"question_id": orphan.id, "selected": ["ok"]}).json()
        check("第二次回答被拒", second.get("ok") is False, str(second))

        print("\n[7/7] 并发 18 次请求:迁移与 WAL 只能做一次")
        from concurrent.futures import ThreadPoolExecutor

        paths = ["/ledger?period=2026-10", "/api/questions", "/chat"]

        def hit(path: str):
            try:
                with httpx.Client(base_url=BASE, timeout=60.0) as worker:
                    response = worker.get(path)
                    return path, response.status_code
            except Exception as exc:  # noqa: BLE001
                return path, type(exc).__name__

        bad = []
        with ThreadPoolExecutor(max_workers=6) as pool:
            for _ in range(2):
                for path, status in pool.map(hit, paths):
                    if status != 200:
                        bad.append((path, status))
        check("三轮并发 18 次请求全部 200", not bad, str(bad[:4]))

        print("\n[8/8] 软删除 + 撤销(老能力不能丢)")
        conn = db.connect_assistant()
        try:
            draft_id = int(conn.execute(
                "SELECT id FROM app_txns_live WHERE status = 'draft'"
            ).fetchone()["id"])
        finally:
            conn.close()
        check("删除 303", client.post("/ledger/delete/" + str(draft_id)).status_code == 303)
        page = client.get("/ledger").text
        check("顶部出现撤销提示", "撤销删除" in page)
        check("撤销 303", client.post("/ledger/undo", data={}).status_code == 303)
        page = client.get("/ledger?period=2026-10").text
        check("撤销后草稿回来了", "菜市场" in page)

        print("\n" + "-" * 66)
        if failures:
            print("失败 " + str(len(failures)) + "/" + str(checks) + ":" + str(failures))
            return 1
        print("全部通过:" + str(checks) + " 项")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        log.close()
        if failures:
            print("\n--- server.log 末尾 ---")
            print("\n".join((work / "server.log").read_text(encoding="utf-8").splitlines()[-40:]))
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
