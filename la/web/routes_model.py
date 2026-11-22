"""设置页 · 模型与语音(D157)。

把"改 .env + 重启"变成"在页面填、下一轮生效":
  * 密钥加密落库(app_secrets),明文不落 HTML / 日志 / 事件;
  * 版本号 config_rev 让各进程重建 Settings,不用重启;
  * 二次验 PIN(sudo)才允许看与改;
  * 非 HTTPS 且非回环的连接**拒绝接收密钥** —— 云上走 nginx 时这是底线。
"""

from __future__ import annotations

import os
import time
from urllib.parse import urlparse

import httpx
from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import config
from ..family import members as member_store
from ..ops import secrets as secret_store
from . import sudo
from .deps import Services, get_services
from .util import flash

#: 测试连接的进程内限频(每个成员 5 秒一次)
_LAST_TEST: dict[int, float] = {}
TEST_INTERVAL_S = 5.0

#: 回环名字。密钥与 PIN 只允许在这些连接上发送。
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "testclient"})


def secret_channel_ok(
    *, scheme: str, forwarded_proto: str, client_host: str, hostname: str
) -> bool:
    """密钥/PIN 能不能过这条连接:HTTPS 或回环可以,其余一律不行。

    ⚠️ 云上走 nginx 时,到进程这一跳是 http,给浏览器的是 https ——
       所以必须同时看 X-Forwarded-Proto(由代理写入,伪造它只会让自己更容易被拦)。
    """
    if os.getenv("LA_ALLOW_INSECURE_SECRETS", "").strip().lower() in (
        "1", "true", "yes", "on"
    ):
        return True
    if scheme == "https" or forwarded_proto == "https":
        return True
    if (client_host or "").strip() in LOOPBACK_HOSTS:
        return True
    return (hostname or "").strip().lower() in ("localhost", "127.0.0.1", "::1")


