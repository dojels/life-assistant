"""设置页的 sudo:改密钥之前,再验一次 PIN。

为什么需要它:管理员 cookie 是 30 天的。家里一台没锁屏的电脑、或者局域网里
别人借用了浏览器,就能直接改"数据发去哪"。sudo 把"能看页面"和"能动凭据"分开,
代价只是每 10 分钟输一次 6 位 PIN。

票据只存 SHA-256(与 app_sessions 同一口径):库被看到也不能直接当 cookie 用。
"""

from __future__ import annotations

import hashlib
import secrets as pysecrets
import sqlite3
from datetime import datetime, timedelta, timezone

COOKIE = "la_sudo"
TTL_SECONDS = 600


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _hash(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def grant(conn: sqlite3.Connection, member_id: int) -> str:
    """发一张 10 分钟的票据;顺手清掉过期的。"""
    token = pysecrets.token_urlsafe(32)
    now = _now()
    conn.execute(
        "INSERT INTO app_sudo_tokens(token_hash, member_id, created_at, expires_at)"
        " VALUES (?, ?, ?, ?)",
        (_hash(token), int(member_id), _iso(now), _iso(now + timedelta(seconds=TTL_SECONDS))),
    )
    conn.execute("DELETE FROM app_sudo_tokens WHERE expires_at < ?", (_iso(now),))
    conn.commit()
    return token


def valid(conn: sqlite3.Connection | None, request, member_id: int | None) -> bool:
    if conn is None or member_id is None:
        return False
    token = ""
    try:
        token = str(request.cookies.get(COOKIE) or "")
    except Exception:  # noqa: BLE001 - 没有 cookie jar 的调用方不算已解锁
        return False
    if not token:
        return False
    try:
        row = conn.execute(
            "SELECT member_id, expires_at FROM app_sudo_tokens WHERE token_hash = ?",
            (_hash(token),),
        ).fetchone()
    except sqlite3.Error:
        return False
    if row is None:
        return False
    if int(row["member_id"]) != int(member_id):
        return False
    return str(row["expires_at"]) > _iso(_now())


def revoke(conn: sqlite3.Connection, request) -> None:
    token = ""
    try:
        token = str(request.cookies.get(COOKIE) or "")
    except Exception:  # noqa: BLE001
        return
    if not token:
        return
    conn.execute("DELETE FROM app_sudo_tokens WHERE token_hash = ?", (_hash(token),))
    conn.commit()


def purge(conn: sqlite3.Connection) -> int:
    cur = conn.execute("DELETE FROM app_sudo_tokens WHERE expires_at < ?", (_iso(_now()),))
    conn.commit()
    return int(cur.rowcount or 0)

