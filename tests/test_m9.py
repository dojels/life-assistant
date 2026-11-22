"""M9 验收测试:统一反馈机制 + 账本收支一体 + 分类决策链。

⭐ 核心 DoD(五条,逐条对应):
   1. **收入**能记、能查、能导入,口径与支出分得开(expense / income / all);
   2. **没有分类的一笔不会先落账** —— 工具返回 needs_category + 候选,由模型挑;
   3. 候选都不合适时 category_create **弹确认框**,用户点头才建分类,而且可以撤销;
   4. **统一反馈机制**:ask_user 的问题经 SSE "ask" 帧下发,
      POST /api/questions/answer 之后模型拿到回答并接着往下做;
      超时/跳过**不等于同意**;
   5. 账本页把收入、支出、结余、分类、明细、待拍板的问题都画出来(纯 SSR,不经模型)。

全部离线跑(LA_LLM_PROVIDER=fake),不需要密钥。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from la import config
from la.agent import feedback
from la.domain.schemas.ledger import ExpenseQuery, TxnCreate
from la.domain.services.ledger_service import LedgerError, LedgerService, UnknownCategoryError
from la.llm.client import ToolCall
from la.llm.fake import FakeClient
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT, migrate
from la.web import app as web_app


class Base(unittest.TestCase):
    def setUp(self) -> None:
        # ⚠️ Windows 上 SQLite 的文件锁会让 rmtree 偶发失败;测试的连接未必都关得上,
        #    所以这里明确允许"清理失败就算了"(数据在临时目录里,不影响任何真实数据)。
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self._patched = {
            key: getattr(config, key)
            for key in ("DATA_DIR", "ARCHIVE_DIR", "MEDIA_DIR", "ASSISTANT_DB", "MEMORY_DB")
        }
        config.DATA_DIR = self.tmp
        config.ARCHIVE_DIR = self.tmp / "archive"
        config.MEDIA_DIR = self.tmp / "media"
        config.ASSISTANT_DB = self.tmp / "assistant.db"
        config.MEMORY_DB = self.tmp / "memory.db"
        self._env = {
            key: os.environ.get(key)
            for key in ("DEEPSEEK_API_KEY", "LA_LLM_PROVIDER", "LA_DIRECT_WRITE")
        }
        os.environ["DEEPSEEK_API_KEY"] = ""
        os.environ["LA_LLM_PROVIDER"] = "fake"
        config._settings = None
        # 每个测试一套干净库
        self.conn = db.connect_assistant()
        migrate(self.conn, MIGRATIONS_ASSISTANT)
        self.ledger = LedgerService(self.conn)

    def tearDown(self) -> None:
        web_app._build_client = self._real_build if hasattr(self, "_real_build") else web_app._build_client
        try:
            self.conn.close()
        except Exception:
            pass
        for key, value in self._patched.items():
            setattr(config, key, value)
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config._settings = None
        self._tmp.cleanup()


# ==========================================================================
# 1. 账本:收入与支出
# ==========================================================================
class IncomeLedgerCase(Base):
    def test_income_has_two_balanced_legs_and_positive_amount(self):
        record = self.ledger.record_expense(
            TxnCreate(direction="income", amount_cents=800000, payee="某公司",
                      category="工资", account="银行卡"),
            status="confirmed",
        )
        self.assertEqual(record.direction, "income")
        self.assertEqual(record.amount_cents, 800000, "金额永远为正,方向看 direction")
        self.assertEqual(record.category, "工资")
        self.assertEqual(record.account, "银行卡")
        self.assertEqual(sum(amount for _account, amount in record.postings), 0, "分录之和必须为零")
        legs = {account: amount for account, amount in record.postings}
        self.assertEqual(legs["银行卡"], 800000, "收入:资产腿为正")
        self.assertEqual(legs["收入"], -800000, "收入:收入腿为负")

    def test_query_separates_expense_income_and_net(self):
        self.ledger.record_expense(
            TxnCreate(amount_cents=3000, payee="午饭", category="餐饮",
                      ts="2026-10-08T12:00:00+08:00"), status="confirmed")
        self.ledger.record_expense(
            TxnCreate(direction="income", amount_cents=100000, payee="公司", category="工资",
                      ts="2026-10-09T09:00:00+08:00"), status="confirmed")

        expense = self.ledger.query_expenses(ExpenseQuery(period="2026-10"))
        self.assertEqual(expense.total_cents, 3000)
        self.assertEqual(expense.txn_count, 1, "默认口径只数支出")
        self.assertEqual(expense.income_cents, 100000, "收入数始终一并给出")
        self.assertEqual(expense.net_cents, 97000)

        income = self.ledger.query_expenses(ExpenseQuery(period="2026-10", direction="income"))
        self.assertEqual(income.total_cents, 100000)
        self.assertEqual(income.txn_count, 1)

        both = self.ledger.query_expenses(
            ExpenseQuery(period="2026-10", direction="all", dimension="category"))
        self.assertEqual(both.expense_cents, 3000)
        self.assertEqual(both.income_cents, 100000)
        groups = {row.key: row for row in both.groups}
        self.assertEqual(groups["餐饮"].expense_cents, 3000)
        self.assertEqual(groups["餐饮"].income_cents, 0)
        self.assertEqual(groups["工资"].income_cents, 100000)

    def test_category_kind_must_match_direction(self):
        with self.assertRaises(UnknownCategoryError) as ctx:
            self.ledger.record_expense(
                TxnCreate(direction="income", amount_cents=100, category="餐饮"), status="confirmed"
            )
        self.assertIn("餐饮", str(ctx.exception))
        self.assertIn("收入", str(ctx.exception), "错误信息要说清哪一类分类可用")

    def test_monthly_trend_reads_from_sql(self):
        # ⚠️ 别把日期写死:这条原来钉在 "2026-10-08",到了 11 月,"最后一个月是当月"
        #    就变成 0 了(2026-11-04 实测)。用**本月**的 8 号,哪个月跑都对。
        now = self.ledger._current_time()
        stamp = now.replace(day=8, hour=12, minute=0, second=0, microsecond=0).isoformat()
        self.ledger.record_expense(
            TxnCreate(amount_cents=5000, category="餐饮", ts=stamp),
            status="confirmed")
        trend = self.ledger.monthly_trend(months=3, end=self.ledger._current_time())
        self.assertEqual(len(trend), 3)
        self.assertEqual(trend[-1]["expense_cents"], 5000, "最后一个月是当月")


# ==========================================================================
# 2. 分类:候选 + 新建(要用户点头)+ 撤销
# ==========================================================================
class CategoryCase(Base):
    def test_advisor_ranks_deterministic_candidates(self):
        advice = self.ledger.advise_category(payee="星巴克", narration="拿铁")
        names = [item.name for item in advice.candidates]
        self.assertIn("餐饮", names)
        self.assertTrue(advice.needs_decision)
        self.assertEqual(advice.rule_category, None)

    def test_create_category_is_receipted_and_undoable(self):
        from la.domain.receipts import ReceiptLog, ReceiptReverter
        from la.domain.reverters import build_reverter

        receipts = ReceiptLog(self.conn)
        self.ledger.receipts = receipts
        result = self.ledger.create_category(name="宠物", kind="expense", reason="给猫买粮",
                                             actor="user")
        self.assertTrue(result["created"])
        self.assertIn("宠物", self.ledger.list_categories("expense"))

        reverter = build_reverter(receipts, ledger=self.ledger)
        note = reverter.undo(result["receipt_id"])["note"]
        self.assertIn("删掉", note)
        self.assertNotIn("宠物", self.ledger.list_categories("expense"))

    def test_creating_the_same_category_twice_is_idempotent(self):
        first = self.ledger.create_category(name="宠物", kind="expense")
        second = self.ledger.create_category(name="宠物", kind="expense")
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(second["status"], "exists")

    def test_undoing_a_used_category_archives_instead_of_deleting(self):
        from la.domain.receipts import ReceiptLog
        from la.domain.reverters import build_reverter

        receipts = ReceiptLog(self.conn)
        self.ledger.receipts = receipts
        created = self.ledger.create_category(name="宠物", kind="expense", actor="user")
        self.ledger.record_expense(
            TxnCreate(amount_cents=9900, payee="宠物店", category="宠物"), status="confirmed")

        reverter = build_reverter(receipts, ledger=self.ledger)
        note = reverter.undo(created["receipt_id"])["note"]
        self.assertIn("归档", note, "有账挂着的分类不能直接删")
        self.assertNotIn("宠物", self.ledger.list_categories("expense"))
        row = self.conn.execute(
            "SELECT COUNT(*) FROM app_postings p JOIN app_categories c ON c.id = p.category_id"
            " WHERE c.name = '宠物'"
        ).fetchone()
        self.assertEqual(row[0], 1, "已记的账照旧")


# ==========================================================================
# 3. 统一反馈机制(纯机制层)
# ==========================================================================
class FeedbackMechanismCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.broker = feedback.QuestionBroker()
        self.log = None  # 机制层不落库也能跑

    async def test_answer_wakes_the_waiter_and_first_answer_wins(self):
        asker = feedback.build_asker(broker=self.broker)
        question = feedback.Question(prompt="这笔放哪个分类?", options=[
            feedback.Option(label="餐饮"), feedback.Option(label="购物")])

        async def answer_soon():
            await asyncio_sleep(0.05)
            self.broker.answer(question.id, selected=["餐饮"])

        import asyncio

        task = asyncio.create_task(answer_soon())
        answer = await asker(question)
        await task
        self.assertEqual(answer.status, feedback.STATUS_ANSWERED)
        self.assertEqual(answer.chosen(), "餐饮")

        again = self.broker.answer(question.id, selected=["购物"])
        self.assertEqual(again.selected, ["餐饮"], "一个问题只有一个答案")

    async def test_timeout_is_not_consent(self):
        asker = feedback.build_asker(broker=self.broker)
        question = feedback.Question(prompt="新建分类吗?", timeout_s=0.05)
        answer = await asker(question)
        self.assertEqual(answer.status, feedback.STATUS_EXPIRED)
        self.assertFalse(answer.answered)
        hint = feedback.ASK_RESULT_HINT[feedback.STATUS_EXPIRED]
        self.assertIn("不是同意", hint)

    async def test_transport_path_records_the_answer(self):
        from la.agent.feedback import Answer, STATUS_ANSWERED

        async def transport(question):
            self.assertEqual(len(question.options), 2)
            return Answer(question_id=question.id, status=STATUS_ANSWERED,
                          selected=["购物"], by="cli")

        asker = feedback.build_asker(broker=self.broker, transport=transport)
        answer = await asker(feedback.Question(prompt="哪个?", options=[
            feedback.Option(label="餐饮"), feedback.Option(label="购物")]))
        self.assertEqual(answer.chosen(), "购物")
        self.assertFalse(self.broker.is_waiting(answer.question_id))

    async def test_options_are_clamped(self):
        question = feedback.Question(
            prompt="很多选项", options=[feedback.Option(label=str(i)) for i in range(20)]
        ).prepare()
        self.assertEqual(len(question.options), feedback.MAX_OPTIONS)


async def asyncio_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


# ==========================================================================
# 4. Web 端到端:ask_user 的问答回路
# ==========================================================================
class WebCase(Base):
    def setUp(self) -> None:
        super().setUp()
        self._real_build = web_app._build_client
        self.client = TestClient(web_app.create_app())

    def use_fake(self, client: FakeClient) -> FakeClient:
        web_app._build_client = lambda settings: client
        return client

    def chat_in_thread(self, text: str) -> dict:
        """后台线程跑一轮对话(SSE 会阻塞在提问上),返回一个会被填好的字典。"""
        box: dict = {}

        def worker():
            try:
                with self.client.stream("POST", "/chat/stream", data={"text": text}) as response:
                    box["body"] = "".join(response.iter_text())
                    box["status"] = response.status_code
            except Exception as exc:  # noqa: BLE001 - 测试里要看到真实异常
                box["error"] = repr(exc)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        box["thread"] = thread
        return box

    def wait_for_question(self, box: dict | None = None, timeout: float = 20.0) -> dict:
        """等一个待回答的问题出现。**失败时把线索一起抛出来** —— 否则只有一句超时。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            payload = self.client.get("/api/questions?pending=1").json()
            if payload["questions"]:
                return payload["questions"][0]
            time.sleep(0.05)
        detail = ""
        if box is not None:
            detail = " | thread_error=" + str(box.get("error")) + " | body=" + str(
                box.get("body")
            )[:600]
        raise AssertionError("等不到提问 —— 统一反馈机制没有把问题下发出去" + detail)


