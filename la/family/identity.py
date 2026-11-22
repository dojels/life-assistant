"""成员身份与会话 id 的**唯一**出处。

一条纪律:会话 id 里带成员,形如 `member:<id>`。Web 与微信都走这里,
不要各自拼字符串 —— 否则哪天改前缀,两边会对不上。
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

DEFAULT_MEMBER_ID = 1
SESSION_PREFIX = "member:"
ENV_MEMBER_ID = "LA_MEMBER_ID"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def session_id_for_member(member_id: int) -> str:
    return SESSION_PREFIX + str(int(member_id))


def member_id_from_session(session_id: str) -> int | None:
    text = str(session_id or "")
    if not text.startswith(SESSION_PREFIX):
        return None
    try:
        return int(text[len(SESSION_PREFIX):])
    except ValueError:
        return None


def ensure_default_member(conn: sqlite3.Connection) -> int:
    """没有成员就建一个管理员;返回当前默认成员 id。

    迁移 18 会先种一条;这里兜底是为了测试库/旧库也能直接跑。
    """
    row = conn.execute(
        "SELECT id FROM app_members WHERE active = 1 ORDER BY id LIMIT 1"
    ).fetchone()
    if row is not None:
        return int(row[0])
    try:
        conn.execute(
            "INSERT INTO app_members(id, name, role, active, created_at)"
            " VALUES (?, ?, 'admin', 1, ?)",
            (DEFAULT_MEMBER_ID, "管理员", _now()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # 另一个进程/迁移刚种下 id=1;重新读一次即可。
        row = conn.execute(
            "SELECT id FROM app_members WHERE active = 1 ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            raise
        return int(row[0])
    return DEFAULT_MEMBER_ID


def web_session_id(conn: sqlite3.Connection | None = None) -> str:
    """网页端当前成员。登录做好之前,取 `LA_MEMBER_ID` 或第一个成员。"""
    raw = (os.getenv(ENV_MEMBER_ID) or "").strip()
    if raw.isdigit():
        return session_id_for_member(int(raw))
    if conn is not None:
        try:
            return session_id_for_member(ensure_default_member(conn))
        except sqlite3.Error:
            pass
    return session_id_for_member(DEFAULT_MEMBER_ID)


def member_for_binding(
    conn: sqlite3.Connection, channel: str, external_id: str
) -> int | None:
    row = conn.execute(
        "SELECT member_id FROM app_member_bindings"
        " WHERE channel = ? AND external_id = ?",
        (str(channel), str(external_id)),
    ).fetchone()
    return int(row[0]) if row is not None else None


def bind_member(
    conn: sqlite3.Connection, channel: str, external_id: str, member_id: int
) -> None:
    """把一个渠道联系人绑到成员。重复绑定 = 改绑(不删旧的历史)。"""
    row = conn.execute(
        "SELECT 1 FROM app_members WHERE id = ? AND active = 1", (int(member_id),)
    ).fetchone()
    if row is None:
        raise ValueError("没有这个成员: " + str(member_id))
    conn.execute(
        "INSERT INTO app_member_bindings(channel, external_id, member_id, created_at)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(channel, external_id) DO UPDATE SET member_id = excluded.member_id",
        (str(channel), str(external_id), int(member_id), _now()),
    )
    conn.commit()


def unbind_member(conn: sqlite3.Connection, channel: str, external_id: str) -> bool:
    cur = conn.execute(
        "DELETE FROM app_member_bindings WHERE channel = ? AND external_id = ?",
        (str(channel), str(external_id)),
    )
    conn.commit()
    return cur.rowcount > 0
