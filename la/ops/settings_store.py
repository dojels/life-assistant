"""运行期开关(键值)—— M12。

聊天页那个「联网」按钮写的就是这里,而不是 `.env`:
`.env` 是**启动配置**(改它要重启),而按钮是"运行中想改就改"。

三层优先级(与凭据链同一条思路,但方向相反):
    sys_settings 里的值  >  `.env` 的默认值(LA_WEB_ENABLED)  >  代码里的 False

⭐ 为什么库里的值优先:用户点过按钮之后,他的点击就是最新的意图;
   `.env` 从此只当"出厂默认"。反过来会让按钮看起来"点了没用"。
"""

from __future__ import annotations
from ..base.clock import utc_now_iso as _now

import sqlite3
from datetime import datetime, timezone

#: 联网总开关
WEB_ENABLED_KEY = "web_enabled"

#: 认得出"真"的写法(与项目里其他布尔开关同一份口径)
TRUE_WORDS = ("1", "true", "yes", "on", "开", "是")




def _table_ready(conn: sqlite3.Connection | None) -> bool:
    if conn is None:
        return False
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'sys_settings'"
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def get_raw(conn: sqlite3.Connection | None, key: str) -> str | None:
    """读原始字符串。表不存在 / 没这个键 → None(调用方决定默认值)。"""
    if not _table_ready(conn):
        return None
    try:
        row = conn.execute("SELECT value FROM sys_settings WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    return None if row is None else str(row["value"])


def get_flag(conn: sqlite3.Connection | None, key: str, *, default: bool = False) -> bool:
    raw = get_raw(conn, key)
    if raw is None:
        return bool(default)
    return raw.strip().lower() in TRUE_WORDS


def get_entry(conn: sqlite3.Connection | None, key: str) -> tuple[str, str] | None:
    """连 updated_at 一起读:(值, 上次改动时间)。没这个键 → None。

    ⭐ 为什么要有它:开关只需要"是不是 1",而人格这种**长文本**还要告诉用户
       "这版是什么时候写的"(页面上的"上次修改")。
    """
    if not _table_ready(conn):
        return None
    try:
        row = conn.execute(
            "SELECT value, updated_at FROM sys_settings WHERE key = ?", (key,)
        ).fetchone()
    except sqlite3.Error:
        return None
    return None if row is None else (str(row["value"]), str(row["updated_at"]))


def set_text(conn: sqlite3.Connection, key: str, value: str) -> None:
    """写一个**字符串**值(开关是它的特例)。

    ⚠️ 与 set_flag 共用同一条 upsert:两种用途写的是同一张表、同一列,
       不另立门户 —— 否则"库里的值优先于代码默认"这条口径会被抄成两份。
    """
    if not _table_ready(conn):
        raise RuntimeError("这个库里还没有 sys_settings 表(迁移 12 没跑)")
    conn.execute(
        "INSERT INTO sys_settings(key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, str(value), _now()),
    )
    conn.commit()


def clear(conn: sqlite3.Connection, key: str) -> None:
    """删掉这个键 = **回到代码里的默认值**(不是"空值")。

    ⭐ 人格页的「恢复内置默认」走的就是它:库里没有记录 = 用出厂那份,
       而不是存一份"空的"进库(空人格会让助手只剩固定的尾巴)。
    """
    if not _table_ready(conn):
        return
    conn.execute("DELETE FROM sys_settings WHERE key = ?", (key,))
    conn.commit()


def set_flag(conn: sqlite3.Connection, key: str, value: bool) -> None:
    set_text(conn, key, "1" if value else "0")


def web_enabled(conn: sqlite3.Connection | None, *, default: bool = False) -> bool:
    """联网到底开没开:库里的按钮状态优先,没有记录才用 `.env` 的默认值。"""
    return get_flag(conn, WEB_ENABLED_KEY, default=default)