class AskUserRoundTripCase(WebCase):
    def test_ask_frame_then_answer_resumes_the_same_turn(self):
        client = self.use_fake(
            FakeClient(
                tool_script=[[
                    ToolCall(
                        id="c1",
                        name="ask_user",
                        arguments=json.dumps(
                            {
                                "prompt": "这笔修车费放哪个分类?",
                                "options": [{"label": "交通"}, {"label": "其他"}],
                                "domain": "ledger",
                            },
                            ensure_ascii=False,
                        ),
                    )
                ]],
                scripted=["好,记到交通。"],
            )
        )
        box = self.chat_in_thread("修车花了 100,记一下")
        question = self.wait_for_question(box)
        self.assertEqual(question["domain"], "ledger")
        self.assertEqual([item["label"] for item in question["options"]], ["交通", "其他"])

        answered = self.client.post(
            "/api/questions/answer",
            json={"question_id": question["id"], "selected": ["交通"]},
        ).json()
        self.assertTrue(answered["ok"], answered)
        self.assertTrue(answered["resumed"], "那一轮还在等,必须被唤醒")

        box["thread"].join(timeout=30)
        self.assertNotIn("error", box, box.get("error"))
        body = box["body"]
        self.assertIn("event: ask", body, "问题要以 SSE 帧下发")
        self.assertIn("这笔修车费放哪个分类?", body)
        self.assertIn("event: done", body)

        # 回答问题成了**工具结果**回到了模型手里,模型接着往下做
        tool_messages = [
            message for call in client.calls for message in call if message.get("role") == "tool"
        ]
        self.assertTrue(tool_messages)
        self.assertIn("交通", tool_messages[-1]["content"])
        self.assertIn("answered", tool_messages[-1]["content"])

    def test_answering_twice_is_rejected(self):
        self.use_fake(FakeClient(scripted=["好的。"]))
        question = feedback.Question(prompt="随便问一句", options=[feedback.Option(label="A")])
        feedback.get_broker().ask(question)
        first = self.client.post(
            "/api/questions/answer", json={"question_id": question.id, "selected": ["A"]}
        ).json()
        second = self.client.post(
            "/api/questions/answer", json={"question_id": question.id, "selected": ["A"]}
        ).json()
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"], "一个问题只能回答一次")
        self.assertIn("已经", second["error"])

    def test_no_asker_means_the_model_is_told_it_cannot_ask(self):
        """没有提问通道时,工具必须**如实**说问不了,而不是假装用户同意了。"""
        self.use_fake(
            FakeClient(
                tool_script=[[
                    ToolCall(id="c1", name="ask_user",
                             arguments=json.dumps({"prompt": "在吗?"}, ensure_ascii=False))
                ]],
                scripted=["好。"],
            )
        )
        # 直接构造一个**没有 asker** 的会话:这是"没接线"的真实形态
        from la.agent.session import MemorySession
        from la.memory.store import MemoryStore

        store = MemoryStore(db.connect_memory())
        session = MemorySession(
            FakeClient(scripted=["好。"]), config.get_settings(),
            store=store, assistant_conn=self.conn, auto_briefing=False,
        )
        import asyncio

        try:
            result = asyncio.run(session._handle_ask({"prompt": "在吗?"}))
        finally:
            store.conn.close()
        self.assertEqual(result["status"], feedback.STATUS_UNAVAILABLE)
        self.assertIn("不要", result["hint"])


