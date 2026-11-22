"""健康页路由(纯 SSR)+ 从 iPhone 同步进来的两个入口。

三件事在这一个模块里:
  ① **页面**:页签式(今天 / 趋势 / 能量 / 同步),读取路径里没有模型;
  ② **上传**:健康 App 导出的 export.zip(首次补历史);
  ③ **接口**:POST /api/health/sync,给快捷指令 / 第三方 App 定时推。

⚠️ 2026-11-11 之前,这个模块只有「记体重」「记运动」两个表单 ——
   用户原话:*「健康领域功能太过简单。我用的 iPhone,没有电话手表,
   所以主要想从手机的健康 App 里拿到状态信息。」*
   而 HealthKit **没有服务端 API**,所以"拿"只能是手机推上来。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import date
from typing import Any

from fastapi import Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import config
from ..domain.schemas.health import HealthSyncPayload
from ..storage import db
from . import health_sync
from .deps import Services, build_services, get_services
from .idempotency import run_once
from .util import flash, int_or

#: 四个页签。默认「今天」——点开健康页最想知道的是"我现在怎么样"。
HEALTH_TABS = ("today", "trend", "energy", "sync")
#: 趋势页可选的指标(顺序就是页面上的顺序;有数据的排前面)。
TREND_METRICS = (
    "weight", "steps", "sleep_hours", "active_kcal", "resting_hr",
    "body_fat", "distance_km", "exercise_min", "hrv", "vo2max",
    "oxygen_saturation", "bp_sys",
)
#: 上传的硬上限。苹果的 export.zip 可能是几百 MB —— 比素材那 32MB 宽松,
#: 但仍然有上限:再大就该在服务器上用命令行导入,而不是走浏览器。
MAX_IMPORT_BYTES = 512 * 1024 * 1024
#: 同步接口的报文上限。一次推一年也就几十 KB;超过 2MB 一定是发错了。
MAX_SYNC_BYTES = 2 * 1024 * 1024
#: 快捷指令可以直接抄的请求体(页面上的示例)。
#: ⭐ 2026-11-14 改成**扁平**写法:快捷指令里「请求体 JSON」只填 3 个字段,
#: 不用手写嵌套数组;date 省略 = 今天(服务端时区)。
SYNC_EXAMPLE = {
    "steps": 8123,
    "active_kcal": 412,
    "exercise_min": 32,
}


def _member_id(request: Request) -> int | None:
    member = getattr(request.state, "member", None)
    return getattr(member, "id", None)


def _polyline(points: list[dict], *, width: float = 100.0, height: float = 34.0):
    """把一串点归一化成 SVG 坐标(不引图表库)。

    ⚠️ 只有 1 个点时画不出线:模板按"只画点"处理,而不是强行连成一条直线
       (那会让"只同步了一天"看起来像趋势)。
    """
    values = [float(point["value"]) for point in points]
    if not values:
        return None
    low, high = min(values), max(values)
    span = (high - low) or 1.0
    step = (width / (len(values) - 1)) if len(values) > 1 else 0.0
    coords = [
        (round(index * step, 2), round(height - (value - low) / span * (height - 4) - 2, 2))
        for index, value in enumerate(values)
    ]
    return {
        "line": " ".join(str(x) + "," + str(y) for x, y in coords),
        "area": (
            " ".join(str(x) + "," + str(y) for x, y in coords)
            + " " + str(width) + "," + str(height) + " 0," + str(height)
        ),
        "dot": coords[-1],
        "dots": coords,
        "low": round(low, 2),
        "high": round(high, 2),
        "single": len(values) == 1,
    }


def _moving_line(points: list[dict], moving: list[dict], *, width: float = 100.0, height: float = 34.0):
    """滑动平均线要跟原始点**共用同一把刻度**,否则两条线各画各的,读不出关系。"""
    if not points or not moving:
        return ""
    values = [float(point["value"]) for point in points]
    low, high = min(values), max(values)
    span = (high - low) or 1.0
    index_of = {point["day"]: index for index, point in enumerate(points)}
    step = (width / (len(points) - 1)) if len(points) > 1 else 0.0
    coords = []
    for point in moving:
        index = index_of.get(point["day"])
        if index is None:
            continue
        y = height - (float(point["value"]) - low) / span * (height - 4) - 2
        coords.append((round(index * step, 2), round(y, 2)))
    return " ".join(str(x) + "," + str(y) for x, y in coords)


def _fmt_minutes(value: float | None) -> str:
    if value is None:
        return ""
    total = int(round(value))
    if total >= 60:
        return str(total // 60) + " 小时 " + str(total % 60) + " 分"
    return str(total) + " 分钟"


def _time_hm(iso: str | None) -> str:
    text = str(iso or "")
    return text[11:16] if len(text) >= 16 else ""


def register_health_routes(app, *, templates: Jinja2Templates) -> None:
    @app.get("/health", response_class=HTMLResponse)
    def health_page(
        request: Request, tab: str = "today", metric: str = "weight",
        days: int = 90, compose: str = "",
        services: Services = Depends(get_services),
    ):
        # 查询串是用户手打的:认不出来的一律回默认,不报错(与 D111/D112 同一条纪律)。
        if tab not in HEALTH_TABS:
            tab = "today"
        if metric not in TREND_METRICS:
            metric = "weight"
        days = int_or(days, 90, low=7, high=730)

        # 体重那一块(今天页的 hero、能量页的参考)一直要用 —— 一次算完。
        trend = services.health.trend({"metric_code": "weight", "days": days})
        points = list(trend.points)
        weight_now = round(points[-1].trend, 1) if points else None
        weight_prev: float | None = None
        weight_prev_day = ""
        if points:
            last_day = date.fromisoformat(points[-1].day)
            for point in reversed(points):
                if (last_day - date.fromisoformat(point.day)).days >= 7:
                    weight_prev = round(point.trend, 1)
                    weight_prev_day = point.day
                    break
        weight_delta = (
            round(weight_now - weight_prev, 1)
            if weight_now is not None and weight_prev is not None else None
        )
        sparkline = ""
        if len(points) >= 2:
            values = [point.trend for point in points]
            low, high = min(values), max(values)
            span = (high - low) or 1.0
            step = 100.0 / (len(values) - 1)
            sparkline = " ".join(
                str(round(index * step, 2)) + ","
                + str(round(30 - (value - low) / span * 28, 2))
                for index, value in enumerate(values)
            )

        energy = services.health.energy_summary(days=7)
        history = services.health.estimate_tdee_from_history(days=21)
        today = services.health.today_snapshot()
        workouts = services.health.recent_workouts(limit=20)
        # ⚠️ energy_summary 给的是**整个 7 天的合计**,旧页面把它当"每日"写,
        #    数字整整大了 7 倍(2026-10-24 修)。这里换算成日均。
        energy_daily = None
        if not energy.get("missing"):
            span = max(1, int(energy.get("days") or 7))
            energy_daily = {
                "days": span,
                "deficit": round(float(energy.get("deficit_kcal") or 0) / span),
                "low": round(float((energy.get("deficit_range") or [0, 0])[0]) / span),
                "high": round(float((energy.get("deficit_range") or [0, 0])[1]) / span),
                "tdee": round(float(energy.get("tdee") or 0) / span),
                "intake": round(float(energy.get("intake_kcal") or 0) / span),
                "burn": round(float(energy.get("burn_kcal") or 0) / span),
            }
        today_day = date.fromisoformat(str(today.get("date")))
        week_workouts = [
            item for item in workouts
            if 0 <= (today_day - date.fromisoformat(str(item["ts"])[:10])).days < 7
        ]

        series = services.health.series(metric, days=days) if tab == "trend" else None
        chart = _polyline(series["points"]) if series else None
        moving = _moving_line(series["points"], series["moving"]) if series else ""

        # ⚠️ 覆盖域在"趋势"页也要:指标 chips 要知道哪些有数据。
        #    它只是两次 GROUP BY,几万行也毫秒级 —— 不值得为省它写两种模板。
        coverage = services.health.coverage()
        metric_types = services.health.list_metric_types()
        metric_options = []
        if coverage is not None:
            with_data = set(coverage["synced_metrics"])
            ordered = [code for code in TREND_METRICS if code in with_data]
            ordered += [code for code in TREND_METRICS if code not in with_data]
            by_code = {item["code"]: item for item in metric_types}
            for code in ordered:
                item = by_code.get(code)
                if item is None:
                    continue
                metric_options.append({**item, "has_data": code in with_data})

        member_id = _member_id(request)
        token = health_sync.get_token(services.assistant, member_id)
        endpoint = health_sync.endpoint(
            request, public_base=services.settings.health_public_base
        )
        sync_context = {
            "token": token,
            "endpoint": endpoint,
            "loopback": health_sync.is_loopback_base(endpoint),
            "syncs": services.health.recent_syncs(limit=8),
            "coverage": coverage,
            "example": SYNC_EXAMPLE,
            "public_base": services.settings.health_public_base,
        }

        return templates.TemplateResponse(
            request,
            "health.html",
            {
                "page": "health", "tab": tab, "days": days, "metric": metric,
                "trend": trend, "energy": energy, "compose": (
                    compose if compose in ("weight", "workout") else ""
                ),
                "energy_daily": energy_daily,
                "weekly_kg": (
                    round(energy_daily["deficit"] * 7 / 7700.0, 2)
                    if energy_daily else None
                ),
                "history": history, "today": today, "workouts": workouts,
                "sparkline": sparkline,
                "weight_now": weight_now, "weight_prev": weight_prev,
                "weight_prev_day": weight_prev_day, "weight_delta": weight_delta,
                "weight_days": len(points),
                "weight_min": round(min([p.trend for p in points]), 1) if points else None,
                "weight_max": round(max([p.trend for p in points]), 1) if points else None,
                "spark_last": sparkline.split(" ")[-1] if sparkline else "",
                "week_minutes": sum(int(item["duration_min"] or 0) for item in week_workouts),
                "week_count": len(week_workouts),
                # ⭐ 2026-11-21(D188):只记了个数/距离的场次没有时长,不能悄悄按 0 算
                #    —— 那会把"3 次运动"说成"0 分钟"。有几条没时长就明说几条。
                "week_untimed": sum(
                    1 for item in week_workouts if item["duration_min"] is None
                ),
                # 进度条的分母:只拿**有时长**的那些比(以前直接 max,含 None 会抛错)。
                "peak_minutes": max(
                    [int(item["duration_min"]) for item in workouts
                     if item["duration_min"] is not None] or [0]
                ) or 1,
                "today_minutes": int(today.get("met_minutes") or 0),
                "series": series, "chart": chart, "moving_line": moving,
                "metric_options": metric_options,
                "sync_context": sync_context,
                "ok": request.query_params.get("ok") or "",
                "err": request.query_params.get("err") or "",
                "fmt_minutes": _fmt_minutes,
                "time_hm": _time_hm,
            },
        )

    @app.post("/health/weight")
    def health_weight(
        value: float = Form(...),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        # ⚠️ 表单的 min/max 只是前端约束;nan 会在 SQLite 的 NOT NULL 上炸成 500,
        #    负数会污染趋势。领域层没有 ge/le,入口必须自己挡(2026-10-18 审计)。
        from math import isfinite

        if not isfinite(value) or value <= 0 or value > 1000:
            return flash("/health", "体重要是 0~1000 之间的数字", error=True)
        run_once(
            services, idem_token, {"weight": value},
            lambda: services.health.log_measurement(
                {"metric_code": "weight", "value": value, "source": "user"}, actor="user"
            ),
        )
        return RedirectResponse("/health", status_code=303)

    @app.post("/health/workout")
    def health_workout(
        activity: str = Form(...),
        minutes: str = Form(""),
        distance_km: str = Form(""),
        reps: str = Form(""),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        """手工记一次运动:时长 / 距离 / 个数,至少填一个。

        ⚠️ 2026-11-21(D188):以前 minutes 是必填的整数,表单里也只放了一个时长 ——
        "做了 10 个仰卧起坐"在网页上根本无法记录。现在三个都是可选文本(空串 =
        没填),由这里判"至少一个",再交给服务层的同一个模型去校验。
        """
        raw = {
            "duration_min": str(minutes or "").strip(),
            "distance_km": str(distance_km or "").strip(),
            "reps": str(reps or "").strip(),
        }
        values: dict[str, Any] = {}
        try:
            if raw["duration_min"]:
                values["duration_min"] = int(raw["duration_min"])
            if raw["distance_km"]:
                values["distance_km"] = float(raw["distance_km"])
            if raw["reps"]:
                values["reps"] = int(raw["reps"])
        except ValueError:
            return flash("/health", "时长与个数填整数,距离填数字(公里)", error=True)
        if not values:
            return flash("/health", "时长、距离、个数至少填一个 —— 填哪个记哪个,不估算", error=True)
        if "duration_min" in values and not 1 <= values["duration_min"] <= 1440:
            return flash("/health", "时长要在 1~1440 分钟之间", error=True)
        if "distance_km" in values and not 0 <= values["distance_km"] <= 1000:
            return flash("/health", "距离要在 0~1000 公里之间", error=True)
        if "reps" in values and not 1 <= values["reps"] <= 5000:
            return flash("/health", "个数要在 1~5000 之间", error=True)
        payload = {"activity": activity.strip(), **values}
        try:
            run_once(
                services, idem_token, payload,
                lambda: services.health.log_workout(payload, actor="user"),
            )
        except ValueError as exc:  # 模型层的"至少一个维度"等校验
            return flash("/health", str(exc)[:120], error=True)
        return RedirectResponse("/health", status_code=303)

    # -- ① 上传导出的 zip / xml -------------------------------------------
    @app.post("/health/import")
    def health_import(
        file: UploadFile = File(...),
        idem_token: str = Form("", alias="_idem"),
        services: Services = Depends(get_services),
    ) -> RedirectResponse:
        """把健康 App 导出的 export.zip 整个吃进来。

        ⚠️ 流式落盘,不 read() 全文 —— 几百 MB 的文件一次读进内存会把进程打死。
        解析在同一个请求里做完(会等一会儿),但导入本身是幂等的(upsert)。
        """
        target_dir = config.DATA_DIR / "health" / "imports"
        target_dir.mkdir(parents=True, exist_ok=True)
        suffix = ".zip" if str(file.filename or "").lower().endswith(".zip") else ".xml"
        target = target_dir / (uuid.uuid4().hex + suffix)
        digest = hashlib.sha256()
        size = 0
        try:
            with target.open("wb") as handle:
                while True:
                    chunk = file.file.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_IMPORT_BYTES:
                        return flash(
                            "/health?tab=sync",
                            "文件太大(上限 " + str(MAX_IMPORT_BYTES // 1024 // 1024) + "MB);"
                            "再大请在服务器上用命令行导入",
                            error=True,
                        )
                    digest.update(chunk)
                    handle.write(chunk)
            if not size:
                return flash("/health?tab=sync", "没有收到文件", error=True)

            def _run() -> RedirectResponse:
                try:
                    report = services.health.import_apple_health(str(target))
                except Exception as exc:  # noqa: BLE001 - 文件坏了要说人话
                    return flash(
                        "/health?tab=sync",
                        "这个文件读不了:" + str(exc)[:200],
                        error=True,
                    )
                message = (
                    "已导入 " + str(report["written"]) + " 条记录"
                    + ("、" + str(report["sleep_nights"]) + " 夜睡眠" if report.get("sleep_nights") else "")
                    + ("、" + str(report["workout_count"]) + " 次运动" if report.get("workout_count") else "")
                    + ";未知类型跳过 " + str(report["skipped_unknown"]) + " 条"
                )
                return flash("/health?tab=sync", message)

            return run_once(
                services, idem_token, {"health_import": digest.hexdigest()}, _run,
                replay=flash("/health?tab=sync", "这份文件已经导入过了"),
            )
        finally:
            target.unlink(missing_ok=True)

    # -- ② 同步口令 -------------------------------------------------------
    @app.post("/health/sync/token")
    def health_sync_token(
        request: Request, services: Services = Depends(get_services)
    ) -> RedirectResponse:
        health_sync.rotate_token(services.assistant, _member_id(request))
        return flash("/health?tab=sync", "新的同步口令已生成,旧口令立即失效")

    # -- ③ 设备推送接口 ---------------------------------------------------
    @app.get("/api/health/sync")
    async def health_sync_info(request: Request) -> JSONResponse:
        """给快捷指令"试一下"用的:带对口令就回最近一次同步。

        ⚠️ 不返回健康数值 —— 这个端点的用处是确认"地址 + 口令"通不通。
        """
        token = _bearer(request)
        conn = db.connect_assistant()
        try:
            valid, member_id = health_sync.resolve_token(conn, token)
            from ..family import members as member_store

            member = member_store.get_member(conn, member_id) if member_id else None
        finally:
            conn.close()
        if not valid:
            return JSONResponse({"ok": False, "error": "口令不对"}, status_code=403)
        services = build_services(member=member)
        try:
            last = services.health.last_sync()
            return JSONResponse({
                "ok": True,
                "endpoint": health_sync.endpoint(request),
                "last_sync": last,
                "hint": "POST 这个地址,正文是 {\"steps\":8123,\"active_kcal\":412};"
                        "不写 date 就是今天;口令放 Authorization: Bearer <token>",
            })
        finally:
            services.close()

    @app.post("/api/health/sync")
    async def health_sync_push(request: Request) -> JSONResponse:
        """设备/快捷指令的写入口。

        鉴权只有 token 一条(没有 cookie);其余四道闸:
          ① Content-Length 与真实字节都 ≤ 2MB;
          ② pydantic 结构校验;
          ③ 数值合理性区间(见 health_service.PLAUSIBLE_RANGES);
          ④ 同一天重复推 = 覆盖(upsert),不会越积越多。
        """
        raw = await request.body()
        if len(raw) > MAX_SYNC_BYTES:
            return JSONResponse(
                {"ok": False, "error": "报文太大(上限 2MB)"}, status_code=413
            )
        token = _bearer(request)
        conn = db.connect_assistant()
        try:
            valid, member_id = health_sync.resolve_token(conn, token)
            from ..family import members as member_store

            member = member_store.get_member(conn, member_id) if member_id else None
        finally:
            conn.close()
        if not valid:
            return JSONResponse({"ok": False, "error": "口令不对"}, status_code=403)
        try:
            payload = HealthSyncPayload.model_validate_json(raw or b"{}")
        except Exception as exc:  # noqa: BLE001 - 报文结构错,给 400 而不是 500
            return JSONResponse(
                {"ok": False, "error": "报文格式不对", "detail": str(exc)[:300]},
                status_code=400,
            )
        services = build_services(member=member)
        try:
            ip = request.client.host if request.client else ""
            result = services.health.sync_push(
                payload, source=payload.source or "shortcut", ip=ip,
                ua=str(request.headers.get("user-agent") or "")[:200],
            )
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)[:200]}, status_code=400)
        finally:
            services.close()
        return JSONResponse(result)


def _bearer(request: Request) -> str:
    """口令只认 Authorization: Bearer;兼容 X-Health-Token,但**不认 URL 参数**。

    ⚠️ 写接口的 URL 会进反代日志,口令拼在 URL 上等于把写权限写进日志。
    """
    auth = str(request.headers.get("authorization") or "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return str(request.headers.get("x-health-token") or "").strip()
