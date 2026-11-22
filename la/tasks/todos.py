"""待办清单:事件溯源 + 到期摘要 + **挂在自己身上的闹钟**。

source 记录来源(user / agent / dispatcher),这样"这条待办是它自己加的"永远查得出来。

⭐ 待办与提醒的关系(D115,用户拍板):**待办是"事",提醒是"事上的闹钟"**。
   * 有产出、能勾掉的事 → 待办;它有明确时刻(due_at_local)就**自动带一次到点提醒**;
   * 只是"到点说一声"(吃饭、喝水)→ 独立提醒,不产生待办;
   * 勾掉待办 → 闹钟**关掉**(不是删掉,撤销勾选要能原样开回来);
     删掉待办 → 闹钟跟着删(撤销时连它一起放回来)。

⚠️ 闹钟这一行仍然住在 schedules 表里、由 la/bridge/reminder_push.py 派发 ——
   抢占式认领、迟到窗口、合并发送、微信额度**一行都没改**,这里只是记住"它属于谁"。
"""

from __future__ import annotations
from ..base.clock import utc_now_iso as _now

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .schedules import KIND_AT, Schedule, ScheduleStore, parse_iso

#: 工具层用它表达"这次不要提醒"(库里存 NULL:提醒分钟数为空 = 不提醒)
REMIND_NONE = -1

STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"
STATUSES = (STATUS_PENDING, STATUS_IN_PROGRESS, STATUS_COMPLETED)

SOURCE_USER = "user"
SOURCE_AGENT = "agent"
SOURCE_DISPATCHER = "dispatcher"




