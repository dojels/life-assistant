"""定时唤醒:任务 + 运行记录 + 待送达报告。

⭐ 与 `schedules`(提醒)刻意分成两张表:
  * 提醒到点只发一句**已经写死的文本**,全程没有模型;
  * 唤醒到点要跑**完整一轮**(检索 + 工具 + 写入),它有自己的失败/重试/配额语义。
  把两者塞进一张表,推送环会分不清"该发文本"还是"该跑模型"。

三条硬约束(与 D 系列同一取向):
  1. **任务只能人建**:这里没有给模型的创建入口,模型永远不能给自己排任务;
  2. **写入是追加型**:删除/移动/取消由 `tools/wake_policy.py` 在工具层拒掉;
  3. **at-most-once**:先抢占 run 行再执行。写到一半崩了就记 unknown,
     绝不自动重跑 ——提醒环可以 at-least-once,因为重复一句文本无害;重复记账有害。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .. import config
from ..base.clock import parse_iso, utc_now_iso

FREQ_DAILY = "daily"
FREQ_WEEKLY = "weekly"
FREQS = (FREQ_DAILY, FREQ_WEEKLY)

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_OK = "ok"
STATUS_REPORTED = "reported"
STATUS_SILENT = "silent"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"
STATUS_SKIPPED = "skipped"
STATUS_UNKNOWN = "unknown"
STATUS_MISSED = "missed"
STATUS_QUOTA = "quota"

REPORT_PENDING = "pending"
REPORT_SENT = "sent"
REPORT_FAILED = "failed"
REPORT_EXPIRED = "expired"

MAX_JOBS_PER_MEMBER = 5
MAX_RUNS_PER_DAY = 5
MAX_TITLE_CHARS = 60
MAX_INSTRUCTION_CHARS = 500
#: 迟到超过这个秒数就不补跑,直接记成"错过"(凌晨补跑昨天的巡检只会打扰人)。
MAX_LATE_S = 6 * 3600.0
#: 报告在微信队列里最多等多久;过期只在网页 /wake 里看。
REPORT_TTL_S = 48 * 3600.0


class WakeError(RuntimeError):
    """唤醒任务的领域错误。"""


def _now() -> str:
    return utc_now_iso()


def _uuid(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:10]


def parse_time_local(text: str) -> tuple[int, int]:
    raw = str(text or "").strip()
    try:
        hour_text, minute_text = raw.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (TypeError, ValueError) as exc:
        raise WakeError("时间要写成 HH:MM,例如 19:00") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise WakeError("时间超范围: " + raw)
    return hour, minute


def next_occurrence(
    now: datetime,
    *,
    freq: str,
    time_local: str,
    weekday: int | None,
    tz: ZoneInfo,
) -> datetime:
    """下一次该跑的时刻(**严格晚于 now**)。

    ⚠️ 不要用"每隔 7 天"的秒数:那样会跨过 DST 漂一小时,而且永远对不齐墙上时间。
       这里按本地日历算,与日历的 RRULE 展开同一取向。
    """
    hour, minute = parse_time_local(time_local)
    moment = now.astimezone(tz)
    candidate = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if freq == FREQ_DAILY:
        if candidate <= moment:
            candidate += timedelta(days=1)
        return candidate
    if freq == FREQ_WEEKLY:
        if weekday is None or not (1 <= int(weekday) <= 7):
            raise WakeError("每周任务要给 weekday(1=周一 … 7=周日)")
        delta = (int(weekday) - moment.isoweekday()) % 7
        candidate += timedelta(days=delta)
        if candidate <= moment:
            candidate += timedelta(days=7)
        return candidate
    raise WakeError("不支持的频率: " + str(freq))


def _aware(value: str | datetime | None, tz: ZoneInfo) -> datetime | None:
    if value is None:
        return None
    moment = parse_iso(value) if isinstance(value, str) else value
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    return moment


@dataclass
class WakeJob:
    id: str
    owner_member_id: int
    title: str
    instruction: str
    freq: str
    time_local: str
    weekday: int | None
    timezone: str
    enabled: bool
    allow_writes: bool
    next_at: str | None
    last_run_at: str | None
    last_status: str
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "WakeJob":
        return cls(
            id=str(row["id"]),
            owner_member_id=int(row["owner_member_id"]),
            title=str(row["title"]),
            instruction=str(row["instruction"]),
            freq=str(row["freq"]),
            time_local=str(row["time_local"]),
            weekday=None if row["weekday"] is None else int(row["weekday"]),
            timezone=str(row["timezone"]),
            enabled=bool(row["enabled"]),
            allow_writes=bool(row["allow_writes"]),
            next_at=row["next_at"],
            last_run_at=row["last_run_at"],
            last_status=str(row["last_status"] or ""),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


@dataclass
class WakeRun:
    id: str
    job_id: str
    title: str
    owner_member_id: int
    scheduled_for: str
    dry_run: bool
    status: str
    started_at: str | None
    finished_at: str | None
    summary: str
    error: str
    writes: int
    receipts: list[int] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    report_id: str = ""
    created_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "WakeRun":
        try:
            receipts = json.loads(row["receipts"] or "[]")
        except (json.JSONDecodeError, TypeError):
            receipts = []
        try:
            usage = json.loads(row["usage"] or "{}")
        except (json.JSONDecodeError, TypeError):
            usage = {}
        return cls(
            id=str(row["id"]),
            job_id=str(row["job_id"]),
            title=str(row["title"] or ""),
            owner_member_id=int(row["owner_member_id"]),
            scheduled_for=str(row["scheduled_for"]),
            dry_run=bool(row["dry_run"]),
            status=str(row["status"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            summary=str(row["summary"] or ""),
            error=str(row["error"] or ""),
            writes=int(row["writes"] or 0),
            receipts=[int(item) for item in receipts if str(item).isdigit()],
            usage=usage if isinstance(usage, dict) else {},
            report_id=str(row["report_id"] or ""),
            created_at=str(row["created_at"] or ""),
        )


@dataclass
class WakeReport:
    id: str
    run_id: str
    job_id: str
    title: str
    owner_member_id: int
    text: str
    status: str
    attempts: int
    created_at: str
    sent_at: str | None
    expires_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "WakeReport":
        return cls(
            id=str(row["id"]),
            run_id=str(row["run_id"]),
            job_id=str(row["job_id"]),
            title=str(row["title"] or ""),
            owner_member_id=int(row["owner_member_id"]),
            text=str(row["text"]),
            status=str(row["status"]),
            attempts=int(row["attempts"] or 0),
            created_at=str(row["created_at"]),
            sent_at=row["sent_at"],
            expires_at=str(row["expires_at"]),
        )


class WakeStore:
    """任务的读写 + 运行的抢占 + 报告的队列。

    `member_id` 给定时只看到自己的任务(网页);跑批的人不传,看全库。
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        settings: config.Settings | None = None,
        member_id: int | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.conn = conn
        self.settings = settings or config.get_settings()
        self.member_id = member_id
        self._now = now_provider

    # -- 基础 -------------------------------------------------------------
    def now(self) -> datetime:
        return self._now() if self._now is not None else self.settings.now()

    def _tz(self, name: str | None = None) -> ZoneInfo:
        return ZoneInfo(str(name or self.settings.timezone))

    def _owner_clause(self) -> tuple[str, list[Any]]:
        if self.member_id is None:
            return "1=1", []
        return "owner_member_id = ?", [int(self.member_id)]

    # -- 任务 -------------------------------------------------------------
    def get(self, job_id: str) -> WakeJob | None:
        clause, params = self._owner_clause()
        row = self.conn.execute(
            "SELECT * FROM wake_jobs WHERE id = ? AND " + clause,
            (str(job_id), *params),
        ).fetchone()
        return WakeJob.from_row(row) if row is not None else None

    def list_jobs(self, *, include_disabled: bool = True, limit: int = 50) -> list[WakeJob]:
        clause, params = self._owner_clause()
        sql = "SELECT * FROM wake_jobs WHERE " + clause
        if not include_disabled:
            sql += " AND enabled = 1"
        sql += " ORDER BY created_at, id LIMIT ?"
        return [
            WakeJob.from_row(row)
            for row in self.conn.execute(sql, (*params, max(int(limit), 1))).fetchall()
        ]

    def count_jobs(self, owner_member_id: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM wake_jobs WHERE owner_member_id = ?",
            (int(owner_member_id),),
        ).fetchone()
        return int(row["n"] if row is not None else 0)

    def create(
        self,
        *,
        owner_member_id: int,
        title: str,
        instruction: str,
        freq: str,
        time_local: str,
        weekday: int | None = None,
    ) -> WakeJob:
        clean_title = str(title or "").strip()
        clean_instruction = str(instruction or "").strip()
        if not clean_title:
            raise WakeError("任务名不能为空")
        if len(clean_title) > MAX_TITLE_CHARS:
            raise WakeError("任务名最多 " + str(MAX_TITLE_CHARS) + " 个字")
        if not clean_instruction:
            raise WakeError("唤醒要求不能为空")
        if len(clean_instruction) > MAX_INSTRUCTION_CHARS:
            raise WakeError("唤醒要求最多 " + str(MAX_INSTRUCTION_CHARS) + " 个字")
        if freq not in FREQS:
            raise WakeError("频率只能是每天 / 每周")
        parse_time_local(time_local)
        if freq == FREQ_WEEKLY and weekday is None:
            raise WakeError("每周任务要选星期几")
        if self.count_jobs(owner_member_id) >= MAX_JOBS_PER_MEMBER:
            raise WakeError("每人最多 " + str(MAX_JOBS_PER_MEMBER) + " 条唤醒任务")
        moment = self.now()
        tz_name = self.settings.timezone
        nxt = next_occurrence(
            moment, freq=freq, time_local=time_local, weekday=weekday, tz=self._tz(tz_name),
        )
        stamp = _now()
        job_id = _uuid("w-")
        self.conn.execute(
            "INSERT INTO wake_jobs(id, owner_member_id, title, instruction, freq,"
            " time_local, weekday, timezone, enabled, allow_writes, next_at,"
            " last_run_at, last_status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, NULL, '', ?, ?)",
            (
                job_id, int(owner_member_id), clean_title, clean_instruction, freq,
                str(time_local), None if weekday is None else int(weekday), tz_name,
                nxt.isoformat(timespec="seconds"), stamp, stamp,
            ),
        )
        self.conn.commit()
        job = self.get(job_id)
        if job is None:
            raise WakeError("任务创建后读不回来")
        return job

    def update(
        self,
        job_id: str,
        *,
        title: str | None = None,
        instruction: str | None = None,
        freq: str | None = None,
        time_local: str | None = None,
        weekday: int | None = None,
        allow_writes: bool | None = None,
    ) -> WakeJob:
        job = self.get(job_id)
        if job is None:
            raise WakeError("没有这条唤醒任务")
        new_title = job.title if title is None else str(title or "").strip()
        new_instruction = job.instruction if instruction is None else str(instruction or "").strip()
        new_freq = job.freq if freq is None else str(freq)
        new_time = job.time_local if time_local is None else str(time_local)
        new_weekday = job.weekday if weekday is None else int(weekday)
        if not new_title or len(new_title) > MAX_TITLE_CHARS:
            raise WakeError("任务名不能为空,且最多 " + str(MAX_TITLE_CHARS) + " 个字")
        if not new_instruction or len(new_instruction) > MAX_INSTRUCTION_CHARS:
            raise WakeError("唤醒要求不能为空,且最多 " + str(MAX_INSTRUCTION_CHARS) + " 个字")
        if new_freq not in FREQS:
            raise WakeError("频率只能是每天 / 每周")
        parse_time_local(new_time)
        if new_freq == FREQ_WEEKLY and new_weekday is None:
            raise WakeError("每周任务要选星期几")
        nxt = next_occurrence(
            self.now(), freq=new_freq, time_local=new_time, weekday=new_weekday,
            tz=self._tz(job.timezone),
        )
        self.conn.execute(
            "UPDATE wake_jobs SET title = ?, instruction = ?, freq = ?, time_local = ?,"
            " weekday = ?, allow_writes = ?, next_at = ?, updated_at = ? WHERE id = ?",
            (
                new_title, new_instruction, new_freq, new_time,
                None if new_weekday is None else int(new_weekday),
                1 if (job.allow_writes if allow_writes is None else allow_writes) else 0,
                nxt.isoformat(timespec="seconds"), _now(), str(job_id),
            ),
        )
        self.conn.commit()
        updated = self.get(job_id)
        if updated is None:
            raise WakeError("任务更新后读不回来")
        return updated

    def set_enabled(self, job_id: str, enabled: bool) -> bool:
        job = self.get(job_id)
        if job is None:
            return False
        next_at = job.next_at
        if enabled:
            next_at = next_occurrence(
                self.now(), freq=job.freq, time_local=job.time_local,
                weekday=job.weekday, tz=self._tz(job.timezone),
            ).isoformat(timespec="seconds")
        cur = self.conn.execute(
            "UPDATE wake_jobs SET enabled = ?, next_at = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, next_at, _now(), str(job_id)),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def reschedule(self, job_id: str, when: datetime) -> bool:
        """把下一次运行挪到指定时刻(用于"用户正在聊天,让路 10 分钟")。"""
        cur = self.conn.execute(
            "UPDATE wake_jobs SET next_at = ?, updated_at = ? WHERE id = ?",
            (when.isoformat(timespec="seconds"), _now(), str(job_id)),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def delete(self, job_id: str) -> bool:
        clause, params = self._owner_clause()
        cur = self.conn.execute(
            "DELETE FROM wake_jobs WHERE id = ? AND " + clause, (str(job_id), *params),
        )
        self.conn.commit()
        return cur.rowcount > 0

    # -- 运行 -------------------------------------------------------------
    def due_jobs(self, now: datetime, *, limit: int = 20) -> list[WakeJob]:
        rows = self.conn.execute(
            "SELECT * FROM wake_jobs WHERE enabled = 1 AND next_at IS NOT NULL"
            " ORDER BY next_at LIMIT ?", (max(int(limit), 1),),
        ).fetchall()
        out: list[WakeJob] = []
        for row in rows:
            job = WakeJob.from_row(row)
            moment = _aware(job.next_at, self._tz(job.timezone))
            if moment is not None and moment <= now:
                out.append(job)
        return out

    def late_seconds(self, job: WakeJob, now: datetime) -> float:
        moment = _aware(job.next_at, self._tz(job.timezone))
        if moment is None:
            return 0.0
        return max((now - moment).total_seconds(), 0.0)

    def claim_job(self, job: WakeJob, now: datetime) -> str | None:
        """抢占一条到点任务:插 run 行 + 推进 next_at。返回 run id 或 None。"""
        scheduled = job.next_at or now.isoformat(timespec="seconds")
        run_id = _uuid("r-")
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO wake_runs(id, job_id, title, owner_member_id,"
            " scheduled_for, dry_run, status, writes, receipts, usage, report_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, 0, 'pending', 0, '[]', '{}', '', ?)",
            (run_id, job.id, job.title, job.owner_member_id, scheduled, _now()),
        )
        if cur.rowcount == 0:
            return None
        nxt = next_occurrence(
            now, freq=job.freq, time_local=job.time_local, weekday=job.weekday,
            tz=self._tz(job.timezone),
        )
        cur = self.conn.execute(
            "UPDATE wake_jobs SET next_at = ?, updated_at = ?"
            " WHERE id = ? AND next_at IS ?",
            (nxt.isoformat(timespec="seconds"), _now(), job.id, job.next_at),
        )
        if cur.rowcount == 0:
            # 另一个进程/线程已经推进了 next_at:这次抢占作废,把 run 行删掉。
            self.conn.execute("DELETE FROM wake_runs WHERE id = ?", (run_id,))
            self.conn.commit()
            return None
        self.conn.commit()
        return run_id

    def enqueue_now(self, job: WakeJob, *, dry_run: bool = False) -> str:
        """立即跑一次(网页按钮)。不推进 next_at —— 这是计划外的一次。"""
        run_id = _uuid("r-")
        self.conn.execute(
            "INSERT INTO wake_runs(id, job_id, title, owner_member_id, scheduled_for,"
            " dry_run, status, writes, receipts, usage, report_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, '[]', '{}', '', ?)",
            (
                run_id, job.id, job.title, job.owner_member_id,
                self.now().isoformat(timespec="microseconds"),
                1 if dry_run else 0, _now(),
            ),
        )
        self.conn.commit()
        return run_id

    def record_missed(self, job: WakeJob, now: datetime) -> None:
        scheduled = job.next_at or now.isoformat(timespec="seconds")
        self.conn.execute(
            "INSERT OR IGNORE INTO wake_runs(id, job_id, title, owner_member_id,"
            " scheduled_for, dry_run, status, writes, receipts, usage, report_id,"
            " created_at, finished_at)"
            " VALUES (?, ?, ?, ?, ?, 0, 'missed', 0, '[]', '{}', '', ?, ?)",
            (_uuid("r-"), job.id, job.title, job.owner_member_id, scheduled,
             _now(), _now()),
        )
        nxt = next_occurrence(
            now, freq=job.freq, time_local=job.time_local, weekday=job.weekday,
            tz=self._tz(job.timezone),
        )
        self.conn.execute(
            "UPDATE wake_jobs SET next_at = ?, last_run_at = ?, last_status = 'missed',"
            " updated_at = ? WHERE id = ?",
            (nxt.isoformat(timespec="seconds"), _now(), _now(), job.id),
        )
        self.conn.commit()

    def pending_runs(self, *, limit: int = 10) -> list[WakeRun]:
        return [
            WakeRun.from_row(row)
            for row in self.conn.execute(
                "SELECT * FROM wake_runs WHERE status = 'pending'"
                " ORDER BY created_at, rowid LIMIT ?", (max(int(limit), 1),),
            ).fetchall()
        ]

    def get_run(self, run_id: str) -> WakeRun | None:
        row = self.conn.execute(
            "SELECT * FROM wake_runs WHERE id = ?", (str(run_id),),
        ).fetchone()
        return WakeRun.from_row(row) if row is not None else None

    def start_run(self, run_id: str) -> bool:
        cur = self.conn.execute(
            "UPDATE wake_runs SET status = 'running', started_at = ?"
            " WHERE id = ? AND status = 'pending'", (_now(), str(run_id)),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def bump_writes(self, run_id: str) -> None:
        self.conn.execute(
            "UPDATE wake_runs SET writes = writes + 1 WHERE id = ?", (str(run_id),),
        )
        self.conn.commit()

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        summary: str = "",
        error: str = "",
        receipts: list[int] | None = None,
        usage: dict[str, Any] | None = None,
        report_id: str = "",
    ) -> None:
        self.conn.execute(
            "UPDATE wake_runs SET status = ?, finished_at = ?, summary = ?, error = ?,"
            " receipts = ?, usage = ?, report_id = ? WHERE id = ?",
            (
                str(status), _now(), str(summary or "")[:2000], str(error or "")[:800],
                json.dumps([int(item) for item in (receipts or [])], ensure_ascii=False),
                json.dumps(usage or {}, ensure_ascii=False, default=str),
                str(report_id or ""), str(run_id),
            ),
        )
        run = self.get_run(run_id)
        if run is not None:
            self.conn.execute(
                "UPDATE wake_jobs SET last_run_at = ?, last_status = ? WHERE id = ?",
                (_now(), str(status), run.job_id),
            )
        self.conn.commit()

    def recover_stale(self, *, older_than_s: float = 600.0) -> list[WakeRun]:
        """把"开始跑了但进程没了"的运行记成 unknown —— 绝不自动重跑。"""
        cutoff = (self.now() - timedelta(seconds=float(older_than_s))).astimezone(
            timezone.utc
        ).isoformat(timespec="seconds")
        rows = self.conn.execute(
            "SELECT * FROM wake_runs WHERE status = 'running'"
            " AND (started_at IS NULL OR started_at < ?)", (cutoff,),
        ).fetchall()
        out: list[WakeRun] = []
        for row in rows:
            run = WakeRun.from_row(row)
            self.conn.execute(
                "UPDATE wake_runs SET status = 'unknown', finished_at = ?,"
                " error = '进程中断,结果未知' WHERE id = ?", (_now(), run.id),
            )
            out.append(run)
        if out:
            self.conn.commit()
        return out

    def runs_today(self, owner_member_id: int, now: datetime | None = None) -> int:
        moment = now or self.now()
        start_local = moment.astimezone(self._tz()).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        start_utc = start_local.astimezone(timezone.utc).isoformat(timespec="seconds")
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM wake_runs WHERE owner_member_id = ?"
            " AND created_at >= ? AND status NOT IN ('skipped', 'quota', 'missed')",
            (int(owner_member_id), start_utc),
        ).fetchone()
        return int(row["n"] if row is not None else 0)

    def recent_runs(self, *, limit: int = 20) -> list[WakeRun]:
        clause, params = self._owner_clause()
        return [
            WakeRun.from_row(row)
            for row in self.conn.execute(
                "SELECT * FROM wake_runs WHERE " + clause
                + " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (*params, max(int(limit), 1)),
            ).fetchall()
        ]

    def month_usage(self, owner_member_id: int, now: datetime | None = None) -> dict[str, int]:
        moment = now or self.now()
        start_local = moment.astimezone(self._tz()).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0,
        )
        start_utc = start_local.astimezone(timezone.utc).isoformat(timespec="seconds")
        rows = self.conn.execute(
            "SELECT usage FROM wake_runs WHERE owner_member_id = ? AND created_at >= ?",
            (int(owner_member_id), start_utc),
        ).fetchall()
        prompt = completion = total = 0
        for row in rows:
            try:
                usage = json.loads(row["usage"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            prompt += int(usage.get("prompt_tokens") or 0)
            completion += int(usage.get("completion_tokens") or 0)
            total += int(usage.get("total_tokens") or 0)
        return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}

    def prune(self, *, days: int = 90) -> int:
        if days <= 0:
            return 0
        cutoff = (self.now() - timedelta(days=int(days))).astimezone(
            timezone.utc
        ).isoformat(timespec="seconds")
        cur = self.conn.execute(
            "DELETE FROM wake_runs WHERE created_at < ? AND status NOT IN ('pending','running')",
            (cutoff,),
        )
        self.conn.execute(
            "DELETE FROM wake_reports WHERE created_at < ?"
            " AND status IN ('sent', 'expired', 'failed')", (cutoff,),
        )
        self.conn.commit()
        return cur.rowcount

    # -- 报告 -------------------------------------------------------------
    def create_report(
        self,
        *,
        run: WakeRun,
        text: str,
        ttl_s: float = REPORT_TTL_S,
    ) -> WakeReport:
        report_id = _uuid("p-")
        created = self.now()
        expires = (created + timedelta(seconds=float(ttl_s))).astimezone(
            timezone.utc
        ).isoformat(timespec="seconds")
        self.conn.execute(
            "INSERT INTO wake_reports(id, run_id, job_id, title, owner_member_id, text,"
            " status, attempts, created_at, sent_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, NULL, ?)",
            (
                report_id, run.id, run.job_id, run.title, run.owner_member_id,
                str(text), _now(), expires,
            ),
        )
        self.conn.execute(
            "UPDATE wake_runs SET report_id = ? WHERE id = ?", (report_id, run.id),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM wake_reports WHERE id = ?", (report_id,),
        ).fetchone()
        if row is None:
            raise WakeError("报告写进去后读不回来")
        return WakeReport.from_row(row)

    def pending_reports(self, *, now: datetime | None = None, limit: int = 20) -> list[WakeReport]:
        moment = now or self.now()
        stamp = moment.astimezone(timezone.utc).isoformat(timespec="seconds")
        return [
            WakeReport.from_row(row)
            for row in self.conn.execute(
                "SELECT * FROM wake_reports WHERE status = 'pending' AND expires_at > ?"
                " ORDER BY created_at, rowid LIMIT ?", (stamp, max(int(limit), 1)),
            ).fetchall()
        ]

    def expired_reports(self, *, now: datetime | None = None, limit: int = 50) -> list[WakeReport]:
        moment = now or self.now()
        stamp = moment.astimezone(timezone.utc).isoformat(timespec="seconds")
        return [
            WakeReport.from_row(row)
            for row in self.conn.execute(
                "SELECT * FROM wake_reports WHERE status = 'pending' AND expires_at <= ?"
                " ORDER BY created_at LIMIT ?", (stamp, max(int(limit), 1)),
            ).fetchall()
        ]

    def mark_report_sent(self, report_id: str) -> bool:
        cur = self.conn.execute(
            "UPDATE wake_reports SET status = 'sent', sent_at = ?, attempts = attempts + 1"
            " WHERE id = ? AND status = 'pending'", (_now(), str(report_id)),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def mark_report_failed(self, report_id: str) -> None:
        self.conn.execute(
            "UPDATE wake_reports SET attempts = attempts + 1 WHERE id = ?",
            (str(report_id),),
        )
        self.conn.commit()

    def mark_report_expired(self, report_id: str) -> bool:
        cur = self.conn.execute(
            "UPDATE wake_reports SET status = 'expired' WHERE id = ? AND status = 'pending'",
            (str(report_id),),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def recent_reports(self, *, limit: int = 20) -> list[WakeReport]:
        clause, params = self._owner_clause()
        return [
            WakeReport.from_row(row)
            for row in self.conn.execute(
                "SELECT * FROM wake_reports WHERE " + clause
                + " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (*params, max(int(limit), 1)),
            ).fetchall()
        ]
