"""账本 SSR:账本页(支出/收入双视图)+ 分类管理 + 记账/改分类/状态/删除/撤销。

从 create_app() 搬出来的第十块业务路由(2026-10-18 架构收口)。
读取口径仍只有一处:summary 来自 query_expenses,明细来自 list_txns。

⚠️ D164(2026-10-24)视觉与交互改版的三条边界:
  1. **数字仍只来自 SQL** —— 页面只做画图(占比、日均是同一份合计除出来的);
  2. **不引前端框架** —— 弹窗用原生 <dialog>,没有 JS 时"记一笔"退化成
     ?compose=1 直接渲染出表单,照样能记;
  3. **时间区间的唯一真源是 period** —— 自定义区间收成 "a..b",老链接
     (/ledger?period=2026-10) 一字不改仍然能用。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any
from urllib.parse import quote

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..domain.schemas.ledger import ExpenseQuery
from ..domain.services.ledger_service import LedgerError, period_bounds
from ..domain.undo import UndoError
from .deps import Services, get_services
from .idempotency import run_once
from .visibility import question_visible

#: 两个视图:支出 / 收入。收入与支出分开看,是用户 2026-10-24 明确要的。
VIEWS = ("expense", "income")
#: 分类排行那个"固定窗口"大约能放下几行。超过它,列表自己在窗口里滚
#: (真实值由 ledger.css 的高度决定,这里只用来决定要不要写"可以滚"那句提示)。
RANK_WINDOW = 6
VIEW_LABELS = {"expense": "支出", "income": "收入"}


def _undo_hint(services: Services) -> str:
    if services.ledger is None:
        return ""
    recent = services.ledger.undo.recent(limit=1)
    return recent[0].label if recent else ""


# ---------------------------------------------------------------------------
# 时间区间:period 是唯一真源,start/end 只是它的两种写法
# ---------------------------------------------------------------------------
def _align_view(url: str, direction: str) -> str:
    """记完一笔,跳到**它自己那一边** —— 在「收入」页选支出记完却还停在收入页,
    用户会以为这笔没记上。"""
    if "view=" not in url:
        return url
    return url.replace("view=expense", "view=" + direction).replace(
        "view=income", "view=" + direction
    )


def _safe_next(value: str) -> str:
    """只允许跳回账本自己的页面 —— 不然就是开放重定向。"""
    text = str(value or "").strip()
    if text.startswith("/ledger") and "//" not in text and "\\" not in text:
        return text
    return "/ledger"


def _range_label(start_iso: str, end_iso: str) -> str:
    """把 [start, end) 说成人话:整月说"2026 年 10 月",其余说起止日期。"""
    try:
        start = datetime.fromisoformat(start_iso)
        end = datetime.fromisoformat(end_iso)
    except ValueError:
        return start_iso + " – " + end_iso
    last = end - timedelta(days=1)
    whole_month = (
        start.day == 1
        and (start.year, start.month) == (last.year, last.month)
        and end.day == 1
        and (end.year, end.month) == (
            start.year + (start.month // 12), (start.month % 12) + 1
        )
    )
    if whole_month:
        return str(start.year) + " 年 " + str(start.month) + " 月"
    if start.year == last.year:
        return (str(start.year) + "." + str(start.month).zfill(2) + "."
                + str(start.day).zfill(2) + " – " + str(last.month).zfill(2) + "."
                + str(last.day).zfill(2))
    return (start.isoformat()[:10] + " – " + last.isoformat()[:10])


def _resolve_range(
    *, period: str, start: str, end: str, now: datetime
) -> dict[str, Any]:
    """period / start..end -> 一个规范化的区间描述(含日期输入框的值)。"""
    start = str(start or "").strip()
    end = str(end or "").strip()
    if start and end:
        chosen = start + ".." + end
    elif start:
        chosen = start + ".." + now.date().isoformat()
    elif end:
        chosen = now.replace(day=1).date().isoformat() + ".." + end
    else:
        chosen = str(period or "").strip() or now.strftime("%Y-%m")
    start_iso, end_iso = period_bounds(chosen, now)
    return {
        "period": chosen,
        "range_label": _range_label(start_iso, end_iso),
        "start_date": start_iso[:10],
        "end_date": (datetime.fromisoformat(end_iso) - timedelta(days=1)).date().isoformat(),
        "start_iso": start_iso,
        "end_iso": end_iso,
    }


def _presets(now: datetime) -> list[dict[str, str]]:
    """快捷区间。⭐ 每个都**直接给出起止日期**,点一下就是一次普通 GET。"""
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = today.replace(day=1)
    prev_end = month_start - timedelta(days=1)
    prev_start = prev_end.replace(day=1)
    year_start = today.replace(month=1, day=1)
    day = today.date().isoformat()
    return [
        {"key": "month", "label": "本月",
         "start": month_start.date().isoformat(), "end": day},
        {"key": "prev", "label": "上月",
         "start": prev_start.date().isoformat(), "end": prev_end.date().isoformat()},
        {"key": "30d", "label": "近 30 天",
         "start": (today - timedelta(days=29)).date().isoformat(), "end": day},
        {"key": "year", "label": "今年",
         "start": year_start.date().isoformat(), "end": day},
    ]


WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _day_label(day: str) -> str:
    """2026-10-24 -> 10 月 24 日 · 周六。日期是人看的,数字不是。"""
    try:
        parsed = datetime.fromisoformat(day)
    except ValueError:
        return day
    return (str(parsed.month) + " 月 " + str(parsed.day) + " 日 · "
            + WEEKDAYS[parsed.weekday()])


def _donut_stops(groups: list[dict[str, Any]]) -> str:
    """把占比画成 conic-gradient 的色标串。

    ⭐ 在 Python 里拼、模板里只管贴:Jinja 里拼逗号要绕 loop.first,
       而且少一个逗号浏览器会整条丢掉,肉眼又看不出来。
    """
    if not groups:
        return "var(--bg-fill) 0% 100%"
    stops: list[str] = []
    acc = 0.0
    for item in groups:
        start = acc
        acc = min(100.0, acc + float(item.get("share") or 0.0))
        stops.append("var(--tone-" + str(item.get("tone", 0)) + ") "
                     + format(start, ".2f") + "% " + format(acc, ".2f") + "%")
    if acc < 99.95:  # 四舍五入留下的缝:用底色补齐,免得图形边缘缺一小块
        stops.append("var(--bg-fill) " + format(acc, ".2f") + "% 100%")
    return ", ".join(stops)


def register_ledger_routes(app, *, templates) -> None:
    # ---------------------------------------------------------------- 账本
    def _ledger_context(
        services: Services, period: str, *, view: str = "expense",
        start: str = "", end: str = "", compose: bool = False, error: str = "",
    ) -> dict[str, Any]:
        """账本页要的全部数据。**全在 SQL 里算**,模板只负责画。

        ⚠️ 口径只有一处:summary 来自 query_expenses(direction="all"),
        明细来自 list_txns(direction=view),两者用的是同一套 SQL 表达式。
        """
        now = services.settings.now()
        chosen_view = view if view in VIEWS else "expense"
        try:
            span = _resolve_range(period=period, start=start, end=end, now=now)
        except LedgerError as exc:
            span = _resolve_range(period="", start="", end="", now=now)
            error = error or str(exc)
        summary = None
        rows: list[dict[str, Any]] = []
        if not error:
            try:
                summary = services.ledger.query_expenses(
                    ExpenseQuery(period=span["period"], dimension="category",
                                 direction="all", include_drafts=False)
                )
            except Exception as exc:  # noqa: BLE001 - 一次查询失败不该让整页打不开
                error = str(exc)

        try:
            rows = services.ledger.list_txns(
                period=span["period"], limit=400, direction=chosen_view
            )
        except LedgerError as exc:
            # ⚠️ 这里以前是无条件调用:period=9999 时 query_expenses 被接住,
            #    list_txns 又抛一次同样的 LedgerError → 整页还是 500。
            #    只接**领域错误**;意外的 RuntimeError 仍走全局错误页(测试钉着)。
            rows = []
            if not error:
                error = str(exc)

        # 按天分组:同一天的账放一起,日期只在第一行出现一次
        grouped: list[dict[str, Any]] = []
        for row in rows:
            if not grouped or grouped[-1]["day"] != row["day"]:
                grouped.append({"day": row["day"], "label": _day_label(row["day"]),
                                "items": [], "total": 0})
            grouped[-1]["items"].append(row)
            grouped[-1]["total"] += int(row["cents"])

        groups = list(summary.groups) if summary is not None else []
        # 视图只决定"拿哪一半金额":同一个分类可能既有支出又有收入,不能混着算
        view_groups = sorted(
            [
                {
                    "key": item.key or "(未分类)",
                    "cents": int(item.expense_cents if chosen_view == "expense"
                                 else item.income_cents),
                    "txn_count": int(item.txn_count),
                }
                for item in groups
                if int(item.expense_cents if chosen_view == "expense"
                       else item.income_cents) > 0
            ],
            key=lambda item: -item["cents"],
        )
        view_total = sum(item["cents"] for item in view_groups) or 1
        for index, item in enumerate(view_groups):
            item["share"] = round(item["cents"] * 100.0 / view_total, 1)
            # 图上的颜色按排名分配:第一名永远是最醒目的那个色,换月份也不会"换了颜色"
            item["tone"] = index % 8

        drafts = [
            item for item in services.ledger.drafts(limit=20)
            if item.get("direction") == chosen_view
        ]
        accounts = services.ledger.asset_accounts()
        try:
            trend = services.ledger.monthly_trend(months=6, end=now)
        except Exception:  # noqa: BLE001 - 趋势条坏了不该拖垮整页
            trend = []
        pending = (
            [item for item in services.questions.recent(limit=10, only_pending=True)
             if question_visible(services, item[0])]
            if services.questions is not None else []
        )
        stats = {
            "expense_cents": int(summary.expense_cents) if summary else 0,
            "income_cents": int(summary.income_cents) if summary else 0,
            "net_cents": int(summary.net_cents) if summary else 0,
            "txn_count": int(summary.txn_count) if summary else 0,
        }
        view_sum = stats["expense_cents"] if chosen_view == "expense" else stats["income_cents"]
        days = max(1, (datetime.fromisoformat(span["end_iso"]).date()
                       - datetime.fromisoformat(span["start_iso"]).date()).days)
        return {
            "page": "ledger", "period": span["period"], "summary": summary, "error": error,
            "view": chosen_view, "view_label": VIEW_LABELS[chosen_view],
            "range_label": span["range_label"],
            "start_date": span["start_date"], "end_date": span["end_date"],
            "presets": [
                {**preset,
                 "on": preset["start"] == span["start_date"]
                 and preset["end"] == span["end_date"]}
                for preset in _presets(now)
            ],
            "compose": bool(compose),
            "stats": stats,
            "view_sum": view_sum,
            "view_avg": int(view_sum / days) if view_sum else 0,
            "groups_by_day": grouped, "rows": rows,
            "groups": view_groups,
            "donut": _donut_stops(view_groups),
            "tones": {item["key"]: item["tone"] for item in view_groups},
            "group_count": len(view_groups),
            "rank_window": RANK_WINDOW,
            "peak_group": max([item["cents"] for item in view_groups] or [0]) or 1,
            "trend": trend,
            #: 有数据的月份数。只有一个月时画六根柱子,画出来是"五根空气 + 一根"——
            #: 那不是趋势,是噪音。>= 2 才画(D165)。
            "trend_active": sum(
                1 for item in trend
                if int(item["expense_cents"]) or int(item["income_cents"])
            ),
            "trend_peak": max(
                [max(int(item["expense_cents"]), int(item["income_cents"])) for item in trend] or [0]
            ) or 1,
            "drafts": drafts,
            "draft_total": sum(int(row.get("cents") or 0) for row in drafts),
            "accounts": accounts,
            "expense_categories": services.ledger.list_categories("expense"),
            "income_categories": services.ledger.list_categories("income"),
            "pending_questions": pending,
            "undoable": _undo_hint(services),
        }

    @app.get("/ledger", response_class=HTMLResponse)
    def ledger_page(
        request: Request, period: str = "", view: str = "expense",
        start: str = "", end: str = "", compose: int = 0, error: str = "",
        services: Services = Depends(get_services),
    ):
        return templates.TemplateResponse(
            request, "ledger.html",
            _ledger_context(services, period, view=view, start=start, end=end,
                            compose=bool(compose), error=error),
        )

    # ------------------------------------------------------------ 分类管理
    @app.get("/ledger/categories", response_class=HTMLResponse)
    def ledger_categories(
        request: Request, kind: str = "", ok: str = "", err: str = "",
        services: Services = Depends(get_services),
    ):
        chosen = kind if kind in VIEWS else "expense"
        return templates.TemplateResponse(
            request, "ledger_categories.html",
            {
                "page": "ledger",
                "kind": chosen,
                "kind_label": VIEW_LABELS[chosen],
                "categories": services.ledger.list_categories_detail(chosen),
                "ok": ok, "err": err,
                "undoable": _undo_hint(services),
            },
        )

    @app.post("/ledger/categories/add")
    def ledger_category_add(
        name: str = Form(""),
        kind: str = Form("expense"),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        chosen = kind if kind in VIEWS else "expense"
        back = "/ledger/categories?kind=" + chosen
        try:
            result = run_once(
                services, idem_token, {"category": name, "kind": chosen},
                lambda: services.ledger.create_category(
                    name=name, kind=chosen, reason="界面新建", actor="user"
                ),
            )
        except LedgerError as exc:
            return RedirectResponse(back + "&err=" + quote(str(exc)), status_code=303)
        message = "分类「" + str(result.get("name") or name) + "」已就绪"
        return RedirectResponse(back + "&ok=" + quote(message), status_code=303)

    @app.post("/ledger/categories/{category_id}/rename")
    def ledger_category_rename(
        category_id: int,
        name: str = Form(""),
        kind: str = Form("expense"),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        chosen = kind if kind in VIEWS else "expense"
        back = "/ledger/categories?kind=" + chosen
        try:
            result = run_once(
                services, idem_token, {"rename": category_id, "name": name},
                lambda: services.ledger.rename_category(category_id, name),
            )
        except LedgerError as exc:
            return RedirectResponse(back + "&err=" + quote(str(exc)), status_code=303)
        if not result.get("changed"):
            return RedirectResponse(back, status_code=303)
        message = ("「" + str(result["old_name"]) + "」改成了「" + str(result["name"]) + "」")
        return RedirectResponse(back + "&ok=" + quote(message), status_code=303)

    @app.post("/ledger/categories/{category_id}/archive")
    def ledger_category_archive(
        category_id: int,
        archived: str = Form("1"),
        kind: str = Form("expense"),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        chosen = kind if kind in VIEWS else "expense"
        back = "/ledger/categories?kind=" + chosen
        wanted = str(archived).strip() not in ("0", "false", "")
        try:
            result = run_once(
                services, idem_token, {"archive": category_id, "archived": wanted},
                lambda: services.ledger.archive_category(category_id, archived=wanted),
            )
        except LedgerError as exc:
            return RedirectResponse(back + "&err=" + quote(str(exc)), status_code=303)
        if wanted:
            message = ("「" + str(result["name"]) + "」已归档 —— 新账不再选它,已记的 "
                       + str(int(result.get("used") or 0)) + " 笔照旧")
        else:
            message = "「" + str(result["name"]) + "」已恢复,可以继续选它了"
        return RedirectResponse(back + "&ok=" + quote(message), status_code=303)

    @app.post("/ledger/add")
    def ledger_add(
        direction: str = Form("expense"),
        amount: str = Form(...),
        category: str = Form(""),
        account: str = Form("支付宝"),
        payee: str = Form(""),
        narration: str = Form(""),
        ts: str = Form(""),
        next_url: str = Form("/ledger", alias="next"),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        """界面手工记一笔(支出或收入)。**用户自己敲的,直接入账**(actor=user)。"""
        back = _safe_next(next_url)

        # ⚠️ 金额不能用 float 中转(2026-10-18 审计修):float("1e400") 是 inf,
        #    int(round(inf)) 抛 OverflowError(不在原来的捕获列表里)→ 500;
        #    "1.005" 这类十进制小数也会因二进制表示舍错。Decimal + HALF_UP 是唯一口径。
        try:
            value = Decimal(str(amount).replace(",", "").strip())
            if not value.is_finite():
                raise InvalidOperation("金额必须是有限数字")
            cents = int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        except (InvalidOperation, ValueError, ArithmeticError):
            return RedirectResponse(_with(back, "error", "金额要填数字"), status_code=303)
        if cents <= 0:
            return RedirectResponse(_with(back, "error", "金额必须大于零"), status_code=303)
        payload = {
            "direction": "income" if direction == "income" else "expense",
            "amount_cents": cents,
            "payee": payee.strip() or None,
            "narration": narration.strip() or None,
            "category": category.strip() or None,
            "account": account.strip() or "支付宝",
            "source": "ui",
            "ts": ts.strip() or None,
        }
        try:
            run_once(
                services, idem_token, payload,
                lambda: services.ledger.record_expense(payload, actor="user", status="confirmed"),
            )
        except LedgerError as exc:
            return RedirectResponse(_with(back, "error", str(exc)), status_code=303)
        return RedirectResponse(_align_view(back, payload["direction"]), status_code=303)

    @app.post("/ledger/recategorize/{txn_id}")
    def ledger_recategorize(
        txn_id: int,
        category: str = Form(""),
        payee: str = Form(""),
        next_url: str = Form("/ledger", alias="next"),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        """就地改分类/对方 —— 改完会自动写成规则,下次同样的商户自动分对。"""
        back = _safe_next(next_url)
        payload = {"txn_id": txn_id, "category": category.strip() or None,
                   "payee": payee.strip() or None}
        try:
            run_once(
                services, idem_token, payload,
                lambda: services.ledger.correct_last(
                    txn_id=txn_id, category=category.strip() or None,
                    payee=payee.strip() or None, actor="user", learn=True,
                ),
            )
        except LedgerError as exc:
            return RedirectResponse(_with(back, "error", str(exc)), status_code=303)
        return RedirectResponse(back, status_code=303)

    @app.post("/ledger/confirm/{txn_id}")
    def ledger_confirm(
        txn_id: int, next_url: str = Form("/ledger", alias="next"),
        idem_token: str = Form("", alias="_idem"), services: Services = Depends(get_services)
    ) -> RedirectResponse:
        run_once(
            services, idem_token, {"txn_id": txn_id}, lambda: services.ledger.confirm(txn_id, actor="user")
        )
        return RedirectResponse(_safe_next(next_url), status_code=303)

    @app.post("/ledger/void/{txn_id}")
    def ledger_void(
        txn_id: int, next_url: str = Form("/ledger", alias="next"),
        idem_token: str = Form("", alias="_idem"), services: Services = Depends(get_services)
    ) -> RedirectResponse:
        run_once(
            services, idem_token, {"txn_id": txn_id}, lambda: services.ledger.void(txn_id, actor="user")
        )
        return RedirectResponse(_safe_next(next_url), status_code=303)

    @app.post("/ledger/delete/{txn_id}")
    def ledger_delete(
        txn_id: int, next_url: str = Form("/ledger", alias="next"),
        idem_token: str = Form("", alias="_idem"), services: Services = Depends(get_services)
    ) -> RedirectResponse:
        """软删除 + 可撤销 —— 误点一次「删除」不该是终点。"""
        run_once(
            services, idem_token, {"txn_id": txn_id},
            lambda: services.ledger.soft_delete(txn_id, reason="web", actor="user"),
        )
        return RedirectResponse(_safe_next(next_url), status_code=303)

    @app.post("/ledger/undo")
    def ledger_undo(
        next_url: str = Form("/ledger", alias="next"),
        idem_token: str = Form("", alias="_idem"), services: Services = Depends(get_services)
    ) -> RedirectResponse:
        def action():
            try:
                services.ledger.undo_last(actor="user")
            except UndoError:
                pass  # 没有可撤销的:重定向回去,不弹错

        run_once(services, idem_token, {"undo": True}, action)
        return RedirectResponse(_safe_next(next_url), status_code=303)


def _with(url: str, key: str, value: str) -> str:
    """给跳转地址挂一个查询参数(它可能已经带了 ?view=…)。"""
    joiner = "&" if "?" in url else "?"
    return url + joiner + key + "=" + quote(str(value))
