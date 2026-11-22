"""iPhone 日历订阅:一次性生成的只读链接 + 日程/待办合并成一份 ICS。

为什么不是 CalDAV:
  * iPhone 的「添加已订阅的日历」只吃一个只读 URL,不需要双向同步;
  * 双向要处理鉴权、冲突合并、改期回写 —— 那是另一个量级的工程;
  * 这个项目的数据在本机/云端,真正需要的是"手机上看得到",不是"在手机上改"。

三条纪律:
  1. **链接是能力**:知道 URL 就能读日历。所以只生成随机 token,存在
     sys_settings(按成员分开),页面上可以随时重置让旧链接立即失效;
  2. **不写 GET**:页面第一次打开不会偷偷生成 token —— 用户点「生成订阅链接」
     才写,重置也是显式动作;
  3. **只读**:这个路由只回 text/calendar,没有任何写入口;token 无效一律 403,
     不回显"这个 token 属于谁"。
"""

from __future__ import annotations

import hmac
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .. import config
from ..ops import settings_store
from . import task_view

#: token 家族:无成员时用裸键;有成员时按 member id 分开,谁的链接只出谁的数据。
TOKEN_KEY = "calendar_ics_token"
TOKEN_PREFIX = TOKEN_KEY + ":"


def _key(member_id: int | None) -> str:
    return TOKEN_KEY if member_id is None else TOKEN_PREFIX + str(int(member_id))


def get_token(conn: sqlite3.Connection | None, member_id: int | None) -> str:
    try:
        return (settings_store.get_raw(conn, _key(member_id)) or "").strip()
    except Exception:  # noqa: BLE001 - 读不到就当作"还没生成",页面会给出生成按钮
        return ""


def rotate_token(conn: sqlite3.Connection, member_id: int | None) -> str:
    """生成一条新链接,**旧链接立即失效**(同一个 key 被覆盖)。"""
    token = secrets.token_urlsafe(32)
    settings_store.set_text(conn, _key(member_id), token)
    return token