# ==========================================================================
# 5. 分类决策链(端到端):无分类 -> 候选 -> 确认框 -> 建好再记
# ==========================================================================
class CategoryDecisionChainCase(WebCase):
    def test_new_category_needs_the_user_to_click_yes(self):
        client = self.use_fake(
            FakeClient(
                tool_script=[
                    [ToolCall(id="c1", name="expense_record",
                              arguments=json.dumps({"amount_cents": 9900, "payee": "宠物店猫粮"},
                                                   ensure_ascii=False))],
                    [ToolCall(id="c2", name="category_create",
                              arguments=json.dumps({"name": "宠物", "kind": "expense",
                                                    "reason": "给猫买粮",
                                                    "sample_payee": "宠物店猫粮"},
                                                   ensure_ascii=False))],
                    [ToolCall(id="c3", name="expense_record",
                              arguments=json.dumps({"amount_cents": 9900, "payee": "宠物店猫粮",
                                                    "category": "宠物"}, ensure_ascii=False))],
                ],
                scripted=["已经记好了:99 元,新开了「宠物」分类。"],
            )
        )
        box = self.chat_in_thread("给猫买猫粮花了 99")

        # 第一轮:还没落账 —— 先要一个分类决定
        question = self.wait_for_question(box)
        self.assertEqual(question["domain"], "ledger")
        self.assertIn("宠物", question["prompt"])
        first_call = [m for m in client.calls[1] if m.get("role") == "tool"][-1]
        self.assertIn("needs_category", first_call["content"])
        self.assertEqual(self.txn_count(), 0, "没定分类之前不该落账")

        # 用户点头 -> 分类建好
        answered = self.client.post(
            "/api/questions/answer", json={"question_id": question["id"], "selected": ["create"]}
        ).json()
        self.assertTrue(answered["ok"], answered)
        box["thread"].join(timeout=30)
        self.assertNotIn("error", box, box.get("error"))

        rows = self.conn.execute(
            "SELECT p.amount_cents, c.name AS category, t.direction_flag FROM ("
            "  SELECT t.id, t.payee,"
            "    (SELECT a.kind FROM app_postings p2 JOIN app_accounts a ON a.id = p2.account_id"
            "      WHERE p2.txn_id = t.id AND a.kind IN ('expense','income') LIMIT 1) AS direction_flag"
            "  FROM app_txns_live t) t"
            " JOIN app_postings p ON p.txn_id = t.id"
            " LEFT JOIN app_categories c ON c.id = p.category_id"
            " WHERE t.payee = '宠物店猫粮'"
        ).fetchall()
        self.assertTrue(rows, "确认之后这笔账必须真的记上")
        self.assertIn("宠物", self.ledger.list_categories("expense"))
        receipt = self.conn.execute(
            "SELECT id, kind, label FROM ops_receipts WHERE kind = 'ledger.category'"
        ).fetchone()
        self.assertIsNotNone(receipt, "新建分类也要有回执")

    def test_declining_leaves_the_ledger_untouched(self):
        self.use_fake(
            FakeClient(
                tool_script=[
                    [ToolCall(id="c1", name="category_create",
                              arguments=json.dumps({"name": "宠物", "kind": "expense"},
                                                   ensure_ascii=False))],
                ],
                scripted=["那先不建。"],
            )
        )
        box = self.chat_in_thread("给猫买猫粮花了 99")
        question = self.wait_for_question(box)
        self.client.post(
            "/api/questions/answer", json={"question_id": question["id"], "selected": ["cancel"]}
        )
        box["thread"].join(timeout=30)
        self.assertNotIn("宠物", self.ledger.list_categories("expense"))
        self.assertEqual(self.txn_count(), 0)

    def txn_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM app_txns_live").fetchone()[0])


