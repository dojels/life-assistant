"""统一反馈机制:让**模型**也能问用户一句,并把回答接回它自己那一轮。

为什么要有它(它和已有的两种「反馈」不是一回事):
  * **写入回执**(ops_receipts)解决「写完了告诉你,不对就撤」 —— 事后告知;
  * **风险审批**(risk=confirm)解决「这个动作放不放行」 —— 事中拦截,
    但它只有 yes/no,而且**只有代码层**发得出来;
  * **本机制**解决「这件事只有用户知道答案」 —— 这个新分类叫什么、两笔账哪笔对、
    这一笔记收入还是支出。发起方既可以是领域代码,也可以是**模型自己**(ask_user 工具)。

三条不变量:
  1. **一个答案只生效一次** —— 第一次回答赢,之后一律拒绝(数据库触发器兜底);
  2. **等待有上限** —— 用户不回答不能把一轮对话永远挂住;超时就如实返回「没回答」,
     让模型自己降级(而不是让它以为用户默认同意了);
  3. **问题必须留痕** —— 落 ops_questions。刷新页面、换设备,还能看到「它在等你回答什么」;
     但**能不能续上那一轮**取决于当时那个进程还在不在,这一点如实告诉用户。

数据流:

    领域工具 / ask_user 工具
        └─> MemorySession.ask(Question)
              ├─> QuestionBroker.ask()       登记 + 落库(表不在就降级,只留内存)
              ├─> notify(question)           传输层:Web 推 SSE ask 帧 / CLI 打印
              └─> QuestionBroker.wait()      等 POST /api/questions/answer
                    └─> Answer{selected, text}
                          └─> 作为**工具结果**回到模型那一轮,它接着往下做

⚠️ 进程级单例是必须的,不是偷懒:提问发生在 SSE 那一轮请求里,
   回答从**另一个** HTTP 请求进来 —— 两者不共享任何对象,只能靠进程内的登记处。

⚠️ **跨进程**(2026-10-13 真机踩到之后补的):网页、微信桥接、提醒推送各是一个进程,
   而"在 A 进程提问、在 B 进程回答"是常态(问题落在共享的 ops_questions 里,
   网页看得见,微信里也发得出去)。所以两个等待方(QuestionBroker.wait 与桥接层的
   Thread._ask)**每隔一秒看一眼库**:那边一旦落定,这边就"认领"它 —— 详见
   QuestionBroker.adopt()。只有"两个进程同时把同一个问题标成终态"才靠数据库触发器兜底。
"""

from __future__ import annotations
from ..base.clock import utc_now_iso as _utc_now

import asyncio
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from ..domain.receipts import DOMAIN_SYSTEM, DOMAINS

# 回答的状态。**闭集**:前端与模型都按这几个值分支。
STATUS_PENDING = "pending"
STATUS_ANSWERED = "answered"
STATUS_EXPIRED = "expired"          # 等超时了,或者压根没人接
STATUS_CANCELLED = "cancelled"      # 用户点了停止 / 连接断了
STATUS_UNAVAILABLE = "unavailable"  # 这个会话没有接提问通道(测试、纯批处理)

TERMINAL_STATUSES = (STATUS_ANSWERED, STATUS_EXPIRED, STATUS_CANCELLED)
# 默认等 5 分钟。再长就是把一轮对话挂死,再短用户来不及看见。
DEFAULT_TIMEOUT_S = 300.0
MAX_OPTIONS = 6

#: 跨进程等待的轮询间隔(秒)。回答可能来自**另一个进程**(网页 / 微信 / 终端),
#: 那边改的是数据库,不是这里的 asyncio.Event —— 所以等待方要有节奏地看库。
#: 1 秒:人感觉不到延迟,又不会把 SQLite 读热。
CROSS_PROCESS_POLL_S = 1.0




