"""2026-10-18 审计修复的回归测试。

每个用例对应审核报告里的一条**确定 bug**;测试名写清症状,避免以后再退化。
只碰临时目录,不写 data/。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from la import config
from la.domain.importers.statement import parse_amount
from la.domain.receipts import (
    Receipt,
    ReceiptLog,
    ReceiptReverter,
    build_fallback_reverter,
)
from la.domain.reverters import build_reverter
from la.domain.services.ledger_service import LedgerError, LedgerService, period_bounds
from la.domain.media import MediaStore
from la.memory.store import MemoryStore
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT, MIGRATIONS_MEMORY
from la.tasks.schedules import ScheduleStore, parse_iso
from la.bridge.reminder_push import ReminderPusher

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 18, 21, 0, tzinfo=TZ)
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6300010000050001"
)


class TempDbCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def memory(self):
        conn = db.connect(self.tmp / "memory.db")
        db.migrate(conn, MIGRATIONS_MEMORY)
        self.addCleanup(conn.close)
        return conn

    def assistant(self):
        conn = db.connect(self.tmp / "assistant.db")
        db.migrate(conn, MIGRATIONS_ASSISTANT)
        self.addCleanup(conn.close)
        return conn


class ConfigFallbackTest(unittest.TestCase):
    def test_invalid_enum_values_fall_back_instead_of_nameerror(self) -> None:
        """非法 LA_REASONING_EFFORT / LA_SRS_ENGINE 必须回落默认,不能 NameError。"""
        previous = config._settings  # noqa: SLF001 - 测试要还原进程级缓存
        config._settings = None  # noqa: SLF001
        try:
            with mock.patch.dict(
                os.environ,
                {"LA_REASONING_EFFORT": "ultra", "LA_SRS_ENGINE": "bogus"},
                clear=False,
            ):
                settings = config.get_settings()
            self.assertEqual(settings.reasoning_effort, config.DEFAULT_REASONING_EFFORT)
            self.assertEqual(settings.srs_engine, "fixed")
        finally:
            config._settings = previous  # noqa: SLF001


class ImportAmountTest(unittest.TestCase):
    def test_third_decimal_uses_decimal_half_up(self) -> None:
        """1.005 元必须进成 101 分,不能因为 float 二进制表示变成 100。"""
        self.assertEqual(parse_amount("1.005"), 101)
        self.assertEqual(parse_amount("2.675"), 268)
        self.assertEqual(parse_amount("0.035"), 4)
        self.assertEqual(parse_amount("38.00"), 3800)
        self.assertEqual(parse_amount("-38"), -3800)


class PeriodRangeTest(unittest.TestCase):
    def test_date_only_right_end_includes_the_whole_day(self) -> None:
        """schema 示例 2026-10-01..2026-10-31 必须含 10 月 31 日。"""
        start, end = period_bounds("2026-10-01..2026-10-31", NOW)
        self.assertEqual(start, "2026-10-01T00:00:00+08:00")
        self.assertEqual(end, "2026-11-01T00:00:00+08:00")
        self.assertIn("+08:00", start, "边界必须带时区,不能和库里的 ts 混比")

    def test_datetime_right_end_stays_half_open(self) -> None:
        _, end = period_bounds("2026-10-01T00:00:00..2026-10-31T12:00:00", NOW)
        self.assertEqual(end, "2026-10-31T12:00:00+08:00")

    def test_out_of_range_year_is_a_domain_error(self) -> None:
        with self.assertRaises(LedgerError):
            period_bounds("9999", NOW)
        with self.assertRaises(LedgerError):
            period_bounds("9999-Q4", NOW)


class ConsolidationRangeTest(TempDbCase):
    def test_consolidated_summary_does_not_hide_uncompressed_history(self) -> None:
        """考古回填的 summary 只能当检索材料,不能被算成“这段已压缩”。"""
        conn = self.memory()
        store = MemoryStore(conn, archive_dir=self.tmp / "archive")
        store.ensure_session("s")
        first = store.append("s", "user/message", {"text": "第一句"})
        second = store.append("s", "assistant/message", {"text": "第二句"})
        store.upsert_summary(
            session_id="s", start_seq=first.seq, end_seq=second.seq,
            start_time=first.time, end_time=second.time,
            text="<compacted-summary>- 事项:回填</compacted-summary>",
            tags=["consolidated"],
        )
        self.assertEqual(store.compacted_ranges("s"), [], "回填摘要不是压缩")
        store.upsert_summary(
            session_id="s", start_seq=first.seq, end_seq=second.seq,
            start_time=first.time, end_time=second.time,
            text="<compacted-summary>- 事项:真压缩</compacted-summary>",
            tags=[],
        )
        self.assertEqual(store.compacted_ranges("s"), [(first.seq, second.seq)])


class ScheduleTimeTest(TempDbCase):
    def test_naive_at_is_normalized_and_can_be_due(self) -> None:
        store = ScheduleStore(self.memory())
        made = store.create("吃药", kind="at", at="2026-10-18T20:00:00")
        self.assertIsNotNone(parse_iso(made.at).tzinfo, "落库前必须补时区")
        self.assertEqual([item.id for item in store.due(NOW)], [made.id])

    def test_old_naive_row_does_not_break_due(self) -> None:
        conn = self.memory()
        store = ScheduleStore(conn)
        made = store.create("旧数据", kind="at", at="2026-10-18T20:00:00")
        conn.execute("UPDATE schedules SET at = ? WHERE id = ?",
                     ("2026-10-18T20:00:00", made.id))
        conn.commit()
        self.assertEqual([item.id for item in store.due(NOW)], [made.id])


class ScheduleUndoTest(TempDbCase):
    def test_undo_delete_restores_target_todo_and_enabled(self) -> None:
        conn = self.memory()
        log = ReceiptLog(conn)
        store = ScheduleStore(conn, receipts=log)
        made = store.create(
            "只提醒妈妈", kind="at", at="2026-10-18T20:00:00",
            todo_id="t-1", target_member_id=2, receipt=False,
        )
        store.set_enabled(made.id, False, receipt=False)
        store.delete(made.id, record_receipt=True, actor="user")
        receipt_id = log.recent(limit=1)[0].id
        reverter = build_reverter(log, schedules=store)
        reverter.undo(receipt_id)
        restored = store.get(made.id)
        self.assertIsNotNone(restored)
        self.assertFalse(restored.enabled, "原本关着的提醒不能自己响")
        self.assertEqual(restored.todo_id, "t-1", "待办归属不能丢")
        self.assertEqual(restored.target_member_id, 2, "私人提醒不能变成全家广播")


class IntakeAssetVisibilityTest(TempDbCase):
    def test_intake_candidate_asset_is_visible_to_its_owner(self) -> None:
        from la.web.app import _asset_visible

        conn = self.assistant()
        media = MediaStore(conn, root=self.tmp / "media")
        asset = media.store_bytes(PNG_1PX, original_name="题.png", media_type="image/png")
        cur = conn.execute(
            "INSERT INTO app_intake_sessions(profile_id, created_at, status, owner_session)"
            " VALUES (NULL, ?, 'reviewing', 'member:2')",
            (NOW.isoformat(timespec="seconds"),),
        )
        conn.execute(
            "INSERT INTO app_intake_candidates(session_id, asset_id, payload_json, confidence,"
            " decision, created_at) VALUES (?, ?, '{}', 0, 'pending', ?)",
            (int(cur.lastrowid), asset.id, NOW.isoformat(timespec="seconds")),
        )
        conn.commit()
        owner = SimpleNamespace(
            member=SimpleNamespace(id=2, role="parent"), assistant=conn
        )
        other = SimpleNamespace(
            member=SimpleNamespace(id=3, role="parent"), assistant=conn
        )
        self.assertTrue(_asset_visible(owner, asset.id), "自己的候选图必须看得到")
        self.assertFalse(_asset_visible(other, asset.id), "别人不能看")


class SearchScopeTest(TempDbCase):
    def test_search_filters_session_before_the_limit(self) -> None:
        """别的会话不能占满候选窗口,把本会话的命中挤掉。"""
        from la.memory.search import search_summaries

        conn = self.memory()
        store = MemoryStore(conn, archive_dir=self.tmp / "archive")
        for session, text in (("a", "项目会议预算"), ("b", "项目会议预算")):
            store.upsert_summary(
                session_id=session, start_seq=1, end_seq=2,
                start_time="2026-10-18T10:00:00+08:00",
                end_time="2026-10-18T11:00:00+08:00",
                text=text, tags=[],
            )
        hits = search_summaries(conn, "项目会议", limit=10, session_id="a")
        self.assertEqual(len(hits), 1)
        row = conn.execute("SELECT session_id FROM summary_pool WHERE id = ?",
                           (next(iter(hits)),)).fetchone()
        self.assertEqual(row[0], "a")


class DueLimitTest(TempDbCase):
    def test_more_than_fifty_due_reminders_are_all_seen(self) -> None:
        store = ScheduleStore(self.memory())
        for index in range(55):
            store.create("提醒 " + str(index), kind="at",
                         at="2026-10-18T20:00:00+08:00", receipt=False)
        self.assertEqual(len(store.due(NOW)), 55, "第 51 条起不能被默认 LIMIT 静默吃掉")


class MemberScopedTasksTest(TempDbCase):
    def test_todos_are_scoped_to_owner_or_family(self) -> None:
        from la.tasks.todos import TodoStore

        conn = self.memory()
        mine = TodoStore(conn, member_id=2)
        todo_mine = mine.add("我的事")
        family = mine.add("全家的事")
        conn.execute("UPDATE todos SET owner_id = NULL WHERE id = ?", (family.id,))
        conn.commit()
        other = TodoStore(conn, member_id=3)
        other.add("别人的事")
        ids = {item.id for item in mine.list(status="all", limit=20)}
        self.assertIn(todo_mine.id, ids)
        self.assertIn(family.id, ids)
        self.assertEqual(len(ids), 2, "不能看到另一个成员的待办")
        self.assertNotIn("别人的事", [item.content for item in mine.list(status="all")])

    def test_targeted_reminder_is_scoped_but_family_reminder_is_visible(self) -> None:
        conn = self.memory()
        mine = ScheduleStore(conn, member_id=2)
        personal = mine.create("只提醒我", kind="at", at="2026-10-18T20:00:00+08:00",
                               target_member_id=2, receipt=False)
        family = mine.create("全家提醒", kind="at", at="2026-10-18T20:00:00+08:00",
                             receipt=False)
        other = ScheduleStore(conn, member_id=3)
        other.create("只提醒别人", kind="at", at="2026-10-18T20:00:00+08:00",
                     target_member_id=3, receipt=False)
        ids = {item.id for item in mine.list(limit=20)}
        self.assertEqual(ids, {personal.id, family.id})


class ReminderBatchTest(TempDbCase):
    def test_batch_overflow_is_deferred_not_marked_missed(self) -> None:
        """一次到点 9 条时,第 9 条只是下一轮再发,不能被算成“已错过”并认领。"""
        memory = self.memory()
        assistant = self.assistant()
        store = ScheduleStore(memory)
        for index in range(9):
            store.create(
                "提醒 " + str(index), kind="at", at="2026-10-18T20:00:00+08:00",
                receipt=False,
            )

        async def send(text: str):
            return True, "ok"

        pusher = ReminderPusher(memory, assistant, send=send)
        live, stale = pusher.collect(NOW)
        self.assertEqual(len(live), 8)
        self.assertEqual(stale, [], "超出一批的条目没有迟到,不能进 stale")
        row = memory.execute(
            "SELECT COUNT(*) FROM schedules WHERE last_fired_at IS NOT NULL"
        ).fetchone()
        self.assertEqual(int(row[0]), 0, "collect 只读,不能认领任何一条")


class PartialPushTest(unittest.TestCase):
    def test_partial_chunk_send_is_not_reported_as_success(self) -> None:
        """只发出 2/3 段时必须返回 False,否则调用方会把整批提醒都认领掉。"""
        from la.bridge.adapters import weixin_ilink, weixin_store

        # ⚠️ 用真的 Conversation,不是 SimpleNamespace:push_text 现在还要看
        #    窗口有没有过期(window_open),假对象会漏掉这条路径(D163)。
        conv = weixin_ilink.Conversation(
            user_id="u", context_token="tok", remaining_quota=3,
            last_seen_ms=int(time.time() * 1000),
        )
        class Client:
            async def send_text(self, user_id, chunk, token):
                return {"remaining": None}

        class Transport:
            def __init__(self):
                self.conversations = {"u": conv}

            def _only_user(self):
                return "u"

            def conversation(self, user_id):
                return conv

        adapter = object.__new__(weixin_ilink.ILinkAdapter)
        adapter.client = Client()
        adapter.transport = Transport()
        with mock.patch.object(weixin_ilink, "split_text", return_value=["a", "b", "c"]), \
             mock.patch.object(weixin_store, "save_session", lambda payload: None):
            ok, reason = asyncio.run(adapter.push_text("x", to_user_id="u"))
        self.assertFalse(ok, reason)
        self.assertIn("2/3", reason)


class ToolPayloadTest(unittest.TestCase):
    def test_list_payload_is_masked_and_budgeted(self) -> None:
        """list[dict] 也要脱敏/按预算剪裁,不能整体绕过 _post_process。"""
        from la.tools.registry import ToolRegistry, ToolSpec

        registry = ToolRegistry()
        spec = ToolSpec(
            name="x", description="x", phase="answer", risk="low",
            timeout_ms=1000, handler=lambda params: None, output_budget_tokens=30,
        )
        raw = {"events": [{"text": "身份证 110101199001011234 " + "字" * 300}]}
        cleaned, truncated = registry._post_process(raw, spec)
        self.assertTrue(truncated)
        rendered = json.dumps(cleaned, ensure_ascii=False)
        self.assertNotIn("110101199001011234", rendered, "身份证号必须被脱敏")
        self.assertIn("中间省略", rendered, "超长文本要带省略标记")


class SkillPathTest(TempDbCase):
    def test_skill_name_cannot_escape_the_skills_root(self) -> None:
        from la.skills import SkillLibrary

        root = self.tmp / "skills"
        root.mkdir()
        outside = self.tmp / "secret"
        outside.mkdir()
        (outside / "SKILL.md").write_text("---\nname: secret\n---\n机密", encoding="utf-8")
        library = SkillLibrary(root)
        for name in ("../secret", "..\\secret", "/etc", "a/b"):
            with self.assertRaises(FileNotFoundError, msg=name):
                library.load(name)


class WeixinAccountStoreTest(TempDbCase):
    def test_two_wechat_accounts_have_separate_credentials(self) -> None:
        from la.bridge.adapters import weixin_store
        from la.bridge.adapters.weixin_protocol import BotCredentials

        old_data = config.DATA_DIR
        config.DATA_DIR = self.tmp
        try:
            weixin_store.use_account("dad")
            dad = BotCredentials(bot_id="b1", token="t1", base_url="https://x", user_id="u1")
            path = weixin_store.save(dad)
            self.assertIn("accounts", str(path))
            self.assertIn("dad", str(path))

            weixin_store.use_account("mom")
            self.assertIsNone(weixin_store.load(), "妈妈账号不能读到爸爸的凭据")
            mom = BotCredentials(bot_id="b2", token="t2", base_url="https://x", user_id="u2")
            weixin_store.save(mom)

            weixin_store.use_account("dad")
            self.assertEqual(weixin_store.load().token, "t1")
        finally:
            weixin_store.use_account("")
            config.DATA_DIR = old_data


class PathPolicyTest(unittest.TestCase):
    def test_archive_trace_and_settings_backup_are_not_readable(self) -> None:
        from la.ops.paths import DEFAULT_POLICY, PathNotAllowed

        for path in (
            config.ARCHIVE_DIR / "member-1.jsonl",
            config.DATA_DIR / "trace" / "context.jsonl",
            config.BASE_DIR / "settings.yaml.bak",
        ):
            with self.assertRaises(PathNotAllowed, msg=str(path)):
                DEFAULT_POLICY.check(path)


class LedgerApiTest(TempDbCase):
    def test_router_is_mounted_as_json_api(self) -> None:
        from fastapi.testclient import TestClient

        from la.web.app import create_app

        keys = ("DATA_DIR", "ASSISTANT_DB", "MEMORY_DB", "ARCHIVE_DIR", "MEDIA_DIR",
                "ARTIFACTS_DIR", "BACKUPS_DIR", "LOGS_DIR", "CACHE_DIR", "TTS_CACHE_DIR")
        previous = {key: getattr(config, key) for key in keys}
        previous_dirs = config.RUNTIME_DIRS
        try:
            config.DATA_DIR = self.tmp
            config.ASSISTANT_DB = self.tmp / "assistant.db"
            config.MEMORY_DB = self.tmp / "memory.db"
            for name in ("ARCHIVE_DIR", "MEDIA_DIR", "ARTIFACTS_DIR", "BACKUPS_DIR",
                         "LOGS_DIR", "CACHE_DIR", "TTS_CACHE_DIR"):
                setattr(config, name, self.tmp / name.lower())
            config.RUNTIME_DIRS = (
                config.DATA_DIR, config.ARCHIVE_DIR, config.MEDIA_DIR,
                config.ARTIFACTS_DIR, config.BACKUPS_DIR, config.LOGS_DIR,
                config.CACHE_DIR, config.TTS_CACHE_DIR,
            )
            client = TestClient(create_app())
            created = client.post("/api/v1/ledger/expenses", json={
                "amount_cents": 1234, "payee": "API 测试", "direction": "expense",
                "ts": "2026-10-18T12:00:00+08:00",
            })
            self.assertEqual(created.status_code, 201, created.text)
            txn_id = int(created.json()["id"])
            got = client.get("/api/v1/ledger/expenses/" + str(txn_id))
            self.assertEqual(got.status_code, 200)
            confirmed = client.post("/api/v1/ledger/expenses/" + str(txn_id) + "/confirm")
            self.assertEqual(confirmed.status_code, 200)
            self.assertEqual(confirmed.json()["status"], "confirmed")
        finally:
            for key, value in previous.items():
                setattr(config, key, value)
            config.RUNTIME_DIRS = previous_dirs


class LedgerTransactionTest(TempDbCase):
    def test_receipt_failure_rolls_back_txn_postings_and_outbox(self) -> None:
        """交易 + 两腿 + outbox + 回执必须同一事务:回执写失败不能留下半笔账。"""
        from la.domain.schemas.ledger import TxnCreate

        conn = self.assistant()
        service = LedgerService(conn)

        class FailingReceipts:
            def record(self, **kwargs):
                raise RuntimeError("回执表炸了")

        service.receipts = FailingReceipts()
        with self.assertRaises(RuntimeError):
            service.record_expense(TxnCreate(
                amount_cents=1234, direction="expense", payee="x",
                ts="2026-10-18T12:00:00+08:00",
            ))
        for table in ("app_txns", "app_postings", "ops_outbox"):
            count = conn.execute("SELECT count(*) FROM " + table).fetchone()[0]
            self.assertEqual(int(count), 0, table + " 不能留下半截写入")


class LedgerCorrectionTest(TempDbCase):
    def test_income_correction_with_same_name_category_in_both_directions(self) -> None:
        """支出/收入允许同名;改收入时必须只在收入分类里找。"""
        from la.domain.schemas.ledger import TxnCreate

        service = LedgerService(self.assistant())
        service.create_category(name="同名", kind="income")
        service.create_category(name="同名", kind="expense")
        txn = service.record_expense(TxnCreate(
            amount_cents=1000, direction="income", payee="x",
            ts="2026-10-18T12:00:00+08:00",
        ))
        fixed = service.correct_last(txn_id=txn.id, category="同名")
        self.assertEqual(fixed.direction, "income")
        self.assertEqual(fixed.category, "同名")


class ReceiptSourceTest(TempDbCase):
    def test_same_id_in_two_books_undoes_the_selected_book(self) -> None:
        log_a = ReceiptLog(self.assistant())
        log_m = ReceiptLog(self.memory())
        log_a.record(kind="x", domain="task", label="assistant 那条")
        log_m.record(kind="x", domain="task", label="memory 那条")
        a = ReceiptReverter(log_a)
        m = ReceiptReverter(log_m)
        a.register("x", lambda receipt: "撤了 assistant")
        m.register("x", lambda receipt: "撤了 memory")
        wrapper = build_fallback_reverter(a, m)
        result = wrapper.undo(1, source="m")
        self.assertEqual(result["note"], "撤了 memory")
        self.assertFalse(log_a.get(1).undone, "不能动另一本账的同号回执")
        self.assertTrue(log_m.get(1).undone)

    def test_receipt_payload_carries_source(self) -> None:
        payload = Receipt(
            id=5, ts="2026-10-18T00:00:00+08:00", actor="agent",
            kind="task.todo", domain="task", label="记待办", source="m",
        ).as_dict()
        self.assertEqual(payload["source"], "m")


if __name__ == "__main__":
    unittest.main()
