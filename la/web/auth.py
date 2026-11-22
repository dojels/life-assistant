"""登录票据与 PIN 限速。

票据纪律:
  * 明文 token 只在 cookie 里出现一次;库里只存 SHA-256;
  * 票据可撤销、有过期时间,不用 JWT(家庭版不需要跨服务);
  * 成员被停用时,它的票据一并撤销。

PIN 是 6 位家用强度,真正有效的防线是**失败限速**。限速记录落库
(`app_login_failures`):重启不清零,多进程共享;窗口内超过上限就拒绝。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from ..family import members as member_store

SESSION_COOKIE = "la_session"
SESSION_TTL_DAYS = 30
LOGIN_LIMIT = 5
LOGIN_WINDOW_S = 60.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def hash_token(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def create_session(
    conn, member_id: int, *, ttl_days: int = SESSION_TTL_DAYS
) -> str:
    if member_store.get_member(conn, member_id) is None:
        raise ValueError("没有这个成员: " + str(member_id))
    token = secrets.token_urlsafe(32)
    now = _now()
    conn.execute(
        "INSERT INTO app_sessions(token_hash, member_id, created_at, expires_at)"
        " VALUES (?, ?, ?, ?)",
        (hash_token(token), int(member_id), _iso(now),
         _iso(now + timedelta(days=max(int(ttl_days), 1)))),
    )
    conn.commit()
    return token


def resolve_session(conn, token: str) -> member_store.Member | None:
    text = str(token or "").strip()
    if not text:
        return None
    row = conn.execute(
        "SELECT m.id, m.name, m.role, m.active, m.pin_hash"
        " FROM app_sessions s JOIN app_members m ON m.id = s.member_id"
        " WHERE s.token_hash = ? AND s.revoked_at IS NULL"
        " AND s.expires_at > ? AND m.active = 1",
        (hash_token(text), _iso(_now())),
    ).fetchone()
    return member_store.Member.from_row(row) if row is not None else None


def revoke_session(conn, token: str) -> bool:
    cur = conn.execute(
        "UPDATE app_sessions SET revoked_at = ?"
        " WHERE token_hash = ? AND revoked_at IS NULL",
        (_iso(_now()), hash_token(token)),
    )
    conn.commit()
    return cur.rowcount > 0


def revoke_all_for_member(conn, member_id: int) -> int:
    cur = conn.execute(
        "UPDATE app_sessions SET revoked_at = ?"
        " WHERE member_id = ? AND revoked_at IS NULL",
        (_iso(_now()), int(member_id)),
    )
    conn.commit()
    return cur.rowcount


def purge_expired(conn) -> int:
    cur = conn.execute(
        "DELETE FROM app_sessions WHERE expires_at <= ?", (_iso(_now()),)
    )
    conn.commit()
    return cur.rowcount


def allow_attempt(
    conn, scope: str, member_id: int, host: str, *,
    limit: int = LOGIN_LIMIT, window_s: float = LOGIN_WINDOW_S,
) -> bool:
    """窗口内失败次数是否还没到上限(顺带清掉过期记录)。"""
    now = _now()
    cutoff = _iso(now - timedelta(seconds=max(float(window_s), 1.0)))
    conn.execute("DELETE FROM app_login_failures WHERE ts < ?", (cutoff,))
    count = int(conn.execute(
        "SELECT COUNT(*) FROM app_login_failures"
        " WHERE scope = ? AND member_id = ? AND host = ? AND ts >= ?",
        (str(scope), int(member_id), str(host), cutoff),
    ).fetchone()[0])
    conn.commit()
    return count < max(int(limit), 1)


def record_failure(conn, scope: str, member_id: int, host: str) -> None:
    conn.execute(
        "INSERT INTO app_login_failures(scope, member_id, host, ts)"
        " VALUES (?, ?, ?, ?)",
        (str(scope), int(member_id), str(host), _iso(_now())),
    )
    conn.commit()


def clear_failures(conn, scope: str, member_id: int, host: str) -> None:
    conn.execute(
        "DELETE FROM app_login_failures"
        " WHERE scope = ? AND member_id = ? AND host = ?",
        (str(scope), int(member_id), str(host)),
    )
    conn.commit()


def failure_count(conn, scope: str, member_id: int, host: str) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM app_login_failures"
        " WHERE scope = ? AND member_id = ? AND host = ?",
        (str(scope), int(member_id), str(host)),
    ).fetchone()[0])
