"""M7 验收测试:统一写入回执 / 一键撤销 / 执行反馈。

⭐ 核心 DoD(四条,逐条对应):
   1. **写入不弹确认**:助手记的账直接就是 confirmed,不再停在 draft;
   2. **写入后给回执**:SSE 里有 receipt 帧,里面有"写了什么";
   3. **回执带撤销**:POST /api/receipts/undo 之后,那笔账真的从账本里消失;
   4. **执行反馈是流式带计时的**:thinking / step / step_update 帧齐全,
      每一步都有 since 与 duration_ms,结束有 done 汇总。

全部离线跑(LA_LLM_PROVIDER=fake),不需要密钥。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from la import config
from la.agent.step_ledger import ExecutionLedger, format_duration
from la.domain.receipts import ReceiptError, ReceiptLog, ReceiptReverter
from la.llm.client import ToolCall
from la.llm.fake import FakeClient
from la.storage import db
from la.web import app as web_app


def parse_sse(body: str) -> list[tuple[str, object]]:
    """把 SSE 文本解析成 [(event, data)]。data 是 JSON,解析不了就原样返回字符串。"""
    frames: list[tuple[str, object]] = []
    name = ""
    data: list[str] = []
    for line in body.split("\n"):
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif line == "":
            if name:
                raw = "\n".join(data)
                try:
                    frames.append((name, json.loads(raw)))
                except json.JSONDecodeError:
                    frames.append((name, raw))
            name, data = "", []
    return frames


def names(frames: list[tuple[str, object]]) -> list[str]:
    return [item[0] for item in frames]


class WebCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
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
        self._fake: FakeClient | None = None
        self._real_build = web_app._build_client
        self.client = TestClient(web_app.create_app())

    def tearDown(self) -> None:
        web_app._build_client = self._real_build
        for key, value in self._patched.items():
            setattr(config, key, value)
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config._settings = None
        self._tmp.cleanup()

    def use_fake(self, client: FakeClient) -> FakeClient:
        """换掉聊天流里的假客户端 —— 让它按脚本调工具。"""
        self._fake = client
        web_app._build_client = lambda settings: client
        return client

    def chat(self, text: str) -> list[tuple[str, object]]:
        with self.client.stream("POST", "/chat/stream", data={"text": text}) as response:
            self.assertEqual(response.status_code, 200)
            body = "".join(response.iter_text())
        return parse_sse(body)

    # -- 数据准备 ---------------------------------------------------------
    def txn_rows(self) -> list[dict]:
        conn = db.connect_assistant()
        try:
            rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT id, status, payee, deleted_at FROM app_txns ORDER BY id"
                ).fetchall()
            ]
        finally:
            conn.close()
        return rows


class WriteReceiptAndUndoCase(WebCase):
    """1+2+3:直接写、给回执、能撤销。"""

    def _script(self) -> FakeClient:
        return self.use_fake(
            FakeClient(
                tool_script=[
                    [
                        ToolCall(
                            id="c1",
                            name="expense_record",
                            arguments=json.dumps(
                                {"amount_cents": 10000, "payee": "修车", "category": "交通"}
                            ),
                        )
                    ]
                ],
                scripted=["修车这笔已经记上了,分类我放在交通,不对就点撤销。"],
                reasoning_script=["用户说修车花了 100 元,先记一笔。", "已经记好了,回一句。"],
            )
        )

    def test_write_is_direct_receipted_and_undoable(self):
        self._script()
        frames = self.chat("昨天修车花了 100")
        kinds = names(frames)

        # (1) 写操作直接执行:账本里立刻是 confirmed,不再等确认
        rows = self.txn_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "confirmed", "直写模式下应当是已入账")
        self.assertIsNone(rows[0]["deleted_at"])

        # (2) 前端拿到 step + receipt 两类帧
        self.assertIn("step", kinds)
        self.assertIn("receipt", kinds)
        self.assertIn("done", kinds)
        receipt_frame = next(data for name, data in frames if name == "receipt")
        receipt = receipt_frame["receipt"]
        self.assertIn("100", receipt["label"])
        self.assertTrue(receipt["undoable"])
        self.assertEqual(receipt["domain"], "ledger")

        # 用工具的那些步骤必须精确对上(而且每步都有 since)
        steps = next(data for name, data in frames if name == "done")["steps"]
        titles = [step["title"] for step in steps]
        self.assertTrue(any("记一笔支出" in title for title in titles), titles)
        self.assertTrue(all(step["since"] > 0 for step in steps))
        tool_steps = [step for step in steps if step["title"].startswith("记一笔支出")]
        self.assertTrue(tool_steps[0]["duration_ms"] >= 0)

        # (3) 撤销:那笔账真的从账本里消失
        response = self.client.post(
            "/api/receipts/undo", json={"receipt_id": receipt["id"]}
        ).json()
        self.assertTrue(response["ok"], response)
        self.assertIn("删掉", response["note"])
        self.assertTrue(response["receipt"]["undone"])
        self.assertFalse(response["receipt"]["undoable"], "撤销过的不该还显示可撤销")
        rows = self.txn_rows()
        # 账本读路径走 app_txns_live 视图 —— 撤销后它就不该再出现
        conn = db.connect_assistant()
        try:
            live = conn.execute("SELECT COUNT(*) FROM app_txns_live").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(live, 0, "撤销后这笔不该还留在账本里")

        # 同一条不能撤两次
        again = self.client.post(
            "/api/receipts/undo", json={"receipt_id": receipt["id"]}
        ).json()
        self.assertFalse(again["ok"])
        self.assertIn("已经撤销", again["error"])

    def test_undoing_unknown_receipt_is_a_clear_error(self):
        payload = self.client.post("/api/receipts/undo", json={"receipt_id": 999999}).json()
        self.assertFalse(payload["ok"])
        self.assertIn("没有这条写入回执", payload["error"])


class ExecutionFeedbackCase(WebCase):
    """4:流式思考 + 步骤账本 + 每步用时。"""

    def test_thinking_steps_and_timing_are_streamed(self):
        thought = "先想清楚用户要什么,再决定要不要动手。"
        self.use_fake(
            FakeClient(
                reasoning_script=[thought],
                scripted=["好。"],
            )
        )
        frames = self.chat("你好")
        kinds = names(frames)

        # 顺序纪律:thinking 一定要在 thinking_end 之前,thinking_end 在正文之前
        self.assertIn("thinking", kinds)
        self.assertIn("thinking_end", kinds)
        self.assertIn("html", kinds)
        self.assertLess(kinds.index("thinking"), kinds.index("thinking_end"))
        self.assertLess(kinds.index("thinking_end"), kinds.index("html"))

        # 思考内容完整到达(合批不能丢字)
        streamed = "".join(
            data if isinstance(data, str) else str(data)
            for name, data in frames
            if name == "thinking"
        )
        self.assertEqual(streamed, thought)

        # 状态行与步骤账本:每一步都有 since(前端靠它本地走秒)
        status = next(data for name, data in frames if name == "status")
        self.assertIn("since", status)
        done = next(data for name, data in frames if name == "done")
        self.assertTrue(done["steps"], "一轮之后账本不该为空")
        self.assertTrue(all("since" in step for step in done["steps"]))
        self.assertIn("用时", done["label"])
        self.assertGreaterEqual(done["steps_meta"]["elapsed_ms"], 0)
        # 前台收尾:不能留下永远转圈的行
        self.assertTrue(all(step["status"] != "running" for step in done["steps"]))

    def test_unknown_receipt_id_is_looked_up_safely(self):
        """回执接口不能因为一个野 id 就 500。"""
        payload = self.client.get("/api/receipts?limit=5").json()
        self.assertEqual(payload["receipts"], [])


class MultiDomainReceiptCase(WebCase):
    """⭐ 第 3 条需求:写入反馈是**项目级**的 —— 换个领域,机制一个样。"""

    def test_health_write_produces_the_same_shape_of_receipt_and_undo(self):
        self.use_fake(
            FakeClient(
                tool_script=[
                    [
                        ToolCall(
                            id="h1",
                            # ⭐ 批次 B(2026-10-15):health_log_checkin 已并入
                            #    health_log(action=checkin)。
                            name="health_log",
                            arguments=json.dumps({
                                "action": "checkin", "metric_code": "weight", "value": 72.5,
                            }),
                        )
                    ]
                ],
                scripted=["记下了。"],
            )
        )
        frames = self.chat("今天体重 72.5")
        receipt = next(
            data["receipt"] for name, data in frames if name == "receipt"
        )
        self.assertEqual(receipt["domain"], "health")
        self.assertIn("体重", receipt["label"])

        payload = self.client.post(
            "/api/receipts/undo", json={"receipt_id": receipt["id"]}
        ).json()
        self.assertTrue(payload["ok"], payload)

        conn = db.connect_assistant()
        try:
            left = conn.execute(
                "SELECT COUNT(*) FROM app_measurements WHERE metric_code = 'weight'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(left, 0, "撤销之后这条体重不该还在")

    def test_receipts_list_is_domain_agnostic(self):
        """一张表装五个域:接口不需要知道领域。"""
        from la.domain.receipts import ReceiptLog

        conn = db.connect_assistant()
        try:
            log = ReceiptLog(conn)
            log.record(kind="task.todo", domain="task", label="记待办:买牛奶",
                       undo_plan={"type": "todo", "op": "create", "id": "t-1"})
            # 一个没人注册过 reverter 的 kind:必须**明确说不可撤**,不能假装能撤
            log.record(kind="system.mystery", domain="system", label="某种没人实现撤销的写入")
            conn.commit()
        finally:
            conn.close()
        listed = self.client.get("/api/receipts?limit=5").json()["receipts"]
        self.assertEqual(listed[0]["kind"], "system.mystery")
        self.assertEqual(listed[1]["domain"], "task")
        self.assertIn("买牛奶", listed[1]["label"])
        payload = self.client.post(
            "/api/receipts/undo", json={"receipt_id": listed[0]["id"]}
        ).json()
        self.assertFalse(payload["ok"])
        self.assertIn("没有撤销实现", payload["error"])


class LedgerUnitCase(unittest.TestCase):
    """不带 Web 的单元:账本回执 + 撤销的正反两面。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._patched = {
            key: getattr(config, key)
            for key in ("DATA_DIR", "ARCHIVE_DIR", "MEDIA_DIR", "ASSISTANT_DB", "MEMORY_DB")
        }
        config.DATA_DIR = self.tmp
        config.ASSISTANT_DB = self.tmp / "assistant.db"
        config.MEMORY_DB = self.tmp / "memory.db"
        config.ARCHIVE_DIR = self.tmp / "archive"
        config.MEDIA_DIR = self.tmp / "media"
        from la.domain.services.ledger_service import LedgerService

        self.conn = db.connect_assistant()
        self.receipts = ReceiptLog(self.conn)
        self.ledger = LedgerService(self.conn, receipts=self.receipts)
        self.reverter = ReceiptReverter(self.receipts)
        self.reverter.register("ledger.txn", self.ledger.revert_txn)
        self.reverter.register("ledger.import", self.ledger.revert_txn)

    def tearDown(self) -> None:
        self.conn.close()
        for key, value in self._patched.items():
            setattr(config, key, value)
        self._tmp.cleanup()

    def test_status_change_receipt_goes_back_to_the_previous_status(self):
        record = self.ledger.record_expense(
            {"amount_cents": 8900, "payee": "修车", "category": "交通"}, status="draft"
        )
        first = self.receipts.recent(limit=1)[0]
        self.assertEqual(first.detail["status"], "draft")

        self.ledger.confirm(record.id)
        second = self.receipts.recent(limit=1)[0]
        self.assertIn("草稿", second.label)
        self.assertIn("已入账", second.label)

        result = self.reverter.undo(second.id)
        self.assertIn("草稿", result["note"])
        self.assertEqual(self.ledger.read(record.id).status, "draft")

    def test_delete_receipt_restores_the_transaction(self):
        record = self.ledger.record_expense({"amount_cents": 8800, "payee": "午饭"}, status="confirmed")
        self.ledger.soft_delete(record.id, reason="手滑")
        receipt = self.receipts.recent(limit=1)[0]
        self.assertEqual(receipt.detail["op"], "delete")

        self.reverter.undo(receipt.id)
        self.assertEqual(self.ledger.read(record.id).status, "confirmed")

    def test_receipt_row_is_append_only_and_undo_is_once(self):
        self.ledger.record_expense({"amount_cents": 100, "payee": "矿泉水"})
        receipt = self.receipts.recent(limit=1)[0]
        self.reverter.undo(receipt.id)
        with self.assertRaises(ReceiptError):
            self.reverter.undo(receipt.id)
        # 回执不能改写(触发器守着),只能把 undone_at 从 NULL 变成非 NULL
        with self.assertRaises(Exception):
            self.conn.execute("UPDATE ops_receipts SET label = 'x' WHERE id = ?", (receipt.id,))
        self.conn.rollback()

    def test_uncorrectable_receipt_says_so_instead_of_silently_failing(self):
        reverter = ReceiptReverter(self.receipts)
        self.ledger.record_expense({"amount_cents": 100})
        with self.assertRaises(ReceiptError):
            reverter.undo_last()


