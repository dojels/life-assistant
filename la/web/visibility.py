"""家庭版可见性判定:会话 / 回执 / 提问 / 素材 / 资产。

从 create_app() 的模块级 helper 搬出来(2026-10-18 架构收口):
这些判定被多个域的读路由共用,放在 app.py 会让按域拆出去的路由
只能反向 import app(循环依赖)。
"""

from __future__ import annotations

from typing import Any

from ..family import identity
from .deps import Services


def web_session(services: Services) -> str:
    """网页端当前会话:登录成员优先,未启用 PIN 时回落到默认成员。"""
    member = getattr(services, "member", None)
    if member is not None:
        return identity.session_id_for_member(member.id)
    return identity.web_session_id(getattr(services, "assistant", None))


def question_visible(services: Services, question: Any) -> bool:
    member = getattr(services, "member", None)
    if member is None or str(getattr(member, "role", "")) == "admin":
        return True
    owner = getattr(question, "session_id", None)
    if owner in (None, ""):
        return True
    return str(owner) == web_session(services)


def trace_sessions(services: Services) -> set[str] | None:
    member = getattr(services, "member", None)
    if member is None or str(getattr(member, "role", "")) == "admin":
        return None
    allowed = {web_session(services)}
    if int(getattr(member, "id", 0)) == 1:
        allowed |= {"web", "default"}
    return allowed


def receipt_visible(services: Services, receipt: Any) -> bool:
    member = getattr(services, "member", None)
    if member is None or str(getattr(member, "role", "")) == "admin":
        return True
    owner = getattr(receipt, "session_id", None)
    if owner in (None, ""):
        return True
    return str(owner) == web_session(services)


def member_can_see(services: Services, owner_session: Any) -> bool:
    member = getattr(services, "member", None)
    if member is None or str(getattr(member, "role", "")) == "admin":
        return True
    if not owner_session:
        return True
    return str(owner_session) == web_session(services)


def owner_id_visible(services: Services, owner_id: Any) -> bool:
    member = getattr(services, "member", None)
    if member is None or str(getattr(member, "role", "")) == "admin":
        return True
    if owner_id in (None, "", 0):
        return True
    return int(owner_id) == int(getattr(member, "id", 0))


def asset_visible(services: Services, asset_id: int) -> bool:
    """资产没有自己的归属列:顺着"谁引用它"判断(素材 / 录入候选 / 错题)。"""
    member = getattr(services, "member", None)
    if member is None or str(getattr(member, "role", "")) == "admin":
        return True
    conn = services.assistant
    row = conn.execute(
        "SELECT thread_id FROM app_materials WHERE asset_id = ? LIMIT 1", (int(asset_id),)
    ).fetchone()
    if row is not None and member_can_see(services, row["thread_id"]):
        return True
    rows = conn.execute(
        "SELECT s.owner_session FROM app_intake_candidates c"
        " JOIN app_intake_sessions s ON s.id = c.session_id"
        " WHERE c.asset_id = ? LIMIT 20",
        (int(asset_id),),
    ).fetchall()
    if rows and any(member_can_see(services, item["owner_session"]) for item in rows):
        return True
    rows = conn.execute(
        "SELECT p.owner_id FROM app_problems p, json_each(p.asset_ids) j"
        " WHERE j.value = ? LIMIT 20",
        (int(asset_id),),
    ).fetchall()
    if rows:
        return any(owner_id_visible(services, item["owner_id"]) for item in rows)
    return False
