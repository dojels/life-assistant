"""到点提醒的**唯一认领入口** —— 先送达,后认领。

为什么单独有这个模块(2026-10-12 实测取证):

  * 提醒原先只有"会话启动"这一条触发路径,而它**根本没有出口**:
    被 auto_briefing 门控(Web 与微信桥都传 False,只有 CLI 是 True);就算触发了,
    `_briefing_text()` 也只放 briefing/study —— reminder 被显式过滤掉,
    而且没有任何界面读 `pending_dispatches`。
  * 后果是 19:10:20 那三条提醒被 `fire_reminders()` claim 之后**凭空消失**:
    `schedules.last_fired_at` 被写死、dispatches 插了三行、用户一个字的提醒都没收到。
    更糟的是 at 类型的 claim 只在 `last_fired_at IS NULL` 时成功 ——
    这条提醒再也不会响第二次。

三条纪律:

  1. **先送达,后认领**。认领 = 已通知;没送到就绝不认领,下一轮还会重试。
     这是 D34("抢占式资源只能有一个认领入口")的延伸 —— 而且那个入口必须站在
     送达的**后面**。
  2. **过期不补**。迟到超过 `max_late_s` 的提醒不再发送,直接记成"已错过"。
     理由是 iLink 的窗口由协议决定(24 小时 + 一个 context_token 10 条额度,见
     weixin_ilink.py 开头):窗口关着时提醒会一直攒着,不设上限的话用户晚上说一句话,
     白天攒下的五条会一起砸过来 —— 与 MIN_EVERY_SECONDS 同一个理由,防消息轰炸。
  3. **整个环里没有模型**。什么时候推、推几条,全部来自 schedules 与
     app_reminder_instances 两张表;模型欠费、断网、挂了都不影响推送。

⚠️ 送达语义是 **at-least-once**:发出去之后、认领之前进程被杀,这条会重发一次。
   反过来(先认领再发)在进程被杀时是**静默丢失** —— 两者相比,重发是便宜的那种错。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from .. import config
from ..base.clock import parse_iso
from ..domain.calendars.reminders import ReminderService
from ..domain.services.calendar_service import CalendarService
from ..tasks.dispatcher import KIND_REMINDER, Dispatcher
from ..tasks.todos import STATUS_COMPLETED
from ..tasks.wake import WakeReport, WakeStore

#: 迟到的上限(秒)。超过就不再发,直接记成"已错过"。
DEFAULT_MAX_LATE_S = 3600.0
#: 心跳间隔(秒)。
DEFAULT_INTERVAL_S = 30.0
#: 日历提醒重新物化的最小间隔(秒)。物化是幂等的(INSERT OR IGNORE),
#: 但 14 天窗口的 RRULE 展开没必要每 30 秒算一遍。
MATERIALIZE_EVERY_S = 900.0
#: 一次最多推几条(合并成一条消息)。真正的上限由"迟到窗口"决定,这里只是兜底。
MAX_BATCH = 8

KIND_MISSED = "reminder-missed"
KIND_WAKE = "wake"
SOURCE_SCHEDULE = "schedule"
SOURCE_CALENDAR = "calendar"
SOURCE_WAKE = "wake-report"
STATUS_CANCELLED = "cancelled"


@dataclass
class PushItem:
    """一条"该响了"的提醒。

    `key` 同时是 dispatches 表的主键 —— 同一个提醒实例在重试之间**必须是同一个 key**,
    否则重试会像新的一样再插一行,派发记录就没法当真相用了。
    """

    key: str
    text: str
    due: datetime
    source: str
    schedule_id: str = ""
    instance_id: int = 0
    #: 非空 = 只推给该成员;None = 全部绑定联系人。
    target_member_id: int | None = None
    #: 非空 = 这条不发,直接记成"已错过"。理由写在这里(例如"事项已取消")。
    note: str = ""
    #: 唤醒报告:原文已经是给用户看的整段(带"⏰ 唤醒 · 标题"),不再套提醒前缀。
    raw: bool = False
    report_id: str = ""
    #: 唤醒报告的过期时刻(24/48 小时),与提醒的"迟到 1 小时"分开判。
    expires_at: datetime | None = None


def render_reminders(items: list[PushItem], now: datetime) -> str:
    """把这一批提醒拼成**一条**消息。

    合并而不是一条一发,是因为窗口额度是按条算的(一个 context_token 10 条):
    三条提醒各发一条就吃掉三格,正文一长,用户再说话时助手就哑了。
    """
    if not items:
        return ""
    raw = [item for item in items if item.raw]
    normal = [item for item in items if not item.raw]
    parts: list[str] = []
    if len(normal) == 1:
        item = normal[0]
        late_s = (now - item.due).total_seconds()
        # 迟到不到一分半就别写"原定"了 —— 那只是心跳的抖动,不是迟到。
        # 迟到时要**明说是迟到的**:否则用户看到"提醒:开会(20:00)"却发现已经 19:50,
        # 会以为提醒错了时间,而不是以为它晚了。
        prefix = "⏰ 提醒:" if late_s < 90 else "⏰ 迟到提醒(原定 " + item.due.strftime("%H:%M") + "):"
        parts.append(prefix + item.text)
    elif normal:
        lines = ["⏰ 到点提醒 " + str(len(normal)) + " 条:"]
        for item in normal:
            lines.append("· " + item.due.strftime("%H:%M") + " " + item.text)
        parts.append("\n".join(lines))
    if raw:
        # 报告正文已经是"⏰ 唤醒 · 标题 + 结论"的整段,不再套提醒前缀。
        parts.append("\n\n".join(item.text for item in raw))
    return "\n\n".join(parts)


class ReminderPusher:
    """把"到点了"变成"用户真的看到了"。

    它同时认领**两个来源**:任务提醒(memory.db.schedules)与日历提醒
    (assistant.db.app_reminder_instances)。两个来源在用户眼里是同一件事 ——
    "它答应过要提醒我" —— 所以出口只有一个,免得修好一半。
    """

    def __init__(
        self,
        memory: sqlite3.Connection,
        assistant: sqlite3.Connection,
        *,
        send: Callable[[str], Awaitable[tuple[bool, str]]],
        send_to: Callable[[str, int | None], Awaitable[tuple[bool, str]]] | None = None,
        settings: config.Settings | None = None,
        max_late_s: float = DEFAULT_MAX_LATE_S,
        interval_s: float = DEFAULT_INTERVAL_S,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.memory = memory
        self.assistant = assistant
        self.send = send
        self.send_to = send_to
        self.settings = settings or config.get_settings()
        self.max_late_s = float(max_late_s)
        self.interval_s = max(5.0, float(interval_s))
        self.log = log

        self.dispatcher = Dispatcher(memory)
        # 唤醒报告也走这个出口:runner 只写 pending,这里"先送达、后认领"。
        self.wake = WakeStore(memory, settings=self.settings)
        # 与 assembly 同一口径:时钟/时区从 settings 来,不在桥接层另起一套。
        self.calendar = CalendarService(
            assistant, now_provider=self.settings.now, tzid=self.settings.timezone,
        )
        self.reminders = ReminderService(
            assistant, self.calendar, now_provider=self.settings.now,
        )
        self.ticks = 0
        self._materialized_at: datetime | None = None
        #: 上一轮说过的话。理由不变就不再重复打日志 —— 窗口关着的时候
        #: 每 30 秒一行"没有 context_token"会把日志淹掉,真问题就看不见了。
        self._last_note = ""

    # -- 日志 -------------------------------------------------------------
    def _say(self, text: str) -> None:
        if self.log is not None:
            self.log(text)
        else:
            print("[reminder-push] " + text, flush=True)

    def close(self) -> None:
        for conn in (self.memory, self.assistant):
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - 关闭失败不该让退出流程炸掉
                pass

    # -- 读 ---------------------------------------------------------------
    def materialize(self, now: datetime) -> int:
        """把未来窗口内的日历提醒实例补进表里(幂等)。

        ⚠️ 这件事原先只在 `MemorySession.collect_dispatches()` 里做,而那条路径
        Web/微信桥都不走 —— 于是**日历提醒根本不会被物化**。推送环常驻,交给它。
        """
        if self._materialized_at is not None:
            elapsed = (now - self._materialized_at).total_seconds()
            if 0 <= elapsed < MATERIALIZE_EVERY_S:
                return 0
        self._materialized_at = now
        return self.reminders.materialize(now=now)

    def collect(self, now: datetime) -> tuple[list[PushItem], list[PushItem]]:
        """返回 (该推的, 已错过的)。**只读,一个字节都不写。**"""
        candidates = (
            self._schedule_items(now)
            + self._calendar_items(now)
            + self._wake_items(now)
        )
        candidates.sort(key=lambda item: item.due)
        live: list[PushItem] = []
        stale: list[PushItem] = []
        for item in candidates:
            if item.note:
                stale.append(item)
                continue
            if item.source == SOURCE_WAKE:
                # 报告不是提醒:晚一两天仍然有用,过期按 expires_at 判,不按 1 小时。
                if item.expires_at is not None and now >= item.expires_at:
                    stale.append(replace(item, note="已过期(超过 48 小时),不再推送"))
                    continue
            else:
                late_s = (now - item.due).total_seconds()
                if late_s > self.max_late_s:
                    stale.append(
                        replace(item, note="迟到 " + str(int(late_s // 60)) + " 分钟,不再补发")
                    )
                    continue
            live.append(item)
        # ⚠️ 超出一批的那部分**不是“错过”**,只是这一轮先不发(2026-10-18 审计):
        #    旧实现把它们塞进 stale,tick 会把 stale 全部 mark_missed() → 写
        #    dispatches 并 claim 源行,第 9 条起永远收不到。这里只返回真正该
        #    丢弃的 stale;剩下的留在原地,下一轮(30 秒后)继续发。
        return live[:MAX_BATCH], stale

    def _schedule_items(self, now: datetime) -> list[PushItem]:
        items: list[PushItem] = []
        for schedule in self.dispatcher.schedules.due(now):
            due = schedule.due_at()
            if due is None:
                continue
            due = self._as_local(due)
            text, note = self._schedule_text(schedule)
            items.append(
                PushItem(
                    key=KIND_REMINDER + ":" + schedule.id + ":"
                    + due.isoformat(timespec="seconds"),
                    text=text,
                    due=due,
                    source=SOURCE_SCHEDULE,
                    schedule_id=schedule.id,
                    target_member_id=getattr(schedule, "target_member_id", None),
                    note=note,
                )
            )
        return items

    def _schedule_text(self, schedule: Any) -> tuple[str, str]:
        """这条闹钟该说什么。

        ⭐ 挂在待办上的闹钟必须**说清是哪件事、以及怎么收尾**(D115):用户收到的是
           "该做:更新云朵智能体(记在待办里;回一句「做完了」我就勾掉)" ——
           而不是孤零零一句事由,让他自己去找那条待办。
        """
        todo_id = getattr(schedule, "todo_id", None)
        if not todo_id:
            return schedule.prompt, ""
        try:
            todo = self.dispatcher.todos.get(str(todo_id))
        except Exception:  # noqa: BLE001 - 查不到待办不该让这次提醒消失
            todo = None
        if todo is None:
            # 待办被删了(理论上闹钟会跟着删):退回原文,别让提醒凭空消失
            return schedule.prompt, ""
        if todo.status == STATUS_COMPLETED:
            # 已完成却还留着闹钟:不打扰,记成"已错过"并写明原因(而不是静默丢掉)
            return todo.content, "待办已完成"
        return (
            "该做:" + todo.content + "(记在待办里;回一句「做完了」我就勾掉)",
            "",
        )

    def _calendar_items(self, now: datetime) -> list[PushItem]:
        items: list[PushItem] = []
        for instance in self.reminders.pending(now=now):
            due = instance.fire_at_local(self.settings.timezone)
            event = self.calendar.get(instance.event_uid)
            # ⚠️ 正文里写的是**事项本身的时刻**,不是提醒时刻 —— 提醒提前 30 分钟响,
            #    用户需要知道的是"几点开会",而不是"我几点提醒你"(后者由前缀说)。
            if event is not None:
                start = self._as_local(datetime.fromisoformat(str(event.dtstart)))
                text = event.title + "(" + start.strftime("%m-%d %H:%M") + ")"
            else:
                text = instance.event_uid
            note = ""
            if event is not None and str(event.status) == STATUS_CANCELLED:
                # 取消掉的事不该再提醒 —— 但实例已经物化在表里了,不处理会一轮轮地捡回来
                note = "事项已取消"
            items.append(
                PushItem(
                    key=KIND_REMINDER + ":" + SOURCE_CALENDAR + ":" + str(instance.id),
                    text=text,
                    due=due,
                    source=SOURCE_CALENDAR,
                    instance_id=instance.id,
                    target_member_id=(
                        getattr(event, "reminder_target_member_id", None)
                        or getattr(event, "owner_id", None)
                    ),
                    note=note,
                )
            )
        return items

    def _wake_items(self, now: datetime) -> list[PushItem]:
        """待送达的唤醒报告。⚠️ 与提醒同一个出口,但**过期按 48 小时**判。"""
        items: list[PushItem] = []
        for report in self.wake.pending_reports(now=now):
            created = parse_iso(report.created_at) or now
            items.append(
                PushItem(
                    key=KIND_WAKE + ":" + report.id,
                    text=report.text,
                    due=self._as_local(created),
                    source=SOURCE_WAKE,
                    target_member_id=report.owner_member_id,
                    raw=True,
                    report_id=report.id,
                    expires_at=parse_iso(report.expires_at),
                )
            )
        return items

    def _as_local(self, moment: datetime) -> datetime:
        if moment.tzinfo is None:
            return moment.replace(tzinfo=self.settings.tz)
        return moment.astimezone(self.settings.tz)

    # -- 写 ---------------------------------------------------------------
    def _claim_source(self, item: PushItem, now: datetime, *, sent: bool = True) -> bool:
        """抢占源行 —— 这才是防重发的真闸(dispatches 只是日志)。"""
        if item.source == SOURCE_SCHEDULE:
            return self.dispatcher.schedules.claim(item.schedule_id, now)
        if item.source == SOURCE_CALENDAR:
            return self.reminders.claim(item.instance_id, now=now)
        if item.source == SOURCE_WAKE:
            if not item.report_id:
                return False
            # 送达 -> sent;过期/放弃 -> expired。**只有真的发出去了才算 sent**。
            return (
                self.wake.mark_report_sent(item.report_id)
                if sent else self.wake.mark_report_expired(item.report_id)
            )
        return False

    def claim(self, item: PushItem, now: datetime, text: str) -> None:
        """**送达成功之后**才调它。"""
        kind = KIND_WAKE if item.source == SOURCE_WAKE else KIND_REMINDER
        self.dispatcher.claim(item.key, kind, text, now=now)
        self._claim_source(item, now, sent=True)

    def mark_missed(self, item: PushItem, now: datetime) -> None:
        """没送出去的那些也要认领 —— 否则每一轮都会重新捡起来,永远在队列里。"""
        reason = item.note or "已错过"
        self.dispatcher.claim(
            KIND_MISSED + ":" + item.key, KIND_MISSED, item.text + " —— " + reason, now=now
        )
        self._claim_source(item, now, sent=False)

    # -- 一轮 -------------------------------------------------------------
    async def tick(self, now: datetime | None = None) -> dict[str, Any]:
        moment = now or self.settings.now()
        report: dict[str, Any] = {
            "at": moment.isoformat(timespec="seconds"),
            "sent": [],
            "missed": [],
            "note": "",
        }
        try:
            self.materialize(moment)
        except Exception as exc:  # noqa: BLE001 - 物化失败不该挡住已经到点的那些
            self._say("物化失败:" + type(exc).__name__ + ": " + str(exc)[:160])

        # 过期报告:不再发,但要标成 expired —— 否则它会一直躺在 pending 里,
        # 页面上看起来"还在排队",实际上永远不会发出。
        for report in self.wake.expired_reports(now=moment):
            self.wake.mark_report_expired(report.id)
        live, stale = self.collect(moment)
        for item in stale:
            self.mark_missed(item, moment)
            report["missed"].append(item.key)
        if not live:
            return report

        # 按收件人分组:一条消息里混着"只给妈妈"和"只给爸爸"会串台。
        groups: dict[int | None, list[PushItem]] = {}
        for item in live:
            groups.setdefault(item.target_member_id, []).append(item)
        notes: list[str] = []
        for target, items in groups.items():
            text = render_reminders(items, moment)
            if target is not None and self.send_to is None:
                # 没有"按成员发送"的通道时**不要广播** —— 一条只该给妈妈的报告
                # 发给全家,比晚一点送到糟得多。
                notes.append("没有按成员发送的通道,这一条暂不发送")
                continue
            if target is not None and self.send_to is not None:
                ok, reason = await self.send_to(text, target)
            else:
                # 老测试/CLI 没有 send_to 时退回广播;生产由 weixin_ilink_main 注入 send_to。
                ok, reason = await self.send(text)
            notes.append(str(reason or "")[:120])
            if not ok:
                # ⭐ 没送到就绝不认领 —— 下一轮(默认 30 秒后)还会重试
                continue
            for item in items:
                self.claim(item, moment, text)
                report["sent"].append(item.key)
        report["note"] = "; ".join(note for note in notes if note)[:200]
        return report

    async def run_forever(self, *, max_ticks: int | None = None) -> None:
        """常驻:每 interval_s 秒看一次。异常只记日志,绝不把任务带走。"""
        while True:
            try:
                report = await self.tick()
                self.ticks += 1
                note = ""
                if report["sent"]:
                    note = "已推送 " + str(len(report["sent"])) + " 条"
                elif report["missed"]:
                    note = "错过 " + str(len(report["missed"])) + " 条"
                elif report["note"]:
                    note = str(report["note"])
                if note and note != self._last_note:
                    self._say(note + "  " + json.dumps(report, ensure_ascii=False)[:300])
                    self._last_note = note
                elif not note:
                    self._last_note = ""
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 后台任务静默死掉是最难查的故障
                self._say("轮次异常:" + type(exc).__name__ + ": " + str(exc)[:200])
            if max_ticks is not None and self.ticks >= max_ticks:
                return
            await asyncio.sleep(self.interval_s)
