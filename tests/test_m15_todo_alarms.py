"""待办与提醒的关系(D115):**待办是"事",提醒是"事上的闹钟"**。

用户 2026-10-13 的原话:*"我说明天中午要更新云朵,智能体给我建了一条提醒,更新云朵不应该
是一项待完成的工作吗?这个工作在开始前,需要智能体提醒我一下。"*

当时的实情:`todos` 与 `schedules` 两张表**没有任何互相引用** —— 真库里就躺着一条
"提醒:给助手加写 HTML 文件的能力(待办)"(同一件事两行,互不知道),而待办只有日期
没有时刻,于是"明天中午要做的活"只能落到提醒表里,**工作本身丢了**,响过一次就什么都不剩。

这一组测试钉住新口径:
  ① 有时刻的待办**自带一条闹钟**(时刻 = 到期时刻 − 提前量);
  ② 勾掉待办 → 闹钟**关掉**(不是删掉);放回未完成、且还没到点 → 重新开起来;
  ③ 删掉待办 → 闹钟跟着走,撤销时**原样**回来(编号、响过没有都保留);
  ④ 改到期时间 → 闹钟跟着**重排**,不是再挂一条;
  ⑤ 到点那条消息要说清"这是哪条待办、怎么收尾";已完成的不打扰;
  ⑥ 界面:待办行看得见 ⏰;提醒页看得见归属、还能就地勾掉;历史里那些"其实是工作"的
     独立提醒由用户点一下补成待办(**不自动改**)。

运行:
    .venv/Scripts/python.exe -m unittest tests.test_m15_todo_alarms -v
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from la.domain.receipts import ReceiptLog
from la.domain.reverters import build_reverter
from la.tasks.dispatcher import Briefing
from la.tasks.schedules import ScheduleStore
from la.tasks.todos import REMIND_NONE, TodoStore, alarm_at
from la.storage import db
from la.storage.migrations import MIGRATIONS_MEMORY

TZ = ZoneInfo("Asia/Shanghai")


def _future_moment(*, days: int = 2, hour: int = 12) -> datetime:
    """一个**永远在未来**的时刻(测试不该有保质期)。

    ⚠️ 这里原来写死 "2026-10-14T12:00:00+08:00" —— 到了 2026-10-15 那天它变成过去,
       test_undoing_the_completion_brings_the_alarm_back 就红了:按设计,**过点的闹钟
       不会自动重开**(否则撤销一次勾选就会立刻补一响)。写死日期的测试要么跟着 now 走,
       要么就得写明"这是一条历史数据" —— 别让它在某天早上突然变红。
    """
    moment = datetime.now(TZ) + timedelta(days=days)
    return moment.replace(hour=hour, minute=0, second=0, microsecond=0)


DUE = _future_moment()                        # 到期时刻(带时区)
DUE_AT = DUE.isoformat(timespec="seconds")
DUE_DAY = DUE.date().isoformat()              # 给了时刻要能补出日期
LEAD_30 = (DUE - timedelta(minutes=30)).isoformat(timespec="seconds")
NEXT_DAY = (DUE + timedelta(days=1)).date().isoformat()
OTHER = _future_moment(days=3, hour=9)        # 「改到期时间」用的另一个时刻
OTHER_AT = OTHER.isoformat(timespec="seconds")
OTHER_DAY = OTHER.date().isoformat()


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.conn = db.connect(Path(self._tmp.name) / "memory.db")
        db.migrate(self.conn, MIGRATIONS_MEMORY)
        self.log = ReceiptLog(self.conn)
        self.schedules = ScheduleStore(self.conn, receipts=self.log)
        self.todos = TodoStore(self.conn, receipts=self.log, )
        # TodoStore 内部的闹钟簿与外面这本要看得见同一批行(同一个连接)
        self.reverter = build_reverter(
            self.log, todos=self.todos, schedules=self.schedules
        )

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
        self._tmp.cleanup()

    def alarms(self, todo_id: str):
        return self.schedules.for_todo(todo_id)


class AlarmRuleCase(StoreCase):
    def test_alarm_time_is_due_minus_lead(self) -> None:
        self.assertEqual(alarm_at(DUE_AT, 0), DUE_AT)
        self.assertEqual(alarm_at(DUE_AT, 30), LEAD_30)
        self.assertIsNone(alarm_at(None, 0), "没有时刻就没有闹钟")
        self.assertIsNone(alarm_at(DUE_AT, None), "不提醒就是没有闹钟")

    def test_a_timed_todo_carries_its_own_alarm(self) -> None:
        todo = self.todos.add("更新云朵智能体", due_at_local=DUE_AT)
        self.assertEqual(todo.due_at_local, DUE_AT)
        self.assertEqual(todo.due_date_local, DUE_DAY, "给了时刻要把日期补出来")
        alarms = self.alarms(todo.id)
        self.assertEqual(len(alarms), 1)
        self.assertEqual(alarms[0].at, DUE_AT)
        self.assertEqual(alarms[0].todo_id, todo.id)
        self.assertIn("更新云朵智能体", alarms[0].prompt)

    def test_lead_time_moves_the_alarm_not_the_todo(self) -> None:
        todo = self.todos.add("开会", due_at_local=DUE_AT, remind_minutes_before=30)
        self.assertEqual(todo.due_at_local, DUE_AT, "待办自己的时刻不动")
        self.assertEqual(self.alarms(todo.id)[0].at, LEAD_30)

    def test_date_only_todo_has_no_alarm(self) -> None:
        todo = self.todos.add("交物业费", due_date_local="2026-10-20")
        self.assertEqual(self.alarms(todo.id), [])

    def test_explicit_no_reminder(self) -> None:
        todo = self.todos.add("只是记一下", due_at_local=DUE_AT,
                              remind_minutes_before=REMIND_NONE)
        self.assertIsNone(todo.remind_minutes_before)
        self.assertEqual(self.alarms(todo.id), [])

    def test_one_receipt_covers_both_rows(self) -> None:
        before = self.log.last_id()
        self.todos.add("更新云朵智能体", due_at_local=DUE_AT)
        fresh = self.log.since(before)
        self.assertEqual(len(fresh), 1, "一次写入 = 一条回执(闹钟不单独记回执)")
        self.assertIn("到点提醒", fresh[0].label)


class StatusLinkCase(StoreCase):
    def test_completing_closes_the_alarm(self) -> None:
        todo = self.todos.add("更新云朵智能体", due_at_local=DUE_AT)
        alarm = self.alarms(todo.id)[0]
        self.todos.set_status(todo.id, "completed")
        self.assertFalse(self.schedules.get(alarm.id).enabled, "活干完了,闹钟不该还响着")
        self.assertIsNotNone(self.schedules.get(alarm.id), "关掉不是删掉(撤销要原样回来)")

    def test_reopening_a_future_alarm_switches_it_back_on(self) -> None:
        future = (datetime.now(TZ) + timedelta(days=3)).replace(microsecond=0)
        todo = self.todos.add("以后要做", due_at_local=future.isoformat(timespec="seconds"))
        self.todos.set_status(todo.id, "completed")
        alarm_id = self.alarms(todo.id)[0].id
        self.assertFalse(self.schedules.get(alarm_id).enabled)
        self.todos.set_status(todo.id, "pending")
        self.assertTrue(self.schedules.get(alarm_id).enabled)

    def test_reopening_does_not_resurrect_a_past_alarm(self) -> None:
        past = (datetime.now(TZ) - timedelta(hours=2)).replace(microsecond=0)
        todo = self.todos.add("早就该做的事", due_at_local=past.isoformat(timespec="seconds"))
        self.todos.set_status(todo.id, "completed")
        self.todos.set_status(todo.id, "pending")
        self.assertFalse(self.schedules.get(self.alarms(todo.id)[0].id).enabled,
                         "已经过点的闹钟不许自动重开 —— 那只会立刻补一响")

    def test_undoing_the_completion_brings_the_alarm_back(self) -> None:
        todo = self.todos.add("更新云朵智能体", due_at_local=DUE_AT)
        alarm_id = self.alarms(todo.id)[0].id
        self.todos.set_status(todo.id, "completed")
        receipt_id = self.log.last_id()          # 撤销的是**这一次勾选**,不是新建
        self.reverter.undo(receipt_id)
        self.assertEqual(self.todos.get(todo.id).status, "pending")
        self.assertTrue(self.schedules.get(alarm_id).enabled, "撤销勾选,闹钟要跟着开回来")


class DueAndDeleteCase(StoreCase):
    def test_set_due_reschedules_instead_of_stacking(self) -> None:
        todo = self.todos.add("更新云朵智能体", due_at_local=DUE_AT)
        self.todos.set_due(todo.id, OTHER_DAY, due_at_local=OTHER_AT)
        alarms = self.alarms(todo.id)
        self.assertEqual(len(alarms), 1, "重排,不是再挂一条")
        self.assertEqual(alarms[0].at, OTHER_AT)

    def test_set_due_date_keeps_the_clock_time(self) -> None:
        todo = self.todos.add("更新云朵智能体", due_at_local=DUE_AT)
        self.todos.set_due_date(todo.id, NEXT_DAY)
        alarms = self.alarms(todo.id)
        self.assertEqual(alarms[0].at, NEXT_DAY + "T12:00:00+08:00", "只改日期要保住钟点")

    def test_undoing_a_reschedule_restores_the_old_alarm_row(self) -> None:
        todo = self.todos.add("更新云朵智能体", due_at_local=DUE_AT)
        old_alarm = self.alarms(todo.id)[0]
        self.todos.set_due(todo.id, OTHER_DAY, due_at_local=OTHER_AT)
        receipt_id = self.log.last_id()          # 撤销的是**这一次改期**
        self.reverter.undo(receipt_id)
        alarms = self.alarms(todo.id)
        self.assertEqual(len(alarms), 1)
        self.assertEqual(alarms[0].id, old_alarm.id, "撤销要回到原来那一条(编号不变)")
        self.assertEqual(alarms[0].at, DUE_AT)

    def test_deleting_a_todo_takes_the_alarm_and_undo_brings_both_back(self) -> None:
        todo = self.todos.add("更新云朵智能体", due_at_local=DUE_AT)
        alarm = self.alarms(todo.id)[0]
        self.schedules.claim(alarm.id, datetime.now(TZ))     # 假装它已经响过了
        receipt_id = None
        self.todos.delete(todo.id, record_receipt=True)
        receipt_id = self.log.last_id()
        self.assertEqual(self.alarms(todo.id), [], "待办没了,闹钟不该还留着")
        self.reverter.undo(receipt_id)
        restored = self.todos.get(todo.id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.due_at_local, DUE_AT)
        back = self.alarms(todo.id)
        self.assertEqual(len(back), 1)
        self.assertEqual(back[0].id, alarm.id, "编号不变")
        self.assertIsNotNone(back[0].last_fired_at, "响过没有也要原样回来")


class WordingCase(StoreCase):
    def test_the_briefing_lists_the_alarm_with_its_todo_not_twice(self) -> None:
        todo = self.todos.add("更新云朵智能体", due_at_local=DUE_AT)
        alarm = self.schedules.get(self.alarms(todo.id)[0].id)
        loose = self.schedules.create("吃药", kind="at", at=DUE_AT, receipt=False)
        text = Briefing(
            date_local="2026-10-13", weekday="一",
            todos=self.todos.list(), reminders=[alarm, loose],
        ).render()
        self.assertIn("更新云朵智能体(12:00 到点提醒)", text)
        self.assertIn("吃药", text)
        self.assertEqual(text.count("更新云朵智能体"), 1, "同一件事不该在简报里出现两次")

    def _pusher(self):
        from la.bridge.reminder_push import ReminderPusher
        from la.tasks.dispatcher import Dispatcher

        return ReminderPusher(
            self.conn, self.conn, send=lambda text: None,
            settings=_settings(), log=lambda text: None,
        )

    def test_the_pushed_line_says_what_to_do_and_how_to_close_it(self) -> None:
        todo = self.todos.add("更新云朵智能体", due_at_local=DUE_AT)
        alarm = self.schedules.get(self.alarms(todo.id)[0].id)
        text, note = self._pusher()._schedule_text(alarm)
        self.assertIn("该做:更新云朵智能体", text)
        self.assertIn("做完了", text)
        self.assertEqual(note, "")

    def test_a_completed_todo_is_not_pushed_again(self) -> None:
        todo = self.todos.add("更新云朵智能体", due_at_local=DUE_AT)
        alarm = self.schedules.get(self.alarms(todo.id)[0].id)
        self.todos.set_status(todo.id, "completed")
        _text, note = self._pusher()._schedule_text(alarm)
        self.assertEqual(note, "待办已完成", "完成了就不再打扰,但要说明原因")


def _settings():
    from la import config

    return config.get_settings()


class CalendarPageAlarmCase(unittest.TestCase):
    """界面层:待办行看得见 ⏰;提醒页看得见归属、能就地勾掉、能补成待办。"""

    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from la import config
        from la.domain.audit import AuditSink
        from la.domain.services.calendar_service import CalendarService
        from la.web import app as web_app

        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self._patched = {
            key: getattr(config, key)
            for key in ("DATA_DIR", "ARCHIVE_DIR", "MEDIA_DIR", "ASSISTANT_DB", "MEMORY_DB")
        }
        self._env = {
            key: os.environ.get(key)
            for key in ("DEEPSEEK_API_KEY", "LA_LLM_PROVIDER", "LA_TZ")
        }
        os.environ["DEEPSEEK_API_KEY"] = ""
        os.environ["LA_LLM_PROVIDER"] = "fake"
        config.DATA_DIR = self.tmp
        config.ARCHIVE_DIR = self.tmp / "archive"
        config.MEDIA_DIR = self.tmp / "media"
        config.ASSISTANT_DB = self.tmp / "assistant.db"
        config.MEMORY_DB = self.tmp / "memory.db"
        config._settings = None
        self.settings = config.get_settings()

        conn = db.connect_assistant()
        CalendarService(conn, audit=AuditSink(conn))       # 建库,顺便确认迁移能跑
        conn.close()
        db.connect_memory().close()
        self.client = TestClient(web_app.create_app())

    def tearDown(self) -> None:
        from la import config

        for key, value in self._patched.items():
            setattr(config, key, value)
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config._settings = None
        self._tmp.cleanup()

    def _stores(self):
        conn = db.connect_memory()
        return conn, TodoStore(conn, receipts=ReceiptLog(conn)), ScheduleStore(conn)

    def _due(self, *, days: int = 1):
        return (self.settings.now() + timedelta(days=days)).replace(
            hour=12, minute=0, second=0, microsecond=0
        )

    def test_the_form_takes_a_time_and_the_row_shows_the_alarm(self) -> None:
        tomorrow = (self.settings.now() + timedelta(days=1)).date().isoformat()
        response = self.client.post(
            "/calendar/todos/add",
            data={"content": "更新云朵智能体", "due": tomorrow, "at": "12:00",
                  "_idem": "tok-alarm-1"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        conn, todos, schedules = self._stores()
        rows = todos.list(status="pending")
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].due_at_local[11:16], "12:00")
        self.assertIn("+08:00", rows[0].due_at_local, "存的必须是带时区的时刻")

        html = self.client.get("/calendar?tab=todos").text
        self.assertIn("更新云朵智能体", html)
        self.assertIn("⏰", html, "待办行上要看得见它的闹钟")
        self.assertIn("12:00", html)

    def test_a_time_without_a_date_is_refused_with_a_reason(self) -> None:
        response = self.client.post(
            "/calendar/todos/add",
            data={"content": "没日期的时刻", "at": "12:00", "_idem": "tok-alarm-2"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("err=", response.headers["location"])

    def test_the_reminder_row_shows_its_todo_and_can_close_it(self) -> None:
        conn, todos, schedules = self._stores()
        todo = todos.add("更新云朵智能体", due_at_local=self._due().isoformat(timespec="seconds"))
        conn.close()

        html = self.client.get("/calendar?tab=reminders").text
        self.assertIn("更新云朵智能体", html, "提醒行要用待办的名字当标题")
        self.assertIn("待办的闹钟", html, "要一眼看出它属于一条待办")
        self.assertIn("完成", html)

        done = self.client.post(
            f"/calendar/todos/{todo.id}/done",
            data={"_idem": "tok-alarm-done", "back": "/calendar?tab=reminders"},
            follow_redirects=False,
        )
        self.assertEqual(done.status_code, 303)
        self.assertIn("tab=reminders", done.headers["location"],
                      "从提醒页点的完成,要回到提醒页")

        conn, todos, schedules = self._stores()
        alarms = schedules.for_todo(todo.id)
        conn.close()
        self.assertEqual(len(alarms), 1)
        self.assertFalse(alarms[0].enabled, "勾掉待办,它的闹钟要停")

    def test_adopting_a_loose_reminder_makes_it_a_todo(self) -> None:
        conn, todos, schedules = self._stores()
        loose = schedules.create(
            "提醒:更新云朵智能体", kind="at",
            at=self._due().isoformat(timespec="seconds"), receipt=False,
        )
        conn.close()

        self.assertIn("转成待办", self.client.get("/calendar?tab=reminders").text)
        response = self.client.post(
            f"/calendar/reminders/{loose.id}/adopt", data={"_idem": "tok-adopt-1"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

        conn, todos, schedules = self._stores()
        adopted = [item for item in todos.list(status="all") if "云朵" in item.content]
        linked = schedules.get(loose.id)
        attached = schedules.for_todo(adopted[0].id) if adopted else []
        conn.close()
        self.assertEqual(len(adopted), 1)
        self.assertEqual(adopted[0].content, "更新云朵智能体", "前缀要去掉")
        self.assertEqual(linked.todo_id, adopted[0].id, "提醒要挂到新待办上")
        self.assertEqual(len(attached), 1, "不许因为它自带时刻就再建第二条闹钟")
        self.assertIn("待办的闹钟", self.client.get("/calendar?tab=reminders").text)

    def test_adopting_an_already_fired_reminder_invents_no_due_date(self) -> None:
        conn, todos, schedules = self._stores()
        loose = schedules.create(
            "下午用云朵生成培训课件", kind="at",
            at=self._due(days=-1).isoformat(timespec="seconds"), receipt=False,
        )
        conn.close()
        self.client.post(f"/calendar/reminders/{loose.id}/adopt",
                         data={"_idem": "tok-adopt-2"}, follow_redirects=False)
        conn, todos, schedules = self._stores()
        adopted = [item for item in todos.list(status="all") if "云朵" in item.content]
        attached = schedules.for_todo(adopted[0].id) if adopted else []
        conn.close()
        self.assertEqual(len(adopted), 1)
        self.assertIsNone(adopted[0].due_at_local,
                          "已经响过的提醒:不知道他做没做,别造一个假的到期时刻")
        self.assertEqual(len(attached), 1, "原来那条还挂在上面")


if __name__ == "__main__":
    unittest.main()

