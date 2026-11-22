"""执行账本:把内部事件投影成用户看得见的「步骤行」(含每步用时)。

这是「执行反馈」的唯一实现 —— 直接对标云朵项目的 app/agent/step_trace.py,
但那边的投影规则是为它的技能/PPT/面板体系写的,这里只保留可迁移的三条:

  1. **一行一步,按 id 原地更新**。前端拿到 id 就能复用 DOM 节点,
     不需要推倒重画 —— 长任务里"整表重渲"会把用户的展开状态和滚动位置吃掉。
  2. **计时在服务端算一次就冻结**。进行中的行给 since(本地走秒),
     终态的行给 duration_ms。**服务端不推心跳**(只在状态变化时发帧),
     否则一个 2 分钟的思考能推出上千个 SSE 帧。
  3. **顺序纪律:step 帧必须排在它对应的 status/正文之前**,
     新行开启时先补发上一行的收尾帧 —— 否则前端那一行会一直转圈。

与云朵的差异(有意为之):
  * 不做前缀合并/标题改写那一套:我们的步骤是**结构化发出**的(标题由发起方给全),
    不是从自由文案里猜出来的 —— 猜的那套是它 22KB 的来源,也是它最脆的部分。
  * 不落库回看:聊天页目前不保留历史会话(与云朵的会话库不同),所以没有 meta.steps。
    将来要回看时,把 ExecutionLedger.finish() 的载荷写进消息 meta 即可。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

# 状态:与前端约定死的四个字面量
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

# kind -> 前端图标(与云朵的 TRACE_KIND_META 对齐,少一档 llm——我们并进 node)
KIND_NODE = "node"
KIND_TOOL = "tool"
KIND_TASK = "task"
KIND_ERROR = "error"

KIND_META: dict[str, dict[str, str]] = {
    KIND_NODE: {"icon": "◆", "cls": "k-node"},
    KIND_TOOL: {"icon": "▸", "cls": "k-tool"},
    KIND_TASK: {"icon": "☑", "cls": "k-task"},
    KIND_ERROR: {"icon": "⚠", "cls": "k-error"},
}

# 工具名 -> (kind, 领域)。**从 tools/metadata.py 派生**(唯一真源,2026-10-18 审计修):
# 以前它是第三份手写映射,与 session.TOOL_TITLES / registry.READ_ONLY_TOOLS /
# session.INTERNAL_TOOLS 各写各的。
from ..tools.metadata import kinds as _tool_kinds

TOOL_KINDS: dict[str, tuple[str, str]] = _tool_kinds()

MAX_ROWS = 80          # 单轮账本行数上限(超出丢最旧的**已完成**行,绝不动进行中的行)
KEEP_ROWS = 60         # done 载荷里回给前端的行数上限
TITLE_MAX = 120
DETAIL_MAX = 300


def _clip(text: Any, limit: int) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: max(limit - 1, 0)] + "…"


@dataclass
class Step:
    """一行步骤。字段名与前端约定死 —— 前端只认这些。"""

    id: str
    kind: str
    title: str
    status: str = STATUS_RUNNING
    domain: str = ""
    detail: str = ""
    since: int = 0              # 行开始的 epoch **毫秒**(前端用它本地走秒)
    duration_ms: int | None = None   # 终态才有;进行中为 None
    at: int = 0                 # 最后一次变化的时间

    def as_dict(self) -> dict[str, Any]:
        meta = KIND_META.get(self.kind, KIND_META[KIND_TOOL])
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "icon": meta["icon"],
            "cls": meta["cls"],
            "title": self.title,
            "status": self.status,
            "since": self.since,
            "at": self.at or self.since,
        }
        if self.domain:
            payload["domain"] = self.domain
        if self.detail:
            payload["detail"] = self.detail
        if self.duration_ms is not None:
            payload["duration_ms"] = int(self.duration_ms)
        return payload


class ExecutionLedger:
    """一轮对话的步骤账本。

    用法::

        ledger = ExecutionLedger(emit)
        ledger.begin("思考中", kind="node", key="phase:thinking")
        ...
        payload = ledger.finish()

    `emit` 每行变化调一次,参数是**已经投影好的事件字典** `{"event": ..., "data": ...}`,
    上层只负责把它塞进 SSE(或打印到终端)。
    """

    def __init__(
        self,
        emit: Callable[[dict[str, Any]], None] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        max_rows: int = MAX_ROWS,
    ) -> None:
        self._emit = emit
        self._clock = clock
        self._wall = wall
        self._max_rows = max_rows
        self.rows: list[Step] = []
        self._index: dict[str, Step] = {}
        self._seq = 0
        self._started = clock()
        self._started_wall = wall()
        self.dropped = 0

    # -- 生命周期 ---------------------------------------------------------
    def begin(
        self, title: str, *, kind: str = KIND_NODE, key: str = "", domain: str = ""
    ) -> Step:
        """开一步。**同一个 key 复用同一行** —— 反复发生的阶段不会刷屏。"""
        row = self._index.get(key) if key else None
        if row is not None:
            row.title = _clip(title, TITLE_MAX)
            row.detail = ""
            row.status = STATUS_RUNNING
            row.duration_ms = None
            row.since = self._now_ms()
            row.at = row.since
            if domain:
                row.domain = domain
            self._push(row, "step_update")
            return row
        self._seq += 1
        row = Step(
            id="s" + str(self._seq),
            kind=kind,
            title=_clip(title, TITLE_MAX),
            domain=domain,
            since=self._now_ms(),
        )
        row.at = row.since
        self.rows.append(row)
        if key:
            self._index[key] = row
        self._shed()
        self._push(row, "step")
        return row

    def update(self, key: str, title: str | None = None, *, detail: str | None = None) -> None:
        row = self._index.get(key)
        if row is None:
            return
        changed = False
        if title is not None and _clip(title, TITLE_MAX) != row.title:
            row.title = _clip(title, TITLE_MAX)
            changed = True
        if detail is not None and _clip(detail, DETAIL_MAX) != row.detail:
            row.detail = _clip(detail, DETAIL_MAX)
            changed = True
        if not changed:
            return  # ⚠️ 只有计时变化时**不发帧**:否则每一秒都在推 SSE
        row.at = self._now_ms()
        self._push(row, "step_update")

    def end(
        self,
        key: str,
        title: str | None = None,
        *,
        ok: bool = True,
        detail: str = "",
    ) -> None:
        """收一步。**没开过的 key 会被开出来再收掉**,这样工具层不必先 begin。"""
        row = self._index.get(key)
        if row is None:
            row = self.begin(title or key, key=key)
        if title:
            row.title = _clip(title, TITLE_MAX)
        if detail:
            row.detail = _clip(detail, DETAIL_MAX)
        row.status = STATUS_DONE if ok else STATUS_FAILED
        row.at = self._now_ms()
        row.duration_ms = max(0, row.at - row.since)
        self._push(row, "step_update")

    def finish(self, status: str = STATUS_DONE) -> dict[str, Any]:
        """收尾:把还在跑的行标成终态(失败时传 failed),返回 done 载荷。

        ⚠️ 必须收尾 —— 否则前端会留一个永远转圈的步骤行(云朵的注释里记着这个坑)。
        """
        for row in self.rows:
            if row.status != STATUS_RUNNING:
                continue
            row.status = status
            row.at = self._now_ms()
            row.duration_ms = max(0, row.at - row.since)
            self._push(row, "step_update")
        return self.payload()

    # -- 载荷 -------------------------------------------------------------
    def payload(self) -> dict[str, Any]:
        rows = self.rows[-KEEP_ROWS:]
        return {
            "steps": [row.as_dict() for row in rows],
            "steps_meta": self.summary(),
        }

    def summary(self) -> dict[str, Any]:
        failed = sum(1 for row in self.rows if row.status == STATUS_FAILED)
        return {
            "v": 1,
            "total": len(self.rows) + self.dropped,
            "shown": len(self.rows),
            "dropped": self.dropped,
            "failed": failed,
            "elapsed_ms": max(0, int((self._clock() - self._started) * 1000)),
            "truncated": bool(self.dropped or len(self.rows) > KEEP_ROWS),
        }

    def label(self) -> str:
        """给摘要条用的一句话:『已执行 N 步 · 用时 Xs』。"""
        summary = self.summary()
        text = "已执行 " + str(summary["total"]) + " 步"
        if summary["failed"]:
            text += " · " + str(summary["failed"]) + " 步失败"
        elapsed = format_duration(summary["elapsed_ms"])
        return text + (" · 用时 " + elapsed if elapsed else "")

    # -- 内部 -------------------------------------------------------------
    def _now_ms(self) -> int:
        return int(self._wall() * 1000)

    def _push(self, row: Step, event: str) -> None:
        if self._emit is None:
            return
        try:
            self._emit(
                {
                    "event": event,
                    "data": {
                        "step": row.as_dict(),
                        "total": len(self.rows) + self.dropped,
                        "truncated": bool(self.dropped),
                    },
                }
            )
        except Exception:
            pass  # 记账失败绝不能影响主流程

    def _shed(self) -> None:
        """超出上限时丢**最旧的已完成行**。进行中的行永不丢。"""
        overflow = len(self.rows) - self._max_rows
        if overflow <= 0:
            return
        kept: list[Step] = []
        for row in self.rows:
            if overflow > 0 and row.status != STATUS_RUNNING:
                overflow -= 1
                self.dropped += 1
                for key, value in list(self._index.items()):
                    if value is row:
                        del self._index[key]
                continue
            kept.append(row)
        self.rows = kept


def format_duration(ms: int | float) -> str:
    """耗时格式化(与前端 _traceFmtSecs 同口径,免得两边显示不一样)。"""
    if not ms or ms < 0:
        return ""
    seconds = ms / 1000.0
    if seconds < 10:
        return str(round(seconds, 1)) + "s"
    if seconds < 60:
        return str(int(round(seconds))) + "s"
    minutes = int(seconds // 60)
    rest = int(round(seconds % 60))
    if minutes < 60:
        return str(minutes) + "m" + (str(rest) + "s" if rest else "")
    return str(minutes // 60) + "h" + str(minutes % 60) + "m"


__all__ = [
    "ExecutionLedger",
    "KIND_ERROR",
    "KIND_META",
    "KIND_NODE",
    "KIND_TASK",
    "KIND_TOOL",
    "MAX_ROWS",
    "KEEP_ROWS",
    "STATUS_DONE",
    "STATUS_FAILED",
    "STATUS_RUNNING",
    "Step",
    "TOOL_KINDS",
    "format_duration",
]
