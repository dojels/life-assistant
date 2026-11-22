"""统一写入回执:项目级的「写入了什么 + 一键撤销」。

为什么要有这一层(而不是每个域各写各的提示):
  * **写入反馈是项目级的**。账本、日历、健康、错题、事项层的写入,对用户来说
    应该是同一件事:「它动了我的数据 → 告诉我动了什么 → 不对就撤」。
    如果每个域自己拼一段文案、自己做一个撤销按钮,五处实现必然漂移。
  * **回执必须与写入同事务**。所以领域服务在**提交前**调用 `ReceiptLog.record()`,
    回执与数据一起落库 —— 不可能出现"写成功了但用户不知道"。
  * **撤销语义属于领域**。`ops_receipts` 只负责"只写一次、只撤一次";
    "退回去到底意味着什么"(改状态?删行?恢复旧值?)由各域注册的 reverter 决定。

三条不变量:
  1. 回执是**只追加**的(触发器强制),唯一允许的改写是"标记已撤销";
  2. 一条回执**只能撤一次** —— 第二次会得到 `ReceiptError`,而不是又执行一遍;
  3. 撤销**失败不吞异常** —— 撤回一半比不撤更危险,所以抛出去让调用方看见。

与既有三张表的分工:
    ops_audit   谁在什么时候做了什么(不可变的行为记录,给人查)
    ops_undo_log 账本域自己的软删除撤销栈(历史包袱,仍在用)
    ops_receipts **给用户看的回执**:写了什么 + 怎么退回去 + 撤没撤
"""

from __future__ import annotations
from ..base.clock import utc_now_iso as _utc_now

import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

# 领域标识:前端用它选图标与配色,后端用它做统计。**闭集**。
DOMAIN_LEDGER = "ledger"
DOMAIN_CALENDAR = "calendar"
DOMAIN_HEALTH = "health"
DOMAIN_STUDY = "study"
DOMAIN_TASK = "task"
DOMAIN_MEMORY = "memory"
# 笔记域(M33):用户手写的本子。**不是记忆** —— 它不可重建、不衰减、可编辑。
DOMAIN_NOTES = "notes"
# 产物层(M12):artifact_write 写出来的文件也要有回执 + 一键撤销。
DOMAIN_ARTIFACT = "artifact"
DOMAIN_SYSTEM = "system"
DOMAINS = (
    DOMAIN_LEDGER, DOMAIN_CALENDAR, DOMAIN_HEALTH,
    DOMAIN_STUDY, DOMAIN_TASK, DOMAIN_MEMORY, DOMAIN_NOTES, DOMAIN_SYSTEM,
)


class ReceiptError(RuntimeError):
    """回执不可撤销(已撤过 / 没有这条 / 目标已不在)。"""




@dataclass
class Receipt:
    id: int
    ts: str
    actor: str
    kind: str
    domain: str
    label: str
    detail: dict[str, Any] = field(default_factory=dict)
    undo_plan: dict[str, Any] = field(default_factory=dict)
    undoable: bool = True
    undone_at: str | None = None
    undone_note: str | None = None
    session_id: str | None = None
    turn: int | None = None
    #: 这条回执来自哪一本账("a"=assistant.db / "m"=memory.db)。
    #: 两张表的 id 各自从 1 自增,前端撤销必须带它,否则同号会撤到另一条写入。
    #: 由 Web 层在合并两本账时填;直接读单本账时是空串。
    source: str = ""

    @property
    def undone(self) -> bool:
        return self.undone_at is not None

    def as_dict(self) -> dict[str, Any]:
        """给前端的形状。字段名稳定 —— 前端只认这些。"""
        return {
            "id": self.id,
            "ts": self.ts,
            "actor": self.actor,
            "kind": self.kind,
            "domain": self.domain,
            "label": self.label,
            "detail": self.detail,
            "undoable": bool(self.undoable and not self.undone),
            "undone": self.undone,
            "undone_at": self.undone_at,
            "note": self.undone_note or "",
            # 两本账的 id 会撞号;前端把它拼成 "a:12" / "m:12" 再撤销。
            "source": self.source,
        }


