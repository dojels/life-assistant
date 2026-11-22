"""轨迹页与使用报表(纯 SSR)。

从 create_app() 搬出来的第二块业务路由(2026-10-18 架构收口)。
页签归一化、可见会话、工具全集由 app 通过参数传入 —— 路由模块不反向
import app,避免循环依赖。
"""

from __future__ import annotations

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse

from .. import config
from . import report_view
from .deps import Services, get_services
from .trace_view import build_period_tabs, build_trace_view


def register_trace_routes(
    app, *, templates, trace_sessions, tool_catalog,
    normalize_tab, normalize_only, tab_report: str,
) -> None:
    @app.get("/trace", response_class=HTMLResponse)
    def trace_page(
        request: Request, tab: str = "", i: str = "", only: str = "",
        services: Services = Depends(get_services),
    ):
        view = build_trace_view(
            services.settings,
            assistant_conn=services.assistant,
            memory_conn=services.memory,
            index=i,
            tab=normalize_tab(tab),
            only=normalize_only(only),
            now_text=config.format_local(services.settings.now()),
            sessions=trace_sessions(services),
        )
        return templates.TemplateResponse(
            request,
            "trace.html",
            {"page": "trace", "view": view,
             "compact_trigger": config.BUDGET_COMPACT_TRIGGER},
        )

    @app.get("/report", response_class=HTMLResponse)
    def report_page(
        request: Request, period: str = "", start: str = "", end: str = "",
        sort: str = "",
        services: Services = Depends(get_services),
    ):
        member = getattr(services, "member", None)
        if member is not None and str(getattr(member, "role", "")) != "admin":
            return HTMLResponse("<h1>使用报表目前是管理员页面</h1>", status_code=403)
        wanted = period if period in report_view.PERIODS else report_view.PERIOD_DAY
        view = build_trace_view(
            services.settings,
            assistant_conn=services.assistant,
            memory_conn=services.memory,
            tab=tab_report,
            now_text=config.format_local(services.settings.now()),
            sessions=trace_sessions(services),
        )
        report = report_view.build_usage_report(
            services.memory,
            period=wanted,
            start=start,
            end=end,
            now=services.settings.now(),
            now_text=view.generated_at,
            catalog=tool_catalog(services),
            sort=sort,
        )
        view.period = report.period
        view.period_label = report_view.PERIOD_LABELS[report.period]
        view.custom_start = start
        view.custom_end = end
        view.default_days = report_view.DEFAULT_RANGE_DAYS
        view.metric_rows = report.metric_rows
        view.tools = report.tools
        view.tools_retired = report.tools_retired
        view.internal_catalog = report.internal_catalog
        view.skills = report.skills
        view.internal_tools = report.internal_tools
        view.notes = report.notes
        view.usage_available = report.usage_available
        view.catalog_size = report.catalog_size
        view.never_called = report.never_called
        view.sort = report.sort
        query = "period=" + report.period
        if report.period == report_view.PERIOD_CUSTOM:
            query += "&start=" + str(start) + "&end=" + str(end)
        view.sort_link_calls = "/report?" + query + "&sort=calls"
        view.sort_link_failed = "/report?" + query + "&sort=failed"
        view.report_rows = report.rows
        view.report_totals = report.totals
        view.range_label = report.range_label
        view.price_note = report.price_note
        view.period_tabs = build_period_tabs(
            view, periods=report_view.PERIODS, labels=report_view.PERIOD_LABELS,
        )
        return templates.TemplateResponse(
            request,
            "report.html",
            {"page": "trace", "view": view},
        )
