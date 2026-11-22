"""微信双人验收诊断:看每个联系人绑到谁、消息落进哪个会话。

用法:
    .venv/Scripts/python.exe scripts/diagnose_bindings.py
    .venv/Scripts/python.exe scripts/diagnose_bindings.py --json

真机验收顺序:
  1. 管理员在 /settings 里加"妈妈"并绑定她的微信联系人;
  2. 让妈妈在微信里发一句"在吗";
  3. 再跑一次本脚本:她的联系人应出现在 bindings,且 member:<id> 的事件数 +2;
  4. 让爸爸也发一句,确认两个联系人的 member 不同;
  5. 未绑定联系人只会收到固定提示,不会进模型(见 bridge.py 的 unbound 闸门)。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from la.family import identity, members  # noqa: E402
from la.storage import db  # noqa: E402


def collect() -> dict:
    ac = db.connect_assistant()
    mc = db.connect_memory()
    try:
        people = members.list_members(ac)
        bindings = [
            dict(row)
            for row in ac.execute(
                "SELECT channel, external_id, member_id FROM app_member_bindings"
                " ORDER BY channel, external_id"
            ).fetchall()
        ]
        member_events = {
            str(row[0]): int(row[1])
            for row in mc.execute(
                "SELECT session_id, COUNT(*) FROM events"
                " WHERE session_id LIKE 'member:%' GROUP BY session_id"
            ).fetchall()
        }
        bridge_events = [
            {"session_id": str(row[0]), "events": int(row[1]),
             "last_time": str(row[2] or "")}
            for row in mc.execute(
                "SELECT session_id, COUNT(*), MAX(time) FROM events"
                " WHERE session_id LIKE 'bridge:%' GROUP BY session_id"
                " ORDER BY session_id"
            ).fetchall()
        ]
    finally:
        ac.close(); mc.close()

    bound_ids = {int(item["member_id"]) for item in bindings}
    result = {
        "members": [
            {"id": person.id, "name": person.name, "role": person.role,
             "active": person.active, "has_pin": person.has_pin,
             "event_count": member_events.get(identity.session_id_for_member(person.id), 0)}
            for person in people
        ],
        "bindings": [
            {
                **item,
                "member_session": identity.session_id_for_member(int(item["member_id"])),
                "event_count": member_events.get(
                    identity.session_id_for_member(int(item["member_id"])), 0
                ),
            }
            for item in bindings
        ],
        "bridge_sessions": bridge_events,
        "warnings": [],
    }
    known_ids = {person.id for person in people}
    for item in bindings:
        if int(item["member_id"]) not in known_ids:
            result["warnings"].append("绑定表里有一条指向不存在成员的记录: " + str(item))
    for person in people:
        if person.active and person.role != "admin" and person.id not in bound_ids:
            result["warnings"].append(
                "成员 " + person.name + " 还没有绑定微信(可以不用微信,但双人验收需要两个绑定)"
            )
    for bridge in bridge_events:
        external = bridge["session_id"].split(":", 1)[1]
        if external.startswith(("verify-", "wxid_verify")):
            continue
        if not any(item["channel"] == "weixin" and item["external_id"] == external
                   for item in bindings):
            result["warnings"].append(
                "未绑定联系人 " + external + " 有过消息;现在只会收到固定提示,不进模型"
            )
    return result

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = collect()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print("成员:")
    for item in result["members"]:
        print(
            "  #" + str(item["id"]) + " " + item["name"]
            + " [" + item["role"] + ("/停用" if not item["active"] else "") + "]"
            + "  PIN=" + ("有" if item["has_pin"] else "无")
            + "  " + identity.session_id_for_member(item["id"])
            + " 事件=" + str(item["event_count"])
        )
    print("微信绑定:")
    if not result["bindings"]:
        print("  (还没有绑定)")
    for item in result["bindings"]:
        print(
            "  " + item["external_id"] + " -> " + item["member_session"]
            + " 事件=" + str(item["event_count"])
        )
    print("桥接历史会话:")
    for item in result["bridge_sessions"]:
        print("  " + item["session_id"] + " 事件=" + str(item["events"])
              + " 最后=" + item["last_time"])
    if result["warnings"]:
        print("提示:")
        for text in result["warnings"]:
            print("  ! " + text)
    print("\n验收:让每位家长各发一句微信,再跑一次;对应 member:<id> 的事件数应增加。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