@dataclass
class Option:
    """一个可点的选项。value 缺省等于 label —— 模型给的通常就是一句话。"""

    label: str
    description: str = ""
    value: str = ""

    def key(self) -> str:
        return self.value or self.label

    def as_dict(self) -> dict[str, str]:
        return {"label": self.label, "description": self.description, "value": self.key()}


@dataclass
class Question:
    """一次提问。**纯数据** —— 传输与等待都在 Broker 里。"""

    prompt: str
    options: list[Option] = field(default_factory=list)
    allow_text: bool = True
    multi: bool = False
    domain: str = DOMAIN_SYSTEM
    context: dict[str, Any] = field(default_factory=dict)
    source: str = "agent"          # agent(模型发起)| domain(领域代码发起)
    session_id: str | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    id: str = ""
    ts: str = ""
    expires_at: str = ""
    status: str = STATUS_PENDING

    def prepare(self) -> "Question":
        """补齐 id / 时间戳,并做上限校验。Broker 在登记前调用一次。"""
        if not self.id:
            self.id = "q_" + uuid.uuid4().hex[:12]
        if not self.ts:
            self.ts = _utc_now()
        if self.domain not in DOMAINS:
            self.domain = DOMAIN_SYSTEM
        self.prompt = (self.prompt or "").strip()[:400] or "(它想问你一件事)"
        self.options = [
            item if isinstance(item, Option) else Option(**dict(item))
            for item in (self.options or [])
        ][:MAX_OPTIONS]
        return self

    def as_dict(self) -> dict[str, Any]:
        """给前端(SSE 帧 / 模板)的形状。字段名稳定 —— 前端只认这些。"""
        return {
            "id": self.id,
            "ts": self.ts,
            "expires_at": self.expires_at,
            "session_id": self.session_id,
            "domain": self.domain,
            "source": self.source,
            "prompt": self.prompt,
            "options": [item.as_dict() for item in self.options],
            "allow_text": bool(self.allow_text),
            "multi": bool(self.multi),
            "context": self.context,
            "status": self.status,
        }


@dataclass
class Answer:
    """用户(或超时)给出的回答。selected 是选项 value 的列表。"""

    question_id: str = ""
    status: str = STATUS_ANSWERED
    selected: list[str] = field(default_factory=list)
    text: str = ""
    by: str = "user"          # user | timeout | system
    note: str = ""
    answered_at: str = ""

    @property
    def answered(self) -> bool:
        return self.status == STATUS_ANSWERED and (
            bool(self.selected) or bool(self.text.strip())
        )

    def chosen(self) -> str:
        """单选场景下的那一个答案(没有就是空串)。"""
        return self.selected[0] if self.selected else ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "status": self.status,
            "selected": list(self.selected),
            "text": self.text,
            "by": self.by,
            "note": self.note,
            "answered_at": self.answered_at,
        }


