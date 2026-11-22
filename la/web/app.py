"""Web 应用:五个入口,一套服务层。

⭐ **最强约束:界面的读取路径里绝不能有 LLM。**
   账本 / 日历 / 健康 / 错题四个页面全部是纯 SSR —— 模型挂了、API 欠费了,
   你仍然能打开它们看数据、改数据。聊天页才用到模型,而且失败也会优雅降级。

**写入动作全部是普通表单 POST + 重定向**(不是 fetch):
即使浏览器禁用了 JS,记账确认、记体重、记复习结果这些操作照样能用。
JS 只用来增强聊天页的流式输出(步骤账本、流式思考、写入回执与撤销)。

⭐ **M7 起 AI 写入的口径变了(D65)**:默认**直写** —— 领域写入直接执行,
不再停在"请到对应页面确认"。安全网是**写入回执 + 一键撤销**:
每次写入在同一个事务里落一条 `ops_receipts`,聊天页在回执卡上就地带「撤销」按钮,
五个域(账本/日历/健康/错题/事项)共用这一套机制。

两道开关,别搞混:
  * `LA_READ_ONLY=1` —— **总闸**,所有写工具直接返回 READ_ONLY,优先级最高;
  * `LA_DIRECT_WRITE=0` —— 退回老行为:risk=confirm 的操作仍等用户放行。
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import ipaddress
import json
import os
import sys
import time
import uuid
from html import escape as _escape_html
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from urllib.parse import urlparse

from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import config
from ..family import identity
from .util import (
    MAX_UPLOAD_BYTES as MAX_WEB_UPLOAD_BYTES,
    flash as _flash, int_or as _int_or, safe_next as _safe_next,
)
from .visibility import (
    asset_visible as _asset_visible,
    member_can_see as _member_can_see,
    owner_id_visible as _owner_id_visible,
    question_visible as _question_visible,
    receipt_visible as _receipt_visible,
    trace_sessions as _trace_sessions,
    web_session as _web_session,
)
from .idempotency import run_once, run_once_async
from .chat_support import _build_client
from .routes_health import register_health_routes
from .routes_journal import register_journal_routes
from .routes_identity import register_identity_routes
from .routes_calendar import register_calendar_routes
from .routes_chat import register_chat_routes
from .routes_ledger import register_ledger_routes
from .routes_materials import register_material_routes
from .routes_memory import register_memory_routes
from .routes_wake import register_wake_routes
from .routes_model import register_model_routes
from .routes_wechat import register_wechat_routes
from .routes_persona import register_persona_routes
from .routes_questions import register_question_routes
from .routes_receipts import register_receipt_routes
from .routes_study import register_study_routes
from .routes_trace import register_trace_routes
from ..base.html_page import HTML_PREVIEW_CSP
from ..agent.prompts import IDENTITY
from ..agent.session import MemorySession
from ..domain.materials import HANDLED_LABELS
from ..domain.receipts import DOMAIN_SYSTEM, ReceiptError
from ..domain.routers.ledger import LedgerRouter
from ..domain.schemas.study import SUBJECTS
from ..llm.client import DeepSeekClient
from ..llm.fake import FakeClient
from ..memory.store import MemoryStore
from ..ops.idempotency import IdempotencyConflict
from ..tasks.schedules import ScheduleError
from ..tasks.todos import SOURCE_USER, STATUS_COMPLETED, STATUS_PENDING
from ..web.deps import Services, build_intake, build_services, get_services
from ..web.markdown import render_inline, render_markdown
from . import task_view

#: 轨迹页的三个页签(D119)。⭐ 用户 2026-10-14:"优化轨迹界面视觉效果,让用户获取更清晰的信息,
#: 可以采用页标签切换" —— 与 D111 / D112 同一套做法(纯链接 + 服务端只渲染当前那一页):
#: 「上下文」是点开就要看的那一份(**默认**),「规则」是读一次的参考材料,「历史」是翻旧账的地方。
#: 过滤器三个取值(全部 / 只看对话 / 只看系统)也在这里收敛:认不出来的不报错。
TRACE_TAB_CONTEXT = "context"
TRACE_TAB_RULES = "rules"
TRACE_TAB_HISTORY = "history"
TRACE_TABS = (TRACE_TAB_CONTEXT, TRACE_TAB_RULES, TRACE_TAB_HISTORY)
#: ⭐ D136:轨迹页那一排页签上的第四个入口 —— **使用报表**(独立页面 /report)。
TRACE_TAB_REPORT = "report"

TRACE_ONLY_ALL = "all"
TRACE_ONLY_TALK = "talk"
TRACE_ONLY_SYSTEM = "system"
TRACE_ONLY_VALUES = (TRACE_ONLY_ALL, TRACE_ONLY_TALK, TRACE_ONLY_SYSTEM)


def normalize_trace_tab(value: str) -> str:
    """轨迹页签收敛到白名单。认不出来就落到「上下文」,不报错(与 D111/D112 同一条纪律)。

    ⚠️ 这里有一条**向后兼容**:改版前 /trace 只有一页,"往前翻一轮"靠 `?i=` ——
       那些老链接不带 tab,而默认正好是「上下文」,所以它们仍然落在用户当时想看的那一页。
    """
    wanted = str(value or "").strip().lower()
    return wanted if wanted in TRACE_TABS else TRACE_TAB_CONTEXT


def normalize_trace_only(value: str) -> str:
    """消息过滤器收敛到白名单(全部 / 只看对话 / 只看系统)。认不出来 = 全部。"""
    wanted = str(value or "").strip().lower()
    return wanted if wanted in TRACE_ONLY_VALUES else TRACE_ONLY_ALL


def _yuan(cents: int | None) -> str:
    """分 -> "1,234.56"。**只做展示**;金额本身仍然是 SQL 聚合出来的整数分。"""
    value = int(cents or 0)
    sign = "-" if value < 0 else ""
    whole, fen = divmod(abs(value), 100)
    return sign + format(whole, ",") + "." + str(fen).rjust(2, "0")


def _cn_date(value: Any) -> str:
    """datetime -> "10月11日 周六"。星期几不依赖系统 locale(Windows 上 %a 会出中文)。"""
    try:
        return (
            str(value.month) + "月" + str(value.day) + "日 "
            + "周" + "一二三四五六日"[value.weekday()]
        )
    except Exception:  # noqa: BLE001 - 展示层不该因为一个日期格式把页面打不开
        return str(value)


# 进程启动时刻。用来发现「代码比进程新」—— 见 stale_code_hint()。
_STARTED_AT = time.time()
_MTIME_CACHE: dict[str, Any] = {"at": 0.0, "newest": 0.0}


def _newest_source_mtime() -> float:
    """**本进程真正加载过的** la/*.py 里,最新的那个修改时间(5 秒缓存)。

    ⚠️ 只统计 sys.modules 里的模块,而不是"目录下所有 .py":
    这个仓库里同时可以有别的入口与别的进程(例如 la/bridge 的渠道适配器、
    终端 sidecar)。改那些文件**不影响 Web 页面**,不该让用户看到"请重启"。
    (实测踩过:另一个会话在改 bridge 适配器,账本页顶部就冒出了重启提示。)
    """
    now = time.time()
    if now - float(_MTIME_CACHE["at"]) < 5.0:
        return float(_MTIME_CACHE["newest"])
    newest = 0.0
    for name, module in list(sys.modules.items()):
        if name != "la" and not name.startswith("la."):
            continue
        path = getattr(module, "__file__", None)
        if not path or not str(path).endswith(".py"):
            continue
        try:
            newest = max(newest, os.path.getmtime(path))
        except OSError:
            continue
    _MTIME_CACHE.update({"at": now, "newest": newest})
    return newest


WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


def stale_code_hint() -> str:
    """代码比进程新 -> 返回一句「请重启」的提示,否则空串。

    为什么需要它(不是多余的贴心):Jinja 的模板**每次请求都重新读盘**,
    而 Python 代码不会。于是"改了代码没重启"的表现是 —— 模板用新字段去读旧对象,
    页面直接 500,而且白底什么都没有,只能靠猜(实测踩过:改完账本页,旧进程里
    点「账本」就是这个 500,原因是旧的 summary 对象没有 net_cents)。
    """
    if _newest_source_mtime() > _STARTED_AT + 1.0:
        return (
            "界面模板已经更新,但这个进程还在跑旧代码 —— 请重启:"
            "Ctrl+C 之后重新 python -m la --web"
        )
    return ""


def _error_page(title: str, detail: str, hint: str) -> str:
    """错误页。**三个参数全部转义**(2026-10-18 审计修)。

    ⚠️ detail 里会带用户输入:例如 /ledger?period=<script>… 的 LedgerError 文案
       原样包含 period。这个函数以前直接拼字符串,于是错误页成了反射型 XSS 的落点。
    """
    title = _escape_html(str(title))
    detail = _escape_html(str(detail))
    hint = _escape_html(str(hint))
    extra = ""
    if hint:
        extra = (
            "<p style=\"margin-top:14px;padding:10px 12px;border-radius:10px;"
            "background:rgba(255,149,0,0.14);color:#8a5a00\">" + hint + "</p>"
        )
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<title>" + title + "</title></head>"
        "<body style=\"font-family:-apple-system,Segoe UI,sans-serif;background:#f2f2f7;"
        "color:#1c1c1e;padding:40px;line-height:1.6\">"
        "<div style=\"max-width:760px;margin:0 auto\">"
        "<h1 style=\"font-size:24px\">" + title + "</h1>"
        "<p style=\"color:#555\">这一页没能画出来。下面是具体原因(原始数据没有被动过):</p>"
        "<pre style=\"background:#fff;border:1px solid rgba(60,60,67,0.12);border-radius:12px;"
        "padding:14px;overflow:auto;font-size:13px\">" + detail + "</pre>"
        + extra
        + "<p><a href=\"/chat\">回聊天页</a> · <a href=\"/healthz\">自检</a></p>"
        "</div></body></html>"
    )


def _tool_catalog_names(services: Services) -> tuple[str, ...]:
    """当前会话的**工具注册表全集**(给使用报表用)。

    ⭐ 用户 2026-10-16:*"工具要求全列,我可以通过成功率进行工具的优化"*。
    注册表在会话里(**每轮发给模型的那一份**),而报表读的是历史事件 ——
    两者取并集才能回答"哪些工具一次都没调过"。

    ⚠️ 只读、不启动会话、不发任何请求;读不到就返回空(页面会如实说
    "这一次没读到注册表",而不是编一份名单出来)。
    """
    try:
        from ..agent.session import MemorySession
        from ..llm.fake import FakeClient

        session = MemorySession(
            FakeClient(), services.settings,
            store=services.store,
            session_id="__catalog__",
            assistant_conn=services.assistant,
            auto_approve=True, auto_briefing=False, surface="catalog",
        )
        return tuple(session.registry.names())
    except Exception:  # noqa: BLE001 - 报表不该因为读不到注册表就打不开
        return ()


def create_app(*, with_ticker: bool = False) -> FastAPI:
    """装配 Web 应用。

    ⚠️ `with_ticker` 默认 **False**:后台心跳会按真实时间派发推送,
    而测试/验收脚本需要的是**确定性**(它们的时钟、数据目录都是临时的)。
    只有 `python -m la --web` 才把它打开 —— 见 la/web/ticker.py。
    """
    app = FastAPI(title="默 · Family Agent", docs_url="/api/docs", redoc_url=None)
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    # 模板里直接用安全渲染器
    templates.env.globals["render_markdown"] = lambda text: render_markdown(text or "").html
    # 行内版:摘要里那一条条"**11:00 会议**"要变成粗体,而不是把星号原样吐给用户。
    # 它同样**先转义再白名单解析**(与 render_markdown 同一套),所以模型写的摘要
    # 不可能借此变成标签。
    templates.env.globals["render_inline"] = render_inline
    # 每个写表单带一枚一次性令牌;重复 POST 同一个令牌 = 重复提交,不是两次操作。
    templates.env.globals["idem"] = lambda: uuid.uuid4().hex
    # 展示层小工具。**纯格式化,没有任何业务判断** —— 数字仍然全部来自 SQL。
    templates.env.globals["yuan"] = _yuan
    templates.env.globals["cn_date"] = _cn_date

    def _status_label(status: Any) -> str:
        from ..domain.services.ledger_service import STATUS_LABELS

        return STATUS_LABELS.get(str(status), str(status))

    def _source_label(source: Any) -> str:
        from ..domain.services.ledger_service import SOURCE_LABELS

        text = str(source or "")
        return SOURCE_LABELS.get(text, text)

    # 状态与来源的人话标签:口径与领域层**同一份**(不在这里再抄一遍)
    templates.env.globals["status_label"] = _status_label
    templates.env.globals["source_label"] = _source_label
    # 模板里可以直接问"你现在跑的代码是不是旧的"(空串表示正常)
    templates.env.globals["stale_code_hint"] = stale_code_hint
    # 月历格子的日期跳转目标(只是拼个锚点,不做业务判断)
    templates.env.globals["day_anchor"] = lambda key: "#d-" + str(key)

    # ---------------------------------------------------------------- 同源守卫
    #
    # 威胁模型(2026-10-17 审计**实测**):这一站没有任何鉴权,写操作也不带 CSRF 令牌 ——
    # `_idem` 是**幂等**令牌,不是 CSRF 令牌(不带它反而"不做幂等保护,原样执行")。
    # 于是任何网页都能朝 127.0.0.1:8765 发一个跨源表单 POST:改人格(下一轮起就是
    # 攻击者的提示词)、删账、跑一整轮对话。浏览器拦得住"读响应",拦不住"发请求"。
    #
    # 三道很便宜的措施,**都不改变正常使用**:
    #   ① 写方法校验 Origin/Referer 的 host 必须等于这次请求自己的 Host。
    #      没有这两个头的请求(curl、脚本、测试客户端)照旧放行 —— 浏览器发起的
    #      跨源请求一定会带上它们,所以这条挡的正是"别的网页"。
    #   ② X-Frame-Options + frame-ancestors:不许被别人的页面 iframe 套住骗点击。
    #      用 SAMEORIGIN 而不是 DENY:将来若要同源内嵌(比如把预览放进卡片)不必改回来。
    #   ③ nosniff:下载与素材路由不再靠浏览器"猜"类型。
    _WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
    _SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
        ("X-Frame-Options", "SAMEORIGIN"),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "same-origin"),
        ("Content-Security-Policy", "frame-ancestors 'self'"),
    )

    def _host_of(raw: str) -> str:
        """从 Host 头或绝对 URL 里取出主机名(去端口、去方括号、小写)。

        ⚠️ 不能用 split(":") —— IPv6 的 [::1]:8765 会被切成 "["。
        """
        text = (raw or "").strip()
        if not text:
            return ""
        try:
            parsed = urlparse(text if "//" in text else "//" + text)
        except ValueError:
            return ""
        return (parsed.hostname or "").strip().lower()

    def _host_allowed(raw: str) -> bool:
        """这台机器认哪些 Host(防 DNS rebinding,2026-10-18 审计修)。

        * 回环名与 testserver:本机访问;
        * **任何 IP 字面量**:局域网用 192.168.x.x 访问是正常用法;
        * 其余域名一律拒绝 —— rebinding 必须借一个域名,而 IP 字面量不会变。

        ⚠️ 只比 Origin 与 Host 是不够的:两者都由客户端给,攻击者让两者相等即可。
        要用默认家庭部署(电脑名/自定义域名)访问,把名字写进 LA_WEB_HOSTS。
        """
        text = (raw or "").strip().lower().strip("[]")
        if text in ("localhost", "127.0.0.1", "::1", "testserver"):
            return True
        try:
            ipaddress.ip_address(text)
            return True
        except ValueError:
            pass
        extra = {str(item).strip().lower() for item in config.get_settings().web_allowed_hosts}
        return text in extra

    def _same_origin(value: str, mine: str) -> bool:
        """这个 Origin/Referer 是不是"发给自己"的。

        空值一律放行(非浏览器客户端没有这个东西);`null` 一律拒(沙箱 iframe)。
        """
        text = (value or "").strip()
        if not text:
            return True
        if text.lower() == "null":
            return False
        return bool(mine) and _host_of(text) == mine

    @app.middleware("http")
    async def _same_origin_guard(request: Request, call_next):  # noqa: ANN202
        # ⚠️ Host 白名单对**所有**方法生效:DNS rebinding 之后浏览器与服务同源,
        #    GET 页面也能被读走(不只是写)。
        if not _host_allowed(_host_of(request.headers.get("host", ""))):
            return Response(
                status_code=403, media_type="text/plain; charset=utf-8",
                content="这个 Host 不被认可。用 127.0.0.1 / localhost / 局域网 IP 访问;"
                        "要用别的名字,请在 .env 里设 LA_WEB_HOSTS=<你的域名>。",
            )
        if request.method in _WRITE_METHODS:
            mine = _host_of(request.headers.get("host", ""))
            origin = request.headers.get("origin", "")
            referer = request.headers.get("referer", "")
            # Origin 优先:它比 Referer 更严格(跨源 POST 一定有 Origin)
            if not _same_origin(origin if origin else referer, mine):
                return Response(
                    status_code=403, media_type="text/plain; charset=utf-8",
                    content="跨站点写入已拒绝:这一个请求不是从本机页面发起的。"
                            "如果你的浏览器插件改写了 Origin,请在设置里放行 127.0.0.1。",
                )
        response = await call_next(request)
        for name, value in _SECURITY_HEADERS:
            response.headers.setdefault(name, value)
        return response

    @app.exception_handler(IdempotencyConflict)
    async def _idem_conflict(request: Request, exc: IdempotencyConflict):  # noqa: ANN202
        """同一个令牌被不同载荷复用 / 上一次还在处理中 —— 明确 409,不假装成功。"""
        detail = (
            "这一个操作令牌已经用于另一次提交,或上一次还在处理中;"
            "为避免重复写入,这次没有执行。请刷新页面后重试。"
        )
        if request.url.path.startswith("/api/"):
            return JSONResponse({"ok": False, "error": detail}, status_code=409)
        return HTMLResponse(_error_page("这次提交没有执行", detail, ""), status_code=409)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):  # noqa: ANN202
        """没接住的异常:给一页**看得懂**的说明,而不是白底 500。

        ⚠️ 最关键的一条线索是 stale_code_hint():这个项目最常见的 500 就是
        "代码改了、进程没重启"(模板每次请求重读,Python 代码不会)。
        轨迹照旧打到 stderr,不吞异常。
        """
        import traceback

        traceback.print_exc()
        message = type(exc).__name__ + ": " + str(exc)[:300]
        hint = stale_code_hint()
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                {"error": type(exc).__name__, "message": str(exc)[:300], "hint": hint},
                status_code=500,
            )
        return HTMLResponse(
            _error_page("这一页出错了", message, hint), status_code=500
        )

    # ---------------------------------------------------------------- 身份层 / 设置
    register_identity_routes(app, templates=templates)
    register_model_routes(app, templates=templates)
    register_wechat_routes(app, templates=templates)

    # ---------------------------------------------------------------- 素材 / 产物 / 预览 / 附件
    register_material_routes(app, templates=templates)

    # ---------------------------------------------------------------- 首页
    @app.get("/", response_class=HTMLResponse)
    def index() -> RedirectResponse:
        # ⚠️ 从 /ledger 改成 /chat:主入口是"说话",数据页是五个并列的去处。
        #    账本页仍然照旧可直达,/ledger 上没有任何东西依赖这个重定向。
        return RedirectResponse("/chat", status_code=307)

    # ---------------------------------------------------------------- 回执与撤销
    # ---------------------------------------------------------------- 回执与撤销
    register_receipt_routes(app)

    @app.get("/healthz")
    def healthz(services: Services = Depends(get_services)) -> dict[str, Any]:
        return {
            "status": "ok",
            "model_configured": bool(services.settings.api_key),
            "provider": services.settings.provider,
            "srs_engine": services.settings.srs_engine,
        }

    # ---------------------------------------------------------------- 账本
    register_ledger_routes(app, templates=templates)

    # ---------------------------------------------------------------- 账本 REST(薄壳)
    # ⭐ 2026-10-18 架构收口:`domain/routers/ledger.py` 以前只被测试引用(死层)。
    #    这里把它挂成真正的 JSON API —— HTML 页面仍走 SSR,但 REST 请求体/错误映射
    #    与 LLM 工具共用同一个 service 与同一份 Pydantic schema(能力域 §6.3)。
    def _api_response(result: Any) -> JSONResponse:
        return JSONResponse(jsonable_encoder(result.body), status_code=int(result.status))

    @app.post("/api/v1/ledger/expenses")
    def api_ledger_create(body: dict[str, Any], services: Services = Depends(get_services)):
        return _api_response(LedgerRouter(services.ledger).create_expense(body or {}))

    @app.post("/api/v1/ledger/expenses/query")
    def api_ledger_query(body: dict[str, Any], services: Services = Depends(get_services)):
        return _api_response(LedgerRouter(services.ledger).query_expenses(body or {}))

    @app.get("/api/v1/ledger/expenses/{txn_id}")
    def api_ledger_get(txn_id: int, services: Services = Depends(get_services)):
        return _api_response(LedgerRouter(services.ledger).get_expense(txn_id))

    @app.post("/api/v1/ledger/expenses/{txn_id}/confirm")
    def api_ledger_confirm(txn_id: int, services: Services = Depends(get_services)):
        return _api_response(LedgerRouter(services.ledger).confirm_expense(txn_id))

    # ---------------------------------------------------------------- 提问与回答
    register_question_routes(app)

    # ---------------------------------------------------------------- 日历
    register_calendar_routes(app, templates=templates)

    # ---------------------------------------------------------------- 健康
    register_health_routes(app, templates=templates)
    register_journal_routes(app, templates=templates)

    # ---------------------------------------------------------------- 错题
    register_study_routes(app, templates=templates)

    # ---------------------------------------------------------------- 记忆
    register_memory_routes(app, templates=templates)

    # ---------------------------------------------------------------- 人格
    register_persona_routes(app, templates=templates)

    # ---------------------------------------------------------------- 唤醒
    register_wake_routes(app, templates=templates)

    # ---------------------------------------------------------------- 轨迹 / 报表
    register_trace_routes(
        app, templates=templates, trace_sessions=_trace_sessions,
        tool_catalog=_tool_catalog_names,
        normalize_tab=normalize_trace_tab, normalize_only=normalize_trace_only,
        tab_report=TRACE_TAB_REPORT,
    )

    # ---------------------------------------------------------------- 聊天
    # ---------------------------------------------------------------- 聊天
    register_chat_routes(app, templates=templates)

    if with_ticker:
        from .ticker import start_ticker

        ticker = start_ticker()
        if ticker is not None:
            print("[ticker] 后台心跳已启动:每分钟检查一次「到点该推的错题复习」")
        app.state.ticker = ticker

        # 定时唤醒的跑批线程。⚠️ 放到 startup 事件里,不在 create_app 里启动:
        #   create_app 会被测试/脚本调用,建一个后台线程去跑模型既不确定也没必要。
        app.state.wake_runner = None

        @app.on_event("startup")
        def _start_wake() -> None:
            from ..tasks.wake_runner import start_wake_runner

            runner = start_wake_runner()
            app.state.wake_runner = runner
            if runner is not None:
                print("[wake] 定时唤醒已启动:每 30 秒看一次;顺序由 /wake 页管理")

        @app.on_event("shutdown")
        def _stop_wake() -> None:
            runner = getattr(app.state, "wake_runner", None)
            if runner is not None:
                runner.stop()
                runner.join(timeout=2.0)
    return app


app = create_app()