def resolve_token(
    conn: sqlite3.Connection, token: str
) -> tuple[bool, int | None]:
    """token -> (是否有效, 成员 id)。无成员=旧单用户库/未启用 PIN。

    ⚠️ 一旦库里已经有 PIN,裸键(无成员)的旧链接必须失效 —— 否则升级家庭版后,
       一条升级前生成的链接仍然能读到管理员视角的全量数据。
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
        suffix = key[len(TOKEN_PREFIX):]
        try:
            member_id = int(suffix)
        except ValueError:
            continue
        return True, member_id
    return False, None


def _public_base(request: Any) -> str:
    """给手机看的绝对地址。反代后面用 X-Forwarded-*;没有就用请求自己的 host。"""
    headers = getattr(request, "headers", {}) or {}
    forwarded_proto = str(headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    forwarded_host = str(headers.get("x-forwarded-host") or "").split(",")[0].strip()
    scheme = forwarded_proto or str(getattr(getattr(request, "url", None), "scheme", "") or "http")
    host = forwarded_host or str(headers.get("host") or "") or str(
        getattr(getattr(request, "url", None), "netloc", "") or ""
    )
    # ⚠️ 2026-10-22 真机:nginx 的 proxy_set_header Host $host 会丢掉端口,
    #    生成 http://203.0.113.10/... —— 80 端口不通,手机点下载会超时。
    #    现在 nginx 改成 $http_host 并补 X-Forwarded-Port;这里再兜一层。
    forwarded_port = str(headers.get("x-forwarded-port") or "").split(",")[0].strip()
    if forwarded_port and host and not (scheme == "http" and forwarded_port == "80") and not (
        scheme == "https" and forwarded_port == "443"
    ):
        bracketed = host.startswith("[")
        has_port = ("]:" in host) if bracketed else (":" in host)
        if not has_port:
            host = host + ":" + forwarded_port
    return (scheme + "://" + host).rstrip("/")


def link_info(request: Any, token: str, *, phone_base: str = "") -> dict[str, Any]:
    """页面要显示的一组地址;没有 token 时全部为空。

    ⭐ `phone_base` = **手机该用的公网地址**(config: LA_CALENDAR_PUBLIC_BASE)。
    2026-10-31 起公网只放行 /calendar/ics 一条只读入口 —— 手机不装 Tailscale 也能订阅,
    但它**打不开**其它页面。所以页面上"给手机的那一条"与"当前这一页的地址"要分开:
    前者能复制去 iPhone,后者只给电脑/尾网用。
    """
    base = _public_base(request)
    host = base.split("://", 1)[1] if "://" in base else base
    loopback = host.split(":", 1)[0] in ("127.0.0.1", "localhost", "::1", "[::1]")
    ics_url = ""
    webcal_url = ""
    download_url = ""
    phone_ics = ""
    phone_webcal = ""
    if token:
        path = "/calendar/ics?token=" + quote(token, safe="")
        ics_url = base + path
        download_url = ics_url + "&download=1"
        webcal_url = "webcal://" + host + path
        public = str(phone_base or "").strip().rstrip("/")
        if public:
            public_host = public.split("://", 1)[1] if "://" in public else public
            phone_ics = public + path
            phone_webcal = "webcal://" + public_host + path
    phone_host = phone_ics.split("://", 1)[1].split("/", 1)[0] if phone_ics else ""
    return {
        "token": token,
        "base": base,
        "ics_url": ics_url,
        "webcal_url": webcal_url,
        "download_url": download_url,
        "loopback": loopback,
        # 手机那条(公网)。phone_only = 它和"当前这一页"不是同一个地址。
        "phone_base": phone_base,
        "phone_ics_url": phone_ics,
        "phone_webcal_url": phone_webcal,
        "phone_only": bool(phone_ics and phone_ics != ics_url),
        # 页面上的复制/订阅按钮用这一组(有公网就用公网)
        "primary_url": phone_ics or ics_url,
        "primary_webcal": phone_webcal or webcal_url,
        "primary_download": (phone_ics + "&download=1") if phone_ics else download_url,
        "primary_host": phone_host or host,
    }


def _add_todo(calendar: Any, services: Any, todo: Any) -> None:
    """把一条未完成待办写成 iCalendar 事件。

    ⚠️ 只有到期日、没有时刻的待办写成**全天事件**(DTSTART 是 date,不是 datetime)——
       写成 00:00 会在 iPhone 上变成"凌晨的事",看起来像另一件事。
    """
    import icalendar

    content = str(getattr(todo, "content", "") or "").strip()
    due = task_view.parse_due(getattr(todo, "due_date_local", None))
    moment = task_view.local_moment(
        getattr(todo, "due_at_local", None), getattr(services.settings, "tz", None)
    )
    if due is None:
        due = moment.date() if moment is not None else None
    if due is None or not content:
        return
    timed = moment is not None and moment.date() == due

    component = icalendar.Event()
    component.add("UID", "todo-" + str(getattr(todo, "id", "") or "") + "@life-assistant")
    component.add("SUMMARY", "待办 · " + content)
    component.add("DESCRIPTION", "来自家庭助手 · 待办")
    component.add("CATEGORIES", ["待办"])
    component.add("STATUS", "CONFIRMED")
    component.add("DTSTAMP", datetime.now(timezone.utc))
    if timed:
        component.add("DTSTART", moment)
        component.add("DTEND", moment + timedelta(minutes=30))
        component.add("TRANSP", "OPAQUE")
        remind = getattr(todo, "remind_minutes_before", None)
        if remind is not None and int(remind) >= 0:
            alarm = icalendar.Alarm()
            alarm.add("ACTION", "DISPLAY")
            alarm.add("DESCRIPTION", content)
            alarm.add("TRIGGER", timedelta(minutes=-int(remind)))
            component.add_component(alarm)
    else:
        component.add("DTSTART", due)
        component.add("DTEND", due + timedelta(days=1))
        component.add("TRANSP", "TRANSPARENT")
    calendar.add_component(component)


def build_ics(services: Any) -> str:
    """日程 + 未完成待办 -> 一份 iPhone 能订阅的 ICS。"""
    import icalendar

    raw = services.calendar.export_ics()
    try:
        calendar = icalendar.Calendar.from_ical(raw)
    except Exception:  # noqa: BLE001 - 坏日历不该让订阅 500;给一份空的也比白屏好
        calendar = icalendar.Calendar()
        calendar.add("VERSION", "2.0")
    calendar["PRODID"] = "-//life-assistant//calendar//CN"
    calendar["CALSCALE"] = "GREGORIAN"
    calendar["METHOD"] = "PUBLISH"
    calendar["X-WR-CALNAME"] = "家庭助手 · 日程与待办"
    calendar["X-WR-CALDESC"] = "日程、待办与提醒的只读视图(来自家庭助手)"
    calendar["X-WR-TIMEZONE"] = str(getattr(services.settings, "timezone", "Asia/Shanghai"))
    # iPhone 会按这个间隔来刷新已订阅日历(实际仍受系统调度影响)
    calendar.add("REFRESH-INTERVAL", icalendar.vDuration(timedelta(minutes=15)))
    calendar["X-PUBLISHED-TTL"] = "PT15M"

    try:
        todos = services.todos.list(status="pending", limit=1000)
    except Exception:  # noqa: BLE001 - 待办层坏了,日程照样要能订阅
        todos = []
    for todo in todos:
        _add_todo(calendar, services, todo)
    return calendar.to_ical().decode("utf-8")


# -- 最后一次来取(回答"为什么手机好几天没更新")-------------------------
#: 每次手机来拉 ICS 就记一笔。⭐ 这个键**只用于观测**,不是业务数据 ——
#: 它回答的是"手机到底还在不在取",而不是"日历里有什么"。
LAST_KEY = "calendar_ics_last"


def _last_key(member_id: int | None) -> str:
    return LAST_KEY if member_id is None else LAST_KEY + ":" + str(int(member_id))


def record_hit(
    conn: sqlite3.Connection,
    member_id: int | None,
    *,
    request: Any = None,
    when: datetime | None = None,
) -> dict[str, Any]:
    """记一笔"手机刚来取过订阅"。写失败**不许**影响订阅本身(观测不是业务)。"""
    headers = getattr(request, "headers", {}) or {}
    client = getattr(request, "client", None)
    payload = {
        "ts": (when or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(
            timespec="seconds"
        ),
        "host": str(headers.get("host") or "")[:120],
        "ip": str(getattr(client, "host", "") or "")[:60],
        "ua": str(headers.get("user-agent") or "")[:160],
    }
    try:
        settings_store.set_text(conn, _last_key(member_id), json.dumps(payload, ensure_ascii=False))
    except Exception:  # noqa: BLE001 - 记账失败不该让手机拿不到日历
        pass
    return payload


def _ua_short(ua: str) -> str:
    """把 UA 说成人话:iOS 的订阅刷新长这样 "iOS/26.6.1 (23G83) dataaccessd/1.0"。"""
    text = str(ua or "")
    if "dataaccessd" in text:
        return "iPhone 日历后台刷新"
    if "iPhone" in text or "iPad" in text:
        return "iPhone/iPad"
    if "Windows" in text:
        return "Windows 浏览器"
    return text[:40]


def last_hit(
    conn: sqlite3.Connection | None, member_id: int | None, *, tzid: str = ""
) -> dict[str, Any] | None:
    """最后一次来取的时间/地址/客户端。没记录过 → None。"""
    raw = settings_store.get_raw(conn, _last_key(member_id))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    text = str(data.get("ts") or "")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    zone = tzid or config.DEFAULT_TIMEZONE
    local = moment.astimezone(ZoneInfo(zone))
    now = datetime.now(timezone.utc)
    seconds = max(0.0, (now - moment).total_seconds())
    if seconds >= 86400:
        age_text = str(int(seconds // 86400)) + " 天"
    elif seconds >= 3600:
        age_text = str(int(seconds // 3600)) + " 小时"
    elif seconds >= 60:
        age_text = str(int(seconds // 60)) + " 分钟"
    else:
        age_text = "刚刚"
    return {
        **data,
        "ts_local": local.strftime("%Y-%m-%d %H:%M"),
        "age_text": age_text,
        "days": seconds / 86400.0,
        # 两天没来取就不正常了(iOS 订阅默认一小时左右刷一次,最慢也远不到两天)
        "stale": seconds >= 2 * 86400,
        "ua_short": _ua_short(str(data.get("ua") or "")),
    }


__all__ = [
    "LAST_KEY",
    "TOKEN_KEY",
    "TOKEN_PREFIX",
    "build_ics",
    "get_token",
    "last_hit",
    "link_info",
    "record_hit",
    "resolve_token",
    "rotate_token",
]