# --------------------------------------------------------------------------
# 落库:ops_questions(表不存在就降级 —— 提问机制坏了不能把领域写入一起拖死)
# --------------------------------------------------------------------------
class QuestionLog:
    """问题的持久化。只负责「登记 / 记答案 / 读」,不负责等待。"""

    def __init__(self, conn: sqlite3.Connection | None) -> None:
        self.conn = conn
        # ⚠️ 只记"在"(2026-10-17 审计,与 ReceiptLog 同一条口径):
        #    把"不在"也永久记住,会让一次瞬时失败变成"这个进程从此提不了问题"。
        self._ready: bool = False
        self._next_probe: float = 0.0
        self._RETRY_S = 5.0

    def _table_ready(self) -> bool:
        if self.conn is None:
            return False
        if self._ready:
            return True
        now = time.monotonic()
        if self._next_probe and now < self._next_probe:
            return False
        try:
            self.conn.execute("SELECT 1 FROM ops_questions LIMIT 1").fetchone()
            self._ready = True
            return True
        except sqlite3.Error:
            self._next_probe = now + self._RETRY_S
            return False

    def record(self, question: Question) -> bool:
        """登记一条待回答的问题。返回是否真的落库了。"""
        if not self._table_ready():
            return False
        assert self.conn is not None
        self.conn.execute(
            "INSERT OR REPLACE INTO ops_questions(id, ts, expires_at, session_id, domain,"
            " source, prompt, options, context, allow_text, multi, status, answer, answered_at,"
            " answered_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)",
            (
                question.id,
                question.ts,
                question.expires_at,
                question.session_id,
                question.domain,
                question.source,
                question.prompt,
                json.dumps([item.as_dict() for item in question.options], ensure_ascii=False),
                json.dumps(question.context or {}, ensure_ascii=False, default=str),
                1 if question.allow_text else 0,
                1 if question.multi else 0,
                question.status,
            ),
        )
        self.conn.commit()
        return True

    def resolve(self, question_id: str, answer: Answer) -> bool:
        """把一条问题标成终态。⚠️ 只允许 pending -> 终态(触发器兜底)。"""
        if not self._table_ready():
            return False
        assert self.conn is not None
        cursor = self.conn.execute(
            "UPDATE ops_questions SET status = ?, answer = ?, answered_at = ?, answered_by = ?"
            " WHERE id = ? AND status = 'pending'",
            (
                answer.status,
                json.dumps(answer.as_dict(), ensure_ascii=False, default=str),
                answer.answered_at or _utc_now(),
                answer.by,
                question_id,
            ),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def get(self, question_id: str) -> Question | None:
        if not self._table_ready():
            return None
        assert self.conn is not None
        row = self.conn.execute(
            "SELECT * FROM ops_questions WHERE id = ?", (question_id,)
        ).fetchone()
        return _row_to_question(row) if row is not None else None

    def recent(
        self, *, limit: int = 20, session_id: str | None = None, only_pending: bool = False
    ) -> list[tuple[Question, Answer | None]]:
        """最近的问题(新的在前)。返回 (问题, 回答) 二元组。"""
        if not self._table_ready():
            return []
        assert self.conn is not None
        where: list[str] = []
        params: list[Any] = []
        if session_id:
            where.append("session_id = ?")
            params.append(session_id)
        if only_pending:
            where.append("status = 'pending'")
        sql = "SELECT * FROM ops_questions"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        out: list[tuple[Question, Answer | None]] = []
        for row in self.conn.execute(sql, params).fetchall():
            out.append((_row_to_question(row), _row_to_answer(row)))
        return out

    def is_terminal(self, question_id: str) -> bool:
        """这条问题在库里是不是已经落定了(被别的入口答过)。

        用于回答方判断:"这次回答要不要写库" —— 库里已是终态就只认领,不再写。
        """
        if not self._table_ready():
            return False
        assert self.conn is not None
        row = self.conn.execute(
            "SELECT status FROM ops_questions WHERE id = ?", (question_id,)
        ).fetchone()
        return row is not None and str(row["status"]) != STATUS_PENDING

    def answer_of(self, question_id: str) -> Answer | None:
        """读一条问题当前的回答;**只有终态才返回**,还在等就返回 None。

        跨进程等待方(桥接层 / 网页)靠它把"别人答的"接回来。
        """
        if not self._table_ready():
            return None
        assert self.conn is not None
        row = self.conn.execute(
            "SELECT * FROM ops_questions WHERE id = ?", (question_id,)
        ).fetchone()
        if row is None or str(row["status"]) == STATUS_PENDING:
            return None
        answer = _row_to_answer(row)
        if answer is None:
            # 终态但没有答案体(例如被标成 expired):给一个结构化结果,别假装有内容
            return Answer(
                question_id=question_id,
                status=str(row["status"]),
                by="system",
                note="这个问题在别处被标成了 " + str(row["status"]),
            )
        return answer

    def expire_pending(self, session_id: str, *, note: str) -> int:
        """把某个会话里**还在等**的问题批量标成 expired,返回条数。

        ⚠️ 用在"等待方已经不在了"的场合(进程重启):等待只活在发起提问的那个进程里,
        进程一走,那轮对话就再也接不上了。不标的话,网页上会**永远**挂着一条
        「它在等你拍板」,而其实没有任何人在等 —— 正是 D98 里那个"说不清的结果"。
        """
        if not self._table_ready():
            return 0
        assert self.conn is not None
        answer = Answer(
            status=STATUS_EXPIRED, by="system", note=note, answered_at=_utc_now()
        )
        cursor = self.conn.execute(
            "UPDATE ops_questions SET status = ?, answer = ?, answered_at = ?, answered_by = ?"
            " WHERE session_id = ? AND status = 'pending'",
            (
                STATUS_EXPIRED,
                json.dumps(answer.as_dict(), ensure_ascii=False),
                answer.answered_at,
                "system",
                session_id,
            ),
        )
        self.conn.commit()
        return int(cursor.rowcount or 0)

    def count_pending(self, session_id: str | None = None) -> int:
        if not self._table_ready():
            return 0
        assert self.conn is not None
        if session_id:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM ops_questions WHERE status = 'pending'"
                " AND session_id = ?",
                (session_id,),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM ops_questions WHERE status = 'pending'"
            ).fetchone()
        return int(row[0] or 0)


