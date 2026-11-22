"""设置页 · 微信登录(D158):二维码、状态轮询、账号列表。

⭐ 页面本身在 `/settings?tab=wechat`(D159 页签化),这里只负责**动作**:
   要码、查状态、通知重连,以及给页面提供账号摘要。

⭐ 与 8788 那个独立登录页的关系:
   * 这里负责"在设置页里点一下就扫",适合日常换号;
   * 8788 保留当救生艇(Web 起不来 / 库锁了 / 首次安装还没跑通时);
   * 两边共用 `weixin_login.poll_once` 与 `weixin_store`,不各写一份扫码流程。

⚠️ 扫码成功之后**不重启进程**:写 `.relogin` 标志,桥接在下一轮循环边界自己重连。
   云端 Web 以普通用户跑,本来也没有 systemctl 权限。
"""

from __future__ import annotations

import os
import time
from datetime import datetime

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..bridge.adapters import weixin_store
from ..bridge.adapters.weixin_login_session import LoginSessionManager
from ..bridge.adapters.weixin_qr import render_svg
from .deps import Services, get_services
from .util import flash

#: 进程内单例。测试可以替换它(注入假客户端,不打真网关)。
MANAGER = LoginSessionManager()

#: 生成二维码的限频(每个成员 3 秒)。网关对扫码窗口有节流,别把它刷爆。
_LAST_START: dict[int, float] = {}
START_INTERVAL_S = 3.0


def accounts_summary() -> list[dict]:
    """给页面用的账号摘要。**只读**,不触发任何网络请求。"""
    rows: list[dict] = []
    for name in weixin_store.known_accounts():
        creds = weixin_store.load(name)
        path = weixin_store.credentials_path(name)
        updated = ""
        if path.exists():
            updated = datetime.fromtimestamp(path.stat().st_mtime).strftime(
                "%Y-%m-%d %H:%M"
            )
        rows.append({
            "name": name,
            "label": name or "默认账号",
            "logged_in": creds is not None,
            "bot_id": (creds.bot_id[:6] + "…") if creds and creds.bot_id else "",
            "updated_at": updated,
            "relogin_pending": weixin_store.relogin_path(name).exists(),
        })
    return rows


def qr_payload(account: str) -> dict:
    """当前会话的展示数据(给页签用)。没有会话就返回 idle。"""
    session = MANAGER.current(account)
    if session is None:
        return {"status": "idle", "svg": "", "expires_in": 0}
    svg = ""
    if session.url and session.status in ("waiting", "scaned"):
        try:
            svg = render_svg(session.url, module=5, quiet_zone=2)
        except ValueError:
            svg = ""
    return {
        "status": session.status,
        "svg": svg,
        "expires_in": MANAGER.expires_in(account),
    }


def register_wechat_routes(app, *, templates) -> None:
    def _admin(request: Request, services: Services):
        member = getattr(request.state, "member", None)
        if member is None:
            return None, RedirectResponse("/setup", status_code=303)
        if str(member.role) != "admin":
            return None, HTMLResponse(
                "<h1>只有管理员能打开设置</h1>", status_code=403
            )
        return member, None

    def _target(account: str) -> str:
        names = {row["name"] for row in accounts_summary()}
        return account if account in names else ""

    @app.post("/settings/wechat/qr")
    async def wechat_qr(
        request: Request,
        account: str = Form(""),
        services: Services = Depends(get_services),
    ):
        member, denied = _admin(request, services)
        if denied is not None:
            return denied
        target = _target(account)
        now = time.monotonic()
        last = _LAST_START.get(int(member.id), 0.0)
        if now - last < START_INTERVAL_S:
            return flash(
                "/settings?tab=wechat&account=" + target,
                "刚生成过,3 秒内不再向网关要新码",
                error=True,
            )
        _LAST_START[int(member.id)] = now
        try:
            await MANAGER.start(target)
        except Exception as exc:  # noqa: BLE001 - 网关不通时给一句人话
            return flash(
                "/settings?tab=wechat&account=" + target,
                "拿不到二维码:" + type(exc).__name__ + "(检查服务器能否访问微信网关)",
                error=True,
            )
        return RedirectResponse(
            "/settings?tab=wechat&account=" + target, status_code=303
        )

    @app.get("/settings/wechat/state")
    async def wechat_state(
        request: Request,
        account: str = "",
        services: Services = Depends(get_services),
    ):
        member, denied = _admin(request, services)
        if denied is not None:
            return JSONResponse({"status": "denied"}, status_code=403)
        session = await MANAGER.poll(account)
        if session is None:
            return JSONResponse({"status": "idle", "account": account})
        payload = {
            "status": session.status,
            "account": account,
            "bot_id": session.credentials.bot_id if session.credentials else "",
            "error": session.error,
            "expires_in": MANAGER.expires_in(account),
            "svg": "",
        }
        if session.url and session.status in ("waiting", "scaned"):
            try:
                payload["svg"] = render_svg(session.url, module=5, quiet_zone=2)
            except ValueError:
                payload["svg"] = ""
        return JSONResponse(payload)

    @app.post("/settings/wechat/restart")
    def wechat_restart(
        request: Request,
        account: str = Form(""),
        services: Services = Depends(get_services),
    ):
        member, denied = _admin(request, services)
        if denied is not None:
            return denied
        target = _target(account)
        weixin_store.request_relogin(target)
        hint = "已通知重连,桥接会在下一轮轮询时自己接上新凭据(通常 5 秒内)。"
        if os.name != "nt":
            unit = "life-assistant-wechat" + ("@" + target if target else "")
            hint += " 若 30 秒内仍无反应,在服务器执行:systemctl restart " + unit
        return flash("/settings?tab=wechat&account=" + target, hint)

