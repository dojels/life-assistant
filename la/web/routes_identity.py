"""身份 / 设置:登录、登出、首次安装、恢复 PIN、成员与微信绑定。

从 create_app() 搬出来的第十一块业务路由(2026-10-18 架构收口)。
纪律不变:没有 PIN 的库不拦;一旦设了 PIN,除白名单外都要求登录。
"""

from __future__ import annotations

import secrets
from urllib.parse import quote

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .. import config
from ..domain import ownership
from ..ops import secrets as secret_store
from ..family import identity
from ..family import members as member_store
from ..storage import db
from . import auth as web_auth
from . import sudo
from .deps import Services, get_services
from .routes_model import secret_channel_ok
from .routes_wechat import accounts_summary, qr_payload
from .util import flash, safe_next
from .visibility import web_session


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.2) -> bool:
    """本机某个端口有没有人在听。

    用途只有一个:提示"8788 的独立登录页还开着" —— 两边同时扫码会互相顶掉
    同一个登录会话。探测失败一律当没开(别为了一个提示把页面弄 500)。
    """
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


def register_identity_routes(app, *, templates) -> None:
    # ---------------------------------------------------------------- 身份层
    #
    # 纪律:**没有 PIN 的库不拦**。这样升级上来的单用户库、以及所有测试,
    # 在第一次 /setup 之前行为不变;一旦设了 PIN,除白名单外全部要求登录。
    # ⚠️ 这不是"安全可选项":家庭版安装向导的第一页就是设 PIN。
    _AUTH_PUBLIC = frozenset({
        "/login", "/setup", "/recover", "/healthz", "/favicon.ico",
        # iPhone 的日历订阅客户端不会带登录 cookie;它只认 URL 里的随机 token。
        # ⚠️ 这个白名单只放行**路径**;token 校验在 /calendar/ics 路由内部完成。
        "/calendar/ics",
    })

    def _is_loopback(request: Request) -> bool:
        host = (request.client.host if request.client else "") or ""
        return host in ("127.0.0.1", "::1", "localhost", "testclient")

    def _cookie_secure(request: Request) -> bool:
        # 云端部署通常在反向代理后面:到进程这一跳是 http,给浏览器的却是 https。
        # 只看 request.url.scheme 会把 cookie 降成非 Secure;X-Forwarded-Proto 由
        # 代理写入(伪造它最多让 cookie 变 Secure,不构成提权)。
        forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        return request.url.scheme == "https" or forwarded == "https"

    def _set_session_cookie(response: Response, request: Request, token: str) -> None:
        response.set_cookie(
            web_auth.SESSION_COOKIE,
            token,
            max_age=web_auth.SESSION_TTL_DAYS * 86400,
            httponly=True,
            samesite="lax",
            secure=_cookie_secure(request),
            path="/",
        )

    @app.middleware("http")
    async def _auth_guard(request: Request, call_next):  # noqa: ANN202
        request.state.member = None
        path = request.url.path
        if path.startswith("/static") or path in ("/healthz", "/favicon.ico"):
            return await call_next(request)
        # ⭐ iPhone 日历订阅:客户端不会带 cookie,只认 URL 里的随机 token。
        #    这里只放行**路径**,token 校验在路由内部(无效一律 403)。
        if path == "/calendar/ics":
            return await call_next(request)
        # ⭐ iPhone 健康同步(2026-11-11):快捷指令/第三方 App 不会带 cookie,
        #    只带 Authorization: Bearer <同步口令>。这里只放行**路径**,
        #    口令校验在路由内部(无效一律 403)。⚠️ 它是**写**入口 ——
        #    所以口令只认请求头,不认 URL 参数(URL 会进反代日志)。
        if path == "/api/health/sync":
            return await call_next(request)

        conn = db.connect_assistant()
        try:
            enabled = member_store.has_any_pin(conn)
            if not enabled:
                return await call_next(request)
            token = request.cookies.get(web_auth.SESSION_COOKIE, "")
            member = web_auth.resolve_session(conn, token) if token else None
        finally:
            conn.close()

        if member is not None:
            request.state.member = member
            if path in ("/login", "/setup", "/recover"):
                return RedirectResponse("/chat", status_code=303)
            return await call_next(request)

        if path in ("/login", "/setup", "/recover"):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"ok": False, "error": "请先登录"}, status_code=401)
        if request.method in ("GET", "HEAD"):
            target = "/login"
            if path and path != "/":
                target += "?next=" + quote(path)
            return RedirectResponse(target, status_code=303)
        return JSONResponse({"ok": False, "error": "请先登录"}, status_code=401)

    def _bootstrap_allowed(request: Request, token: str) -> bool:
        """本机直接放行;云端必须带 LA_BOOTSTRAP_TOKEN。没有配置就只允许本机。"""
        if _is_loopback(request):
            return True
        expected = config.get_settings().bootstrap_token
        return bool(expected) and secrets.compare_digest(str(token or ""), expected)

    def _setup_page(
        request: Request, *, token: str = "", error: str = "", status_code: int = 200
    ):
        conn = db.connect_assistant()
        try:
            if member_store.has_any_pin(conn):
                return RedirectResponse("/login", status_code=303)
            rows = member_store.list_members(conn)
            admin = rows[0] if rows else None
        finally:
            conn.close()
        return templates.TemplateResponse(
            request, "setup.html",
            {"page": "setup", "admin_name": admin.name if admin else "管理员",
             "error": error, "bootstrap_token": str(token or "")},
            status_code=status_code,
        )

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(request: Request, token: str = ""):
        if not _bootstrap_allowed(request, token):
            return HTMLResponse(
                "云端首次安装需要一次性口令:在服务端设置 LA_BOOTSTRAP_TOKEN,<br>"
                "再用 /setup?token=&lt;口令&gt; 打开本页。完成后请从环境变量里删掉它。",
                status_code=403,
            )
        return _setup_page(request, token=token)

    @app.post("/setup", response_class=HTMLResponse)
    def setup_submit(
        request: Request,
        pin: str = Form(...),
        pin2: str = Form(...),
        bootstrap_token: str = Form("", alias="token"),
    ):
        if not _bootstrap_allowed(request, bootstrap_token):
            return HTMLResponse(
                "云端首次安装需要一次性口令(LA_BOOTSTRAP_TOKEN)。", status_code=403
            )
        if not member_store.PIN_PATTERN.match(str(pin)):
            return _setup_page(request, token=bootstrap_token,
                               error="PIN 必须是 6 位数字", status_code=400)
        if pin != pin2:
            return _setup_page(request, token=bootstrap_token,
                               error="两次输入的 PIN 不一样", status_code=400)
        conn = db.connect_assistant()
        try:
            if member_store.has_any_pin(conn):
                return RedirectResponse("/login", status_code=303)
            admin_id = identity.ensure_default_member(conn)
            member_store.set_pin(conn, admin_id, pin)
            recovery_code = member_store.set_recovery(conn, admin_id)
            session_token = web_auth.create_session(conn, admin_id)
        except ValueError as exc:
            return _setup_page(request, token=bootstrap_token,
                               error=str(exc), status_code=400)
        finally:
            conn.close()
        response = templates.TemplateResponse(
            request, "setup_recovery.html", {"code": recovery_code}
        )
        _set_session_cookie(response, request, session_token)
        return response

    def _login_page(
        request: Request, *, selected: int = 0, next_url: str = "",
        error: str = "", status_code: int = 200,
    ):
        conn = db.connect_assistant()
        try:
            if not member_store.has_any_pin(conn):
                return RedirectResponse("/setup", status_code=303)
            rows = [
                row for row in member_store.list_members(conn, active_only=True)
                if row.has_pin
            ]
        finally:
            conn.close()
        if not rows:
            return RedirectResponse("/setup", status_code=303)
        chosen = selected or rows[0].id
        return templates.TemplateResponse(
            request, "login.html",
            {"members": rows, "selected": chosen, "next_url": safe_next(next_url),
             "error": error},
            status_code=status_code,
        )

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = ""):
        return _login_page(request, next_url=next)

    @app.post("/login", response_class=HTMLResponse)
    def login_submit(
        request: Request,
        member_id: int = Form(...),
        pin: str = Form(...),
        next: str = Form(""),
    ):
        host = (request.client.host if request.client else "") or "?"
        conn = db.connect_assistant()
        try:
            if not member_store.has_any_pin(conn):
                return RedirectResponse("/setup", status_code=303)
            if not web_auth.allow_attempt(conn, "login", member_id, host):
                return _login_page(
                    request, selected=member_id, next_url=next,
                    error="尝试次数太多,请一分钟后再试", status_code=429,
                )
            member = member_store.get_member(conn, member_id)
            if member is None or not member.active or not member.has_pin:
                web_auth.record_failure(conn, "login", member_id, host)
                return _login_page(
                    request, selected=member_id, next_url=next,
                    error="成员或 PIN 不对", status_code=400,
                )
            if not member_store.verify_pin(conn, member_id, pin):
                web_auth.record_failure(conn, "login", member_id, host)
                return _login_page(
                    request, selected=member_id, next_url=next,
                    error="成员或 PIN 不对", status_code=400,
                )
            web_auth.clear_failures(conn, "login", member_id, host)
            token = web_auth.create_session(conn, member_id)
        finally:
            conn.close()
        response = RedirectResponse(safe_next(next), status_code=303)
        _set_session_cookie(response, request, token)
        return response

    @app.post("/logout")
    def logout(request: Request):
        token = request.cookies.get(web_auth.SESSION_COOKIE, "")
        if token:
            conn = db.connect_assistant()
            try:
                web_auth.revoke_session(conn, token)
            finally:
                conn.close()
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(web_auth.SESSION_COOKIE, path="/")
        return response


    def _recover_page(
        request: Request, *, selected: int = 0, error: str = "",
        status_code: int = 200,
    ):
        conn = db.connect_assistant()
        try:
            if not member_store.has_any_pin(conn):
                return RedirectResponse("/setup", status_code=303)
            rows = [
                row for row in member_store.list_members(conn, active_only=True)
                if row.has_pin and member_store.has_recovery(conn, row.id)
            ]
        finally:
            conn.close()
        if not rows:
            return HTMLResponse(
                "没有可用的恢复码。请运行 "
                "<code>python scripts/reset_pin.py --member &lt;id&gt; --pin &lt;新PIN&gt; --yes</code>。",
                status_code=400,
            )
        chosen = selected or rows[0].id
        return templates.TemplateResponse(
            request, "recover.html",
            {"members": rows, "selected": chosen, "error": error},
            status_code=status_code,
        )

    @app.get("/recover", response_class=HTMLResponse)
    def recover_page(request: Request):
        # 云端部署可能没有“本机浏览器”;恢复码本身就是凭证,靠下面的限流防爆破。
        return _recover_page(request)

    @app.post("/recover", response_class=HTMLResponse)
    def recover_submit(
        request: Request,
        member_id: int = Form(...),
        code: str = Form(...),
        pin: str = Form(...),
        pin2: str = Form(...),
    ):
        host = (request.client.host if request.client else "") or "?"
        if not member_store.PIN_PATTERN.match(str(pin)):
            return _recover_page(
                request, selected=member_id, error="新 PIN 必须是 6 位数字",
                status_code=400,
            )
        if pin != pin2:
            return _recover_page(
                request, selected=member_id, error="两次输入的新 PIN 不一样",
                status_code=400,
            )
        conn = db.connect_assistant()
        try:
            if not member_store.has_any_pin(conn):
                return RedirectResponse("/setup", status_code=303)
            if not web_auth.allow_attempt(conn, "recover", member_id, host):
                return _recover_page(
                    request, selected=member_id,
                    error="尝试次数太多,请一分钟后再试", status_code=429,
                )
            member = member_store.get_member(conn, member_id)
            if (
                member is None or not member.active or not member.has_pin
                or not member_store.has_recovery(conn, member_id)
                or not member_store.verify_recovery(conn, member_id, code)
            ):
                web_auth.record_failure(conn, "recover", member_id, host)
                return _recover_page(
                    request, selected=member_id,
                    error="恢复码不对(它区分大小写,但会自动忽略连字符)",
                    status_code=400,
                )
            try:
                member_store.set_pin(conn, member_id, pin)
                web_auth.revoke_all_for_member(conn, member_id)
                new_code = member_store.set_recovery(conn, member_id)
                token = web_auth.create_session(conn, member_id)
                web_auth.clear_failures(conn, "recover", member_id, host)
            except ValueError as exc:
                return _recover_page(
                    request, selected=member_id, error=str(exc), status_code=400
                )
        finally:
            conn.close()
        response = templates.TemplateResponse(
            request, "recover_done.html", {"code": new_code}
        )
        _set_session_cookie(response, request, token)
        return response

    def _settings_guard(request: Request):
        member = getattr(request.state, "member", None)
        if member is None:
            return RedirectResponse("/setup", status_code=303)
        if str(member.role) != "admin":
            return HTMLResponse(
                "<h1>只有管理员能打开设置</h1><p>请让管理员来改成员与微信绑定。</p>",
                status_code=403,
            )
        return None

    def _render_settings(
        request: Request,
        tab: str,
        ok: str,
        err: str,
        account: str,
        services: Services,
    ):
        """三个页签共用一处装配:数据只取当前页签需要的那些。"""
        member = getattr(request.state, "member", None)
        rows = member_store.list_members(services.assistant)
        bindings = [
            dict(row)
            for row in services.assistant.execute(
                "SELECT b.channel, b.external_id, b.member_id, m.name"
                " FROM app_member_bindings b"
                " LEFT JOIN app_members m ON m.id = b.member_id"
                " ORDER BY b.channel, b.external_id"
            ).fetchall()
        ]
        bound = {str(row["external_id"]) for row in bindings}
        pending: list[str] = []
        try:
            sessions = services.memory.execute(
                "SELECT DISTINCT session_id FROM events WHERE session_id LIKE 'bridge:%'"
            ).fetchall()
            pending = sorted({
                str(sid).split(":", 1)[1]
                for (sid,) in sessions
                if ":" in str(sid) and str(sid).split(":", 1)[1] not in bound
            })
        except Exception:  # noqa: BLE001 - 设置页不该因为读不到旧会话而 500
            pending = []
        share_modes = [
            {
                "domain": domain,
                "label": ownership.DOMAIN_LABELS[domain],
                "mode": ownership.get_share_mode(services.assistant, domain),
            }
            for domain in ownership.SHAREABLE_DOMAINS
        ]
        fixed_modes = [
            {"domain": domain, "label": ownership.DOMAIN_LABELS[domain]}
            for domain in ownership.DOMAINS
            if domain not in ownership.SHAREABLE_DOMAINS
        ]
        context = {
            "page": "settings", "tab": tab,
            "members": rows, "bindings": bindings, "pending": pending,
            "ok": ok, "err": err,
            "share_modes": share_modes, "fixed_modes": fixed_modes,
            "role_labels": {"admin": "管理员", "parent": "家长", "child": "孩子"},
        }
        if tab == "model":
            entries = {item.key: item for item in secret_store.describe(services.assistant)}
            context.update({
                "entries": entries,
                "settings": config.get_settings(),
                "sudo_ok": sudo.valid(services.assistant, request, member.id),
                "secure": secret_channel_ok(
                    scheme=request.url.scheme,
                    forwarded_proto=(request.headers.get("x-forwarded-proto") or "")
                    .split(",")[0].strip().lower(),
                    client_host=(request.client.host if request.client else "") or "",
                    hostname=request.url.hostname or "",
                ),
            })
        elif tab == "wechat":
            accounts = accounts_summary()
            names = {row["name"] for row in accounts}
            chosen = account if account in names else ""
            qr = qr_payload(chosen)
            context.update({
                "accounts": accounts,
                "account": chosen,
                "svg": qr["svg"],
                "status": qr["status"],
                "expires_in": qr["expires_in"],
                "other_login_open": _port_open(8788),
            })
        return templates.TemplateResponse(request, "settings.html", context)

    def _guard_and_member(request: Request):
        member = getattr(request.state, "member", None)
        if member is None:
            return None, RedirectResponse("/setup", status_code=303)
        if str(member.role) != "admin":
            return None, HTMLResponse(
                "<h1>只有管理员能打开设置</h1><p>请让管理员来改成员与微信绑定。</p>",
                status_code=403,
            )
        return member, None

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(
        request: Request, tab: str = "general", ok: str = "", err: str = "",
        account: str = "",
        services: Services = Depends(get_services),
    ):
        member, denied = _guard_and_member(request)
        if denied is not None:
            return denied
        wanted = tab if tab in ("general", "wechat", "model") else "general"
        return _render_settings(request, wanted, ok, err, account, services)

    # 老链接(以及上一版页面里的按钮)继续能用:内容一样,只是强制到对应页签。
    @app.get("/settings/model", response_class=HTMLResponse)
    def settings_model_page(
        request: Request, ok: str = "", err: str = "",
        services: Services = Depends(get_services),
    ):
        member, denied = _guard_and_member(request)
        if denied is not None:
            return denied
        return _render_settings(request, "model", ok, err, "", services)

    @app.get("/settings/wechat", response_class=HTMLResponse)
    def settings_wechat_page(
        request: Request, ok: str = "", err: str = "", account: str = "",
        services: Services = Depends(get_services),
    ):
        member, denied = _guard_and_member(request)
        if denied is not None:
            return denied
        return _render_settings(request, "wechat", ok, err, account, services)

    @app.post("/settings/share")
    def settings_share(
        request: Request,
        domain: str = Form(...),
        mode: str = Form(...),
        services: Services = Depends(get_services),
    ):
        denied = _settings_guard(request)
        if denied is not None:
            return denied
        try:
            ownership.set_share_mode(services.assistant, domain, mode)
        except ValueError as exc:
            return flash("/settings", str(exc), error=True)
        return flash("/settings", "已保存:只影响以后写入的数据")

    @app.post("/settings/members/add")
    def settings_member_add(
        request: Request,
        name: str = Form(...),
        role: str = Form("parent"),
        pin: str = Form(...),
        services: Services = Depends(get_services),
    ):
        denied = _settings_guard(request)
        if denied is not None:
            return denied
        try:
            member = member_store.create_member(services.assistant, name, role=role, pin=pin)
        except ValueError as exc:
            return flash("/settings", str(exc), error=True)
        return flash("/settings", "已添加成员:" + member.name)

    @app.post("/settings/members/{member_id}/pin")
    def settings_member_pin(
        request: Request,
        member_id: int,
        pin: str = Form(...),
        services: Services = Depends(get_services),
    ):
        denied = _settings_guard(request)
        if denied is not None:
            return denied
        try:
            member_store.set_pin(services.assistant, member_id, pin)
            # ⚠️ 重置 PIN 必须把该成员已有票据一起撤掉(2026-10-18 审计修):
            #    PIN 是"孩子设备丢了"的唯一止损手段;不撤票的话旧 cookie 还能用 30 天,
            #    重置等于没做。与 /recover 那条路径同一口径。
            web_auth.revoke_all_for_member(services.assistant, member_id)
        except ValueError as exc:
            return flash("/settings", str(exc), error=True)
        return flash("/settings", "PIN 已重置;该成员已登录的设备需要重新登录")

    @app.post("/settings/members/{member_id}/deactivate")
    def settings_member_deactivate(
        request: Request,
        member_id: int,
        services: Services = Depends(get_services),
    ):
        denied = _settings_guard(request)
        if denied is not None:
            return denied
        current = getattr(request.state, "member", None)
        if current is not None and int(member_id) == int(current.id):
            return flash("/settings", "不能停用当前登录的成员", error=True)
        try:
            member_store.deactivate_member(services.assistant, member_id)
        except ValueError as exc:
            return flash("/settings", str(exc), error=True)
        return flash("/settings", "已停用;该成员的登录票据也一并撤销")

    @app.post("/settings/bind")
    def settings_bind(
        request: Request,
        external_id: str = Form(...),
        member_id: int = Form(...),
        services: Services = Depends(get_services),
    ):
        denied = _settings_guard(request)
        if denied is not None:
            return denied
        external = str(external_id or "").strip()
        if external.startswith("bridge:"):
            external = external[len("bridge:"):]
        if not external:
            return flash("/settings", "微信联系人不能为空", error=True)
        try:
            identity.bind_member(services.assistant, "weixin", external, member_id)
        except ValueError as exc:
            return flash("/settings", str(exc), error=True)
        return flash("/settings", "已绑定:" + external)

    @app.post("/settings/unbind")
    def settings_unbind(
        request: Request,
        external_id: str = Form(...),
        services: Services = Depends(get_services),
    ):
        denied = _settings_guard(request)
        if denied is not None:
            return denied
        identity.unbind_member(services.assistant, "weixin", str(external_id or "").strip())
        return flash("/settings", "已解绑")