@dataclass
class Todo:
    id: str
    content: str
    status: str
    source: str
    due_date_local: str | None
    created_at: str
    updated_at: str
    #: 到期的**时刻**(ISO8601 带时区)。空 = 只有日期或没有日期。
    #: ⭐ 以前只有 due_date_local,"明天中午更新云朵"在待办里**无处安放** ——
    #:    于是那句话只能落到提醒表里,工作本身丢了(D115 的起因)。
    due_at_local: str | None = None
    #: 提前多少分钟提醒:空 = 不提醒;0 = 到点;N = 提前 N 分钟。
    remind_minutes_before: int | None = None
    #: 待办归属:它身上闹钟只提醒这个人。
    owner_id: int | None = None

    @property
    def is_open(self) -> bool:
        return self.status != STATUS_COMPLETED

    @property
    def wants_alarm(self) -> bool:
        return bool(self.due_at_local) and self.remind_minutes_before is not None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Todo":
        return cls(
            id=row["id"],
            content=row["content"],
            status=row["status"],
            source=row["source"],
            due_date_local=row["due_date_local"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            due_at_local=row["due_at_local"],
            remind_minutes_before=row["remind_minutes_before"],
            owner_id=(int(row["owner_id"]) if row["owner_id"] is not None else None),
        )


def _parse_due_at(text: str | None) -> datetime | None:
    """待办的"到期时刻"-> datetime;裸时刻按**本地时区**解释(与提醒同一口径)。"""
    moment = parse_iso(text)
    if moment is None:
        return None
    if moment.tzinfo is None:
        from ..config import get_settings

        return moment.replace(tzinfo=get_settings().tz)
    return moment


def alarm_at(due_at_local: str | None, remind_minutes_before: int | None) -> str | None:
    """待办的**闹钟时刻** = 到期时刻 − 提前量。给不出就 None(那就不建闹钟)。

    口径只有这一处:建待办、改到期时间、界面预览"几点响"走的都是它。
    """
    if remind_minutes_before is None or int(remind_minutes_before) < 0:
        return None
    moment = _parse_due_at(due_at_local)
    if moment is None:
        return None
    return (moment - timedelta(minutes=int(remind_minutes_before))).isoformat(timespec="seconds")


class TodoStore:
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
        self.member_id = member_id
        # 统一写入回执(M7)。待办在记忆库里,所以它的回执也写在这个连接上 ——
        # 与待办行同一个事务。类型用 Any:不 import,免得记忆层反向依赖领域层。
        self.receipts = receipts

    # -- 闹钟(D115:提醒是待办的属性,不是另起一件事) ---------------------
    def _alarms(self) -> ScheduleStore:
        """同一个连接上的提醒簿。

        ⚠️ 两条纪律:
          ① **不传回执**:闹钟是待办的一部分 —— "记待办"那一条回执要能一次撤销两行;
          ② emit 照传:提醒行的变更同样要进事件日志(否则回放时闹钟凭空出现)。
        """
        return ScheduleStore(self.conn, emit=self._emit)

    @staticmethod
    def _normalize_remind(
        remind_minutes_before: int | None, due_at_local: str | None
    ) -> int | None:
        """提前量收敛成库里那一列的语义:NULL=不提醒,0=到点,N=提前 N 分钟。

        * 没有时刻 → 没有闹钟(NULL);
        * 给了时刻但没说提前量 → **到点提醒一次**(用户的原话:"这个工作在开始前提醒我一下");
        * 显式给负数(工具层的 -1)→ 这次不要提醒。
        """
        if not due_at_local:
            return None
        if remind_minutes_before is None:
            return 0
        value = int(remind_minutes_before)
        return None if value < 0 else value

    @staticmethod
    def _when_text(due_date_local: str | None, due_at_local: str | None) -> str:
        """回执文案里的"什么时候":有时刻就写时刻,只有日期就写日期。"""
        if due_at_local:
            return "(" + str(due_at_local)[:16].replace("T", " ") + ")"
        if due_date_local:
            return "(" + str(due_date_local) + " 到期)"
        return ""

    @staticmethod
    def _date_of(due_at_local: str | None) -> str | None:
        """时刻自带日期 —— 只给"明天 12:00"不给日期时,把日期补出来(分桶/简报都靠它)。"""
        moment = parse_iso(due_at_local)
        return moment.date().isoformat() if moment is not None else None

    def _create_alarm(
        self, todo_id: str, text: str, due_at_local: str | None, remind: int | None
    ) -> str:
        """按"到期时刻 − 提前量"挂一条闹钟,返回它的 id(没挂就是空串)。"""
        moment = alarm_at(due_at_local, remind)
        if moment is None:
            return ""
        schedule = self._alarms().create(
            "该做:" + text, kind=KIND_AT, at=moment, todo_id=todo_id,
            target_member_id=getattr(self, "member_id", None), receipt=False,
        )
        return schedule.id

    def _drop_alarms(self, todo_id: str) -> list[dict[str, Any]]:
        """摘掉这条待办身上的闹钟,并把整行**原样**交回去(撤销要靠它放回来)。"""
        store = self._alarms()
        rows: list[dict[str, Any]] = []
        for schedule in store.for_todo(todo_id):
            rows.append({
                "id": schedule.id, "kind": schedule.kind, "at": schedule.at,
                "every_seconds": schedule.every_seconds, "prompt": schedule.prompt,
                "enabled": schedule.enabled, "last_fired_at": schedule.last_fired_at,
                "created_at": schedule.created_at, "updated_at": schedule.updated_at,
                "todo_id": schedule.todo_id,
                "target_member_id": schedule.target_member_id,
            })
            store.delete(schedule.id, receipt=False)
        return rows

    def _sync_alarms(self, todo_id: str, *, closing: bool) -> list[str]:
        """待办状态一变,它的闹钟跟着变:勾掉 = 关掉;放回未完成 = 还没到的重新开起来。

        ⚠️ 只重开**还没响过、且时刻还在将来**的那条:到点了再开起来,下一轮心跳就会
           立刻补一响 —— 那是噪音,不是提醒。关掉而不是删掉,是为了撤销时能原样回来
           (里面含"它响过没有")。
        """
        store = self._alarms()
        changed: list[str] = []
        now = datetime.now(timezone.utc)
        for schedule in store.for_todo(todo_id):
            if closing:
                if schedule.enabled and store.set_enabled(schedule.id, False):
                    changed.append(schedule.id)
                continue
            moment = schedule.due_at()
            if moment is not None and moment.tzinfo is None:
                moment = moment.replace(tzinfo=now.tzinfo)
            if (
                not schedule.enabled
                and schedule.last_fired_at is None
                and moment is not None
                and moment > now
                and store.set_enabled(schedule.id, True)
            ):
                changed.append(schedule.id)
        return changed

    def _publish(self, event: str, todo: Todo) -> None:
        if self._emit is not None:
            self._emit(
                "todo/change",
                {
                    "event": event,
                    "todo": {
                        "id": todo.id,
                        "content": todo.content,
                        "status": todo.status,
                        "source": todo.source,
                        "due_date_local": todo.due_date_local,
                    },
                },
            )

    # -- 读 ---------------------------------------------------------------
    def _owner_clause(self) -> tuple[str, list[Any]]:
        """本成员可见范围:全家(NULL)+ 自己的。member_id 为空 = 管理员/CLI,全看。"""
        member_id = getattr(self, "member_id", None)
        if member_id is None:
            return "1=1", []
        return "(owner_id IS NULL OR owner_id = ?)", [int(member_id)]

    def get(self, todo_id: str) -> Todo | None:
        row = self.conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
        return Todo.from_row(row) if row else None

    def list(self, *, status: str | None = None, limit: int = 50) -> list[Todo]:
        if status == "all":
            status = None
        clause, owner_params = self._owner_clause()
        sql = "SELECT * FROM todos WHERE " + clause
        params: list[Any] = list(owner_params)
        if status == STATUS_PENDING:
            sql += " AND status IN ('pending', 'in_progress')"
        elif status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY COALESCE(due_date_local, '9999'), created_at LIMIT ?"
        params.append(limit)
        return [Todo.from_row(row) for row in self.conn.execute(sql, params).fetchall()]

    def counts(self) -> dict[str, int]:
        """各状态条数(一次 GROUP BY)。给"我现在看得到什么"用 —— 别在 Python 里数。"""
        clause, params = self._owner_clause()
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM todos WHERE " + clause
            + " GROUP BY status", params,
        ).fetchall()
        counts = {status: 0 for status in STATUSES}
        for row in rows:
            counts[str(row["status"])] = int(row["n"])
        counts["all"] = sum(counts.values())
        counts["open"] = counts[STATUS_PENDING] + counts[STATUS_IN_PROGRESS]
        return counts

    def due(self, today_local: str) -> list[Todo]:
        """今天到期或已经逾期的未完成待办。"""
        clause, params = self._owner_clause()
        rows = self.conn.execute(
            "SELECT * FROM todos WHERE " + clause
            + " AND status IN ('pending', 'in_progress')"
            " AND due_date_local IS NOT NULL AND due_date_local <= ?"
            " ORDER BY due_date_local",
            (*params, today_local),
        ).fetchall()
        return [Todo.from_row(row) for row in rows]

    # -- 写 ---------------------------------------------------------------
    def add(
        self,
        content: str,
        *,
        source: str = SOURCE_USER,
        due_date_local: str | None = None,
        due_at_local: str | None = None,
        remind_minutes_before: int | None = None,
        actor: str = "agent",
    ) -> Todo:
        """记一条待办。**给了时刻就自带一条闹钟**(见模块头)。

        ⚠️ 一条回执管两行:待办 + 它的闹钟在同一个事务里落下,撤销时一起撤。
        """
        text = (content or "").strip()
        if not text:
            raise ValueError("待办内容不能为空")
        at_value = str(due_at_local or "").strip() or None
        date_value = str(due_date_local or "").strip() or self._date_of(at_value)
        remind = self._normalize_remind(remind_minutes_before, at_value)
        stamp = _now()
        todo_id = "t-" + uuid.uuid4().hex[:10]
        self.conn.execute(
            "INSERT INTO todos(id, content, status, source, due_date_local, created_at,"
            " updated_at, due_at_local, remind_minutes_before, owner_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (todo_id, text, STATUS_PENDING, source, date_value, stamp, stamp, at_value, remind,
             getattr(self, "member_id", None)),
        )
        alarm_id = self._create_alarm(todo_id, text, at_value, remind)
        if self.receipts is not None:
            self.receipts.record(
                kind="task.todo",
                domain="task",
                label="记待办 " + text[:36] + self._when_text(date_value, at_value)
                + ("(到点提醒)" if alarm_id else ""),
                detail={"id": todo_id, "content": text, "due_date_local": date_value,
                        "due_at_local": at_value, "remind_minutes_before": remind,
                        "alarm_id": alarm_id, "op": "create"},
                undo_plan={"type": "todo", "op": "create", "id": todo_id,
                           "alarm_id": alarm_id},
                actor=actor,
            )
        self.conn.commit()
        todo = self.get(todo_id)
        assert todo is not None
        self._publish("created", todo)
        return todo

    def set_status(
        self, todo_id: str, status: str, *, receipt: bool = True, actor: str = "agent"
    ) -> Todo:
        """改状态。receipt=False 是**撤销路径**用的:撤销本身不该再产生一条可撤销回执。"""
        if status not in STATUSES:
            raise ValueError("未知状态: " + str(status))
        todo = self.get(todo_id)
        if todo is None:
            raise ValueError("待办不存在: " + todo_id)
        self.conn.execute(
            "UPDATE todos SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now(), todo_id),
        )
        # ⭐ 勾掉待办 = 它的闹钟也停(否则就是"活干完了闹钟还在响");放回未完成再开回来
        changed = self._sync_alarms(todo_id, closing=status == STATUS_COMPLETED)
        if receipt and self.receipts is not None:
            self.receipts.record(
                kind="task.todo",
                domain="task",
                label=("完成待办 " if status == STATUS_COMPLETED else "待办改为 " + status + " ")
                + todo.content[:32],
                detail={"id": todo_id, "content": todo.content, "op": "status",
                        "from": todo.status, "to": status, "alarms_changed": changed},
                undo_plan={"type": "todo", "op": "status", "id": todo_id,
                           "before": todo.status},
                actor=actor,
            )
        self.conn.commit()
        updated = self.get(todo_id)
        assert updated is not None
        self._publish(status, updated)
        return updated

    def set_content(self, todo_id: str, content: str, *, receipt: bool = True) -> Todo:
        """改待办内容(用户改主意了)。状态与到期日都不动。"""
        text = (content or "").strip()
        if not text:
            raise ValueError("待办内容不能为空")
        todo = self.get(todo_id)
        if todo is None:
            raise ValueError("待办不存在: " + todo_id)
        self.conn.execute(
            "UPDATE todos SET content = ?, updated_at = ? WHERE id = ?",
            (text, _now(), todo_id),
        )
        if receipt and self.receipts is not None:
            self.receipts.record(
                kind="task.todo",
                domain="task",
                label="改待办 " + text[:36],
                detail={"id": todo_id, "op": "content", "from": todo.content, "to": text},
                undo_plan={"type": "todo", "op": "content", "id": todo_id,
                           "before": todo.content},
                actor="agent",
            )
        self.conn.commit()
        updated = self.get(todo_id)
        assert updated is not None
        self._publish("content_changed", updated)
        return updated

    def set_due(
        self,
        todo_id: str,
        due_date_local: str | None,
        *,
        due_at_local: str | None = None,
        remind_minutes_before: int | None = None,
        receipt: bool = True,
        actor: str = "agent",
    ) -> Todo:
        """改到期时间(日期 + 时刻 + 提前量)。**闹钟跟着重排**,不是再挂一条。

        ⚠️ 重排 = 先摘掉旧闹钟(整行记进回执,撤销要能放回原样),再按新时刻建一条。
           待办本身 id 不变;闹钟的 id 会变 —— 它是待办的实现细节,用户手里不该有它。
        """
        todo = self.get(todo_id)
        if todo is None:
            raise ValueError("待办不存在: " + todo_id)
        at_value = str(due_at_local or "").strip() or None
        date_value = str(due_date_local or "").strip() or self._date_of(at_value)
        remind = self._normalize_remind(remind_minutes_before, at_value)
        before = {
            "due_date_local": todo.due_date_local,
            "due_at_local": todo.due_at_local,
            "remind_minutes_before": todo.remind_minutes_before,
            "alarms": self._drop_alarms(todo_id),
        }
        self.conn.execute(
            "UPDATE todos SET due_date_local = ?, due_at_local = ?, remind_minutes_before = ?,"
            " updated_at = ? WHERE id = ?",
            (date_value, at_value, remind, _now(), todo_id),
        )
        alarm_id = self._create_alarm(todo_id, todo.content, at_value, remind)
        if receipt and self.receipts is not None:
            self.receipts.record(
                kind="task.todo",
                domain="task",
                label="改待办到期时间 " + todo.content[:32] + self._when_text(date_value, at_value),
                detail={"id": todo_id, "content": todo.content, "op": "due",
                        "from": {"due_date_local": todo.due_date_local,
                                 "due_at_local": todo.due_at_local,
                                 "remind_minutes_before": todo.remind_minutes_before},
                        "to": {"due_date_local": date_value, "due_at_local": at_value,
                               "remind_minutes_before": remind},
                        "alarm_id": alarm_id},
                undo_plan={"type": "todo", "op": "due", "id": todo_id, "before": before},
                actor=actor,
            )
        self.conn.commit()
        updated = self.get(todo_id)
        assert updated is not None
        self._publish("due_changed", updated)
        return updated

    def set_due_date(self, todo_id: str, due_date_local: str | None, *,
                     receipt: bool = True) -> Todo:
        """只改**日期**(老入口,保留):时刻与提前量原样留着,闹钟跟着挪到新的那一天。"""
        todo = self.get(todo_id)
        if todo is None:
            raise ValueError("待办不存在: " + todo_id)
        return self.set_due(
            todo_id,
            due_date_local,
            due_at_local=self._shift_due_at(todo.due_at_local, due_date_local),
            remind_minutes_before=todo.remind_minutes_before,
            receipt=receipt,
        )

    @staticmethod
    def _shift_due_at(due_at_local: str | None, due_date_local: str | None) -> str | None:
        """把时刻挪到新的日期上(只改日期时用);日期被清掉,时刻也一起清掉。"""
        if not due_at_local:
            return None
        date_value = str(due_date_local or "").strip()
        moment = parse_iso(due_at_local)
        if not date_value or moment is None:
            return None
        try:
            moved = moment.replace(
                year=int(date_value[:4]), month=int(date_value[5:7]), day=int(date_value[8:10])
            )
        except (TypeError, ValueError):
            return due_at_local
        return moved.isoformat(timespec="seconds")

    def restore(self, todo_id: str, *, content: str, status: str = STATUS_PENDING,
                source: str = SOURCE_AGENT, due_date_local: str | None = None,
                due_at_local: str | None = None, remind_minutes_before: int | None = None,
                owner_id: int | None = None, alarms: Any = ()) -> Todo:
        """把一条被删掉的待办**原样放回来**(撤销"删除"用)。

        ⚠️ 只有撤销路径该调它:它不做幂等检查,也不发回执 ——
        回执已经被那条"删除"用掉了(一条回执只能撤一次)。
        """
        existing = self.get(todo_id)
        if existing is not None:
            return existing
        stamp = _now()
        self.conn.execute(
            "INSERT INTO todos(id, content, status, source, due_date_local, created_at,"
            " updated_at, due_at_local, remind_minutes_before, owner_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (todo_id, content, status, source, due_date_local, stamp, stamp,
             due_at_local, remind_minutes_before,
             owner_id if owner_id is not None else getattr(self, "member_id", None)),
        )
        # ⭐ 闹钟**原样**放回来(编号、响过没有都保留)—— "撤销删除"要回到删除前那一秒
        store = self._alarms()
        for item in alarms or ():
            store.restore(Schedule(**item))
        self.conn.commit()
        restored = self.get(todo_id)
        assert restored is not None
        self._publish("restored", restored)
        return restored

    def delete(
        self, todo_id: str, *, record_receipt: bool = False, actor: str = "agent"
    ) -> bool:
        """删掉一条待办(撤销"新建"用)。

        ⚠️ 只删自己刚建的那一条:调用方给的是回执里的 id,不做任何模糊匹配。
        """
        row = self.get(todo_id)
        if row is None:
            return False
        # 待办一删,它的闹钟跟着走(不然就是"事情没了,闹钟还在响")
        alarms = self._drop_alarms(todo_id)
        self.conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
        if record_receipt and self.receipts is not None:
            # 撤销"删除"= 把这一行(连它的闹钟)原样插回去,所以 before 里要带上整行。
            self.receipts.record(
                kind="task.todo",
                domain="task",
                label="删掉待办 " + row.content[:36]
                + ("(连它的闹钟)" if alarms else ""),
                detail={"id": todo_id, "content": row.content, "op": "delete",
                        "status": row.status, "due_date_local": row.due_date_local,
                        "due_at_local": row.due_at_local, "alarms": len(alarms)},
                undo_plan={"type": "todo", "op": "delete", "id": todo_id,
                           "before_content": row.content, "before_status": row.status,
                           "before_source": row.source,
                           "before_due": row.due_date_local,
                           "before_due_at": row.due_at_local,
                           "before_remind": row.remind_minutes_before,
                           "before_alarms": alarms},
                actor=actor,
            )
        self.conn.commit()
        self._publish("deleted", row)
        return True
