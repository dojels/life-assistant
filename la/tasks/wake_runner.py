"""定时唤醒的跑批线程。

⭐ 为什么跑在 Web 进程的**独立线程**里:
  * 唤醒要调模型,而 WeChat 长轮询进程不能停(停了主动推送就哑) —— 不能塞进那边;
  * 网页进程已经有后台心跳(ticker),但唤醒是**完整一轮**,必须有自己的事件循环,
    不能占着 uvicorn 的循环;
  * 线程里跑 asyncio.run,慢工具只冻这一个线程,不冻网页与微信。
  * 真正的并发保护是 `turn_lock`(跨进程)—— 用户正在聊天时,唤醒 5 秒拿不到锁就
    记 busy,十分钟后再试。
"""

from __future__ import annotations

import asyncio
import os
import threading
import traceback
from datetime import datetime, timedelta
from typing import Any

from .. import config
from ..agent.prompts import SURFACE_WEIXIN
from ..agent.session import MemorySession
from ..family import identity
from ..family import members as member_store
from ..llm.factory import build_client
from ..ops import settings_store
from ..storage import db
from ..web.deps import build_services
from .wake import (
    MAX_LATE_S,
    MAX_RUNS_PER_DAY,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_QUOTA,
    STATUS_REPORTED,
    STATUS_SILENT,
    STATUS_TIMEOUT,
    WakeJob,
    WakeRun,
    WakeStore,
)

#: 唤醒总开关(0/false/no/off = 停)。与网页上的"全局暂停"是两道闸,都要能停。
WAKE_ENV = "LA_WAKE"
#: 网页上的全局暂停键(存在 sys_settings)。
WAKE_PAUSED_KEY = "wake_paused"
DEFAULT_INTERVAL_S = 30.0
#: 单次唤醒的墙钟上限。比普通一轮(900 秒)短 —— 它不该占着用户会话太久。
WAKE_TIMEOUT_S = 300.0
#: 一个 tick 最多跑 1 条,剩下的下一轮(30 秒后)再来,别让线程失联。
MAX_RUNS_PER_TICK = 1
#: 报告里"本轮写入"那一行的回执上限(微信一屏)。
MAX_RECEIPTS_IN_TEXT = 8


def wake_enabled() -> bool:
    return (os.getenv(WAKE_ENV) or "1").strip().lower() not in ("0", "false", "no", "off")


def _log(text: str) -> None:
    print("[wake] " + text, flush=True)