class ReceiptLog:
    """回执读写。它**只记账**,不执行撤销(那是 ReceiptReverter 的事)。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.session_id: str | None = None
        self.turn: int | None = None
        # 表在不在:探一次就记住 —— 但**只记"在"**(2026-10-17 审计)。
        #
        # ⚠️ 旧写法把"不在"也永久记住:一次瞬时失败(locked / 表刚好在建)之后,
        #    这个实例就再也写不出回执了 —— 而 record() 返回 0 的约定是"调用方不必处理",
        #    于是失败**完全无声**:没有回执 = 没有撤销按钮 = 没人会知道。
        #    微信桥那个实例活整个进程,一次失败就是永久。
        #    现在:成功才缓存;失败只退避 5 秒重探。
        self._ready: bool = False
        self._next_probe: float = 0.0
        #: 因为"表不可用"而丢掉的回执条数(自检/测试可读)
        self.dropped = 0

    _RETRY_S = 5.0

    def _table_ready(self) -> bool:
        if self._ready:
            return True
        now = time.monotonic()
        if self._next_probe and now < self._next_probe:
            return False
        try:
            self.conn.execute("SELECT 1 FROM ops_receipts LIMIT 1").fetchone()
            self._ready = True
            return True
        except sqlite3.Error:
            self._next_probe = now + self._RETRY_S
            return False

    # -- 写 ---------------------------------------------------------------
    def record(
        self,
        *,
        kind: str,
        domain: str,
        label: str,
        detail: dict[str, Any] | None = None,
        undo_plan: dict[str, Any] | None = None,
        actor: str = "agent",
        undoable: bool = True,
        session_id: str | None = None,
        turn: int | None = None,
    ) -> int:
        """写一条回执。**不 commit** —— 调用方与领域写入同一个事务提交。

        这样做的理由:如果回执先提交、领域写入后失败,用户会看到一条
        "已写入"的回执但数据并不存在 —— 比没有回执更糟。

        返回 0 表示"这次没记成"(表不存在)—— 调用方不必处理,写入照常。
        """
        if not self._table_ready():
            return 0
        cursor = self.conn.execute(
            "INSERT INTO ops_receipts(ts, actor, kind, domain, label, detail, undo_plan,"
            " undoable, session_id, turn) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _utc_now(),
                actor,
                kind,
                domain,
                label,
                json.dumps(detail or {}, ensure_ascii=False, default=str),
                json.dumps(undo_plan or {}, ensure_ascii=False, default=str)
                if undoable else None,
                1 if undoable else 0,
                session_id if session_id is not None else self.session_id,
                turn if turn is not None else self.turn,
            ),
        )
        return int(cursor.lastrowid or 0)

    # -- 读 ---------------------------------------------------------------
    def get(self, receipt_id: int) -> Receipt:
        if not self._table_ready():
            raise ReceiptError("这个库里还没有写入回执表")
        row = self.conn.execute(
            "SELECT * FROM ops_receipts WHERE id = ?", (int(receipt_id),)
        ).fetchone()
        if row is None:
            raise ReceiptError("没有这条写入回执: " + str(receipt_id))
        return _row_to_receipt(row)

    def recent(
        self,
        *,
        limit: int = 20,
        session_id: str | None = None,
        include_undone: bool = True,
    ) -> list[Receipt]:
        """最近的回执(新的在前)。

        `session_id` 给定时只取那个会话的 —— 聊天页用它把"这一轮写了什么"
        与历史回执分开。不给则取全局最近若干条(回执页用)。
        """
        if not self._table_ready():
            return []
        where: list[str] = []
        params: list[Any] = []
        if session_id:
            where.append("session_id = ?")
            params.append(session_id)
        if not include_undone:
            where.append("undone_at IS NULL")
        sql = "SELECT * FROM ops_receipts"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        return [_row_to_receipt(row) for row in self.conn.execute(sql, params).fetchall()]

    def since(self, after_id: int, *, limit: int = 50, session_id: str | None = None) -> list[Receipt]:
        """取 id 大于 after_id 的回执(**时间正序**)—— 一轮对话结束后捞这一轮的写入。"""
        if not self._table_ready():
            return []
        sql = "SELECT * FROM ops_receipts WHERE id > ?"
        params: list[Any] = [int(after_id)]
        if session_id:
            # NULL = 旧库/手工写入的回执;水位线也算上它们,否则护栏会把
            # “真的写了”误判成“没写”(2026-10-18 审计)。
            sql += " AND (session_id = ? OR session_id IS NULL)"
            params.append(session_id)
        sql += " ORDER BY id ASC LIMIT ?"
        params.append(int(limit))
        return [_row_to_receipt(row) for row in self.conn.execute(sql, params).fetchall()]

    def last_id(self, *, session_id: str | None = None) -> int:
        """当前最大回执 id —— 一轮开始前记下来,结束时用它捞新增的那些。"""
        if not self._table_ready():
            return 0
        if session_id:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM ops_receipts"
                " WHERE session_id = ? OR session_id IS NULL",
                (session_id,),
            ).fetchone()
        else:
            row = self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM ops_receipts").fetchone()
        return int(row[0] or 0)

    # -- 标记 -------------------------------------------------------------
    def mark_undone(self, receipt_id: int, *, note: str = "") -> None:
        """把回执标成已撤销(**只允许 NULL -> 非 NULL**,触发器强制)。"""
        if not self._table_ready():
            raise ReceiptError("这个库里还没有写入回执表")
        cursor = self.conn.execute(
            "UPDATE ops_receipts SET undone_at = ?, undone_note = ?"
            " WHERE id = ? AND undone_at IS NULL",
            (_utc_now(), note, int(receipt_id)),
        )
        if cursor.rowcount == 0:
            raise ReceiptError("这条回执已经撤销过了: " + str(receipt_id))

    def claim_undone(self, receipt_id: int) -> bool:
        """把回执**认领**成"正在撤销"。返回 False 表示别人已经认领/撤过了。

        ⚠️ 与 mark_undone 的区别:它不写 note,而且调用方**随后**用 finish_undone()
           补上说明。两个入口并发时,只有一方能拿到 True(见 ReceiptReverter.undo)。
        """
        if not self._table_ready():
            raise ReceiptError("这个库里还没有写入回执表")
        cursor = self.conn.execute(
            "UPDATE ops_receipts SET undone_at = ?, undone_note = ?"
            " WHERE id = ? AND undone_at IS NULL",
            (_utc_now(), CLAIM_NOTE, int(receipt_id)),
        )
        return cursor.rowcount == 1

    def finish_undone(self, receipt_id: int, *, note: str = "") -> None:
        """认领之后补上"退回去做了什么"(已认领的行,不重复改 undone_at)。"""
        self.conn.execute(
            "UPDATE ops_receipts SET undone_note = ? WHERE id = ?",
            (note, int(receipt_id)),
        )

    def release_undone(self, receipt_id: int) -> None:
        """撤销中途失败:把认领还回去(回执重新变成"可撤销")。

        ⚠️ 这一步要求触发器允许"收回认领",见迁移 17 —— 而且**只允许**收回
           还带着认领标记(CLAIM_NOTE)的那些行:已经撤完的回执收不回去。
        """
        self.conn.execute(
            "UPDATE ops_receipts SET undone_at = NULL, undone_note = ''"
            " WHERE id = ? AND undone_note = ?",
            (int(receipt_id), CLAIM_NOTE),
        )

    def count_open(self) -> int:
        if not self._table_ready():
            return 0
        row = self.conn.execute(
            "SELECT COUNT(*) FROM ops_receipts WHERE undone_at IS NULL AND undoable = 1"
        ).fetchone()
        return int(row[0] or 0)


def _row_to_receipt(row: sqlite3.Row) -> Receipt:
    def _load(text: Any) -> dict[str, Any]:
        try:
            loaded = json.loads(text or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    return Receipt(
        id=int(row["id"]),
        ts=row["ts"],
        actor=row["actor"],
        kind=row["kind"],
        domain=row["domain"],
        label=row["label"],
        detail=_load(row["detail"]),
        undo_plan=_load(row["undo_plan"]),
        undoable=bool(row["undoable"]),
        undone_at=row["undone_at"],
        undone_note=row["undone_note"],
        session_id=row["session_id"],
        turn=row["turn"],
    )


# ---------------------------------------------------------------- 撤销执行
#: 认领标记。撤销分两步:"先认领(写这个标记)、再动数据"。
#: ⚠️ 迁移 17 的触发器里有一份同名的字符串字面量(SQL 里不能引用 Python 常量),
#:    改一处必须改两处 —— tests/test_receipts_invariants.py 钉着它们一致。
CLAIM_NOTE = "撤销中"

Reverter = Callable[[Receipt], str]


class ReceiptReverter:
    """按 `kind` 分流的撤销执行器。

    ```python
    reverter = ReceiptReverter(log)
    reverter.register("ledger.txn", ledger_service.revert_txn)
    reverter.undo(receipt_id)          # 撤指定的一条
    reverter.undo_last(domain="ledger")  # 撤某域最近一条
    ```

    ⚠️ 注册是**显式**的:没注册 kind 的回执一律不可撤销,并给出明确错误,
    而不是"看起来能点,点了没反应"。
    """

    def __init__(self, log: ReceiptLog) -> None:
        self.log = log
        self._handlers: dict[str, Reverter] = {}
        # 领域 -> 该域自己那本撤销栈(账本的旧路径)。撤完要一起标 used,
        # 否则前端会出现"回执显示已撤销、账本页还在提示可撤销"的分裂。
        self._stacks: dict[str, Any] = {}

    def register(self, kind: str, handler: Reverter) -> "ReceiptReverter":
        self._handlers[kind] = handler
        return self

    def register_undo_stack(self, domain: str, stack: Any) -> "ReceiptReverter":
        self._stacks[domain] = stack
        return self

    def kinds(self) -> list[str]:
        return sorted(self._handlers)

    def undo(
        self, receipt_id: int, *, actor: str = "user", source: str | None = None
    ) -> dict[str, Any]:
        """撤销指定回执。返回 {"receipt": {...}, "note": "..."}。

        source 只有 fallback 包装器会用(指定去 a: / m: 哪一本找),
        单本回执日志忽略它。
        """
        receipt = self.log.get(receipt_id)
        if receipt.undone:
            raise ReceiptError("这条已经撤销过了:" + receipt.label)
        if not receipt.undoable:
            raise ReceiptError("这条写入不可撤销:" + receipt.label)
        handler = self._handlers.get(receipt.kind)
        if handler is None:
            raise ReceiptError("这类写入还没有撤销实现(" + receipt.kind + "):" + receipt.label)

        # ⚠️ 先**认领**,再动数据(2026-10-17 审计)。
        #
        # 旧顺序是 handler 先跑、mark_undone 在后:两个入口(网页 + 微信、或两个标签页)
        # 同时点撤销时,双方都读到 undone=False,都会执行一遍 reverter —— 而"已经撤过"
        # 的错是在**副作用之后**才抛出来的。账本的软删除重放一次问题不大,但人格/记忆
        # 这类"写回旧值"的二次撤销会盖掉更新的内容。
        #
        # claim_undone 是 UPDATE ... WHERE undone_at IS NULL,只有一方能拿到 rowcount=1。
        if not self.log.claim_undone(receipt.id):
            raise ReceiptError("这条已经撤销过了:" + receipt.label)
        try:
            note = handler(receipt)
        except Exception:
            # 撤到一半失败:把回执**放回去**,让人能重试 —— 撤回一半比不撤更危险,
            # 但"回执被标成已撤销、数据没退"是更难查的坏状态。
            self.log.release_undone(receipt.id)
            raise
        self.log.finish_undone(receipt.id, note=note)
        stack = self._stacks.get(receipt.domain)
        undo_id = receipt.undo_plan.get("undo_log_id")
        if stack is not None and undo_id:
            try:
                stack.mark_used(int(undo_id))
            except Exception:  # 旧栈只是冗余账本,标不上不该让撤销失败
                pass
        self.log.conn.commit()
        return {"receipt": self.log.get(receipt.id).as_dict(), "note": note}

    def can_handle(self, receipt: "Receipt") -> bool:
        """这条回执我撤得了吗(有实现 + 还没撤过)。多本回执账共存时用它选路。"""
        return (
            not receipt.undone
            and receipt.undoable
            and receipt.kind in self._handlers
        )

    def undo_last(self, *, domain: str | None = None, actor: str = "user") -> dict[str, Any]:
        """撤销最近一条**可撤销且未撤销**的回执。没有则抛 ReceiptError。"""
        for receipt in self.log.recent(limit=50):
            if receipt.undone or not receipt.undoable:
                continue
            if domain and receipt.domain != domain:
                continue
            if receipt.kind not in self._handlers:
                continue
            return self.undo(receipt.id, actor=actor)
        raise ReceiptError("没有可撤销的写入")


def _find_receipt(reverter: ReceiptReverter, receipt_id: int) -> Receipt | None:
    try:
        return reverter.log.get(int(receipt_id))
    except ReceiptError:
        return None


def build_fallback_reverter(
    primary: ReceiptReverter, fallback: ReceiptReverter | None
) -> ReceiptReverter:
    """把两本回执账(领域库 / 记忆库)拼成一个撤销入口。

    ⚠️ 为什么必须拼:回执**与它记录的那次写入同库**(见 migrations.py 的说明),
    所以待办/目标/提醒/记忆的回执在 memory.db,账本/日历/健康/错题/产物在 assistant.db。
    而界面只有一个「撤销」按钮 —— 只要有一个域撤不了,那条回执就是**假的**。

    选路规则:先问主本;id 不在主本里(或主本撤不了这种 kind)就交给另一本。
    没找到就返回 None,调用方给出"没这条回执"的明确错误。
    """
    if fallback is None or fallback is primary:
        return primary
    candidates = (primary, fallback)

    def pick(receipt_id: int, source: str | None = None) -> ReceiptReverter:
        # 界面上的 "m:12" / "a:12" 前缀是**消歧**用的:两张表 id 各自从 1 开始,
        # 不带前缀时按"主本优先"会撤到另一个域的写入。给了前缀就只认那本。
        chosen = candidates
        if source == "a":
            chosen = (primary,)
        elif source == "m":
            chosen = (fallback,)
        found = None
        for reverter in chosen:
            receipt = _find_receipt(reverter, receipt_id)
            if receipt is None:
                continue
            if reverter.can_handle(receipt):
                return reverter
            found = reverter
        if found is not None:
            return found          # 让 undo() 去报"已经撤过 / 这类写入没有实现"
        raise ReceiptError(
            "没有这条写入回执: " + str(receipt_id) + "(两张回执表里都查不到)"
        )

    class _FallbackReverter(ReceiptReverter):
        """只覆写"选哪一本";撤销语义仍然是各域自己注册的那份。"""

        def undo(
            self, receipt_id: int, *, actor: str = "user", source: str | None = None
        ) -> dict[str, Any]:
            return pick(receipt_id, source).undo(receipt_id, actor=actor)

        def undo_last(self, *, domain: str | None = None, actor: str = "user") -> dict[str, Any]:
            for reverter in candidates:
                try:
                    return reverter.undo_last(domain=domain, actor=actor)
                except ReceiptError:
                    continue
            raise ReceiptError("没有可撤销的写入")

        def kinds(self) -> list[str]:
            kinds: set[str] = set()
            for reverter in candidates:
                kinds.update(reverter.kinds())
            return sorted(kinds)

    wrapper = _FallbackReverter(primary.log)
    for reverter in candidates:
        wrapper._handlers.update(reverter._handlers)  # noqa: SLF001 - 同一模块内的装配
        wrapper._stacks.update(reverter._stacks)      # noqa: SLF001
    return wrapper


def format_receipts(receipts: list[Receipt], *, limit: int = 10) -> str:
    """把回执渲染成人看的几行 —— CLI(--receipts / 跑完一轮之后)用它。

    单独一个函数而不是塞在 CLI 里:回执的输出格式属于回执机制本身,
    换成 Web/CLI/将来别的界面时,这句话只该有一份实现。
    """
    if not receipts:
        return "还没有任何写入回执。"
    lines: list[str] = []
    for item in receipts[:limit]:
        mark = "↩ 已撤销" if item.undone else "✎"
        lines.append(
            mark + " #" + str(item.id) + " [" + item.domain + "] " + item.label
            + (("  → " + item.undone_note) if item.undone and item.undone_note else "")
        )
    if len(receipts) > limit:
        lines.append("… 还有 " + str(len(receipts) - limit) + " 条")
    return "\n".join(lines)


__all__ = [
    "DOMAINS",
    "DOMAIN_CALENDAR",
    "DOMAIN_HEALTH",
    "DOMAIN_LEDGER",
    "DOMAIN_MEMORY",
    "DOMAIN_STUDY",
    "DOMAIN_SYSTEM",
    "DOMAIN_TASK",
    "Receipt",
    "ReceiptError",
    "ReceiptLog",
    "ReceiptReverter",
    "format_receipts",
]