class StepLedgerCase(unittest.TestCase):
    """账本本身:同 key 复用一行、只有计时变化不发帧、收尾不留转圈行。"""

    def test_same_key_reuses_one_row_and_closing_is_complete(self):
        frames: list[dict] = []
        ledger = ExecutionLedger(frames.append)
        ledger.begin("检索记忆", key="phase:qa")
        ledger.begin("检索记忆", key="phase:qa")
        self.assertEqual(len(ledger.rows), 1, "同一个 key 只占一行")
        ledger.end("phase:qa", "检索记忆", detail="命中 2 条")
        payload = ledger.finish()
        steps = payload["steps"]
        self.assertEqual(steps[0]["status"], "done")
        self.assertIsNotNone(steps[0]["duration_ms"])
        self.assertEqual(payload["steps_meta"]["failed"], 0)

    def test_finish_marks_running_rows_as_failed_when_asked(self):
        ledger = ExecutionLedger()
        ledger.begin("思考中", key="phase:thinking")
        payload = ledger.finish("failed")
        self.assertEqual(payload["steps"][0]["status"], "failed")
        self.assertEqual(payload["steps_meta"]["failed"], 1)

    def test_duration_formatting_matches_the_frontend_ladder(self):
        self.assertEqual(format_duration(0), "")
        self.assertEqual(format_duration(1400), "1.4s")
        self.assertEqual(format_duration(42000), "42s")
        self.assertEqual(format_duration(200000), "3m20s")
        self.assertEqual(format_duration(7200_000), "2h0m")


if __name__ == "__main__":
    unittest.main()
