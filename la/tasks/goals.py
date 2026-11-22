"""长期目标:事件溯源 + revision CAS。

语义照抄 DSH(设计深化 §3.7):
  * 每次变更写一条 `goal/change` 事件(全量快照)—— 目标是可重放的;
  * `revision` 做乐观并发控制(CAS),不匹配就拒绝而不是覆盖;
  * `max_rounds` 耗尽 -> 自动 blocked(由代码判定,不靠模型自觉);
  * `resume` 只接受**用户直接**发起的请求。
"""

from __future__ import annotations
from ..base.clock import utc_now_iso as _now

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

PHASE_ACTIVE = "active"
PHASE_PAUSED = "paused"
PHASE_BLOCKED = "blocked"
PHASE_COMPLETE = "complete"
PHASES = (PHASE_ACTIVE, PHASE_PAUSED, PHASE_BLOCKED, PHASE_COMPLETE)

ACTION_EDIT = "edit"
ACTION_PAUSE = "pause"
ACTION_RESUME = "resume"
ACTION_COMPLETE = "complete"
ACTION_BLOCKED = "blocked"

ACTIONS = (ACTION_EDIT, ACTION_PAUSE, ACTION_RESUME, ACTION_COMPLETE, ACTION_BLOCKED)

SOURCE_USER = "user"
SOURCE_AGENT = "agent"


class GoalError(RuntimeError):
    """目标操作的领域错误。"""


class RevisionConflict(GoalError):
    """revision 不匹配 —— 有人在别处改过这个目标。"""




