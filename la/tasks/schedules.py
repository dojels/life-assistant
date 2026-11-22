"""定时提醒:会话内调度 + 抢占式派发。

⚠️ 两条硬约束(设计深化 §3.7 / 能力域 §5.2):
  * `every` 的最小间隔是 **300 秒** —— 写在数据库 CHECK 里,不只是 Python 校验;
  * 派发用**条件更新抢占**(`UPDATE ... WHERE last_fired_at IS NULL` 并检查受影响行数),
    这样进程重启不会重复提醒。
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .. import config
from ..base.clock import parse_iso as _clock_parse_iso
from ..base.clock import utc_now_iso as _now

KIND_AFTER = "after"
KIND_AT = "at"
KIND_EVERY = "every"
KINDS = (KIND_AFTER, KIND_AT, KIND_EVERY)

MIN_EVERY_SECONDS = 300


class ScheduleError(RuntimeError):
    """提醒操作的领域错误。"""




def parse_iso(text: str | None) -> datetime | None:
    return _clock_parse_iso(text)


def _normalize_at(text: str | None) -> str | None:
    """把 at 收敛成**带时区**的 ISO 串(2026-10-18 审计)。

    ⚠️ 模型经常只写 `2026-10-18T20:00:00`(schema 的 description 才提时区),
       而 `is_due()` 拿它和 aware 的 now 比较会抛 TypeError。一条这样的旧数据
       能让整个提醒推送环每一轮都失败。这里按**配置时区**补上 tzinfo;
       旧库里已有的 naive 行由 `is_due()` 兜底。
    """
    moment = parse_iso(text)
    if moment is None:
        return text
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=config.get_settings().tz)
    return moment.isoformat(timespec="seconds")


@dataclass
class Schedule:
    id: str
    kind: str
    at: str | None
    every_seconds: int | None
    prompt: str
    enabled: bool
    last_fired_at: str | None
    created_at: str
    updated_at: str
    #: 这条提醒**属于哪条待办**(空 = 独立提醒,例如"吃饭""喝水")。
    #: ⭐ 它就是"提醒是待办的闹钟"这句话在库里的落点(D115):用户说"明天中午更新云朵",
    #:    记的是一条待办,闹钟只是挂在它身上的一次派发 —— 勾掉待办,闹钟跟着关。
    todo_id: str | None = None
    #: 提醒推给谁:NULL/0 = 全部绑定联系人;数字 = 只推给该成员。
    target_member_id: int | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Schedule":
        return cls(
            id=row["id"],
            kind=row["kind"],
            at=row["at"],
            every_seconds=row["every_seconds"],
            prompt=row["prompt"],
            enabled=bool(row["enabled"]),
            last_fired_at=row["last_fired_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            todo_id=row["todo_id"],
            target_member_id=(
                int(row["target_member_id"])
                if row["target_member_id"] is not None else None
            ),
        )

    def due_at(self) -> datetime | None:
        """这条提醒**该响的时刻**;永远不该响返回 None。

        ⚠️ 单独抽出来是因为"迟到了多久"要拿它减 now —— 而迟到的上限决定了
        这条提醒**还发不发**(见 la/bridge/reminder_push.py)。两处各写一份算式,
        迟到的判据迟早会和触发的判据分叉,那时"该响的没响、不该响的响了"
        就会同时出现。is_due 因此也改成走它。
        """
        if self.kind == KIND_AT:
            return parse_iso(self.at)
        if self.kind == KIND_AFTER:
            created = parse_iso(self.created_at)
            if created is None:
                return None
            return created + timedelta(seconds=self.every_seconds or 0)
        if self.kind == KIND_EVERY:
            if self.every_seconds is None:
                return None
            fired = parse_iso(self.last_fired_at)
            if fired is None:
                # at 是"第一次该响的时刻"(例如"每天 9 点提醒我" -> 下一个 9 点)
                return parse_iso(self.at) or parse_iso(self.created_at)
            return fired + timedelta(seconds=self.every_seconds)
        return None

    def is_due(self, now: datetime) -> bool:
        moment = self.due_at()
        if moment is None:
            return False
        # 旧数据/手写库里的 naive at:按比较对象的时区补全,不让一条坏数据把
        # 整个 due()/推送环打死(2026-10-18 审计)。
        if moment.tzinfo is None and now.tzinfo is not None:
            moment = moment.replace(tzinfo=now.tzinfo)
        elif now.tzinfo is None and moment.tzinfo is not None:
            now = now.replace(tzinfo=moment.tzinfo)
        if now < moment:
            return False
        if self.kind == KIND_EVERY:
            return True          # 重复提醒响过还会再响
        # at / after 都是一次性的:响过就不再响
        return parse_iso(self.last_fired_at) is None


class ScheduleStore:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        emit: Callable[[str, dict], None] | None = None,
        receipts: Any = None,
        member_id: int | None = None,
    ):
        self.conn = conn
        self._emit = emit
        #: 当前成员。None = CLI/后台/未启用成员,全看。
        self.member_id = member_id
        # 统一写入回执(M7),与提醒行同一个事务
        self.receipts = receipts

    def _publish(self, event: str, schedule: Schedule) -> None:
        if self._emit is not None:
            self._emit(
                "schedule/change",
                {
                    "event": event,
                    "schedule": {
                        "id": schedule.id,
                        "kind": schedule.kind,
                        "at": schedule.at,
                        "every_seconds": schedule.every_seconds,
                        "prompt": schedule.prompt,
                        "enabled": schedule.enabled,
                        "target_member_id": schedule.target_member_id,
                    },
                },
            )

    # -- 读 ---------------------------------------------------------------
    def _owner_clause(self) -> tuple[str, list[Any]]:
        """本成员可见范围:全家(NULL)+ 只发给自己的。"""
        member_id = getattr(self, "member_id", None)
        if member_id is None:
            return "1=1", []
        return "(target_member_id IS NULL OR target_member_id = ?)", [int(member_id)]

    def get(self, schedule_id: str) -> Schedule | None:
        clause, params = self._owner_clause()
        row = self.conn.execute(
            "SELECT * FROM schedules WHERE id = ? AND " + clause,
            (schedule_id, *params),
        ).fetchone()
        return Schedule.from_row(row) if row else None

    def list(self, *, enabled_only: bool = True, limit: int = 50) -> list[Schedule]:
        clause, params = self._owner_clause()
        sql = "SELECT * FROM schedules WHERE " + clause
        if enabled_only:
            sql += " AND enabled = 1"
        sql += " ORDER BY created_at LIMIT ?"
        return [Schedule.from_row(row)
                for row in self.conn.execute(sql, (*params, limit)).fetchall()]

    def for_todo(self, todo_id: str) -> list[Schedule]:
        """这条待办身上的全部闹钟(正常情况下最多一条,但不假设只有一条)。"""
        rows = self.conn.execute(
            "SELECT * FROM schedules WHERE todo_id = ? ORDER BY created_at", (str(todo_id),)
        ).fetchall()
        return [Schedule.from_row(row) for row in rows]

    def due(self, now: datetime, *, limit: int = 500) -> list[Schedule]:
        """到点的提醒。

        ⚠️ 不要复用 `list()` 的默认 50 条(2026-10-18 审计):启用中的提醒
        超过 50 时,第 51 条起永远不会被看到,而且没有任何提示。
        """
        return [item for item in self.list(limit=max(int(limit), 1)) if item.is_due(now)]

    # -- 写 ---------------------------------------------------------------
    def create(
        self,
        prompt: str,
        *,
        kind: str = KIND_EVERY,
        at: str | None = None,
        every_seconds: int | None = None,
        todo_id: str | None = None,
        target_member_id: int | None = None,
        receipt: bool = True,
        actor: str = "agent",
    ) -> Schedule:
        text = (prompt or "").strip()
        if not text:
            raise ScheduleError("提醒内容不能为空")
        if kind not in KINDS:
            raise ScheduleError("未知提醒类型: " + str(kind))
        if kind == KIND_EVERY:
            if every_seconds is None:
                raise ScheduleError("every 类型必须给出 every_seconds")
            if at is not None and parse_iso(at) is None:
                raise ScheduleError("at 必须是合法的 ISO8601 时间")
            if every_seconds < MIN_EVERY_SECONDS:
                raise ScheduleError(
                    "间隔不得小于 " + str(MIN_EVERY_SECONDS) + " 秒(避免变成消息轰炸)"
                )
        if kind == KIND_AT and parse_iso(at) is None:
            raise ScheduleError("at 类型必须给出合法的 ISO8601 时间")
        if kind == KIND_AFTER and not every_seconds:
            raise ScheduleError("after 类型必须给出秒数")
        at = _normalize_at(at)

        stamp = _now()
        schedule_id = "s-" + uuid.uuid4().hex[:10]
        target = int(target_member_id) if target_member_id is not None else None
        self.conn.execute(
            "INSERT INTO schedules(id, kind, at, every_seconds, prompt, enabled, created_at,"
            " updated_at, todo_id, target_member_id) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
            (schedule_id, kind, at, every_seconds, text, stamp, stamp,
             (str(todo_id).strip() or None) if todo_id else None, target),
        )
        if receipt and self.receipts is not None:
            when = at or ("每 " + str(every_seconds) + " 秒")
            self.receipts.record(
                kind="task.schedule",
                domain="task",
                label="定一个提醒 " + when + ":" + text[:28],
                detail={"id": schedule_id, "kind": kind, "at": at,
                        "every_seconds": every_seconds, "prompt": text, "op": "create",
                        "target_member_id": target},
                undo_plan={"type": "schedule", "schedule_id": schedule_id},
                actor=actor,
            )
        self.conn.commit()
        schedule = self.get(schedule_id)
        assert schedule is not None
        self._publish("created", schedule)
        return schedule

    def update(
        self,
        schedule_id: str,
        *,
        prompt: str | None = None,
        at: str | None = None,
        every_seconds: int | None = None,
        reset_fired: bool = True,
        receipt: bool = True,
    ) -> Schedule:
        """改一条已存在的提醒(**不新建**)。

        ⚠️ 语义只做三件事:改内容 / 改时刻 / 改间隔。
          * kind **不许改** —— "把一次性的改成每天的"是一次新的约定,
            让模型删了重定,比在旧行上悄悄换语义安全(它也说不清 what changed)。
          * 重排之后把 `last_fired_at` 清空 = "新的时刻还没响过"。
            否则用户把 19:00 挪到 20:00,而 19:00 已经响过一次,
            `is_due` 会因为 fired 非空而不再触发 —— 表现为"改了时间但没提醒"。
          * `at` 只对 at / every 有意义(after 是"建好之后 N 秒",没有绝对时刻)。
        """
        schedule = self.get(schedule_id)
        if schedule is None:
            raise ScheduleError("提醒不存在: " + schedule_id)

        new_prompt = schedule.prompt if prompt is None else (prompt or "").strip()
        if not new_prompt:
            raise ScheduleError("提醒内容不能为空")
        new_at = _normalize_at(schedule.at if at is None else at)
        new_every = schedule.every_seconds if every_seconds is None else int(every_seconds)

        if schedule.kind in (KIND_AT, KIND_EVERY) and new_at is not None:
            if parse_iso(new_at) is None:
                raise ScheduleError("at 必须是合法的 ISO8601 时间")
        if schedule.kind == KIND_AT and parse_iso(new_at) is None:
            raise ScheduleError("at 类型必须给出合法的 ISO8601 时间")
        if schedule.kind == KIND_EVERY:
            if new_every is None or new_every < MIN_EVERY_SECONDS:
                raise ScheduleError(
                    "间隔不得小于 " + str(MIN_EVERY_SECONDS) + " 秒(避免变成消息轰炸)"
                )
        if schedule.kind == KIND_AFTER and not new_every:
            raise ScheduleError("after 类型必须给出秒数")

        stamp = _now()
        self.conn.execute(
            "UPDATE schedules SET prompt = ?, at = ?, every_seconds = ?, last_fired_at = ?,"
            " updated_at = ? WHERE id = ?",
            (
                new_prompt,
                new_at,
                new_every,
                None if reset_fired else schedule.last_fired_at,
                stamp,
                schedule_id,
            ),
        )
        if receipt and self.receipts is not None:
            self.receipts.record(
                kind="task.schedule",
                domain="task",
                label="改提醒 " + (new_at or ("每 " + str(new_every) + " 秒")) + ":" + new_prompt[:24],
                detail={"id": schedule_id, "op": "update",
                        "from": {"at": schedule.at, "every_seconds": schedule.every_seconds,
                                 "prompt": schedule.prompt},
                        "to": {"at": new_at, "every_seconds": new_every, "prompt": new_prompt}},
                undo_plan={"type": "schedule", "op": "update", "schedule_id": schedule_id,
                           "before": {"at": schedule.at,
                                      "every_seconds": schedule.every_seconds,
                                      "prompt": schedule.prompt,
                                      "last_fired_at": schedule.last_fired_at}},
                actor="agent",
            )
        self.conn.commit()
        updated = self.get(schedule_id)
        assert updated is not None
        self._publish("updated", updated)
        return updated

    def set_enabled(
        self, schedule_id: str, enabled: bool, *, receipt: bool = True, actor: str = "agent"
    ) -> bool:
        """开关一条提醒(不删行)。

        ⭐ 待办勾掉时走这里:闹钟**关掉而不是删掉** —— 撤销那次勾选要把原样放回来,
           而"原样"里包含"它响过没有"。删掉再建会把 last_fired_at 弄丢。
        """
        schedule = self.get(schedule_id)
        if schedule is None:
            return False
        if schedule.enabled == bool(enabled):
            return True
        self.conn.execute(
            "UPDATE schedules SET enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, _now(), schedule_id),
        )
        if receipt and self.receipts is not None:
            self.receipts.record(
                kind="task.schedule",
                domain="task",
                label=("开启提醒 " if enabled else "关闭提醒 ") + schedule.prompt[:36],
                detail={"id": schedule_id, "op": "enabled", "to": bool(enabled),
                        "prompt": schedule.prompt},
                undo_plan={"type": "schedule", "op": "enabled", "schedule_id": schedule_id,
                           "before": {"enabled": schedule.enabled}},
                actor=actor,
            )
        self.conn.commit()
        updated = self.get(schedule_id)
        if updated is not None:
            self._publish("enabled" if enabled else "disabled", updated)
        return True

    def attach_todo(
        self, schedule_id: str, todo_id: str, *, receipt: bool = True, actor: str = "agent"
    ) -> bool:
        """把一条**已经存在**的提醒挂到待办上(D115 的"补记"路径)。

        ⚠️ 只改归属,不动时刻与内容 —— 用户点的是"这本来就是那件事",不是"重定一次提醒"
           (重定会换 id,他手里的上下文就断了)。todo_id 传空串 = 恢复成独立提醒(撤销走这里)。
        """
        schedule = self.get(schedule_id)
        if schedule is None:
            return False
        before = schedule.todo_id
        value = str(todo_id).strip() or None
        self.conn.execute(
            "UPDATE schedules SET todo_id = ?, updated_at = ? WHERE id = ?",
            (value, _now(), schedule_id),
        )
        if receipt and self.receipts is not None:
            self.receipts.record(
                kind="task.schedule",
                domain="task",
                label="把提醒挂到待办上 " + schedule.prompt[:32],
                detail={"id": schedule_id, "op": "attach", "todo_id": value, "from": before},
                undo_plan={"type": "schedule", "op": "attach", "schedule_id": schedule_id,
                           "before": {"todo_id": before}},
                actor=actor,
            )
        self.conn.commit()
        updated = self.get(schedule_id)
        if updated is not None:
            self._publish("attached" if value else "detached", updated)
        return True

    def restore(self, schedule: Schedule) -> Schedule:
        """把一条被删掉的提醒**原样放回来**(撤销"删除"用)。

        ⚠️ 与 TodoStore.restore 同一个道理:只有撤销路径该调它。
        """
        existing = self.get(schedule.id)
        if existing is not None:
            return existing
        self.conn.execute(
            "INSERT INTO schedules(id, kind, at, every_seconds, prompt, enabled, last_fired_at,"
            " created_at, updated_at, todo_id, target_member_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (schedule.id, schedule.kind, schedule.at, schedule.every_seconds, schedule.prompt,
             1 if schedule.enabled else 0, schedule.last_fired_at, schedule.created_at,
             schedule.updated_at, schedule.todo_id, schedule.target_member_id),
        )
        self.conn.commit()
        restored = self.get(schedule.id)
        assert restored is not None
        self._publish("restored", restored)
        return restored

    def delete(self, schedule_id: str, *, record_receipt: bool = False,
               receipt: bool = True, actor: str = "agent") -> bool:
        schedule = self.get(schedule_id)
        if schedule is None:
            return False
        self.conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
        if record_receipt and receipt and self.receipts is not None:
            # 撤销"删除"= 原样插回去,所以 before 里带上整行(含 last_fired_at)。
            self.receipts.record(
                kind="task.schedule",
                domain="task",
                label="删掉提醒 " + schedule.prompt[:36],
                detail={"id": schedule_id, "op": "delete", "kind": schedule.kind,
                        "at": schedule.at, "every_seconds": schedule.every_seconds},
                undo_plan={"type": "schedule", "op": "delete", "schedule_id": schedule_id,
                           "before": {"kind": schedule.kind, "at": schedule.at,
                                      "every_seconds": schedule.every_seconds,
                                      "prompt": schedule.prompt,
                                      "enabled": schedule.enabled,
                                      "todo_id": schedule.todo_id,
                                      "target_member_id": schedule.target_member_id,
                                      "last_fired_at": schedule.last_fired_at,
                                      "created_at": schedule.created_at,
                                      "updated_at": schedule.updated_at}},
                actor=actor,
            )
        self.conn.commit()
        self._publish("deleted", schedule)
        return True

    def claim(self, schedule_id: str, fired_at: datetime) -> bool:
        """抢占式派发:只有真正抢到的那一次才返回 True。

        ⚠️ 这里刻意只在 `last_fired_at IS NULL` 时更新 —— 对 every 类型不合适,
        所以 every 用"期望旧值"做 CAS:两个进程同时到点,只有一个能改成功。
        """
        schedule = self.get(schedule_id)
        if schedule is None:
            return False
        stamp = fired_at.isoformat(timespec="seconds")
        if schedule.kind == KIND_EVERY:
            # 重复提醒:用"读到的旧值"做 CAS —— 两个进程同时到点,只有一个改得成功
            expected = schedule.last_fired_at
            cursor = self.conn.execute(
                "UPDATE schedules SET last_fired_at = ?, updated_at = ?"
                " WHERE id = ? AND (last_fired_at IS ? OR last_fired_at = ?)",
                (stamp, _now(), schedule_id, expected, expected),
            )
        else:
            # 一次性提醒(at / after):只有从未触发过才抢得到
            cursor = self.conn.execute(
                "UPDATE schedules SET last_fired_at = ?, updated_at = ?"
                " WHERE id = ? AND last_fired_at IS NULL",
                (stamp, _now(), schedule_id),
            )
        self.conn.commit()
        return cursor.rowcount == 1
