"""成员管理与 PIN 校验。

PIN 只存 scrypt 哈希 + 随机盐;不存明文,也不把 PIN 放进日志/回执。
6 位数字是**家用强度**,不是银行强度 —— 真正的防线是登录限速(见 web/auth.py)。

⚠️ 这里不碰会话:成员是谁由 la.family.identity 决定;登录票据由 la.web.auth 管。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

ROLE_ADMIN = "admin"
ROLE_PARENT = "parent"
ROLE_CHILD = "child"
ROLES = (ROLE_ADMIN, ROLE_PARENT, ROLE_CHILD)
PIN_PATTERN = re.compile(r"^\d{6}$")
#: 恢复码:去掉 I/O/0/1 等易混字符;12 位约 60 bit,足够挡住在线猜。
RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
RECOVERY_LENGTH = 12

#: scrypt 参数。N=2^14 在 2026 年的家用机器上约几十毫秒,足够挡住离线爆破。
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Member:
    id: int
    name: str
    role: str
    active: bool
    has_pin: bool

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Member":
        return cls(
            id=int(row["id"]),
            name=str(row["name"]),
            role=str(row["role"]),
            active=bool(row["active"]),
            has_pin=bool(row["pin_hash"]),
        )


def _row(conn: sqlite3.Connection, member_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, name, role, active, pin_hash FROM app_members WHERE id = ?",
        (int(member_id),),
    ).fetchone()


def get_member(conn: sqlite3.Connection, member_id: int) -> Member | None:
    row = _row(conn, member_id)
    return Member.from_row(row) if row is not None else None


def list_members(
    conn: sqlite3.Connection, *, active_only: bool = False
) -> list[Member]:
    sql = "SELECT id, name, role, active, pin_hash FROM app_members"
    params: list[object] = []
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY id"
    return [Member.from_row(row) for row in conn.execute(sql, params)]


def has_any_pin(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM app_members WHERE active = 1 AND pin_hash IS NOT NULL LIMIT 1"
    ).fetchone()
    return row is not None


def create_member(
    conn: sqlite3.Connection,
    name: str,
    *,
    role: str = ROLE_PARENT,
    pin: str | None = None,
) -> Member:
    clean = str(name or "").strip()
    if not clean:
        raise ValueError("成员名字不能为空")
    if len(clean) > 20:
        raise ValueError("成员名字最多 20 个字")
    if role not in ROLES:
        raise ValueError("未知角色: " + str(role))
    if pin is not None and not PIN_PATTERN.match(str(pin)):
        raise ValueError("PIN 必须是 6 位数字")
    pin_hash, pin_salt, pin_at = (
        _secret_parts(str(pin)) if pin is not None else (None, None, None)
    )
    cur = conn.execute(
        "INSERT INTO app_members(name, role, active, created_at, pin_hash, pin_salt,"
        " pin_updated_at) VALUES (?, ?, 1, ?, ?, ?, ?)",
        (clean, role, _now(), pin_hash, pin_salt, pin_at),
    )
    conn.commit()
    member = get_member(conn, int(cur.lastrowid))
    if member is None:
        raise RuntimeError("成员创建后读不回来")
    return member


def set_pin(conn: sqlite3.Connection, member_id: int, pin: str) -> None:
    if not PIN_PATTERN.match(str(pin)):
        raise ValueError("PIN 必须是 6 位数字")
    if _row(conn, member_id) is None:
        raise ValueError("没有这个成员: " + str(member_id))
    pin_hash, pin_salt, pin_at = _secret_parts(str(pin))
    conn.execute(
        "UPDATE app_members SET pin_hash = ?, pin_salt = ?, pin_updated_at = ?"
        " WHERE id = ?",
        (pin_hash, pin_salt, pin_at, int(member_id)),
    )
    conn.commit()


def verify_pin(conn: sqlite3.Connection, member_id: int, pin: str) -> bool:
    row = conn.execute(
        "SELECT pin_hash, pin_salt, active FROM app_members WHERE id = ?",
        (int(member_id),),
    ).fetchone()
    if row is None or not row["active"] or not row["pin_hash"] or not row["pin_salt"]:
        return False
    text = str(pin or "")
    if not PIN_PATTERN.match(text):
        return False
    try:
        salt = base64.b64decode(str(row["pin_salt"]))
        expected = base64.b64decode(str(row["pin_hash"]))
    except (ValueError, TypeError):
        return False
    digest = hashlib.scrypt(
        text.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return hmac.compare_digest(digest, expected)


def generate_recovery_code() -> str:
    raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_LENGTH))
    return "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


def normalize_recovery(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(text or "").upper())


def set_recovery(
    conn: sqlite3.Connection, member_id: int, code: str | None = None
) -> str:
    """设置/轮换恢复码,返回**明文**(只在此刻返回一次,库里只存哈希)。"""
    if _row(conn, member_id) is None:
        raise ValueError("没有这个成员: " + str(member_id))
    plain = code or generate_recovery_code()
    normalized = normalize_recovery(plain)
    if len(normalized) != RECOVERY_LENGTH:
        raise ValueError("恢复码长度不对")
    digest, salt, stamp = _secret_parts(normalized)
    conn.execute(
        "UPDATE app_members SET recovery_hash = ?, recovery_salt = ?, recovery_at = ?"
        " WHERE id = ?",
        (digest, salt, stamp, int(member_id)),
    )
    conn.commit()
    return "-".join(normalized[i:i + 4] for i in range(0, len(normalized), 4))


def has_recovery(conn: sqlite3.Connection, member_id: int) -> bool:
    row = conn.execute(
        "SELECT recovery_hash FROM app_members WHERE id = ?", (int(member_id),)
    ).fetchone()
    return bool(row and row["recovery_hash"])


def verify_recovery(conn: sqlite3.Connection, member_id: int, code: str) -> bool:
    row = conn.execute(
        "SELECT recovery_hash, recovery_salt, active FROM app_members WHERE id = ?",
        (int(member_id),),
    ).fetchone()
    if row is None or not row["active"] or not row["recovery_hash"] or not row["recovery_salt"]:
        return False
    normalized = normalize_recovery(code)
    if len(normalized) != RECOVERY_LENGTH:
        return False
    try:
        salt = base64.b64decode(str(row["recovery_salt"]))
        expected = base64.b64decode(str(row["recovery_hash"]))
    except (ValueError, TypeError):
        return False
    digest = hashlib.scrypt(
        normalized.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return hmac.compare_digest(digest, expected)


def deactivate_member(conn: sqlite3.Connection, member_id: int) -> None:
    row = _row(conn, member_id)
    if row is None:
        raise ValueError("没有这个成员: " + str(member_id))
    if str(row["role"]) == ROLE_ADMIN:
        others = conn.execute(
            "SELECT COUNT(*) FROM app_members WHERE active = 1"
            " AND role = 'admin' AND id != ?",
            (int(member_id),),
        ).fetchone()[0]
        if int(others) == 0:
            raise ValueError("不能停用最后一个管理员")
    conn.execute("UPDATE app_members SET active = 0 WHERE id = ?", (int(member_id),))
    conn.execute("UPDATE app_sessions SET revoked_at = ? WHERE member_id = ?", (_now(), int(member_id)))
    conn.commit()


def _secret_parts(secret: str) -> tuple[str, str, str]:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        str(secret).encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return (
        base64.b64encode(digest).decode("ascii"),
        base64.b64encode(salt).decode("ascii"),
        _now(),
    )



