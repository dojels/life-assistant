"""M3 验收测试:目标 / 待办 / 提醒 / 确定性调度器 / 技能包,以及场景 S5 / S6。

运行:
    .venv/Scripts/python.exe -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from la import config
from la.agent.session import MemorySession
from la.llm.client import ToolCall
from la.llm.fake import FakeClient
from la.memory.store import MemoryStore
from la.skills import SkillLibrary
from la.storage import db
from la.storage.migrations import MIGRATIONS_MEMORY
from la.tasks.dispatcher import KIND_BRIEFING, KIND_REMINDER, Dispatcher
from la.tasks.goals import (
    ACTION_COMPLETE,
    ACTION_PAUSE,
    ACTION_RESUME,
    PHASE_ACTIVE,
    PHASE_BLOCKED,
    PHASE_COMPLETE,
    PHASE_PAUSED,
    SOURCE_AGENT,
    SOURCE_USER,
    GoalError,
    GoalStore,
    RevisionConflict,
)
from la.tasks.schedules import (
    MIN_EVERY_SECONDS,
    ScheduleError,
    ScheduleStore,
)
from la.tasks.todos import STATUS_COMPLETED, TodoStore

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 8, 9, 30, tzinfo=TZ)
TOMORROW_MORNING = datetime(2026, 10, 9, 9, 0, tzinfo=TZ)


def make_settings(session_id: str = "s1") -> config.Settings:
    return dataclasses.replace(
        config.get_settings(reload=True), provider="fake", session_id=session_id
    )


class Fixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.conn = db.connect(self.tmp / "memory.db")
        db.migrate(self.conn, MIGRATIONS_MEMORY)
        self.store = MemoryStore(self.conn, archive_dir=self.tmp / "archive")
        self.goals = GoalStore(self.conn)
        self.todos = TodoStore(self.conn)
        self.schedules = ScheduleStore(self.conn)

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def dispatcher(self, *, briefing_hour: int = 9) -> Dispatcher:
        return Dispatcher(
            self.conn,
            settings=make_settings(),
            todos=self.todos,
            goals=self.goals,
            schedules=self.schedules,
            summaries_between=self.store.summaries_between,
            briefing_hour=briefing_hour,
        )

    def new_session(self, client=None, **kwargs) -> MemorySession:
        session = MemorySession(
            client or FakeClient(),
            make_settings(),
            store=self.store,
            session_id="s1",
            **kwargs,
        )
        session.start()
        return session


# ==========================================================================
# 目标
# ==========================================================================
class GoalTestCase(Fixture):
    def test_lifecycle_and_revision(self):
        goal = self.goals.create("减重 5kg", max_rounds=30)
        self.assertEqual(goal.phase, PHASE_ACTIVE)
        self.assertEqual(goal.revision, 1)

        paused = self.goals.update(goal.id, ACTION_PAUSE, expected_revision=1)
        self.assertEqual(paused.phase, PHASE_PAUSED)
        self.assertEqual(paused.revision, 2)

        resumed = self.goals.update(
            goal.id, ACTION_RESUME, expected_revision=2, source=SOURCE_USER
        )
        self.assertEqual(resumed.phase, PHASE_ACTIVE)

        done = self.goals.update(
            goal.id, ACTION_COMPLETE, expected_revision=resumed.revision
        )
        self.assertEqual(done.phase, PHASE_COMPLETE)

    def test_revision_conflict_is_rejected(self):
        goal = self.goals.create("读完三本书")
        self.goals.update(goal.id, ACTION_PAUSE, expected_revision=1)
        with self.assertRaises(RevisionConflict):
            self.goals.update(goal.id, ACTION_COMPLETE, expected_revision=1)

    def test_resume_requires_direct_user_request(self):
        goal = self.goals.create("每天走一万步")
        self.goals.update(goal.id, ACTION_PAUSE)
        with self.assertRaises(GoalError):
            self.goals.update(goal.id, ACTION_RESUME, source=SOURCE_AGENT)
        resumed = self.goals.update(goal.id, ACTION_RESUME, source=SOURCE_USER)
        self.assertEqual(resumed.phase, PHASE_ACTIVE)

    def test_max_rounds_exhaustion_blocks_automatically(self):
        goal = self.goals.create("坚持早睡", max_rounds=2)
        self.goals.bump_round(goal.id)
        after_first = self.goals.get(goal.id)
        self.assertEqual(after_first.phase, PHASE_ACTIVE)
        after_second = self.goals.bump_round(goal.id)
        self.assertEqual(after_second.phase, PHASE_BLOCKED)
        self.assertIn("轮次预算耗尽", after_second.blocked_reason or "")

    def test_goal_changes_are_event_sourced(self):
        events: list[tuple[str, dict]] = []
        goals = GoalStore(self.conn, emit=lambda kind, data: events.append((kind, data)))
        created = goals.create("每天背 20 个单词")
        goals.update(created.id, ACTION_PAUSE)
        self.assertEqual([kind for kind, _ in events], ["goal/change", "goal/change"])
        self.assertEqual(events[-1][1]["event"], "pause")
        self.assertEqual(events[-1][1]["goal"]["phase"], PHASE_PAUSED)


# ==========================================================================
# 待办与提醒
# ==========================================================================
class TodoTestCase(Fixture):
    def test_add_list_and_complete(self):
        first = self.todos.add("交周报")
        self.todos.add("买菜", due_date_local="2026-10-08")
        self.assertEqual(len(self.todos.list(status="pending")), 2)

        self.todos.set_status(first.id, STATUS_COMPLETED)
        pending = self.todos.list(status="pending")
        self.assertEqual([item.content for item in pending], ["买菜"])
        self.assertEqual(len(self.todos.list(status="all")), 2)

    def test_due_only_returns_overdue_or_today(self):
        self.todos.add("今天到期", due_date_local="2026-10-08")
        self.todos.add("明天到期", due_date_local="2026-10-09")
        self.todos.add("没有日期")
        due = self.todos.due("2026-10-08")
        self.assertEqual([item.content for item in due], ["今天到期"])


class ScheduleTestCase(Fixture):
    def test_every_below_300_seconds_is_rejected_twice_over(self):
        with self.assertRaises(ScheduleError):
            self.schedules.create("每秒都提醒", kind="every", every_seconds=60)
        # ⚠️ 即使绕过 Python 校验,数据库 CHECK 也必须挡住
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO schedules(id, kind, every_seconds, prompt, created_at, updated_at)"
                " VALUES ('x', 'every', 60, 'p', 't', 't')"
            )
        self.conn.rollback()

    def test_every_uses_at_as_first_fire(self):
        schedule = self.schedules.create(
            "看今日待办",
            kind="every",
            every_seconds=86400,
            at=TOMORROW_MORNING.isoformat(timespec="seconds"),
        )
        self.assertFalse(schedule.is_due(NOW))
        self.assertTrue(schedule.is_due(TOMORROW_MORNING + timedelta(minutes=1)))

    def test_at_fires_once_and_claim_is_exclusive(self):
        schedule = self.schedules.create(
            "开会", kind="at", at=(NOW - timedelta(minutes=1)).isoformat(timespec="seconds")
        )
        self.assertTrue(schedule.is_due(NOW))
        self.assertTrue(self.schedules.claim(schedule.id, NOW))
        self.assertFalse(self.schedules.claim(schedule.id, NOW), "抢占只能成功一次")
        refreshed = self.schedules.get(schedule.id)
        self.assertFalse(refreshed.is_due(NOW + timedelta(minutes=1)))


# ==========================================================================
# 确定性调度器
# ==========================================================================
class DispatcherTestCase(Fixture):
    def test_briefing_only_fires_once_per_day(self):
        dispatcher = self.dispatcher()
        first = dispatcher.deliver_briefing(NOW)
        self.assertIsNotNone(first)
        self.assertEqual(first.kind, KIND_BRIEFING)
        self.assertIsNone(dispatcher.deliver_briefing(NOW), "同一天不能重复播报")
        self.assertIsNone(dispatcher.deliver_briefing(NOW + timedelta(hours=2)))
        next_day = dispatcher.deliver_briefing(NOW + timedelta(days=1))
        self.assertIsNotNone(next_day, "第二天应当重新播报")

    def test_briefing_respects_hour_gate(self):
        dispatcher = self.dispatcher(briefing_hour=9)
        self.assertFalse(dispatcher.briefing_due(NOW.replace(hour=7)))
        self.assertIsNone(dispatcher.deliver_briefing(NOW.replace(hour=7)))

    def test_deliver_pending_never_claims_schedules(self):
        """派发播报时**不许**顺手认领提醒 —— 认领 = 已通知,而这里没有送达渠道。

        实测踩过(2026-10-12 19:10:20):三条提醒被这条路径认领后凭空消失,
        schedules.last_fired_at 被写死,用户当天一条都没收到,而且再也不会响第二次。
        提醒的认领入口只有 la/bridge/reminder_push.py(先送达、后认领)。
        """
        self.schedules.create(
            "喝水", kind="at", at=(NOW - timedelta(minutes=5)).isoformat(timespec="seconds")
        )
        items = self.dispatcher().deliver_pending(NOW)
        self.assertEqual([item.kind for item in items if item.kind == KIND_REMINDER], [])
        self.assertEqual(len(self.schedules.list()), 1)
        self.assertIsNone(
            self.schedules.list()[0].last_fired_at, "提醒绝不能在这里被认领"
        )

    def test_briefing_contains_todos_goals_and_yesterday_summary(self):
        self.todos.add("交周报", due_date_local="2026-10-08")
        self.goals.create("减重 5kg")
        self.store.upsert_summary(
            session_id="s1",
            start_seq=1,
            end_seq=4,
            start_time="2026-10-07T20:00:00+08:00",
            end_time="2026-10-07T21:00:00+08:00",
            text="<compacted-summary>- 事项:昨天聊了体检安排</compacted-summary>",
        )
        text = self.dispatcher().build_briefing(NOW).render()
        self.assertIn("交周报", text)
        self.assertIn("减重 5kg", text)
        self.assertIn("体检安排", text)
        self.assertIn("星期四", text)


# ==========================================================================
# 技能
# ==========================================================================
#
# ⚠️ 「显式事项由代码规则识别」那一组测试(D20 / la/tasks/intents.py)已随 D91 删除。
#    删它的理由不是「规则不好维护」,而是**它会写错东西**:那条正则用 re.search
#    匹配「提醒我」,再用贪婪的 (.+) 把后面**整行**吃掉 —— 于是
#    「今天中午你没有提醒我吃饭,是不是你没法主动发起推送?」这句**抱怨**
#    会变成内容为「吃饭,是不是你没法主动发起推送?」的一条提醒。
#    取证:当天 5 条提醒里 4 条出自它。现在显式指令全部交给模型工具调用。
class SkillTestCase(unittest.TestCase):
    def test_catalog_is_cheap_and_body_is_on_demand(self):
        library = SkillLibrary()
        names = library.names()
        self.assertIn("memory-protocol", names)
        catalog = library.catalog_text()
        self.assertIn("scheduling", catalog)
        self.assertNotIn("## 默认值", catalog, "目录里不能带正文")

        body = library.load("scheduling").body
        self.assertIn("300 秒", body)

    def test_unknown_skill_raises(self):
        with self.assertRaises(FileNotFoundError):
            SkillLibrary().load("no-such-skill")


# ==========================================================================
# 场景验收
# ==========================================================================
def tool_client(name: str, arguments: dict) -> FakeClient:
    """一个"只会调一次工具"的假客户端。

    ⚠️ 为什么需要它:显式指令**不再由代码正则识别**了(D91 删掉了那条路径),
    现在一律走模型的工具调用。所以"说一句『帮我定个目标』就有一个目标"这件事,
    必须由**工具调用**来驱动 —— 走的是完整的工具管线(门禁 / 配额 / 审计 / 回执),
    只是没有真 API。直接调 store 会把这条链路整段绕过去,验证不到任何东西。
    """
    return FakeClient(tool_script=[[
        ToolCall(id="c1", name=name, arguments=json.dumps(arguments, ensure_ascii=False))
    ]])


class ScenarioS5(Fixture):
    """S5:建一个目标,次日重开会话能接续,并且轮次由代码推进。"""

    async def test_goal_continues_across_sessions(self):
        session = self.new_session(
            client=tool_client("goal", {"action": "create", "objective": "减重 5kg"}),
            auto_briefing=False,
        )
        await session.respond("帮我定个目标:减重 5kg")
        goal = self.goals.list(open_only=True)[0]
        self.assertEqual(goal.rounds_started, 0)

        # 次日:简报到点派发 -> 轮次推进
        # (辅助会话关掉自动播报,否则它会用真实 now 抢先派发"今天"的简报)
        self.new_session(auto_briefing=False).collect_dispatches(TOMORROW_MORNING)
        advanced = self.goals.get(goal.id)
        self.assertEqual(advanced.rounds_started, 1, "派发简报就是推进一轮")

        # 再开一个全新会话:投影里必须带着目标状态
        revived = self.new_session(client=FakeClient(), auto_briefing=False)
        messages, _total = revived.projection_preview()
        system_text = "\n".join(m["content"] for m in messages if m["role"] == "system")
        self.assertIn("<goals>", system_text)
        self.assertIn("减重 5kg", system_text)
        self.assertIn("轮次 1", system_text)

    async def test_briefing_only_advances_rounds_when_delivered(self):
        session = self.new_session(
            client=tool_client("goal", {"action": "create", "objective": "减重 5kg"}),
            auto_briefing=False,
        )
        await session.respond("帮我定个目标:减重 5kg")
        goal = self.goals.list(open_only=True)[0]
        for _ in range(3):
            self.new_session(auto_briefing=False).collect_dispatches(TOMORROW_MORNING)
        self.assertEqual(self.goals.get(goal.id).rounds_started, 1, "同一天只推进一轮")
        del session


class ScenarioS6(Fixture):
    """S6:每天 9:00 的待办播报,内容来自待办清单 + 昨日概要摘要。"""

    async def test_daily_briefing_is_injected_into_the_projection(self):
        session = self.new_session(
            client=tool_client("todo_write", {"content": "交周报", "source": "user"}),
            auto_briefing=False,
        )
        await session.respond("记个待办:交周报")
        self.assertEqual(len(self.todos.list(status="pending")), 1)

        # 次日 9:00 开新会话:自动派发简报
        client = FakeClient()
        revived = self.new_session(client=client, auto_briefing=False)
        revived.collect_dispatches(TOMORROW_MORNING)
        messages, _total = revived.projection_preview()
        system_text = "\n".join(m["content"] for m in messages if m["role"] == "system")
        self.assertIn("<briefing", system_text)
        self.assertIn("交周报", system_text)

        # 播报落盘了,而且只落一次
        dispatches = [e for e in self.store.events("s1") if e.type == "dispatch"]
        self.assertEqual(len(dispatches), 1)
        self.assertEqual(dispatches[0].data["kind"], KIND_BRIEFING)

        self.new_session(auto_briefing=False).collect_dispatches(TOMORROW_MORNING)
        dispatch_count = len([e for e in self.store.events("s1") if e.type == "dispatch"])
        self.assertEqual(dispatch_count, 1, "同一天不能重复打扰")
        del session


if __name__ == "__main__":
    unittest.main()