def _safe_load(text: Any, fallback: Any) -> Any:
    try:
        loaded = json.loads(text or "")
    except (TypeError, json.JSONDecodeError):
        return fallback
    return loaded if isinstance(loaded, type(fallback)) else fallback


def _row_to_answer(row: sqlite3.Row) -> Answer | None:
    loaded = _safe_load(row["answer"], {})
    if not loaded:
        return None
    return Answer(
        question_id=str(loaded.get("question_id") or row["id"]),
        status=str(loaded.get("status") or STATUS_ANSWERED),
        selected=[str(item) for item in (loaded.get("selected") or [])],
        text=str(loaded.get("text") or ""),
        by=str(loaded.get("by") or "user"),
        note=str(loaded.get("note") or ""),
        answered_at=str(loaded.get("answered_at") or ""),
    )


def _row_to_question(row: sqlite3.Row) -> Question:
    options = [
        Option(
            label=str(item.get("label") or ""),
            description=str(item.get("description") or ""),
            value=str(item.get("value") or ""),
        )
        for item in _safe_load(row["options"], [])
        if isinstance(item, dict)
    ]
    return Question(
        id=str(row["id"]),
        ts=str(row["ts"]),
        expires_at=str(row["expires_at"] or ""),
        session_id=row["session_id"],
        domain=str(row["domain"]),
        source=str(row["source"] or "agent"),
        prompt=str(row["prompt"]),
        options=options,
        context=_safe_load(row["context"], {}),
        allow_text=bool(row["allow_text"]),
        multi=bool(row["multi"]),
        status=str(row["status"]),
    )


# --------------------------------------------------------------------------
# 登记处:进程内等待 + 跨请求唤醒
# --------------------------------------------------------------------------
@dataclass
class _Pending:
    question: Question
    event: asyncio.Event
    loop: Any
    answer: Answer | None = None