# ==========================================================================
# 6. 账本页(纯 SSR)
# ==========================================================================
class LedgerPageCase(WebCase):
    def seed(self) -> None:
        self.ledger.record_expense(
            TxnCreate(amount_cents=3800, payee="老张牛肉面", category="餐饮",
                      ts="2026-10-08T12:00:00+08:00"), status="confirmed")
        self.ledger.record_expense(
            TxnCreate(direction="income", amount_cents=100000, payee="某公司", category="工资",
                      ts="2026-10-09T09:00:00+08:00"), status="confirmed")

    def test_page_shows_income_expense_and_net(self):
        self.seed()
        html = self.client.get("/ledger?period=2026-10").text
        self.assertIn("收入", html)
        self.assertIn("支出", html)
        self.assertIn("结余", html)
        self.assertIn("1,000.00", html, "收入合计要画出来")
        self.assertIn("38.00", html)
        self.assertIn("老张牛肉面", html)
        self.assertIn("工资", html)
        self.assertIn("记一笔", html)

    def test_manual_entry_from_the_page(self):
        response = self.client.post(
            "/ledger/add",
            data={"direction": "income", "amount": "500.50", "category": "奖金",
                  "account": "银行卡", "payee": "年终", "narration": ""},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        row = self.conn.execute(
            "SELECT t.payee, p.amount_cents, a.kind AS account_kind, c.name AS category"
            " FROM app_txns t JOIN app_postings p ON p.txn_id = t.id"
            " JOIN app_accounts a ON a.id = p.account_id"
            " LEFT JOIN app_categories c ON c.id = p.category_id"
            " WHERE t.payee = '年终'"
        ).fetchall()
        self.assertEqual(len(row), 2, "两腿")
        legs = {item["account_kind"]: item["amount_cents"] for item in row}
        self.assertEqual(legs["asset"], 50050)
        self.assertEqual(legs["income"], -50050)

    def test_inline_recategorize_learns_a_rule(self):
        record = self.ledger.record_expense(
            TxnCreate(amount_cents=2800, payee="星巴克", category="其他"), status="confirmed")
        response = self.client.post(
            "/ledger/recategorize/" + str(record.id),
            data={"category": "餐饮", "payee": "星巴克"}, follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        again = self.ledger.read(record.id)
        self.assertEqual(again.category, "餐饮")
        rules = self.conn.execute("SELECT COUNT(*) FROM app_rules").fetchone()[0]
        self.assertEqual(rules, 1, "纠正要回写成规则")

    def test_pending_question_is_visible_on_the_page(self):
        from la.agent.feedback import QuestionLog

        question = feedback.Question(
            prompt="新建「宠物」这个支出分类吗?", options=[feedback.Option(label="新建")],
            domain="ledger",
        ).prepare()
        QuestionLog(self.conn).record(question)
        html = self.client.get("/ledger").text
        self.assertIn("等你拍板", html)
        self.assertIn("新建「宠物」这个支出分类吗?", html)


# ==========================================================================
# 7. 出错了要**看得懂**:白底 500 是最难查的一种失败(实测踩过)
# ==========================================================================
class ErrorPageCase(WebCase):
    def setUp(self) -> None:
        super().setUp()
        # ⚠️ 必须关掉"把服务端异常直接抛给测试"的默认行为 ——
        #    否则测的是 TestClient 的脾气,而不是应用返回的那一页。
        self.client = TestClient(web_app.create_app(), raise_server_exceptions=False)

    def test_page_error_is_explained_and_points_at_the_real_cause(self):
        """把账本页弄坏,要求:① 不是白底 500;② 说明原因;③ 提示"代码比进程新"。"""
        from la.domain.services.ledger_service import LedgerService

        original = LedgerService.list_txns

        def boom(self, **kwargs):
            raise RuntimeError("list_txns 挂了(测试故意)")

        LedgerService.list_txns = boom
        started = web_app._STARTED_AT
        web_app._STARTED_AT = 0.0          # 让"代码比进程新"成立
        web_app._MTIME_CACHE.update({"at": 0.0, "newest": 0.0})
        try:
            response = self.client.get("/ledger")
        finally:
            LedgerService.list_txns = original
            web_app._STARTED_AT = started
            web_app._MTIME_CACHE.update({"at": 0.0, "newest": 0.0})
        self.assertEqual(response.status_code, 500)
        body = response.text
        self.assertIn("这一页出错了", body)
        self.assertIn("list_txns 挂了", body, "要给出真正的原因,而不是一句 Internal Server Error")
        self.assertIn("请重启", body, "代码比进程新时必须直说")

    def test_api_errors_stay_json(self):
        """接口坏了要回 JSON —— 前端按 JSON 解,突然塞一页 HTML 会更难查。"""
        from la.agent.feedback import QuestionLog

        original = QuestionLog.recent

        def boom(self, **kwargs):
            raise RuntimeError("接口挂了(测试故意)")

        QuestionLog.recent = boom
        try:
            response = self.client.get("/api/questions")
        finally:
            QuestionLog.recent = original
        self.assertEqual(response.status_code, 500)
        payload = response.json()
        self.assertIn("接口挂了", payload["message"])
        self.assertIn("error", payload)


if __name__ == "__main__":
    unittest.main()
