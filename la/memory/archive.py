"""M3 档案层:JSONL 事件日志 —— 记忆的事实源。

不变量(设计深化 §1.1,第一条就写进代码):
  1. 事件日志**永不删除、永不修改**:追加式 JSONL;
  2. 任何"压缩/合并/回填"只产生新事件,并在 data 里记录 supersedes 引用;
  3. memory.db 里的 events 表只是镜像,坏了可以删掉重放重建。
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

from .. import config

# 事件类型 v1(设计深化 §1.2)。surface 决定它是否进入对话投影。
EVENT_TYPES: frozenset[str] = frozenset(
    {
        "session/start",
        "user/message",
        "assistant/message",
        # ⚠️ 预留:流式增量目前**不落盘**(只走 SSE 给前端),所以这个类型永远是 0 条。
        #    留着是因为设计深化 §1.2 的事件清单里有它,将来要落盘时不用改白名单。
        "assistant/chunk",
        "tool/result",
        "memory/note",
        "memory/retrieval",
        "compaction/start",
        "compaction/summary",
        # ⭐ 2026-11-21(D185):一次性重生成旧摘要的审计事件(不 surface)。
        "summary/regenerated",
        # 压缩在生成期间发现区间被追加过 -> 主动放弃这一轮(设计深化 §1.3-③ 的 assertStable)
        "compaction/skipped",
        # 巩固重试 2 次仍失败(设计深化 §1.5:写事件后丢弃,不阻塞主流程)
        "consolidate/failed",
        "subagent/result",
        # 未绑定的微信联系人第一次说话:只记标识,供设置页列出待绑定。
        "bridge/unbound",
        "phase/transition",
        "goal/change",
        "schedule/change",
        "todo/change",
        "dispatch",
        "ledger.txn.created",
        "ledger.txn.confirmed",
        "ledger.txn.void",
        "ledger.txn.corrected",
        "ledger.refund.linked",
        "ledger.import.completed",
        "ledger.category.created",
        "feedback/asked",
        "study.problem.added",
        "study.problem.confirmed",
        "study.review.recorded",
        "health.measurement.logged",
        "health.workout.logged",
        "health.food.logged",
        # ⭐ 每一次模型调用(含一轮里的工具往返)的 token 账 —— 使用报表(D136)的事实源。
        #    不 surface:它是观测数据,不是聊过的内容(也不该进对话投影)。
        "llm/usage",
        # ⭐ D148:联网的**请求账**(Search / Fetch 按成功请求计费,5 元/1000 次)。
        #    它不在 token 账里,所以必须单独一类事件;同样不 surface。
        "web/usage",
        "turn/start",
        "turn/end",
        # 模型说了"已经写好"、但这一轮一条回执都没有(见 la/agent/session.py 的护栏与 D113)
        "turn/write-claim-without-write",
        # ⭐ M33 笔记域:领域 -> 记忆的单向桥(经 ops_outbox 搬过来)。
        #    它们是**记忆"知道"笔记存在**的唯一途径,但笔记正文永远不在这里 ——
        #    只带 id / 夹 / 标题 / 日期(全文去 assistant.db 查)。
        "journal/note",
        "journal/folder",
        # 每轮自动召回的留痕:命中了哪几条。**不进投影**,只给排查用 ——
        # 它绕过了工具管线(无门禁/无预算),不记就查不出"当时为什么没看见"。
        "journal/hint",
    }
)

# 默认是否 surface(不 surface 的事件不进入投影,但永远留在日志里)
SURFACE_DEFAULT: dict[str, bool] = {
    "session/start": False,
    "user/message": True,
    "assistant/message": True,
    "assistant/chunk": False,
    "tool/result": True,
    "memory/note": False,
    "memory/retrieval": False,
    "compaction/start": False,
    "compaction/summary": True,
    "summary/regenerated": False,
    "compaction/skipped": False,
    "consolidate/failed": False,
    "subagent/result": False,
    "bridge/unbound": False,
    "phase/transition": False,
    "goal/change": False,
    "schedule/change": False,
    "todo/change": False,
    "dispatch": False,
    "ledger.txn.created": False,
    "ledger.txn.confirmed": False,
    "ledger.txn.void": False,
    "ledger.txn.corrected": False,
    "ledger.refund.linked": False,
    "ledger.import.completed": False,
    "ledger.category.created": False,
    # 提问本身不进投影(它是执行轨迹,不是聊过的内容)——
    # 用户看到的"它问过我什么"由 ops_questions 那张表负责。
    "feedback/asked": False,
    "study.problem.added": False,
    "study.problem.confirmed": False,
    "study.review.recorded": False,
    "health.measurement.logged": False,
    "health.workout.logged": False,
    "health.food.logged": False,
    "llm/usage": False,
    "web/usage": False,
    "turn/start": False,
    "turn/end": False,
    "turn/write-claim-without-write": False,
    "journal/note": False,
    "journal/folder": False,
    "journal/hint": False,
}


def now_iso() -> str:
    return datetime.now(config.get_settings().tz).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Event:
    seq: int
    type: str
    time: str
    data: dict[str, Any] = field(default_factory=dict)
    surface: bool = True
    # 只由 MemoryStore 从库里读出来时填充;不写进 JSONL(文件名已经带了会话 id)
    session_id: str | None = None

    def to_line(self) -> str:
        payload = {
            "seq": self.seq,
            "type": self.type,
            "time": self.time,
            "surface": self.surface,
            "data": self.data,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_line(cls, line: str) -> "Event":
        payload = json.loads(line)
        return cls(
            seq=int(payload["seq"]),
            type=str(payload["type"]),
            time=str(payload["time"]),
            data=payload.get("data") or {},
            surface=bool(payload.get("surface", True)),
        )


#: 文件名里保留下来的字符。其余字符(尤其是 Windows 的 `:`)一律 percent-encode。
#: 为什么不用 `_` 直接替换:`bridge:a` 和 `bridge_a` 会撞名;percent-encoding 可逆、无碰撞。
_SAFE_NAME_CHARS = "@._-"


def archive_filename(session_id: str) -> str:
    """把会话 id 变成跨平台安全的 JSONL 文件名。

    ⚠️ 不能直接把 session_id 拼进路径:Windows 上 `bridge:o9cq…` 会被解释成
       NTFS 备用数据流(ADS),落盘变成一个 0 字节的 `bridge` 文件 + 一个流;
       资源管理器、zip、robocopy 都看不见它,备份会静默漏掉整个微信档案。
    """
    return quote(str(session_id), safe=_SAFE_NAME_CHARS) + ".jsonl"


#: 抢档案写锁的最长等待。家庭场景下每次追加都是毫秒级,15 秒足够兜底。
_LOCK_TIMEOUT_S = 15.0


@contextlib.contextmanager
def _locked_file(path: Path, *, timeout: float = _LOCK_TIMEOUT_S):
    """对 ``<path>.lock`` 加进程间排他锁。

    为什么锁必须落在文件上:Web 与微信是**两个进程**,各自有自己的
    ``EventLog._seq`` 缓存。不锁的话两边会同时从同一个尾号 +1,写出重复 seq;
    而 ``MemoryStore.append`` 是 ``INSERT OR REPLACE`` —— 后写的会静默覆盖先写的。
    锁文件常驻,不删除:删除一个别的进程正持有的锁文件会让互斥失效。
    """
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + max(float(timeout), 0.0)
        if os.name == "nt":
            import msvcrt

            while True:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("档案写锁超时: " + str(lock_path))
                    time.sleep(0.02)
            try:
                yield
            finally:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("档案写锁超时: " + str(lock_path))
                    time.sleep(0.02)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class EventLog:
    """单个会话的事件日志(追加式 JSONL)。"""

    def __init__(self, session_id: str, directory: Path | None = None) -> None:
        self.session_id = session_id
        self.directory = Path(directory) if directory is not None else config.ARCHIVE_DIR
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / archive_filename(session_id)
        #: 旧版本用 `session_id + ".jsonl"`;在 Windows 上,带冒号的 id 会落到 ADS。
        self.legacy_path = self.directory / (session_id + ".jsonl")
        self._migrated_from_legacy = self._migrate_legacy()

    @property
    def migrated_from_legacy(self) -> bool:
        """本次打开时是否把旧路径(可能是 ADS)迁移成了普通文件。"""
        return self._migrated_from_legacy

    def _migrate_legacy(self) -> bool:
        """把旧路径的档案**复制**到新文件名;旧文件/流保留不动(归档只增不改)。"""
        legacy = self.legacy_path
        if legacy == self.path or self.path.exists():
            return False
        try:
            if not legacy.exists():
                return False
            data = legacy.read_bytes()
        except OSError:
            return False
        if not data:
            return False
        with _locked_file(self.path):
            # 拿到锁后再确认一次:另一个进程可能刚迁移完,并且已经开始追加。
            if self.path.exists():
                return False
            tmp = self.path.with_name(self.path.name + "." + str(os.getpid()) + ".tmp")
            try:
                tmp.write_bytes(data)
                os.replace(tmp, self.path)
            finally:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)
        return True

    # -- 读 ---------------------------------------------------------------
    def _last_seq_on_disk(self) -> int:
        """读文件尾部的最后一个 seq。

        只读最后 64KB:正常情况下最后一行就在里面;如果尾部是坏行/半行,
        先往前找完整行,实在找不到再退回全文件扫描。
        """
        try:
            with self.path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                end = handle.tell()
                if end <= 0:
                    return 0
                size = min(end, 64 * 1024)
                handle.seek(end - size)
                chunk = handle.read(size)
        except OSError:
            return 0
        for raw in reversed(chunk.splitlines()):
            line = raw.strip()
            if not line:
                continue
            try:
                return int(json.loads(line)["seq"])
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
        return self._scan_full_last_seq()

    def _scan_full_last_seq(self) -> int:
        if not self.path.exists():
            return 0
        last = 0
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    last = int(json.loads(line)["seq"])
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue  # 坏行不影响既有事实
        return last

    @property
    def last_seq(self) -> int:
        return self._last_seq_on_disk()

    def read(self, start_seq: int = 1, end_seq: int | None = None) -> list[Event]:
        events: list[Event] = []
        if not self.path.exists():
            return events
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = Event.from_line(line)
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
                if event.seq < start_seq:
                    continue
                if end_seq is not None and event.seq > end_seq:
                    break
                events.append(event)
        return events

    def iter_surface(self, start_seq: int = 1, end_seq: int | None = None) -> Iterator[Event]:
        for event in self.read(start_seq, end_seq):
            if event.surface:
                yield event

    # -- 写(唯一写入路径)-------------------------------------------------
    def append(
        self,
        event_type: str,
        data: dict[str, Any] | None = None,
        *,
        surface: bool | None = None,
        time: str | None = None,
    ) -> Event:
        if event_type not in EVENT_TYPES:
            raise ValueError("未知事件类型: " + event_type)
        # 锁的范围必须覆盖「读尾号 + 追加」两步:只锁写会让两个进程读到同一个尾号。
        with _locked_file(self.path):
            event = Event(
                seq=self._last_seq_on_disk() + 1,
                type=event_type,
                time=time or now_iso(),
                data=data or {},
                surface=SURFACE_DEFAULT.get(event_type, True) if surface is None else surface,
            )
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(event.to_line() + "\n")
        return event