@dataclass
class Goal:
    id: str
    objective: str
    phase: str
    revision: int
    blocked_reason: str | None
    max_rounds: int | None
    rounds_started: int
    created_at: str
    updated_at: str

    @property
    def is_open(self) -> bool:
        return self.phase in (PHASE_ACTIVE, PHASE_PAUSED)

    def render(self) -> str:
        """给投影用的紧凑描述 —— 轮次一定要显示,否则"接续进度"就无从谈起。"""
        parts = [self.objective, "(阶段 " + self.phase]
        rounds = "/ 轮次 " + str(self.rounds_started)
        if self.max_rounds:
            rounds += "/" + str(self.max_rounds)
        parts.append(rounds)
        if self.blocked_reason:
            parts.append("/ 原因 " + self.blocked_reason)
        return " ".join(parts) + ")"

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Goal":
        return cls(
            id=row["id"],
            objective=row["objective"],
            phase=row["phase"],
            revision=int(row["revision"]),
            blocked_reason=row["blocked_reason"],
            max_rounds=row["max_rounds"],
            rounds_started=int(row["rounds_started"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class GoalStore:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        emit: Callable[[str, dict], None] | None = None,
        receipts: Any = None,
    ):
        self.conn = conn
        self._emit = emit
        # 统一写入回执(M7),与目标行同一个事务
        self.receipts = receipts

    def _snapshot(self, goal: Goal) -> dict[str, Any]:
        return {
            "id": goal.id,
            "objective": goal.objective,
            "phase": goal.phase,
            "revision": goal.revision,
            "blocked_reason": goal.blocked_reason,
            "max_rounds": goal.max_rounds,
            "rounds_started": goal.rounds_started,
        }

    def _publish(self, event: str, goal: Goal) -> None:
        if self._emit is not None:
            self._emit("goal/change", {"event": event, "goal": self._snapshot(goal)})

    # -- 读 ---------------------------------------------------------------
    def get(self, goal_id: str) -> Goal | None:
        row = self.conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
        return Goal.from_row(row) if row else None

    def list(self, *, open_only: bool = False, limit: int = 20) -> list[Goal]:
        sql = "SELECT * FROM goals"
        params: list[Any] = []
        if open_only:
            sql += " WHERE phase IN ('active', 'paused')"
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        return [Goal.from_row(row) for row in self.conn.execute(sql, params).fetchall()]

    # -- 写 ---------------------------------------------------------------
    def create(self, objective: str, *, max_rounds: int | None = None) -> Goal:
        text = (objective or "").strip()
        if not text:
            raise GoalError("目标内容不能为空")
        if max_rounds is not None and max_rounds <= 0:
            raise GoalError("max_rounds 必须是正整数")
        stamp = _now()
        goal_id = "g-" + uuid.uuid4().hex[:10]
        self.conn.execute(
            "INSERT INTO goals(id, objective, phase, revision, max_rounds, rounds_started,"
            " created_at, updated_at) VALUES (?, ?, ?, 1, ?, 0, ?, ?)",
            (goal_id, text, PHASE_ACTIVE, max_rounds, stamp, stamp),
        )
        if self.receipts is not None:
            self.receipts.record(
                kind="task.goal",
                domain="task",
                label="定一个目标 " + text[:36],
                detail={"id": goal_id, "objective": text, "op": "create"},
                undo_plan={"type": "goal", "goal_id": goal_id},
                actor="agent",
            )
        self.conn.commit()
        goal = self.get(goal_id)
        assert goal is not None
        self._publish("created", goal)
        return goal

    def update(
        self,
        goal_id: str,
        action: str,
        *,
        expected_revision: int | None = None,
        objective: str | None = None,
        blocked_reason: str | None = None,
        source: str = SOURCE_AGENT,
    ) -> Goal:
        if action not in ACTIONS:
            raise GoalError("未知动作: " + str(action))
        goal = self.get(goal_id)
        if goal is None:
            raise GoalError("目标不存在: " + goal_id)
        if expected_revision is not None and expected_revision != goal.revision:
            raise RevisionConflict(
                "revision 不匹配(期望 " + str(expected_revision) + ",实际 " + str(goal.revision) + ")"
            )
        if action == ACTION_RESUME and source != SOURCE_USER:
            raise GoalError("resume 只接受用户直接发起的请求")

        phase = goal.phase
        reason = goal.blocked_reason
        text = goal.objective
        if action == ACTION_EDIT:
            if not (objective or "").strip():
                raise GoalError("edit 必须给出新的 objective")
            text = objective.strip()
        elif action == ACTION_PAUSE:
            phase = PHASE_PAUSED
        elif action == ACTION_RESUME:
            phase = PHASE_ACTIVE
            reason = None
        elif action == ACTION_COMPLETE:
            phase = PHASE_COMPLETE
        elif action == ACTION_BLOCKED:
            phase = PHASE_BLOCKED
            reason = (blocked_reason or "未说明原因").strip()

        self.conn.execute(
            "UPDATE goals SET objective = ?, phase = ?, blocked_reason = ?,"
            " revision = revision + 1, updated_at = ? WHERE id = ?",
            (text, phase, reason, _now(), goal_id),
        )
        if self.receipts is not None:
            self.receipts.record(
                kind="task.goal",
                domain="task",
                label="目标「" + text[:24] + "」改为 " + str(phase),
                detail={"id": goal_id, "op": action, "from": goal.phase, "to": phase},
                # 撤销 = 回到上一个 phase(以及上一版 objective)。
                undo_plan={"type": "goal", "op": "state", "goal_id": goal_id,
                           "before": {"phase": goal.phase, "objective": goal.objective,
                                      "blocked_reason": goal.blocked_reason}},
                actor="agent",
            )
        self.conn.commit()
        updated = self.get(goal_id)
        assert updated is not None
        self._publish(action, updated)
        return updated

    def bump_round(self, goal_id: str) -> Goal | None:
        """推进一轮。max_rounds 耗尽时**由代码**自动置 blocked(设计深化 §3.7)。"""
        goal = self.get(goal_id)
        if goal is None or not goal.is_open:
            return goal
        rounds = goal.rounds_started + 1
        if goal.max_rounds is not None and rounds >= goal.max_rounds:
            self.conn.execute(
                "UPDATE goals SET rounds_started = ?, phase = ?, blocked_reason = ?,"
                " revision = revision + 1, updated_at = ? WHERE id = ?",
                (rounds, PHASE_BLOCKED, "轮次预算耗尽(max_rounds=" + str(goal.max_rounds) + ")",
                 _now(), goal_id),
            )
        else:
            self.conn.execute(
                "UPDATE goals SET rounds_started = ?, revision = revision + 1, updated_at = ?"
                " WHERE id = ?",
                (rounds, _now(), goal_id),
            )
        self.conn.commit()
        updated = self.get(goal_id)
        if updated is not None:
            self._publish("round", updated)
        return updated
