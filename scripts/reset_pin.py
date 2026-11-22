"""本机重置成员 PIN(忘记 PIN 且恢复码也丢了时的最后兜底)。

⚠️ 这个脚本**不需要登录**:能运行它的人已经能直接读 data/ 里的数据库。
   它不绕过任何网络边界,只是把"本机管理员"这件事做得明确一点。

用法:
    .venv/Scripts/python.exe scripts/reset_pin.py --list
    .venv/Scripts/python.exe scripts/reset_pin.py --member 1 --pin 123456 --yes
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from la.family import members  # noqa: E402
from la.storage import db  # noqa: E402
from la.web import auth as web_auth  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="列出成员")
    parser.add_argument("--member", type=int, default=0, help="成员 id")
    parser.add_argument("--pin", default="", help="新的 6 位 PIN")
    parser.add_argument("--yes", action="store_true", help="确认重置")
    args = parser.parse_args()

    conn = db.connect_assistant()
    try:
        rows = members.list_members(conn)
        if args.list or not args.member:
            print("id  名字                 角色      状态    PIN   恢复码")
            for row in rows:
                print(
                    f"{row.id:<3} {row.name:<20} {row.role:<8} "
                    f"{'在用' if row.active else '停用':<5} "
                    f"{'有' if row.has_pin else '无':<4} "
                    f"{'有' if members.has_recovery(conn, row.id) else '无'}"
                )
            if not args.list:
                print("\n要重置: --member <id> --pin <6位> --yes")
            return 0

        if not members.PIN_PATTERN.match(str(args.pin)):
            print("PIN 必须是 6 位数字")
            return 2
        member = members.get_member(conn, args.member)
        if member is None:
            print("没有这个成员:", args.member)
            return 2
        if not args.yes:
            print(
                "确认要重置 " + member.name + " 的 PIN 吗?它会撤销该成员所有登录票据。"
                "\n确认请加 --yes"
            )
            return 2
        members.set_pin(conn, member.id, args.pin)
        web_auth.revoke_all_for_member(conn, member.id)
        code = members.set_recovery(conn, member.id)
        print("已重置 " + member.name + " 的 PIN,并撤销旧票据。")
        print("新的恢复码(只显示这一次): " + code)
        print("请让本人登录后在「设置」里改成自己的 PIN。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
