"""iPhone 把健康数据推上来:一次性口令 + 端点地址。

与日历订阅(calendar_subscribe.py)同一套"链接是能力"的做法,但**方向相反**:
日历那条是**只读**的 GET,这条是**可写**的 POST。所以多三条纪律:

  1. 口令只走 Authorization: Bearer <token> 头,**不要拼进 URL** ——
     写接口的 URL 会进 nginx access log,日志泄露就等于写权限泄露;
  2. 口令按成员分开;一旦库里已经有 PIN,升级前的"裸口令"立即失效
     (否则家庭版里一条旧链接能写进管理员的健康数据);
  3. 页面上随时可重置,旧口令立即失效(同一个 key 被覆盖)。

⚠️ HealthKit **没有服务端 API**:服务器不可能自己去手机上拉。数据只能是
手机推过来(快捷指令 / 第三方 App),或者用户自己在电脑上传导出的 zip。
"""

from __future__ import annotations

import hmac
import secrets
import sqlite3
from typing import Any

from ..ops import settings_store

#: token 家族:无成员时用裸键;有成员时按 member id 分开。
TOKEN_KEY = "health_sync_token"
TOKEN_PREFIX = TOKEN_KEY + ":"
#: 端点路径(页面与文档共用一份;改这里不会漏改另一处)。
SYNC_PATH = "/api/health/sync"


def _key(member_id: int | None) -> str:
    return TOKEN_KEY if member_id is None else TOKEN_PREFIX + str(int(member_id))


def get_token(conn: sqlite3.Connection | None, member_id: int | None) -> str:
    try:
        return (settings_store.get_raw(conn, _key(member_id)) or "").strip()
    except Exception:  # noqa: BLE001 - 读不到就当作"还没生成",页面给生成按钮
        return ""


def rotate_token(conn: sqlite3.Connection, member_id: int | None) -> str:
    """生成新口令,**旧口令立即失效**。"""
    token = secrets.token_urlsafe(32)
    settings_store.set_text(conn, _key(member_id), token)
    return token


def resolve_token(conn: sqlite3.Connection, token: str) -> tuple[bool, int | None]:
    """口令 -> (是否有效, 成员 id)。

    ⚠️ 与日历订阅同一条边界:库里已经有 PIN 之后,"裸键"(无成员)的旧口令
       不能再写 —— 否则升级家庭版后旧链接仍然是管理员写权限。
    """
    text = str(token or "").strip()
    if not text:
        return False, None
    try:
        from ..family import members as member_store

        pin_enabled = member_store.has_any_pin(conn)
    except Exception:  # noqa: BLE001 - 探测失败按"没有 PIN"处理(单用户旧库)
        pin_enabled = False
    rows = conn.execute(
        "SELECT key, value FROM sys_settings WHERE key = ? OR key LIKE ?",
        (TOKEN_KEY, TOKEN_PREFIX + "%"),
    ).fetchall()
    for row in rows:
        if not hmac.compare_digest(text, str(row["value"] or "")):
            continue
        key = str(row["key"] or "")
        if key == TOKEN_KEY:
            if pin_enabled:
                return False, None
            return True, None
        try:
            return True, int(key[len(TOKEN_PREFIX):])
        except ValueError:
            continue
    return False, None


def request_base(request: Any) -> str:
    """这次请求自己的绝对地址(反代后面认 X-Forwarded-*)。"""
    headers = getattr(request, "headers", {}) or {}
    proto = str(headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    host = str(headers.get("x-forwarded-host") or "").split(",")[0].strip()
    scheme = proto or str(getattr(getattr(request, "url", None), "scheme", "") or "http")
    host = host or str(headers.get("host") or "")
    port = str(headers.get("x-forwarded-port") or "").split(",")[0].strip()
    if port and host and not (scheme == "http" and port == "80") and not (
        scheme == "https" and port == "443"
    ):
        bracketed = host.startswith("[")
        has_port = ("]:" in host) if bracketed else (":" in host)
        if not has_port:
            host = host + ":" + port
    return (scheme + "://" + host).rstrip("/")


def endpoint(request: Any, *, public_base: str = "") -> str:
    """给手机看的完整端点。公网地址优先(手机连不上 127.0.0.1)。"""
    base = (public_base or "").strip().rstrip("/") or request_base(request)
    return base + SYNC_PATH


def is_loopback_base(url: str) -> bool:
    """这个地址是不是只有本机能用 —— 页面要据此提醒"手机推不过来"。"""
    text = str(url or "").lower()
    return (
        "://127.0.0.1" in text or "://localhost" in text or "://[::1]" in text
        or text.startswith("127.0.0.1") or text.startswith("localhost")
    )