class QuestionBroker:
    """进程内的问题登记处:登记、唤醒、超时、取消。

    ⚠️ 线程安全靠一把锁 + loop.call_soon_threadsafe —— 回答那个 HTTP 端点跑在
    FastAPI 的线程池里,而等待发生在 SSE 那条协程里,**不能直接 set()**。
    """

    # 已经落定的问题保留一小段:用来回答"这个问题是不是已经答过了"。
    # ⚠️ 只留最近 200 条,内存不随对话无限长。
    SETTLED_KEEP = 200

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._settled: dict[str, Answer] = {}

    # -- 登记与等待 -------------------------------------------------------
    def ask(
        self,
        question: Question,
        *,
        log: QuestionLog | None = None,
        notify: Callable[[Question], None] | None = None,
    ) -> Question:
        """登记一个问题并通知传输层。**不阻塞** —— 等的是 wait()。"""
        prepared = question.prepare()
        prepared.status = STATUS_PENDING
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:      # 同步上下文里提问:没人能等,只落库与通知
            loop = None
        if log is not None:
            try:
                log.record(prepared)
            except Exception:     # 落库失败不影响提问本身
                pass
        if loop is not None:
            with self._lock:
                self._pending[prepared.id] = _Pending(
                    question=prepared, event=asyncio.Event(), loop=loop
                )
        if notify is not None:
            try:
                notify(prepared)
            except Exception:
                pass
        return prepared

    async def wait(
        self, question_id: str, *, timeout_s: float | None = None, log: QuestionLog | None = None
    ) -> Answer:
        """等一个回答。超时/被取消都返回**结构化的终态**,不抛异常(除了取消)。"""
        with self._lock:
            entry = self._pending.get(question_id)
        if entry is None:
            # ⚠️ 不在本进程的登记处里,不等于"没人答":提问可能来自**另一个进程**
            #    (微信桥接),而回答就落在这张共享的表里。先看库,再看不了才回 unavailable。
            try:
                stored = log.answer_of(question_id) if log is not None else None
            except Exception:
                stored = None
            if stored is not None and stored.status in TERMINAL_STATUSES:
                return stored
            return Answer(
                question_id=question_id, status=STATUS_UNAVAILABLE, by="system",
                note="这个问题没人接(进程重启了,或者不是在提问的那个进程里回答的)",
            )
        limit = float(timeout_s if timeout_s is not None else entry.question.timeout_s)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(limit, 1.0)
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    answer = Answer(
                        question_id=question_id, status=STATUS_EXPIRED, by="timeout",
                        note="等了 " + str(int(limit)) + " 秒没有回答",
                    )
                    self._settle(question_id, answer, log=log)
                    return answer
                try:
                    await asyncio.wait_for(
                        entry.event.wait(), timeout=min(remaining, CROSS_PROCESS_POLL_S)
                    )
                except asyncio.TimeoutError:
                    # 这一秒没人叫醒 —— 看一眼库里是不是**别的进程**已经答过了。
                    # ⚠️ 读库失败(锁住/磁盘抖)绝不能打断提问:看不到就接着等。
                    try:
                        stored = log.answer_of(question_id) if log is not None else None
                    except Exception:
                        stored = None
                    if stored is not None and stored.status in TERMINAL_STATUSES:
                        return self.adopt(question_id, stored)
                    continue
                break
        except asyncio.CancelledError:
            self._settle(
                question_id,
                Answer(question_id=question_id, status=STATUS_CANCELLED, by="system",
                       note="这一轮被中断了(点了停止或断开连接)"),
                log=log,
            )
            raise
        # ⚠️ 用**捕获到的那个 entry**,不要再从 _pending 里取一次:
        #    回答方在唤醒之前就已经把它摘出去了(实测踩过 —— 会误报"没人回答")。
        if entry.answer is None:
            return Answer(
                question_id=question_id, status=STATUS_UNAVAILABLE, by="system",
                note="这个问题没有拿到回答",
            )
        return entry.answer

    # -- 回答 -------------------------------------------------------------
    def answer(
        self,
        question_id: str,
        *,
        selected: list[str] | None = None,
        text: str = "",
        by: str = "user",
        log: QuestionLog | None = None,
    ) -> Answer:
        """从**另一个请求**里回答。幂等:第二次回答会被拒绝(不覆盖第一次)。"""
        return self._settle(
            question_id,
            Answer(
                question_id=question_id,
                status=STATUS_ANSWERED,
                selected=[str(item) for item in (selected or []) if str(item).strip()],
                text=(text or "").strip(),
                by=by,
                answered_at=_utc_now(),
            ),
            log=log,
        )

    def adopt(self, question_id: str, answer: Answer) -> Answer:
        """认领一个**别的进程已经落定**的答案。

        与 answer() 的区别只有一条,但很关键:**它不写库**。库里已经是终态了
        (是网页/终端那个进程写的),再写一次会拿到 "重复回答" ——
        调用方会把 by=duplicate 当成"没人回答",于是一个已经答过的问题被当成超时。
        它只做两件事:把等待方叫醒、把这条记进已落定表。
        """
        with self._lock:
            entry = self._pending.pop(question_id, None)
            if entry is not None:
                entry.answer = answer
            if answer.status in TERMINAL_STATUSES:
                self._settled[question_id] = answer
                while len(self._settled) > self.SETTLED_KEEP:
                    self._settled.pop(next(iter(self._settled)), None)
        if entry is not None:
            self._wake(entry)
        return answer

    def expire(self, question_id: str, *, note: str = "", log: QuestionLog | None = None) -> Answer:
        return self._settle(
            question_id,
            Answer(question_id=question_id, status=STATUS_EXPIRED, by="system", note=note),
            log=log,
        )

    def cancel(self, question_id: str, *, note: str = "", log: QuestionLog | None = None) -> Answer:
        return self._settle(
            question_id,
            Answer(question_id=question_id, status=STATUS_CANCELLED, by="system", note=note),
            log=log,
        )

    def _settle(self, question_id: str, answer: Answer, *, log: QuestionLog | None) -> Answer:
        """落定一个终态:登记处 + 落库,**第一次赢**。

        by="duplicate" 表示"这个问题早就处理过了" —— 调用方据此回绝重复回答,
        而不是把它当成一次新的回答(否则两个浏览器各点一下就会各执行一遍)。
        """
        with self._lock:
            entry = self._pending.pop(question_id, None)
            if entry is not None:
                if entry.answer is not None:      # 已经有人回答过了
                    return self._duplicate(entry.answer)
                if answer.status == STATUS_ANSWERED:
                    entry.answer = answer
            else:
                previous = self._settled.get(question_id)
                if previous is not None:
                    return self._duplicate(previous)
        if log is not None:
            try:
                if not log.resolve(question_id, answer):
                    # 库里已经是终态:以库里那份为准(它才是唯一真相)
                    stored = log.get(question_id)
                    if stored is not None and stored.status != STATUS_PENDING:
                        return self._duplicate(
                            Answer(question_id=question_id, status=stored.status or STATUS_ANSWERED,
                                   by="db", note="这个问题已经处理过了")
                        )
            except Exception:
                pass
        if answer.status in TERMINAL_STATUSES:
            with self._lock:
                self._settled[question_id] = answer
                while len(self._settled) > self.SETTLED_KEEP:
                    self._settled.pop(next(iter(self._settled)), None)
        if entry is not None:
            self._wake(entry)
        return answer

    @staticmethod
    def _duplicate(previous: Answer) -> Answer:
        return Answer(
            question_id=previous.question_id or "",
            status=previous.status,
            selected=list(previous.selected),
            text=previous.text,
            by="duplicate",
            note="这个问题已经回答过了" + ((":" + previous.chosen()) if previous.chosen() else ""),
        )

    def _wake(self, entry: _Pending) -> None:
        """把等待方叫醒。跨线程必须 call_soon_threadsafe —— 见类注释。"""
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        try:
            if running is entry.loop:
                entry.event.set()
            else:
                entry.loop.call_soon_threadsafe(entry.event.set)
        except Exception:
            pass

    # -- 观测 -------------------------------------------------------------
    def waiting(self) -> list[Question]:
        with self._lock:
            return [item.question for item in self._pending.values()]

    def is_waiting(self, question_id: str) -> bool:
        with self._lock:
            return question_id in self._pending


