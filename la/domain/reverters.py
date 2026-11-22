"""把所有领域的撤销语义装进同一个回执撤销器(M7)。

为什么要有这个"装配文件":
  * **写入反馈是项目级的** —— 五个域(账本/日历/健康/错题/事项)的回执长在同一张表里,
    撤销也必须走同一个入口。散在各处的 register 调用迟早会漏掉一个域,
    而"漏掉"的表现是"按钮点了没反应",最难查。
  * **装配与实现分离** —— 每个域只提供自己的 revert_* 方法(它才知道退回去意味着什么),
    这里只负责把它们按 kind 接到 ReceiptReverter 上。

⚠️ 没注册的 kind 一律**不可撤销**并给出明确错误(ReceiptReverter 的行为),
   不会静默失败。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..tasks.schedules import Schedule
from .receipts import DOMAIN_TASK, Receipt, ReceiptError, ReceiptLog, ReceiptReverter



def build_reverter(
    receipts: ReceiptLog,
    *,
    ledger: Any = None,
    calendar: Any = None,
    health: Any = None,
    study: Any = None,
    todos: Any = None,
    goals: Any = None,
    schedules: Any = None,
    artifacts: Any = None,
    memory: Any = None,
    persona: Any = None,
    notes: Any = None,
) -> ReceiptReverter:
    """按域注册撤销实现。传进来的服务才注册 —— 没有该域就没有该域的回执。"""
    reverter = ReceiptReverter(receipts)

    if ledger is not None:
        reverter.register("ledger.txn", ledger.revert_txn)
        reverter.register("ledger.import", ledger.revert_txn)
        # 新建分类也是一次写入(M9):没人用过就删掉,用过了就归档。
        reverter.register("ledger.category", ledger.revert_txn)
        # 账本还有一本老的软删除撤销栈(ops_undo_log):回执撤销后要一起标 used,
        # 否则会出现"回执说撤销了、账本页还在提示可撤销"的分裂。
        reverter.register_undo_stack("ledger", ledger.undo)
    if calendar is not None:
        reverter.register("calendar.event", calendar.revert_event)
    if health is not None:
        reverter.register("health.measurement", health.revert_row)
        reverter.register("health.workout", health.revert_row)
        reverter.register("health.food", health.revert_row)
        reverter.register("health.import", health.revert_import)
        reverter.register("health.goal", health.revert_goal)
    if study is not None:
        reverter.register("study.problem", study.revert_problem)
        reverter.register("study.review", study.revert_review)
        reverter.register("study.knowledge_point", study.revert_aliases)

    if artifacts is not None:
        # 文件写入(M12 产物 / M13 工作区文件):"写要留痕、可一键撤销"。
        file_reverter = _files_reverter(artifacts)
        reverter.register("artifact.file", file_reverter)
        reverter.register("file.workspace", file_reverter)

    if memory is not None:
        # 记忆的"改口/遗忘"也要能一键撤回(旧说法重新生效)。
        reverter.register("memory.fact", _memory_reverter(memory))

    if notes is not None:
        # 笔记(M33):新增/改/删/挪/建夹/改名/归档 —— 每种都有自己的反操作,
        # 全在 NotesService.revert_note / revert_folder 里(这里只负责按 kind 接上)。
        reverter.register("journal.note", notes.revert_note)
        reverter.register("journal.folder", notes.revert_folder)

    if persona is not None:
        # 人格改写(D117):回执里带着**上一版全文** —— 撤销 = 把上一版写回去。
        # ⚠️ 它跟"记忆"一样属于"改错了很难自己回想起来"的那类内容,所以必须有得退。
        reverter.register("persona.text", _persona_reverter(persona))

    if todos is not None or goals is not None or schedules is not None:
        task_reverter = _task_reverter(todos=todos, goals=goals, schedules=schedules)
        for kind in ("task.todo", "task.goal", "task.schedule"):
            reverter.register(kind, task_reverter)
    return reverter


def _memory_reverter(memory: Any):
    """撤销"改口/遗忘":旧事实重新生效,顺手把刚写下的新事实置失效。

    ⚠️ 这里**没有删除**。撤销 = 把 active 翻回去,与新事实互指。
    """

    def revert(receipt: Receipt) -> str:
        plan = receipt.undo_plan or {}
        fact_id = int(plan.get("fact_id") or 0)
        if not fact_id:
            raise ReceiptError("这条回执里没有事实编号,撤不了")
        op = str(plan.get("op") or "")

        if op == "retype":
            before_type = str(plan.get("before_type") or "other")
            if not memory.set_fact_type(fact_id, before_type):
                raise ReceiptError("原来那条事实已经不在了(编号 " + str(fact_id) + ")")
            return "记忆的类别已改回 " + before_type

        if op == "supersede_pending":
            # 当时只是把新说法排进了候选队列:撤销 = 作废那条候选(旧说法从头到尾没动过)
            candidate_id = int(plan.get("candidate_id") or 0)
            dropped = False
            if candidate_id and hasattr(memory, "set_candidate_status"):
                try:
                    dropped = memory.set_candidate_status(
                        candidate_id, "rejected", reason="用户撤销了这次改口",
                    )
                except Exception:  # noqa: BLE001 - 候选表不可用不该让撤销失败
                    dropped = False
            if not memory.set_fact_active(fact_id, True):
                raise ReceiptError("原来那条事实已经不在了(编号 " + str(fact_id) + ")")
            return "这次改口已撤回(旧说法仍然有效)" + (
                ";排队的候选已作废" if dropped else ""
            )

        if op == "release":
            # 撤销"人工放行"(2026-10-25):刚写进去的那条置失效,候选退回待放行 ——
            # 只撤数据,不删行(facts 表只追加,与整层的口径一致)。
            if not memory.set_fact_active(fact_id, False):
                raise ReceiptError("放行时写的那条事实已经不在了(编号 " + str(fact_id) + ")")
            candidate_id = int(plan.get("candidate_id") or 0)
            restored = False
            if candidate_id and hasattr(memory, "set_candidate_status"):
                try:
                    restored = memory.set_candidate_status(
                        candidate_id, "review", reason="用户撤销了这次放行",
                    )
                except Exception:  # noqa: BLE001 - 候选表不可用不该让撤销失败
                    restored = False
            return "这次放行已撤回:那条事实不再生效" + (
                ";候选已退回待放行" if restored else ""
            )

        new_id = int(plan.get("new_fact_id") or 0)
        if new_id:
            memory.set_fact_active(new_id, False)
        if not memory.set_fact_active(fact_id, True):
            raise ReceiptError("原来那条事实已经不在了(编号 " + str(fact_id) + ")")
        return "记忆已改回上一版" + ("(新写的那条已置为失效)" if new_id else "")

    return revert


def _persona_reverter(persona: Any):
    """撤销"改写人格":把上一版主体(以及名字,如果有)原样写回去。

    ⚠️ 上一版是**空串**时表示"当时还没自定义过" —— 那就删掉这个键,回到出厂默认,
       而不是存一份空的进去(空人格会让助手只剩固定的尾巴)。回执的措辞也照实说。
    """

    def revert(receipt: Receipt) -> str:
        plan = receipt.undo_plan or {}
        if "before" not in plan:
            raise ReceiptError("这条回执里没留下上一版人格,退不回去")
        before = str(plan.get("before") or "")
        # ⭐ D144:回执里带 before_name 的,名字一起退;老回执没有这个字段 ——
        #    那就别动名字(None),否则撤销一次正文会把用户后改的名字也抹掉。
        name = plan.get("before_name") if "before_name" in plan else None
        try:
            persona.restore(before, name)
        except Exception as exc:  # noqa: BLE001 - 退不回去要说清楚,不能假装成功
            raise ReceiptError("退回上一版失败:" + str(exc)[:120]) from exc
        if before.strip():
            return "人格已退回上一版" + ("(名字也退回了)" if name is not None else "")
        return "人格已恢复出厂默认"

    return revert


def _files_reverter(artifacts: Any):
    """文件写入的撤销语义。

    三种情况,一份实现:
      * **新建**(existed=False):把文件删掉(产物还要连登记一起删,否则下载入口 404);
      * **覆盖**(existed=True 且留了备份):把旧内容写回去 —— 撤销要的是"回到上一版",
        不是"文件消失";
      * 备份丢了(产物区被清理过):如实说"退不回去",绝不假装成功。
    """

    def revert(receipt: Receipt) -> str:
        plan = receipt.undo_plan or {}
        target = Path(str(plan.get("path") or ""))
        artifact_id = str(plan.get("artifact_id") or "")
        name = str(plan.get("name") or target.name or artifact_id)
        if not str(target) and not artifact_id:
            raise ReceiptError("这条回执里没有文件位置,撤不了")
        existed = bool(plan.get("existed"))
        backup = Path(str(plan.get("backup") or ""))

        if not existed:
            if artifact_id:
                try:
                    removed = artifacts.delete(artifact_id)
                except Exception as exc:  # noqa: BLE001 - 删不掉要说清楚,不能静默
                    raise ReceiptError("删产物失败:" + str(exc)[:120]) from exc
                if not removed:
                    return "这个产物已经不在了(可能已被清理)"
                return "已删除产物 " + name
            try:
                if target.exists():
                    target.unlink()
            except OSError as exc:
                raise ReceiptError("删不掉这个文件:" + str(exc)[:120]) from exc
            return "已删掉刚写的文件 " + name

        if not backup or not backup.exists():
            raise ReceiptError("旧内容的那份备份已经不在了(产物区可能被清理过),退不回上一版")
        try:
            payload = backup.read_bytes()
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_bytes(payload)
            tmp.replace(target)
        except OSError as exc:
            raise ReceiptError("写回旧内容失败:" + str(exc)[:120]) from exc
        if artifact_id:
            # 登记还在(同一个 id),只需让字节大小与磁盘重新一致。
            try:
                artifacts.register_path(target, rel_path=artifacts.get(artifact_id).rel_path)
            except Exception:  # noqa: BLE001 - 登记对不上不该让撤销失败
                pass
        return "已把 " + name + " 退回上一版"

    return revert


def _task_reverter(*, todos: Any, goals: Any, schedules: Any):
    """事项层的撤销:新建 -> 删掉;改状态 -> 改回去;目标改动 -> 回到上一版。"""

    def _restore_alarms(todo_id: str, rows: list) -> None:
        """把待办的闹钟恢复成**原来的那几条**(重排时新建的先摘掉)。

        ⚠️ 为什么要"先摘再放":改到期时间会按新时刻建一条新闹钟,而撤销要回到旧的那一条 ——
           编号与 last_fired_at 都得是旧的(否则"已经响过"的记录会丢,响过的还会再响)。
        """
        if schedules is None:
            return
        for item in schedules.for_todo(todo_id):
            schedules.delete(item.id, receipt=False)
        for row in rows:
            schedules.restore(Schedule(**row))

    def revert(receipt: Receipt) -> str:
        plan = receipt.undo_plan or {}
        kind = str(plan.get("type") or "")
        if kind == "todo":
            if todos is None:
                raise ReceiptError("待办存储不在,撤不了")
            op = str(plan.get("op") or "create")
            todo_id = str(plan.get("id") or "")
            if op == "create":
                if not todos.delete(todo_id):
                    raise ReceiptError("这条待办已经不存在了")
                return "已删掉刚记的待办"
            if op == "delete":
                # 撤销"删除"= 原样放回来(所以删除时必须把整行 + 它的闹钟写进 undo_plan)
                todos.restore(
                    todo_id,
                    content=str(plan.get("before_content") or ""),
                    status=str(plan.get("before_status") or "pending"),
                    source=str(plan.get("before_source") or "agent"),
                    due_date_local=plan.get("before_due"),
                    due_at_local=plan.get("before_due_at"),
                    remind_minutes_before=plan.get("before_remind"),
                    alarms=plan.get("before_alarms") or [],
                )
                return "已把删掉的待办放回来"
            if op == "due":
                before = plan.get("before")
                if isinstance(before, dict):
                    # D115 起到期时间带**时刻**与**闹钟**:按旧值重排,再把它原来的那条闹钟
                    # 原样放回来(编号、响过没有都保留)—— 重排新建的那条要扔掉。
                    todos.set_due(
                        todo_id,
                        before.get("due_date_local"),
                        due_at_local=before.get("due_at_local"),
                        remind_minutes_before=before.get("remind_minutes_before"),
                        receipt=False,
                    )
                    _restore_alarms(todo_id, before.get("alarms") or [])
                    return "待办到期时间已改回去"
                # 老回执:before 是个字符串(只有日期的那一版)
                todos.set_due_date(todo_id, before, receipt=False)
                return "待办到期日已改回去"
            if op == "content":
                todos.set_content(todo_id, str(plan.get("before") or ""), receipt=False)
                return "待办内容已改回上一版"
            todos.set_status(todo_id, str(plan.get("before") or "pending"), receipt=False)
            return "待办状态已改回去"
        if kind == "goal":
            if goals is None:
                raise ReceiptError("目标存储不在,撤不了")
            goal_id = str(plan.get("goal_id") or "")
            before = plan.get("before")
            if not before:
                raise ReceiptError("「新建目标」目前不支持一键撤销,请到目标里手动暂停或删除")
            goals.update(
                goal_id,
                "edit" if before.get("objective") else "resume",
                objective=before.get("objective"),
                source="user",
            )
            return "目标已退回上一版"
        if kind == "schedule":
            if schedules is None:
                raise ReceiptError("提醒存储不在,撤不了")
            schedule_id = str(plan.get("schedule_id") or "")
            op = str(plan.get("op") or "create")
            before = plan.get("before") or {}
            if op == "delete":
                # 撤销"删除"= **原样**放回来(编号不变,last_fired_at 也保留):
                # 编号变了的话,用户手里的 id、"这条提醒已经删了"的上下文全部对不上。
                restored = schedules.restore(
                    Schedule(
                        id=schedule_id,
                        kind=str(before.get("kind") or "at"),
                        at=before.get("at"),
                        every_seconds=before.get("every_seconds"),
                        prompt=str(before.get("prompt") or ""),
                        # ⚠️ 这三格必须原样回去(2026-10-18 审计):旧实现把
                        #    enabled 硬编码 True,且完全不带 todo_id/target_member_id ——
                        #    删掉一条“只提醒妈妈 / 挂着待办 / 已关闭”的提醒再撤销,
                        #    它会变成全家广播、重新开始响、且勾掉待办也停不掉它。
                        enabled=bool(before.get("enabled", True)),
                        todo_id=before.get("todo_id"),
                        target_member_id=before.get("target_member_id"),
                        last_fired_at=before.get("last_fired_at"),
                        created_at=str(before.get("created_at") or ""),
                        updated_at=str(before.get("updated_at") or ""),
                    )
                )
                return "已把删掉的提醒放回来(" + restored.id + ")"
            if op == "attach":
                before = plan.get("before") or {}
                schedules.attach_todo(
                    schedule_id, str(before.get("todo_id") or ""), receipt=False
                )
                return "提醒已恢复成原来的归属"
            if op == "enabled":
                schedules.set_enabled(
                    schedule_id, bool(before.get("enabled", True)), receipt=False
                )
                return "提醒已改回原来的开关状态"
            if op == "update":
                schedules.update(
                    schedule_id,
                    prompt=before.get("prompt"),
                    at=before.get("at"),
                    every_seconds=before.get("every_seconds"),
                    reset_fired=False,
                    receipt=False,
                )
                if before.get("last_fired_at"):
                    schedules.conn.execute(
                        "UPDATE schedules SET last_fired_at = ? WHERE id = ?",
                        (before.get("last_fired_at"), schedule_id),
                    )
                    schedules.conn.commit()
                return "提醒已改回上一版"
            if not schedules.delete(schedule_id, receipt=False):
                raise ReceiptError("这条提醒已经不存在了")
            return "已删掉刚定的提醒"
        raise ReceiptError("不认识的回执类型: " + kind)

    return revert


__all__ = ["DOMAIN_TASK", "build_reverter"]
