"""唤醒:每个成员给自己定时的巡检任务。

与 /memory 的 admin-only 不同:这一页**每个成员都能用**,只看得到自己的任务;
管理员额外看到全局暂停与用量。所有写入口都走 run_once 幂等令牌。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import config
from ..base.clock import parse_iso
from ..family import identity
from ..ops import settings_store
from ..tasks.wake import (
    FREQ_DAILY,
    FREQ_WEEKLY,
    MAX_JOBS_PER_MEMBER,
    MAX_RUNS_PER_DAY,
    WakeError,
    WakeStore,
)
from ..tasks.wake_runner import WAKE_PAUSED_KEY
from .deps import Services, get_services
from .idempotency import run_once

STATUS_LABELS = {
    "pending": "排队中",
    "running": "运行中",
    "ok": "完成(试跑)",
    "reported": "已报告",
    "silent": "静默完成",
    "failed": "失败",
    "timeout": "超时",
    "skipped": "让路",
    "unknown": "中断(结果未知)",
    "missed": "错过",
    "quota": "超过配额",
}
REPORT_LABELS = {
    "pending": "待送达",
    "sent": "已送达",
    "expired": "已过期",
    "failed": "发送失败",
}


def _owner(services: Services) -> tuple[int, Any]:
    member = getattr(services, "member", None)
    if member is not None:
        return int(member.id), member
    return identity.ensure_default_member(services.assistant), None


def _store(services: Services) -> WakeStore:
    owner_id, _member = _owner(services)
    return WakeStore(
        services.memory, settings=services.settings, member_id=owner_id,
    )


def _local_text(value: str | None, settings: config.Settings) -> str:
    moment = parse_iso(value)
    if moment is None:
        return "—"
    return config.format_local(moment.astimezone(settings.tz))


def _schedule_text(job: Any) -> str:
    if job.freq == FREQ_DAILY:
        return "每天 " + job.time_local
    names = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "日"}
    return "每周" + names.get(int(job.weekday or 0), "?") + " " + job.time_local


def _wechat_bound(services: Services, owner_id: int) -> str:
    try:
        row = services.assistant.execute(
            "SELECT external_id FROM app_member_bindings"
            " WHERE member_id = ? AND channel = 'weixin' LIMIT 1",
            (int(owner_id),),
        ).fetchone()
    except Exception:  # noqa: BLE001 - 绑定表读不到就按未绑定显示
        return ""
    return str(row[0]) if row is not None else ""


def register_wake_routes(app, *, templates) -> None:
    def _view(services: Services, flash_text: str = "") -> dict[str, Any]:
        owner_id, member = _owner(services)
        store = _store(services)
        now = services.settings.now()
        jobs = store.list_jobs()
        runs = store.recent_runs(limit=20)
        reports = store.recent_reports(limit=20)
        paused = settings_store.get_flag(
            services.assistant, WAKE_PAUSED_KEY, default=False,
        )
        # 没启用 PIN 的单用户模式(member is None)按管理员算 —— 与 visibility 同一口径。
        is_admin = member is None or str(getattr(member, "role", "")) == "admin"
        bound = _wechat_bound(services, owner_id)
        return {
            "page": "wake",
            "flash": flash_text,
            "jobs": [
                {
                    "id": job.id,
                    "title": job.title,
                    "instruction": job.instruction,
                    "freq": job.freq,
                    "time_local": job.time_local,
                    "weekday": job.weekday,
                    "enabled": job.enabled,
                    "allow_writes": job.allow_writes,
                    "schedule_text": _schedule_text(job),
                    "next_text": _local_text(job.next_at, services.settings)
                    if job.enabled else "已暂停",
                    "last_text": _local_text(job.last_run_at, services.settings),
                    "last_status": STATUS_LABELS.get(job.last_status, job.last_status or "没跑过"),
                }
                for job in jobs
            ],
            "runs": [
                {
                    "id": run.id,
                    "title": run.title,
                    "status": run.status,
                    "status_text": STATUS_LABELS.get(run.status, run.status),
                    "dry_run": run.dry_run,
                    "started": _local_text(run.started_at or run.created_at, services.settings),
                    "writes": run.writes,
                    "receipts": run.receipts,
                    "summary": run.summary,
                    "error": run.error,
                    "usage": run.usage,
                }
                for run in runs
            ],
            "reports": [
                {
                    "id": report.id,
                    "title": report.title,
                    "text": report.text,
                    "status": report.status,
                    "status_text": REPORT_LABELS.get(report.status, report.status),
                    "created": _local_text(report.created_at, services.settings),
                }
                for report in reports
            ],
            "paused": paused,
            "is_admin": is_admin,
            "wechat_bound": bound,
            "runs_today": store.runs_today(owner_id, now),
            "month_usage": store.month_usage(owner_id, now),
            "max_jobs": MAX_JOBS_PER_MEMBER,
            "max_runs": MAX_RUNS_PER_DAY,
        }

    @app.get("/wake", response_class=HTMLResponse)
    def wake_page(request: Request, services: Services = Depends(get_services)):
        return templates.TemplateResponse(
            request, "wake.html", _view(services, str(request.query_params.get("flash") or "")),
        )

    @app.post("/wake/jobs")
    def wake_create(
        request: Request,
        title: str = Form(...),
        instruction: str = Form(...),
        freq: str = Form(FREQ_DAILY),
        time_local: str = Form("19:00"),
        weekday: str = Form(""),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ):
        owner_id, _member = _owner(services)
        store = _store(services)

        def _do():
            try:
                job = store.create(
                    owner_member_id=owner_id,
                    title=title,
                    instruction=instruction,
                    freq=freq,
                    time_local=time_local,
                    weekday=int(weekday) if str(weekday).strip().isdigit() else None,
                )
            except WakeError as exc:
                return RedirectResponse("/wake?flash=" + quote(str(exc)), 303)
            return RedirectResponse(
                "/wake?flash=" + quote("建好了:" + job.title + ",下次 " + _schedule_text(job)), 303,
            )

        return run_once(
            services, idem_token,
            {"op": "create", "title": title, "freq": freq, "time": time_local},
            _do,
            replay=RedirectResponse("/wake?flash=这一次已经建过了", 303),
        )

    @app.post("/wake/jobs/{job_id}/save")
    def wake_save(
        request: Request,
        job_id: str,
        title: str = Form(...),
        instruction: str = Form(...),
        freq: str = Form(FREQ_DAILY),
        time_local: str = Form("19:00"),
        weekday: str = Form(""),
        # ⚠️ 复选框没勾时浏览器**不发这个字段**;默认值必须是 0,
        #    否则"取消勾选"会被当成"仍然允许写"。
        allow_writes: str = Form("0"),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ):
        store = _store(services)

        def _do():
            try:
                job = store.update(
                    job_id,
                    title=title,
                    instruction=instruction,
                    freq=freq,
                    time_local=time_local,
                    weekday=int(weekday) if str(weekday).strip().isdigit() else None,
                    allow_writes=str(allow_writes) == "1",
                )
            except WakeError as exc:
                return RedirectResponse("/wake?flash=" + quote(str(exc)), 303)
            return RedirectResponse(
                "/wake?flash=" + quote("已保存:" + job.title + ",下次 " + _schedule_text(job)), 303,
            )

        return run_once(
            services, idem_token, {"op": "save", "job": job_id, "title": title},
            _do, replay=RedirectResponse("/wake?flash=这一次已经保存过了", 303),
        )

    @app.post("/wake/jobs/{job_id}/toggle")
    def wake_toggle(
        request: Request,
        job_id: str,
        enabled: str = Form("0"),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ):
        store = _store(services)

        def _do():
            ok = store.set_enabled(job_id, str(enabled) == "1")
            return RedirectResponse(
                "/wake?flash=" + quote("已更新" if ok else "没有这条任务"), 303,
            )

        return run_once(
            services, idem_token, {"op": "toggle", "job": job_id, "enabled": enabled},
            _do, replay=RedirectResponse("/wake?flash=这一次已经切换过了", 303),
        )

    @app.post("/wake/jobs/{job_id}/delete")
    def wake_delete(
        request: Request,
        job_id: str,
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ):
        store = _store(services)

        def _do():
            ok = store.delete(job_id)
            return RedirectResponse(
                "/wake?flash=" + quote("已删除" if ok else "没有这条任务"), 303,
            )

        return run_once(
            services, idem_token, {"op": "delete", "job": job_id},
            _do, replay=RedirectResponse("/wake?flash=这一次已经删过了", 303),
        )

    @app.post("/wake/jobs/{job_id}/run")
    def wake_run_now(
        request: Request,
        job_id: str,
        dry_run: str = Form("1"),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ):
        owner_id, _member = _owner(services)
        store = _store(services)

        def _do():
            if settings_store.get_flag(
                services.assistant, WAKE_PAUSED_KEY, default=False,
            ):
                return RedirectResponse(
                    "/wake?flash=" + quote("全局暂停中,先恢复再试跑"), 303,
                )
            job = store.get(job_id)
            if job is None:
                return RedirectResponse("/wake?flash=没有这条任务", 303)
            if store.runs_today(owner_id) >= MAX_RUNS_PER_DAY:
                return RedirectResponse(
                    "/wake?flash=" + quote("今天的唤醒次数已到上限"), 303,
                )
            store.enqueue_now(job, dry_run=str(dry_run) == "1")
            return RedirectResponse(
                "/wake?flash=" + quote(
                    "已排队,30 秒内开始" + ("(试跑:只读、不发送)" if str(dry_run) == "1" else "")
                ), 303,
            )

        return run_once(
            services, idem_token, {"op": "run", "job": job_id, "dry": dry_run},
            _do, replay=RedirectResponse("/wake?flash=这一次已经排过了", 303),
        )

    @app.post("/wake/pause")
    def wake_pause(
        request: Request,
        paused: str = Form("1"),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ):
        member = getattr(services, "member", None)
        if member is not None and str(getattr(member, "role", "")) != "admin":
            return HTMLResponse("<h1>只有管理员能全局暂停唤醒</h1>", status_code=403)

        def _do():
            settings_store.set_flag(
                services.assistant, WAKE_PAUSED_KEY, str(paused) == "1",
            )
            return RedirectResponse(
                "/wake?flash=" + quote("已全局暂停" if str(paused) == "1" else "已恢复唤醒"), 303,
            )

        return run_once(
            services, idem_token, {"op": "pause", "paused": paused},
            _do, replay=RedirectResponse("/wake?flash=这一次已经处理过了", 303),
        )
