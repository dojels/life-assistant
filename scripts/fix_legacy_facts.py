"""存量事实的逐条处置:把准入闸**上线之前**混进来的事实清一遍。

背景(2026-10-13):
    在事实准入闸上线之前,规则匹配到什么就永久记住什么。实测 11 条生效事实里 5 条有问题:
        #1  "用户比较喜欢 吃香菜"        <- 用户说的是"我不吃香菜"(极性写反)
        #3  "用户喜欢 吃什么东西"        <- 疑问句
        #5  "用户喜欢 打游戏,…然"        <- 截在半句话上
        #9  "，明天晚上7点钟发信息提醒我"  <- 开头是标点 + 未来动作
        #11 "用户喜欢 什么"             <- 疑问句(与 #3 重复)
    这些是**已经写进库**的行,闸门只能拦住新的 —— 所以要用这个脚本处置旧的。

它做什么:
    1. 逐条跑 `guard.check_candidate`(确定性规则,不需要模型),打印判决;
    2. 把判决为"拒收"的条目**停用**(active=0)、写一条 memory/note 事件说明原因;
    3. 其余条目原样保留,并打印出来供人工核对(改口/停用请到记忆页 L3 上点)。

它**不删除任何行**。停用 = 不再进上下文、不再被检索命中,但行还在库里、可查、可在页面上恢复。

用法:
    python scripts/fix_legacy_facts.py            # 只报告,不改库(dry-run,默认)
    python scripts/fix_legacy_facts.py --apply    # 真的停用"判决为拒收"的那些
    python scripts/fix_legacy_facts.py --apply --extra-ids 7   # 连 #7 一起停用(人工点名的)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from la import config  # noqa: E402
from la.memory.guard import check_candidate, normalize  # noqa: E402
from la.memory.store import MemoryStore  # noqa: E402
from la.storage import db  # noqa: E402


def audit(store: MemoryStore) -> list[dict]:
    rows = store.conn.execute(
        "SELECT id, type, content, active, confidential, created_at FROM facts ORDER BY id"
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        verdict = check_candidate(str(row["content"]))
        out.append({
            "id": int(row["id"]),
            "type": str(row["type"]),
            "content": str(row["content"]),
            "active": bool(row["active"]),
            "confidential": bool(row["confidential"]),
            "created_at": str(row["created_at"]),
            "ok": verdict.ok,
            "needs_review": verdict.needs_review,
            "reason": verdict.reason,
            "code": verdict.code,
        })
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="存量事实处置(默认只报告)")
    parser.add_argument("--apply", action="store_true", help="真的停用判决为拒收的条目")
    parser.add_argument("--extra-ids", type=int, nargs="*", default=[],
                        help="人工点名要停用的事实编号")
    parser.add_argument("--include-inactive", action="store_true", help="连已停用的也列出来")
    parser.add_argument("--keep-review", action="store_true",
                        help="把「待审」的条目也留着(默认连它们一起停用)")
    args = parser.parse_args()

    conn = db.connect_memory()
    db.migrate(conn, __import__("la.storage.migrations", fromlist=["MIGRATIONS_MEMORY"])
               .MIGRATIONS_MEMORY)
    store = MemoryStore(conn)
    items = [item for item in audit(store) if item["active"] or args.include_inactive]

    if not items:
        print("没有生效中的事实,不用处置。")
        return 0

    print("编号  判决      说明                              内容")
    print("----  --------  --------------------------------  ------------------------------")
    to_deactivate: list[dict] = []
    for item in items:
        if not item["active"]:
            tag = "已停用"
        elif not item["ok"] and not item["needs_review"]:
            tag = "拒收"          # 结构性垃圾:疑问句 / 半句 / 开头标点
        elif item["needs_review"]:
            # 待审 = 模板拼装痕迹之类。**存量数据里它们没有"人放行"过**
            # (闸门是 2026-10-13 才上线的),所以默认按"没核过"处理。
            tag = "待审" if args.keep_review else "拒收"
        else:
            tag = "保留"
        if item["id"] in args.extra_ids and item["active"]:
            tag = "人工点名"
        print("#%-4d %-8s  %-32s  %s" % (
            item["id"], tag, (item["reason"] or "—")[:32], item["content"][:40]))
        if tag in ("拒收", "人工点名"):
            to_deactivate.append(item)

    print()
    if not to_deactivate:
        print("没有需要自动停用的条目。")
        return 0
    print("将停用 " + str(len(to_deactivate)) + " 条:" + "、".join(
        "#" + str(item["id"]) for item in to_deactivate))
    if not args.apply:
        print("(这是 dry-run。加 --apply 才会真的停用。)")
        return 0

    now = __import__("la.memory.archive", fromlist=["now_iso"]).now_iso()
    for item in to_deactivate:
        store.set_fact_active(item["id"], False)
        reason = item["reason"] or "人工点名停用"
        store.append(
            "cli", "memory/note",
            {
                "content": "存量处置:停用 #" + str(item["id"]) + " " + item["content"],
                "fact_type": item["type"],
                "explicit": True,
                "via": "fix_legacy_facts",
                "fact_id": item["id"],
                "reason": reason,
                "at": now,
            },
        )
        print("  已停用 #%d(%s)" % (item["id"], reason))
    print("")
    print("完成。这些行仍在库里(可在记忆页 L3 恢复),只是不再进上下文。")
    print("下一步:到 /memory 的 L3 上核对剩下的条目,需要改口的用「改口」按钮。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