_BROKER = QuestionBroker()


def get_broker() -> QuestionBroker:
    """本进程唯一的问题登记处。"""
    return _BROKER


# --------------------------------------------------------------------------
# 装配:一行把「登记 -> 通知 -> 等待 / 交互」接起来
# --------------------------------------------------------------------------
AskHandler = Callable[[Question], Awaitable[Answer]]
Transport = Callable[[Question], Awaitable[Answer]]


def build_asker(
    *,
    log: QuestionLog | None = None,
    notify: Callable[[Question], None] | None = None,
    transport: Transport | None = None,
    session_id: str | None = None,
    broker: QuestionBroker | None = None,
) -> AskHandler:
    """造一个提问处理器,交给 MemorySession 与领域工具用。

    transport 给了就走它(CLI:在终端里问一句);没给就等
    POST /api/questions/answer(Web)。
    """
    board = broker or _BROKER

    async def ask(question: Question) -> Answer:
        question.session_id = question.session_id or session_id
        registered = board.ask(question, log=log, notify=notify)
        if transport is None:
            return await board.wait(
                registered.id, timeout_s=registered.timeout_s, log=log
            )
        try:
            produced = await transport(registered)
        except asyncio.CancelledError:
            board.cancel(registered.id, note="交互被中断", log=log)
            raise
        except Exception as exc:   # 传输层坏了 -> 如实返回,绝不替用户猜
            return board.cancel(
                registered.id, note="提问通道出错:" + type(exc).__name__, log=log
            )
        if produced.status == STATUS_ANSWERED:
            # 库里已经是终态 = 用户在**别的进程**里答的(网页/终端)。
            # 这种情况只能认领,不能再写一次(见 QuestionBroker.adopt)。
            if log is not None and log.is_terminal(registered.id):
                return board.adopt(registered.id, produced)
            return board.answer(
                registered.id, selected=produced.selected, text=produced.text,
                by=produced.by or "user", log=log,
            )
        return board.cancel(registered.id, note=produced.note or "没有拿到回答", log=log)

    return ask


