"""领域数据的归属规则。

owner_id 语义:
  * NULL   = 全家可见(默认);
  * 数字   = 只有该成员可见;
  * 成员未登录 / CLI / 后台任务(member_id=None)= 不限制(管理员视角)。

每域一个开关 `share.<domain>`:
  * family = 新数据写 NULL(全家);
  * own    = 新数据写当前成员 id。
切换只影响**以后写入**,旧行保持原样 —— 这是"切回去不丢数据"的前提。

⚠️ 多孩子(child_id)先不做;本模块只回答"这条数据属于哪个成员"。
"""

from __future__ import annotations

import sqlite3

from ..ops import settings_store

DOMAIN_LEDGER = "ledger"
DOMAIN_CALENDAR = "calendar"
DOMAIN_STUDY = "study"
DOMAIN_HEALTH = "health"
#: 笔记(M33):默认**各自**。笔记是私人本子 —— 装在同一个助手里的两个人,
#: 不该因为共用一台机器就互相看得见对方的随笔。
DOMAIN_NOTES = "notes"
DOMAINS = (DOMAIN_LEDGER, DOMAIN_CALENDAR, DOMAIN_STUDY, DOMAIN_HEALTH, DOMAIN_NOTES)
#: 只有这两个领域允许在设置里选"全家/各自"。日程与健康始终各自，不可设。
SHAREABLE_DOMAINS = (DOMAIN_STUDY, DOMAIN_LEDGER)
DOMAIN_LABELS = {
    DOMAIN_LEDGER: "记账",
    DOMAIN_CALENDAR: "日程/提醒",
    DOMAIN_STUDY: "错题/复习",
    DOMAIN_HEALTH: "健康",
    DOMAIN_NOTES: "笔记",
}

MODE_FAMILY = "family"
MODE_OWN = "own"
MODES = (MODE_FAMILY, MODE_OWN)
#: 只有错题默认全家共享(两个家长共同管孩子的错题);
#: 记账/日程/健康默认各自 —— 私人数据不该因为"装了同一个助手"就互相可见。
DEFAULT_MODE = {domain: MODE_OWN for domain in DOMAINS}
DEFAULT_MODE[DOMAIN_STUDY] = MODE_FAMILY


def share_key(domain: str) -> str:
    return "share." + str(domain)


def get_share_mode(conn: sqlite3.Connection, domain: str) -> str:
    if domain not in SHAREABLE_DOMAINS:
        return MODE_OWN
    raw = settings_store.get_raw(conn, share_key(domain))
    return raw if raw in MODES else DEFAULT_MODE.get(domain, MODE_FAMILY)


def set_share_mode(conn: sqlite3.Connection, domain: str, mode: str) -> None:
    if domain not in DOMAINS:
        raise ValueError("未知领域: " + str(domain))
    if domain not in SHAREABLE_DOMAINS:
        raise ValueError("这个领域始终各自，不能设为家庭共享: " + str(domain))
    if mode not in MODES:
        raise ValueError("未知共享模式: " + str(mode))
    settings_store.set_text(
        conn, "share." + domain, MODE_OWN if mode == MODE_OWN else MODE_FAMILY
    )


def owner_for_new(
    conn: sqlite3.Connection, domain: str, member_id: int | None
) -> int | None:
    """新行写什么 owner_id:全家 -> None;各自 -> 当前成员。"""
    if member_id is None:
        return None
    if domain not in SHAREABLE_DOMAINS:
        return int(member_id)
    return int(member_id) if get_share_mode(conn, domain) == MODE_OWN else None


def visible(owner_id: int | None, member_id: int | None) -> bool:
    if owner_id is None or member_id is None:
        return True
    return int(owner_id) == int(member_id)


def owner_filter(
    member_id: int | None, column: str = "owner_id"
) -> tuple[str, list[object]]:
    """WHERE 片段 + 参数。member_id=None(CLI/后台)= 不限制。"""
    if member_id is None:
        return "1=1", []
    return "(" + column + " IS NULL OR " + column + " = ?)", [int(member_id)]
