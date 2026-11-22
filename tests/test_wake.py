"""唤醒:任务、跑批、静默、写入、报告送达。

守四件事:
  1. 到点只跑一次(抢占 + unique),崩了记 unknown、不自动重跑;
  2. 静默的一轮**不落聊天事件**;有报告才成对落 user/assistant(via=wake);
  3. 追加型写入放行、删除/移动/取消在参数级被拒;
  4. 报告先写 pending,微信进程送达成功后才 sent;48 小时没发出就 expired。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from la import config
from la.agent.session import MemorySession
from la.base.phases import PHASE_ANSWER, CircuitBreaker
from la.bridge.reminder_push import ReminderPusher
from la.family import identity
from la.llm.client import ToolCall
from la.llm.fake import FakeClient
from la.memory.store import MemoryStore
from la.storage import db
from la.tasks import wake_runner
from la.tasks.wake import FREQ_DAILY, WakeStore, next_occurrence

BASE_DIR = Path(__file__).resolve().parent.parent
TZ = ZoneInfo("Asia/Shanghai")


class WakeCase(unittest.TestCase):
    PATHS = (
        "DATA_DIR", "ARCHIVE_DIR", "MEDIA_DIR", "ARTIFACTS_DIR", "LOGS_DIR",
        "ASSISTANT_DB", "MEMORY_DB", "RUNTIME_DIRS",
    )

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._patched = {key: getattr(config, key) for key in self.PATHS}
        config.DATA_DIR = self.tmp
        config.ARCHIVE_DIR = self.tmp / "archive"
        config.MEDIA_DIR = self.tmp / "media"
        config.ARTIFACTS_DIR = self.tmp / "artifacts"
        config.LOGS_DIR = self.tmp / "logs"
        config.ASSISTANT_DB = self.tmp / "assistant.db"
        config.MEMORY_DB = self.tmp / "memory.db"
        config.RUNTIME_DIRS = (
            self.tmp, config.ARCHIVE_DIR, config.MEDIA_DIR,
            config.ARTIFACTS_DIR, config.LOGS_DIR,
        )
        self._settings_cache = config._settings  # noqa: SLF001
        config._settings = None  # noqa: SLF001
        self.assistant = db.connect_assistant()
        self.memory = db.connect_memory()
        self.member_id = identity.ensure_default_member(self.assistant)
        self.store = WakeStore(self.memory, member_id=None)
        self._real_factory = wake_runner.build_client
        self._run_clock = None

    def tearDown(self) -> None:
        wake_runner.build_client = self._real_factory
        try:
            self.assistant.close()
        except Exception:
            pass
        try:
            self.memory.close()
        except Exception:
            pass
        for key, value in self._patched.items():
            setattr(config, key, value)
        config._settings = self._settings_cache  # noqa: SLF001
        self._tmp.cleanup()

    def settings(self):
        return dataclasses.replace(
            config.get_settings(reload=True), provider="fake", session_id="member:1",
        )

    def job(self, *, title="核对账本", instruction="查未分类", due=None):
        moment = due or datetime(2026, 11, 15, 19, 0, tzinfo=TZ)
        store = WakeStore(self.memory, member_id=None, now_provider=lambda: moment)
        job = store.create(
            owner_member_id=self.member_id, title=title, instruction=instruction,
            freq=FREQ_DAILY, time_local="19:00",
        )
        self.memory.execute(
            "UPDATE wake_jobs SET next_at = ? WHERE id = ?",
            ((moment - timedelta(minutes=1)).isoformat(timespec="seconds"), job.id),
        )
        self.memory.commit()
        return job, moment


class TestNextOccurrence(unittest.TestCase):
    def test_daily_rolls_to_tomorrow_when_time_has_passed(self) -> None:
        now = datetime(2026, 11, 10, 19, 1, tzinfo=TZ)
        got = next_occurrence(now, freq=FREQ_DAILY, time_local="19:00", weekday=None, tz=TZ)
        self.assertEqual(got, datetime(2026, 11, 11, 19, 0, tzinfo=TZ))

    def test_daily_stays_today_when_time_is_ahead(self) -> None:
        now = datetime(2026, 11, 10, 18, 59, tzinfo=TZ)
        got = next_occurrence(now, freq=FREQ_DAILY, time_local="19:00", weekday=None, tz=TZ)
        self.assertEqual(got, datetime(2026, 11, 10, 19, 0, tzinfo=TZ))

    def test_weekly_lands_on_sunday(self) -> None:
        now = datetime(2026, 11, 10, 9, 0, tzinfo=TZ)  # 周二
        got = next_occurrence(now, freq="weekly", time_local="19:00", weekday=7, tz=TZ)
        self.assertEqual(got.isoweekday(), 7)
        self.assertEqual(got, datetime(2026, 11, 15, 19, 0, tzinfo=TZ))


class TestWakeStore(WakeCase):
    def test_claim_is_unique_and_advances_next_run(self) -> None:
        job, moment = self.job()
        snapshot = self.store.get(job.id)
        first = self.store.claim_job(snapshot, moment)
        self.assertIsNotNone(first)
        # 用**同一份旧快照**再抢一次:unique(job_id, scheduled_for) 必须挡住。
        second = self.store.claim_job(snapshot, moment)
        self.assertIsNone(second)

    def test_finish_records_receipts_and_usage(self) -> None:
        job, moment = self.job()
        run_id = self.store.claim_job(self.store.get(job.id), moment)
        self.assertTrue(self.store.start_run(run_id))
        self.store.finish_run(
            run_id, status="reported", summary="有 2 笔",
            receipts=[3, 4], usage={"total_tokens": 123},
        )
        run = self.store.get_run(run_id)
        self.assertEqual(run.status, "reported")
        self.assertEqual(run.receipts, [3, 4])
        self.assertEqual(run.usage["total_tokens"], 123)


class TestWakePolicy(WakeCase):
    def _session(self, *, wake: bool = True) -> MemorySession:
        store = MemoryStore(self.memory, archive_dir=self.tmp / "archive")
        return MemorySession(
            FakeClient(scripted=["x"]), self.settings(), store=store,
            session_id="member:1", assistant_conn=self.assistant, wake=wake,
            asker=None, surface="weixin", direct_write=False,
        )

    def test_write_tool_registered_only_in_wake(self) -> None:
        self.assertIsNotNone(self._session(wake=True).registry.get("wake_report"))
        self.assertIsNone(self._session(wake=False).registry.get("wake_report"))

    def test_delete_actions_are_refused(self) -> None:
        session = self._session()
        breaker = CircuitBreaker(phase=PHASE_ANSWER)

        async def go():
            todo = await session.registry.execute(
                "todo_write", {"action": "delete", "todo_id": "t1"}, breaker=breaker,
            )
            schedule = await session.registry.execute(
                "schedule", {"action": "delete", "id": "s1"}, breaker=breaker,
            )
            return todo, schedule

        todo, schedule = asyncio.run(go())
        self.assertEqual(todo.error_code, "WAKE_POLICY")
        self.assertEqual(schedule.error_code, "WAKE_POLICY")

    def test_ask_user_is_refused(self) -> None:
        session = self._session()
        result = asyncio.run(
            session.registry.execute(
                "ask_user", {"prompt": "在吗"}, breaker=CircuitBreaker(phase=PHASE_ANSWER),
            )
        )
        self.assertEqual(result.error_code, "WAKE_POLICY")


class TestWakeSession(WakeCase):
    def _session(self, client: FakeClient) -> MemorySession:
        store = MemoryStore(self.memory, archive_dir=self.tmp / "archive")
        return MemorySession(
            client, self.settings(), store=store, session_id="member:1",
            assistant_conn=self.assistant, wake=True, asker=None,
            surface="weixin", direct_write=False,
        )

    def test_silent_run_writes_no_chat_events(self) -> None:
        session = self._session(FakeClient(scripted=["查过了,没问题。"]))
        asyncio.run(session.run_wake("核对本周账本"))
        rows = self.memory.execute(
            "SELECT COUNT(*) FROM events WHERE type IN ('user/message','assistant/message')"
        ).fetchone()
        self.assertEqual(int(rows[0]), 0)

    def test_report_run_persists_pair_and_hides_from_ui(self) -> None:
        call = ToolCall(
            id="c1", name="wake_report",
            arguments=json.dumps({"text": "有 2 笔未分类", "level": "warn"}, ensure_ascii=False),
        )
        session = self._session(FakeClient(tool_script=[[call]], scripted=["已记录。"]))
        asyncio.run(session.run_wake("核对本周账本"))
        self.assertIsNotNone(session.wake_report)
        self.assertEqual(len(session.conversation_history(include_wake=True)), 2)
        self.assertEqual(len(session.conversation_history(include_wake=False)), 0)


class TestWakeRunner(WakeCase):
    def test_silent_run_records_run_and_no_report(self) -> None:
        job, moment = self.job()
        wake_runner.build_client = lambda settings: FakeClient(scripted=["没问题。"])
        runner = wake_runner.WakeRunner()
        runner.tick(now=moment)
        runs = self.store.recent_runs(limit=5)
        self.assertEqual(runs[0].status, "silent")
        self.assertEqual(self.store.recent_reports(limit=5), [])

    def test_report_run_creates_pending_report(self) -> None:
        job, moment = self.job(title="查异常")
        call = ToolCall(
            id="c1", name="wake_report",
            arguments=json.dumps({"text": "有 2 笔未分类"}, ensure_ascii=False),
        )
        wake_runner.build_client = lambda settings: FakeClient(
            tool_script=[[call]], scripted=["已记录。"]
        )
        wake_runner.WakeRunner().tick(now=moment)
        run = self.store.recent_runs(limit=5)[0]
        self.assertEqual(run.status, "reported")
        reports = self.store.recent_reports(limit=5)
        self.assertEqual(len(reports), 1)
        self.assertIn("有 2 笔未分类", reports[0].text)

    def test_append_write_is_recorded_and_reported(self) -> None:
        job, moment = self.job(title="记一条", instruction="记一条笔记")
        call = ToolCall(
            id="c1", name="note_add",
            arguments=json.dumps({"action": "add", "body": "唤醒写的一条笔记"}, ensure_ascii=False),
        )
        wake_runner.build_client = lambda settings: FakeClient(
            tool_script=[[call]], scripted=["记好了。"]
        )
        wake_runner.WakeRunner().tick(now=moment)
        run = self.store.recent_runs(limit=5)[0]
        self.assertEqual(run.status, "reported")
        self.assertEqual(run.writes, 1)
        self.assertTrue(run.receipts)
        notes = self.assistant.execute("SELECT COUNT(*) FROM app_notes").fetchone()[0]
        self.assertEqual(int(notes), 1)
        self.assertIn("本轮写入 1 条", self.store.recent_reports(limit=5)[0].text)

    def test_stale_running_run_becomes_unknown_with_report(self) -> None:
        job, moment = self.job()
        run_id = self.store.claim_job(self.store.get(job.id), moment)
        self.assertTrue(self.store.start_run(run_id))
        old = (self.store.now() - timedelta(hours=1)).astimezone(
            timezone.utc
        ).isoformat(timespec="seconds")
        self.memory.execute(
            "UPDATE wake_runs SET started_at = ? WHERE id = ?", (old, run_id),
        )
        self.memory.commit()
        wake_runner.build_client = lambda settings: FakeClient(scripted=["x"])
        wake_runner.WakeRunner().tick(now=moment)
        self.assertEqual(self.store.get_run(run_id).status, "unknown")
        self.assertTrue(self.store.recent_reports(limit=5))


class TestWakeDelivery(WakeCase):
    def _pending(self) -> str:
        job, moment = self.job()
        run_id = self.store.claim_job(self.store.get(job.id), moment)
        self.store.start_run(run_id)
        self.store.finish_run(run_id, status="reported", summary="x")
        report = self.store.create_report(
            run=self.store.get_run(run_id), text="⏰ 唤醒 · 账本\n有 2 笔未分类",
        )
        return report.id

    def test_sent_only_after_send_succeeds(self) -> None:
        report_id = self._pending()
        sent: list[tuple[str, int | None]] = []

        async def send_to(text, target):
            sent.append((text, target))
            return True, "ok"

        pusher = ReminderPusher(
            self.memory, self.assistant,
            send=lambda text: send_to(text, None),
            send_to=send_to,
            settings=config.get_settings(),
        )
        asyncio.run(pusher.tick(now=config.get_settings().now()))
        self.assertEqual(self.store.recent_reports(limit=5)[0].status, "sent")
        self.assertEqual(sent[0][1], self.member_id)
        self.assertTrue(sent[0][0].startswith("⏰ 唤醒"))

    def test_expired_report_is_marked_expired(self) -> None:
        report_id = self._pending()
        self.memory.execute(
            "UPDATE wake_reports SET expires_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00+00:00", report_id),
        )
        self.memory.commit()
        pusher = ReminderPusher(
            self.memory, self.assistant,
            send=lambda text: asyncio.sleep(0, result=(True, "ok")),
            send_to=lambda text, target: asyncio.sleep(0, result=(True, "ok")),
            settings=config.get_settings(),
        )
        asyncio.run(pusher.tick(now=config.get_settings().now()))
        self.assertEqual(self.store.recent_reports(limit=5)[0].status, "expired")


class TestWakeWeb(WakeCase):
    def test_page_create_and_pause(self) -> None:
        from fastapi.testclient import TestClient

        from la.ops import settings_store
        from la.tasks.wake_runner import WAKE_PAUSED_KEY
        from la.web.app import create_app

        client = TestClient(create_app(), follow_redirects=False)
        page = client.get("/wake")
        self.assertEqual(page.status_code, 200)
        self.assertIn("唤醒", page.text)
        self.assertIn('href="/wake"', page.text)

        created = client.post(
            "/wake/jobs",
            data={
                "title": "核对账本", "instruction": "查未分类",
                "freq": "daily", "time_local": "19:00", "weekday": "",
                "_idem": "k-create-1",
            },
        )
        self.assertEqual(created.status_code, 303)
        page = client.get("/wake")
        self.assertIn("核对账本", page.text)

        paused = client.post("/wake/pause", data={"paused": "1", "_idem": "k-pause-1"})
        self.assertEqual(paused.status_code, 303)
        self.assertTrue(
            settings_store.get_flag(self.assistant, WAKE_PAUSED_KEY, default=False)
        )


if __name__ == "__main__":
    unittest.main()