def register_model_routes(app, *, templates) -> None:
    def _admin(request: Request, services: Services):
        member = getattr(request.state, "member", None)
        if member is None:
            return None, RedirectResponse("/setup", status_code=303)
        if str(member.role) != "admin":
            return None, HTMLResponse(
                "<h1>只有管理员能打开设置</h1>", status_code=403
            )
        return member, None

    def _is_https(request: Request) -> bool:
        proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        return request.url.scheme == "https" or proto == "https"

    def _secure(request: Request) -> bool:
        proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        return secret_channel_ok(
            scheme=request.url.scheme,
            forwarded_proto=proto,
            client_host=(request.client.host if request.client else "") or "",
            hostname=request.url.hostname or "",
        )

    def _needs_tls_message() -> str:
        return (
            "这个连接不是 HTTPS,不接受 PIN 与密钥。请先给站点上 TLS;"
            "临时要用可在 .env 里设 LA_ALLOW_INSECURE_SECRETS=1"
        )

    @app.post("/settings/sudo")
    def settings_sudo(
        request: Request,
        pin: str = Form(...),
        services: Services = Depends(get_services),
    ):
        member, denied = _admin(request, services)
        if denied is not None:
            return denied
        if not _secure(request):
            return flash("/settings?tab=model", _needs_tls_message(), error=True)
        if not member_store.verify_pin(services.assistant, member.id, pin):
            return flash("/settings?tab=model", "PIN 不对", error=True)
        token = sudo.grant(services.assistant, member.id)
        response = RedirectResponse("/settings?tab=model", status_code=303)
        response.set_cookie(
            sudo.COOKIE,
            token,
            max_age=sudo.TTL_SECONDS,
            httponly=True,
            samesite="lax",
            secure=_is_https(request),
            path="/",
        )
        return response

    @app.post("/settings/model")
    def model_save(
        request: Request,
        provider: str = Form(""),
        base_url: str = Form(""),
        model: str = Form(""),
        reasoner_model: str = Form(""),
        reasoning_effort: str = Form(""),
        api_key: str = Form(""),
        tts_provider: str = Form(""),
        tts_voice: str = Form(""),
        tts_api_key: str = Form(""),
        clear: str = Form(""),
        services: Services = Depends(get_services),
    ):
        member, denied = _admin(request, services)
        if denied is not None:
            return denied
        if not sudo.valid(services.assistant, request, member.id):
            return flash("/settings?tab=model", "请先输入 PIN 解锁", error=True)
        if not _secure(request):
            return flash("/settings?tab=model", _needs_tls_message(), error=True)
        conn = services.assistant
        try:
            if clear:
                if clear in secret_store.SECRET_NAMES:
                    secret_store.clear_secret(conn, clear)
                elif clear in secret_store.PUBLIC_NAMES:
                    secret_store.clear_setting(conn, clear)
                else:
                    return flash("/settings?tab=model", "未知的清除项", error=True)
                return flash(
                    "/settings?tab=model",
                    "已清除:" + clear + "(回到文件里的默认值)",
                )
            if provider.strip() and provider.strip() not in config.VALID_PROVIDERS:
                return flash("/settings?tab=model", "不认识的服务商:" + provider, error=True)
            if reasoning_effort.strip() and reasoning_effort.strip() not in config.VALID_EFFORTS:
                return flash("/settings?tab=model", "思考强度只能是 low/high/max", error=True)
            if base_url.strip():
                parsed = urlparse(base_url.strip())
                if parsed.scheme not in ("http", "https") or not parsed.netloc:
                    return flash("/settings?tab=model", "接口地址要写成 https://... 的完整地址", error=True)
            current = config.get_settings()
            # ⚠️ 只写"真的改过"的项:表单里预填的就是当前值,原样提交也写库的话,
            #    等于把文件里的默认值冻进库 —— 以后改 .env 就再也不生效了。
            for key, value, old in (
                ("llm.provider", provider, current.provider),
                ("llm.base_url", base_url, current.base_url),
                ("llm.model", model, current.model),
                ("llm.reasoner_model", reasoner_model, current.reasoner_model),
                ("llm.reasoning_effort", reasoning_effort, current.reasoning_effort),
                ("tts.provider", tts_provider, current.tts_provider),
                ("tts.voice", tts_voice, current.tts_voice),
            ):
                text = value.strip()
                if text and text.rstrip("/") != str(old).rstrip("/"):
                    secret_store.set_setting(conn, key, text)
            if api_key.strip():
                secret_store.set_secret(conn, "llm.api_key", api_key, member_id=member.id)
            if tts_api_key.strip():
                secret_store.set_secret(conn, "tts.api_key", tts_api_key, member_id=member.id)
            conn.commit()
        except ValueError as exc:
            return flash("/settings?tab=model", str(exc), error=True)
        return flash("/settings?tab=model", "已保存,下一轮对话就生效(不用重启)")

    @app.post("/settings/model/test")
    def model_test(
        request: Request,
        api_key: str = Form(""),
        base_url: str = Form(""),
        services: Services = Depends(get_services),
    ):
        member, denied = _admin(request, services)
        if denied is not None:
            return denied
        if not sudo.valid(services.assistant, request, member.id):
            return flash("/settings?tab=model", "请先输入 PIN 解锁", error=True)
        if not _secure(request):
            return flash("/settings?tab=model", _needs_tls_message(), error=True)
        now = time.monotonic()
        last = _LAST_TEST.get(int(member.id), 0.0)
        if now - last < TEST_INTERVAL_S:
            return flash("/settings?tab=model", "刚测过,5 秒内不再重复请求", error=True)
        _LAST_TEST[int(member.id)] = now
        settings = config.get_settings()
        key = (api_key or "").strip() or settings.api_key
        url = ((base_url or "").strip() or settings.base_url).rstrip("/")
        if not key:
            return flash("/settings?tab=model", "还没有密钥可测:先填一个,或保存后再测", error=True)
        started = time.monotonic()
        try:
            response = httpx.get(
                url + "/models",
                headers={"Authorization": "Bearer " + key},
                timeout=10.0,
            )
        except httpx.TimeoutException:
            return flash("/settings?tab=model", "连接超时(10 秒):检查网络/代理", error=True)
        except httpx.HTTPError as exc:
            return flash(
                "/settings?tab=model",
                "连不上:" + type(exc).__name__ + "(检查接口地址与网络)",
                error=True,
            )
        cost = time.monotonic() - started
        if response.status_code != 200:
            hints = {
                401: "密钥无效或被吊销",
                403: "这个密钥没有权限",
                404: "接口地址可能少了 /v1",
            }
            hint = hints.get(response.status_code, "接口返回了错误")
            return flash(
                "/settings?tab=model",
                "失败 " + str(response.status_code) + ":" + hint,
                error=True,
            )
        try:
            payload = response.json()
            names = [str(item.get("id", "")) for item in (payload.get("data") or [])]
        except Exception:  # noqa: BLE001 - 返回体不是约定格式也不该 500
            names = []
        return flash(
            "/settings?tab=model",
            "连接正常 · %.2fs · 可用模型:%s" % (cost, ", ".join(names[:6]) or "(接口没列)"),
        )

