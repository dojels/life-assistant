"""到点提醒的送达环:先送达、后认领(la/bridge/reminder_push.py)。

为什么值得单独一组测试:这条链路上的每一个 bug 都是**静默**的 ——
提醒被认领了、库里有记录,只有用户什么都没收到。实测踩过两次:

  1. deliver_pending 先把提醒认领进"投影",而会话那条链路根本没有出口
     (_briefing_text 只放 briefing/study),三条提醒凭空消失;
  2. 日历提醒那段 try 里用了一个从未导入的名字 KIND_REMINDER,NameError
     被 except Exception 吞掉 —— 实例在 claim() 之后立刻被丢弃。
     取证:app_reminder_instances 的 fired_at 有值,而 dispatches / events 里
     一条 calendar-reminder 都没有。

所以这里的判据全部盯着一件事:**认领有没有发生在送达之后**。

运行:
    .venv/Scripts/python.exe -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from la import config
from la.bridge.reminder_push import (
    KIND_MISSED,
    ReminderPusher,
    render_reminders,
)
from la.domain.schemas.calendar import CalendarEventCreate
from la.domain.services.calendar_service import CalendarService
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT, MIGRATIONS_MEMORY
from la.tasks.dispatcher import KIND_REMINDER
from la.tasks.schedules import ScheduleStore

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 12, 19, 0, tzinfo=TZ)


class _Sender:
    """可控的发送器:记下发过什么,并能按需失败。"""

    def __init__(self, ok: bool = True, reason: str = "已推送 1 条") -> None:
        self.ok = ok
        self.reason = reason
        self.sent: list[str] = []

    async def __call__(self, text: str) -> tuple[bool, str]:
        self.sent.append(text)
        if not self.ok:
            return False, self.reason
        return True, self.reason


class Fixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.memory = db.connect(self.tmp / "memory.db")
        db.migrate(self.memory, MIGRATIONS_MEMORY)
        self.assistant = db.connect(self.tmp / "assistant.db")
        db.migrate(self.assistant, MIGRATIONS_ASSISTANT)
        self.schedules = ScheduleStore(self.memory)
        self.sender = _Sender()
        self.pusher = ReminderPusher(
            self.memory, self.assistant,
            send=self.sender,
            settings=dataclasses.replace(
                config.get_settings(reload=True), timezone="Asia/Shanghai"
            ),
            log=lambda line: None,
        )

    def tearDown(self) -> None:
        self.pusher.close()
        self._tmp.cleanup()

    def at_reminder(self, prompt: str = "吃饭", *, offset_minutes: int = 0):
        moment = NOW + timedelta(minutes=offset_minutes)
        return self.schedules.create(
            prompt, kind="at", at=moment.isoformat(timespec="seconds"), receipt=False
        )

    def dispatches(self, kind: str | None = None) -> list[dict]:
        sql = "SELECT id, kind, payload FROM dispatches"
        params: list = []
        if kind:
            sql += " WHERE kind = ?"
            params.append(kind)
        return [dict(row) for row in self.memory.execute(sql, params).fetchall()]

    def calendar_event(self, *, minutes_from_now: int = 30, title: str = "开会"):
        service = CalendarService(self.assistant)
        start = NOW + timedelta(minutes=minutes_from_now)
        return service.create_event(CalendarEventCreate(
            title=title,
            start=start.isoformat(timespec="seconds"),
            end=(start + timedelta(hours=1)).isoformat(timespec="seconds"),
            reminder_minutes=30,
        ))


# ==========================================================================
# 送达与认领的先后
# ==========================================================================
class DeliveryOrderTestCase(Fixture):
    async def test_not_claimed_when_delivery_fails(self):
        made = self.at_reminder()
        self.sender.ok = False
        self.sender.reason = "没有 context_token(用户还没说过话)"
        report = await self.pusher.tick(NOW)
        self.assertEqual(report["sent"], [])
        self.assertEqual(self.dispatches(KIND_REMINDER), [], "没送到就不许写派发记录")
        self.assertIsNone(
            self.schedules.get(made.id).last_fired_at, "没送到就不许认领"
        )

    async def test_claims_only_after_delivery(self):
        made = self.at_reminder()
        report = await self.pusher.tick(NOW)
        self.assertEqual(len(report["sent"]), 1)
        self.assertEqual(self.sender.sent, ["⏰ 提醒:吃饭"])
        self.assertIsNotNone(self.schedules.get(made.id).last_fired_at)
        self.assertEqual(len(self.dispatches(KIND_REMINDER)), 1)

    async def test_no_resend_after_claim(self):
        self.at_reminder()
        await self.pusher.tick(NOW)
        again = await self.pusher.tick(NOW + timedelta(seconds=30))
        self.assertEqual(again["sent"], [])
        self.assertEqual(len(self.sender.sent), 1, "重启/重复轮询都不得重发")

    async def test_targeted_reminder_only_goes_to_the_selected_member(self):
        self.schedules.create(
            "妈妈的药", kind="at", at=NOW.isoformat(timespec="seconds"),
            target_member_id=2, receipt=False,
        )
        calls: list[tuple[str, int | None]] = []

        async def send_to(text: str, member_id: int | None):
            calls.append((text, member_id))
            return True, "ok"

        self.pusher.send_to = send_to
        report = await self.pusher.tick(NOW)
        self.assertEqual(len(report["sent"]), 1)
        self.assertEqual(calls, [("⏰ 提醒:妈妈的药", 2)])
        self.assertEqual(self.sender.sent, [], "指定收件人时不能走广播")

    async def test_two_targets_are_rendered_and_sent_separately(self):
        self.schedules.create(
            "妈妈的药", kind="at", at=NOW.isoformat(timespec="seconds"),
            target_member_id=2, receipt=False,
        )
        self.schedules.create(
            "爸爸的会", kind="at", at=NOW.isoformat(timespec="seconds"),
            target_member_id=3, receipt=False,
        )
        calls: list[tuple[str, int | None]] = []

        async def send_to(text: str, member_id: int | None):
            calls.append((text, member_id))
            return True, "ok"

        self.pusher.send_to = send_to
        report = await self.pusher.tick(NOW)
        self.assertEqual(len(report["sent"]), 2)
        self.assertEqual([member for _, member in calls], [2, 3])
        self.assertEqual(
            [text for text, _member in calls],
            ["⏰ 提醒:妈妈的药", "⏰ 提醒:爸爸的会"],
        )

    async def test_retry_once_the_window_opens(self):
        """窗口关着时先攒着;窗口一开(下一轮)照样发得出去。"""
        self.at_reminder(offset_minutes=-10)
        self.sender.ok = False
        await self.pusher.tick(NOW)
        self.sender.ok = True
        report = await self.pusher.tick(NOW + timedelta(seconds=30))
        self.assertEqual(len(report["sent"]), 1)
        self.assertIn("原定", self.sender.sent[-1], "迟到要如实说明原定时刻")


# ==========================================================================
# 过期不补(窗口由协议决定,不设上限就会集中轰炸)
# ==========================================================================
class StalenessTestCase(Fixture):
    async def test_too_late_is_not_sent_but_still_claimed(self):
        made = self.at_reminder(offset_minutes=-180)
        report = await self.pusher.tick(NOW)
        self.assertEqual(self.sender.sent, [], "迟到 3 小时不该再补发")
        self.assertEqual(len(report["missed"]), 1)
        missed = self.dispatches(KIND_MISSED)
        self.assertEqual(len(missed), 1)
        self.assertIn("不再补发", missed[0]["payload"])
        self.assertIsNotNone(
            self.schedules.get(made.id).last_fired_at,
            "错过也要认领,否则每一轮都会重新捡起来",
        )

    async def test_missed_does_not_come_back(self):
        self.at_reminder(offset_minutes=-180)
        await self.pusher.tick(NOW)
        again = await self.pusher.tick(NOW + timedelta(seconds=30))
        self.assertEqual(again["missed"], [])
        self.assertEqual(len(self.dispatches(KIND_MISSED)), 1)

    async def test_just_inside_the_window_is_still_sent(self):
        self.at_reminder(offset_minutes=-59)
        report = await self.pusher.tick(NOW)
        self.assertEqual(len(report["sent"]), 1)


# ==========================================================================
# 合并成一条(窗口额度按条算)
# ==========================================================================
class BatchTestCase(Fixture):
    async def test_several_due_are_merged_into_one_message(self):
        self.at_reminder("喝水", offset_minutes=-3)
        self.at_reminder("吃药", offset_minutes=-2)
        report = await self.pusher.tick(NOW)
        self.assertEqual(len(report["sent"]), 2)
        self.assertEqual(len(self.sender.sent), 1, "两条提醒只该占一格额度")
        text = self.sender.sent[0]
        self.assertIn("到点提醒 2 条", text)
        self.assertIn("喝水", text)
        self.assertIn("吃药", text)

    def test_render_shapes(self):
        self.assertEqual(render_reminders([], NOW), "")
        single = self.at_reminder("吃药")
        item = self.pusher._schedule_items(NOW)[0]
        self.assertEqual(render_reminders([item], NOW), "⏰ 提醒:吃药")
        self.assertIsNotNone(single.id)


# ==========================================================================
# 日历提醒走同一扇门
# ==========================================================================
class CalendarReminderTestCase(Fixture):
    async def test_calendar_reminder_goes_through_the_same_door(self):
        self.calendar_event(minutes_from_now=30)
        report = await self.pusher.tick(NOW)
        self.assertEqual(len(report["sent"]), 1)
        self.assertIn("开会", self.sender.sent[0])
        row = self.assistant.execute(
            "SELECT fired_at FROM app_reminder_instances"
        ).fetchone()
        self.assertIsNotNone(row["fired_at"])

    async def test_calendar_reminder_not_claimed_when_send_fails(self):
        self.calendar_event(minutes_from_now=30)
        self.sender.ok = False
        await self.pusher.tick(NOW)
        row = self.assistant.execute(
            "SELECT fired_at FROM app_reminder_instances"
        ).fetchone()
        self.assertIsNone(row["fired_at"], "发不出去就不许认领(旧代码就死在这里)")

    async def test_cancelled_event_is_not_pushed(self):
        event = self.calendar_event(minutes_from_now=30)
        self.pusher.materialize(NOW)          # 先物化出实例
        CalendarService(self.assistant).cancel({"uid": event.uid})   # 再取消
        report = await self.pusher.tick(NOW)
        self.assertEqual(self.sender.sent, [], "取消掉的事不该再提醒")
        self.assertEqual(len(report["missed"]), 1)


# ==========================================================================
# 迟到的判据必须与触发的判据同源(la/tasks/schedules.py 的 due_at)
# ==========================================================================
class DueAtTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self._tmp.name) / "memory.db")
        db.migrate(self.conn, MIGRATIONS_MEMORY)
        self.schedules = ScheduleStore(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_at_is_due_once_then_never_again(self):
        made = self.schedules.create(
            "开会", kind="at", at=NOW.isoformat(timespec="seconds"), receipt=False
        )
        self.assertEqual(self.schedules.get(made.id).due_at(), NOW)
        self.assertFalse(self.schedules.get(made.id).is_due(NOW - timedelta(minutes=1)))
        self.schedules.claim(made.id, NOW)
        self.assertFalse(self.schedules.get(made.id).is_due(NOW + timedelta(hours=1)))

    def test_after_counts_from_creation(self):
        made = self.schedules.create(
            "喝水", kind="after", every_seconds=600, receipt=False
        )
        due = self.schedules.get(made.id).due_at()
        created = datetime.fromisoformat(self.schedules.get(made.id).created_at)
        self.assertEqual(due, created + timedelta(seconds=600))
        self.assertTrue(self.schedules.get(made.id).is_due(due))

    def test_every_advances_and_stays_due(self):
        made = self.schedules.create(
            "看周报", kind="every", every_seconds=86400,
            at=NOW.isoformat(timespec="seconds"), receipt=False
        )
        self.assertEqual(self.schedules.get(made.id).due_at(), NOW)
        self.schedules.claim(made.id, NOW)
        advanced = self.schedules.get(made.id)
        self.assertEqual(advanced.due_at(), NOW + timedelta(days=1))
        self.assertTrue(advanced.is_due(NOW + timedelta(days=2)))
        self.assertFalse(advanced.is_due(NOW + timedelta(hours=1)))


if __name__ == "__main__":
    unittest.main()

# ==========================================================================
# 微信主动推送这一端:能不能发、发不发得出去,必须如实回答
# ==========================================================================
class _FakeILinkClient:
    """只实现 push_text 会碰到的那一个方法。"""

    def __init__(self, remaining: int | None = None) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.remaining = remaining

    async def send_text(self, to_user: str, text: str, context_token: str) -> dict:
        self.sent.append((to_user, text, context_token))
        payload: dict = {}
        if self.remaining is not None:
            payload["remaining"] = self.remaining
        return payload


class PushTextTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # ⚠️ 数据目录必须**整个用例**都指向临时目录。实测踩过:早先只用一个 with
        #    包住 ILinkAdapter(...) 构造,而 push_text 里的 save_session 跑在 with
        #    外面 —— 单元测试真的往 data/wechat/session.json 写了一条 wxid-1/tok
        #    的假会话,常驻服务起来会"接回"一个不存在的联系人并往那儿发消息。
        #    教训:隔离要包住**整个用例**,不是包住你以为会写盘的那一行。
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(config, "DATA_DIR", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def adapter(self, client):
        from la.bridge.adapters.weixin_ilink import ILinkAdapter

        return ILinkAdapter(bridge=None, client=client, log=lambda line: None)

    def test_data_dir_is_isolated(self) -> None:
        """钉住上面那条:这个用例组永远不许写到真实的 data/ 里去。"""
        from la.bridge.adapters import weixin_store

        path = weixin_store.session_path()
        self.assertTrue(str(path).startswith(self.tmp.name), str(path))
        self.assertNotEqual(path, Path(config.BASE_DIR) / "data" / "wechat" / "session.json")

    async def test_pushing_never_writes_the_real_session_file(self) -> None:
        """真正跑一次推送,再确认落盘落在临时目录里(而不是 data/wechat/)。"""
        from la.bridge.adapters import weixin_store

        # ⚠️ 判据是"**没被动过**",不是"不存在":用户真在跑微信桥接时,
        #    真实 session.json 本来就躺在那里 —— 原来那条断言会把"环境里有这个文件"
        #    误判成"测试写进去了"(2026-10-12 实测:这条让整个测试套件长期红着)。
        real = Path(config.BASE_DIR) / "data" / "wechat" / "session.json"
        before = real.stat().st_mtime_ns if real.exists() else None

        client = _FakeILinkClient()
        adapter = self.adapter(client)
        conv = adapter.transport.conversation("wxid-1")
        conv.context_token = "tok"
        conv.remaining_quota = 5
        ok, _reason = await adapter.push_text("⏰ 提醒:吃饭")
        self.assertTrue(ok)
        self.assertTrue(weixin_store.session_path().exists())
        after = real.stat().st_mtime_ns if real.exists() else None
        self.assertEqual(before, after, "这次推送动到了真实会话文件")

    async def test_no_contact_yet(self):
        ok, reason = await self.adapter(_FakeILinkClient()).push_text("⏰ 提醒:吃饭")
        self.assertFalse(ok)
        self.assertIn("联系人", reason)

    async def test_no_context_token(self):
        adapter = self.adapter(_FakeILinkClient())
        adapter.transport.conversation("wxid-1")
        ok, reason = await adapter.push_text("⏰ 提醒:吃饭")
        self.assertFalse(ok)
        self.assertIn("context_token", reason)

    async def test_last_quota_slot_is_reserved_for_the_reply(self):
        adapter = self.adapter(_FakeILinkClient())
        conv = adapter.transport.conversation("wxid-1")
        conv.context_token = "tok"
        conv.remaining_quota = 1
        ok, reason = await adapter.push_text("⏰ 提醒:吃饭")
        self.assertFalse(ok, "额度只剩一条时必须留给用户说完话之后的回复")
        self.assertIn("留给回复", reason)

    async def test_sends_and_reports_remaining(self):
        client = _FakeILinkClient(remaining=4)
        adapter = self.adapter(client)
        conv = adapter.transport.conversation("wxid-1")
        conv.context_token = "tok"
        ok, reason = await adapter.push_text("⏰ 提醒:吃饭")
        self.assertTrue(ok, reason)
        self.assertEqual(client.sent, [("wxid-1", "⏰ 提醒:吃饭", "tok")])
        self.assertEqual(conv.remaining_quota, 4, "以服务端回报的剩余额度为准")

    async def test_empty_text_is_rejected(self):
        ok, reason = await self.adapter(_FakeILinkClient()).push_text("   ")
        self.assertFalse(ok)
        self.assertIn("空消息", reason)


class ConversationPersistenceTestCase(unittest.TestCase):
    def test_round_trip(self):
        from la.bridge.adapters.weixin_ilink import Conversation

        conv = Conversation(user_id="wxid-1", context_token="tok", last_seen_ms=123)
        conv.remaining_quota = 7
        again = Conversation.from_dict(conv.as_dict())
        self.assertEqual(again.user_id, "wxid-1")
        self.assertEqual(again.context_token, "tok")
        self.assertEqual(again.remaining_quota, 7)

    def test_broken_payload_falls_back_to_defaults(self):
        from la.bridge.adapters.weixin_ilink import Conversation

        conv = Conversation.from_dict({})
        self.assertEqual(conv.user_id, "")
        self.assertTrue(conv.remaining_quota > 0)

    def test_store_ignores_a_session_without_token(self):
        from la.bridge.adapters import weixin_store

        with mock.patch.object(
            weixin_store, "session_path", return_value=Path(self._write("{}"))
        ):
            self.assertIsNone(weixin_store.load_session())

    def _write(self, text: str) -> str:
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        handle.write(text)
        handle.close()
        return handle.name


if __name__ == "__main__":
    unittest.main()