class WakeRunner(threading.Thread):
    def __init__(self, *, interval_s: float = DEFAULT_INTERVAL_S) -> None:
        super().__init__(name="wake-runner", daemon=True)
        self.interval_s = max(5.0, float(interval_s))
        self._stop = threading.Event()
        self.ticks = 0
        self._last_prune_day = ""

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:  # noqa: D401 - threading 的入口
        # 启动提示由 app.py 的 startup 事件打一次;这里不再重复 ——
        # 两行几乎一样,排查的人会以为起了两个线程。
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - 后台线程静默死掉是最难查的故障
                _log("轮次异常:" + type(exc).__name__ + ": " + str(exc)[:200])
                traceback.print_exc()
            self.ticks += 1
            self._stop.wait(self.interval_s)

    # -- 一轮 -------------------------------------------------------------
    def tick(self, now: datetime | None = None) -> dict[str, Any]:
        report: dict[str, Any] = {"ran": [], "skipped": [], "error": ""}
        services = build_services()
        try:
            store = WakeStore(services.memory, settings=services.settings)
            moment = now or services.settings.now()
            if settings_store.get_flag(
                services.assistant, WAKE_PAUSED_KEY, default=False
            ):
                report["skipped"].append("全局暂停")
                return report
            # 把"跑到一半进程没了"的运行记成 unknown,并留一条待送达报告 ——
            # 绝不自动重跑(可能已经写了一半)。
            for run in store.recover_stale():
                self._report_unknown(store, run)
                report["skipped"].append("恢复:" + run.id)
            self._prune(store, moment)

            pending = store.pending_runs(limit=MAX_RUNS_PER_TICK)
            for run in pending:
                self._execute(store, run)
                report["ran"].append(run.id)
            if pending:
                return report

            for job in store.due_jobs(moment, limit=5)[:MAX_RUNS_PER_TICK]:
                late = store.late_seconds(job, moment)
                if late > MAX_LATE_S:
                    store.record_missed(job, moment)
                    report["skipped"].append("迟到:" + job.id)
                    continue
                if store.runs_today(job.owner_member_id, moment) >= MAX_RUNS_PER_DAY:
                    run_id = store.claim_job(job, moment)
                    if run_id:
                        store.finish_run(
                            run_id, status=STATUS_QUOTA,
                            summary="今天的唤醒次数已到上限(" + str(MAX_RUNS_PER_DAY) + " 次)",
                        )
                    report["skipped"].append("配额:" + job.id)
                    continue
                run_id = store.claim_job(job, moment)
                if run_id is None:
                    continue
                run = store.get_run(run_id)
                if run is not None:
                    self._execute(store, run)
                    report["ran"].append(run.id)
        except Exception as exc:  # noqa: BLE001
            report["error"] = type(exc).__name__ + ": " + str(exc)[:200]
            raise
        finally:
            services.close()
        return report

    def _prune(self, store: WakeStore, now: datetime) -> None:
        day = now.strftime("%Y-%m-%d")
        if day == self._last_prune_day:
            return
        self._last_prune_day = day
        try:
            store.prune(days=90)
        except Exception:  # noqa: BLE001 - 清理失败不该影响这一轮
            pass

    # -- 执行 -------------------------------------------------------------
    def _execute(self, store: WakeStore, run: WakeRun) -> None:
        if not store.start_run(run.id):
            return
        job = store.get(run.job_id)
        if job is None:
            store.finish_run(run.id, status="skipped", summary="任务已删除")
            return
        if store.runs_today(run.owner_member_id) > MAX_RUNS_PER_DAY:
            store.finish_run(run.id, status=STATUS_QUOTA, summary="今天的唤醒次数已到上限")
            return

        services = None
        try:
            conn = db.connect_assistant()
            try:
                member = member_store.get_member(conn, run.owner_member_id)
            finally:
                conn.close()
            if member is None or not member.active:
                store.finish_run(run.id, status="failed", error="成员不存在或已停用")
                return
            services = build_services(member=member)
            settings = services.settings

            def _on_wake_write() -> None:
                try:
                    store.bump_writes(run.id)
                except Exception:  # noqa: BLE001 - 记数失败不该打断写入
                    pass

            session = MemorySession(
                build_client(settings),
                settings,
                store=services.store,
                session_id=identity.session_id_for_member(member.id),
                assistant_conn=services.assistant,
                auto_briefing=False,
                asker=None,
                surface=SURFACE_WEIXIN,
                auto_approve=False,
                direct_write=False,
                wake=True,
                on_wake_write=_on_wake_write,
            )

            async def _go():
                return await asyncio.wait_for(
                    session.run_wake(
                        job.instruction,
                        allow_writes=job.allow_writes,
                        dry_run=run.dry_run,
                    ),
                    timeout=WAKE_TIMEOUT_S,
                )

            result = asyncio.run(_go())
            usage = result.usage.as_dict() if result is not None else {}
            writes = int(session.wake_write_calls)
            receipts = list(dict.fromkeys(session.wake_receipts))
            # 工具结果不一定带 receipt_id(note_add 就不带),所以按"水位线"再捞一遍。
            marks = getattr(session, "wake_receipt_marks", (0, 0))
            for log, mark in (
                (session.receipts, marks[0]),
                (session.memory_receipts, marks[1]),
            ):
                if log is None:
                    continue
                try:
                    for item in log.since(int(mark), session_id=session.session_id):
                        if item.id not in receipts:
                            receipts.append(item.id)
                except Exception:  # noqa: BLE001 - 捞不到回执不该让整次运行失败
                    pass
            report = dict(session.wake_report or {})
            body = str(report.get("text") or "").strip()

            if run.dry_run:
                store.finish_run(
                    run.id, status=STATUS_OK,
                    summary=(body or "试跑完成:没有要处理的事。")[:2000],
                    receipts=receipts, usage=usage,
                )
                return
            if not body and writes:
                # 模型忘了报告,但确实写了:自动补一条,别让用户第二天看见新账目发懵。
                body = (
                    "我按任务写了 " + str(writes) + " 条数据。"
                    "请到对应页面确认(账本类会先落草稿,等你确认)。"
                )
            if body:
                text = self._report_text(job, body, writes, receipts)
                created = store.create_report(run=run, text=text)
                store.finish_run(
                    run.id, status=STATUS_REPORTED, summary=body,
                    receipts=receipts, usage=usage, report_id=created.id,
                )
            else:
                store.finish_run(
                    run.id, status=STATUS_SILENT,
                    summary="检查完成,没有要处理的事,也没有写数据。",
                    receipts=receipts, usage=usage,
                )
        except TimeoutError as exc:
            # ⚠️ Python 3.11+ 里 asyncio.TimeoutError 就是内置 TimeoutError,
            #    所以只能靠消息区分"整轮超时"与"会话锁没拿到"。
            if "会话正忙" in str(exc):
                # turn_lock 5 秒没拿到 = 用户正在聊天。记 busy,不阻塞、不重试。
                store.finish_run(
                    run.id, status="skipped",
                    summary="用户正在聊天,这次唤醒让路,10 分钟后再试。",
                )
                # 把下一次挪到 10 分钟后;否则 claim 已把 next_at 推到明天,
                # "10 分钟后再试"就成了一句空话。
                store.reschedule(
                    run.job_id, store.now() + timedelta(minutes=10),
                )
            else:
                self._fail(
                    store, run, job, "timeout",
                    "超过 " + str(int(WAKE_TIMEOUT_S)) + " 秒",
                )
        except Exception as exc:  # noqa: BLE001 - 失败要落库,不能只打日志
            self._fail(store, run, job, "failed", type(exc).__name__ + ": " + str(exc)[:300])
        finally:
            if services is not None:
                services.close()

    def _fail(
        self, store: WakeStore, run: WakeRun, job: WakeJob, kind: str, detail: str
    ) -> None:
        """失败也要让用户知道"可能写了数据" —— 静默失败是最坏的一种。"""
        status = STATUS_TIMEOUT if kind == "timeout" else STATUS_FAILED
        try:
            current = store.get_run(run.id) or run
            writes = int(current.writes)
            text = ""
            if writes:
                text = (
                    "⏰ 唤醒 · " + job.title + "\n"
                    "这次执行中途失败了(" + detail + "),可能已经写了 "
                    + str(writes) + " 条数据。请到网页「唤醒」页看这次记录并核对。"
                )
            report_id = ""
            if text:
                report_id = store.create_report(run=current, text=text).id
            store.finish_run(
                run.id, status=status, error=detail,
                summary=("中途失败,可能写了 " + str(writes) + " 条") if writes else "执行失败",
                report_id=report_id,
            )
        except Exception:  # noqa: BLE001 - 失败处理本身失败只能记日志
            _log("失败落库失败:" + traceback.format_exc()[:400])

    def _report_unknown(self, store: WakeStore, run: WakeRun) -> None:
        if run.dry_run:
            return
        try:
            store.create_report(
                run=run,
                text=(
                    "⏰ 唤醒 · " + (run.title or "定时任务") + "\n"
                    "上次执行中断了(进程退出),结果未知,可能已经写了数据。"
                    "请到网页「唤醒」页看这次的记录并核对。"
                ),
            )
        except Exception:  # noqa: BLE001
            _log("恢复报告写入失败:" + traceback.format_exc()[:300])

    @staticmethod
    def _report_text(
        job: WakeJob, body: str, writes: int, receipts: list[int]
    ) -> str:
        text = "⏰ 唤醒 · " + job.title + "\n" + body.strip()
        if writes:
            text += "\n本轮写入 " + str(writes) + " 条"
            if receipts:
                shown = receipts[:MAX_RECEIPTS_IN_TEXT]
                text += "(回执 " + "、".join(str(item) for item in shown)
                if len(receipts) > len(shown):
                    text += " 等"
                text += ";回复 /撤销 编号 可撤回)"
        return text[:2000]


def start_wake_runner() -> WakeRunner | None:
    if not wake_enabled():
        return None
    runner = WakeRunner()
    runner.start()
    return runner