def unavailable_asker(reason: str = "当前会话没有接提问通道") -> AskHandler:
    """没有传输通道时的兜底:如实告诉模型「问不了」,别让它假装用户同意了。"""

    async def ask(question: Question) -> Answer:
        return Answer(
            question_id=question.id, status=STATUS_UNAVAILABLE, by="system", note=reason
        )

    return ask


# 回答状态 -> 给模型的一句话解释。**这是契约的一部分**:
# 说清楚「没回答 != 同意」,否则模型会把超时当成默认放行。
ASK_RESULT_HINT: dict[str, str] = {
    STATUS_ANSWERED: "用户已经回答,按 selected / text 执行。",
    STATUS_EXPIRED: (
        "用户没有在时限内回答,**这不是同意**。"
        "可以按你事先说明过的默认做法继续,但要在回复里讲清楚;拿不准就停下来问他。"
    ),
    STATUS_CANCELLED: "这一轮被用户中断了,**这不是同意**。不要据此写数据。",
    STATUS_UNAVAILABLE: (
        "当前没有可用的提问通道,问不到用户 —— **不要**假定他同意。"
        "改用确定性的做法(比如挑一个已有分类),或者直接说明你需要他确认什么。"
    ),
}


def format_question(question: Question) -> str:
    """人读的一行(CLI / 日志用)。"""
    lines = ["[需要你拍板] " + question.prompt]
    for index, item in enumerate(question.options, start=1):
        suffix = ("  —— " + item.description) if item.description else ""
        lines.append("  " + str(index) + ") " + item.label + suffix)
    if question.allow_text:
        lines.append("  (也可以直接打字回答)")
    return "\n".join(lines)


__all__ = [
    "ASK_RESULT_HINT",
    "Answer",
    "AskHandler",
    "CROSS_PROCESS_POLL_S",
    "DEFAULT_TIMEOUT_S",
    "Option",
    "Question",
    "QuestionBroker",
    "QuestionLog",
    "STATUS_ANSWERED",
    "STATUS_CANCELLED",
    "STATUS_EXPIRED",
    "STATUS_PENDING",
    "STATUS_UNAVAILABLE",
    "build_asker",
    "format_question",
    "get_broker",
    "unavailable_asker",
]
