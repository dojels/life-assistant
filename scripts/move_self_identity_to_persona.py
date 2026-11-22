"""把"关于助手自己"的事实挪进**人格页**(2026-10-25)。

背景(用户点名):
    *"智能体英文名……应该是属于人格部分的内容,见图3,智能体应该是要更新到自己的
      人格界面。"*
    在此之前,模型把"助手的英文名是 Mo"当**用户的事实**写进永久事实层:事实层每轮
    无条件进上下文,而"她叫什么"本来就在人格里(sys_settings.persona_text)——
    两处各说各的,页面上还各占一块。

它做什么(默认 dry-run):
    1. 扫**生效中**的事实,用 persona.is_self_identity 认出"主语是助手自己 + 名字类
       陈述"的那些(确定性规则,不需要模型);
    2. 把这些句子收成"人格口吻"的一句(去掉"助手的/小默的"这种第三人称前缀);
    3. --apply 时:一次性追加到人格主体末尾(**一次写入、一条撤销回执**),
       再把对应事实**停用**(active=0,不删除)、每一条留一条 memory/note 事件。

⚠️ 不删除任何行:停用的事实仍在库里、在记忆页 L3 上可恢复。
⚠️ 人格里已经有同一句时不会重复追加(append 自己会跳过并说明)。

用法:
    python scripts/move_self_identity_to_persona.py                 # 只看会改什么
    python scripts/move_self_identity_to_persona.py --apply         # 真的迁移
    python scripts/move_self_identity_to_persona.py --apply --ids 8 # 只迁 #8
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from la.agent.persona import PersonaStore, as_persona_line, is_self_identity  # noqa: E402
from la.memory.store import MemoryStore  # noqa: E402
from la.storage import db  # noqa: E402
from la.storage.migrations import MIGRATIONS_MEMORY  # noqa: E402


def find_self_identity(store: MemoryStore, name: str, only_ids: set[int]) -> list[dict]:
    rows = store.conn.execute(
        "SELECT id, type, content, session_id FROM facts WHERE active = 1 ORDER BY id"
    ).fetchall()
    found: list[dict] = []
    for row in rows:
        fact_id = int(row["id"])
        if only_ids and fact_id not in only_ids:
            continue
        content = str(row["content"])
        if not is_self_identity(content, name):
            continue
        found.append({
            "id": fact_id,
            "type": str(row["type"]),
            "content": content,
            "line": as_persona_line(content, name),
            "session_id": row["session_id"],
        })
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description="把助手身份事实迁进人格页(默认只报告)")
    parser.add_argument("--apply", action="store_true", help="真的迁移(默认 dry-run)")
    parser.add_argument("--ids", type=int, nargs="*", default=[],
                        help="只迁这几条事实编号(默认:所有认得出的)")
    parser.add_argument(
        "--extra-line", action="append", default=[],
        help="额外追加进人格的一句(可重复);例如 --extra-line '英文名:Mo。'",
    )
    args = parser.parse_args()

    conn = db.connect_memory()
    db.migrate(conn, MIGRATIONS_MEMORY)
    store = MemoryStore(conn)
    assistant = db.connect_assistant()
    persona = PersonaStore(assistant)
    state = persona.state()
    found = find_self_identity(store, state.name, set(args.ids))

    if not found:
        print("没有找到「关于助手自己」的生效事实,不用迁。")
        return 0

    print("编号  内容")
    print("----  ----------------------------------------------------------------")
    fresh: list[dict] = []
    for item in found:
        already = item["line"] in state.body
        print("#%-4d %s%s" % (item["id"], item["content"][:70],
                              "(人格里已有这句)" if already else ""))
        if not already:
            fresh.append(item)
    print()
    # ⚠️ 逐句判重(而不是把整块丢给 append):append 只看"整段在不在正文里",
    #    多句一起传时,其中已经存在的那句会被再写一遍。
    extra = [
        as_persona_line(item, state.name) for item in args.extra_line if str(item).strip()
    ]
    new_lines: list[str] = []
    for line in [item["line"] for item in fresh] + extra:
        if line and line not in state.body and line not in new_lines:
            new_lines.append(line)
    print("人格页当前 %d 字 / 上限 %d 字" % (state.chars, __import__(
        "la.agent.persona", fromlist=["MAX_CHARS"]).MAX_CHARS))
    if not new_lines:
        print("要迁的句子人格里都已经有了;仍会停用对应事实(避免两处各说各的)。")
    else:
        print("将追加到人格页:")
        for line in new_lines:
            print("  + " + line)

    if not args.apply:
        print()
        print("(这是 dry-run。加 --apply 才会真的写入人格页并停用这些事实。)")
        return 0

    before = state
    previous = ""
    receipt_id = 0
    if new_lines:
        previous, notes = persona.append("\n".join(new_lines))
        after = persona.state()
        from la.domain.receipts import DOMAIN_SYSTEM, ReceiptLog

        log = ReceiptLog(assistant)
        if log._table_ready():
            receipt_id = log.record(
                kind="persona.text", domain=DOMAIN_SYSTEM,
                label="人格:迁入助手身份事实 " + "、".join(
                    "#" + str(item["id"]) for item in fresh) + (
                    "(+" + str(len(extra)) + " 句)" if extra else ""),
                detail={"chars": after.chars, "before_chars": before.chars, "op": "migrate",
                        "facts": [item["id"] for item in fresh],
                        "added": " / ".join(new_lines)[:200]},
                undo_plan={"type": "persona", "before": previous,
                           "before_name": before.name if before.name_custom else ""},
                actor="user", session_id="cli",
            )
            if receipt_id:
                log.conn.commit()
        for note in notes:
            print("  · " + note)
        print("  人格已更新:%d 字 -> %d 字%s" % (
            before.chars, after.chars, "(回执 %d,可撤销)" % receipt_id if receipt_id else ""))

    for item in found:
        store.set_fact_active(item["id"], False)
        store.append(
            str(item["session_id"] or "cli"), "memory/note",
            {
                "content": "迁进人格页:停用 #" + str(item["id"]) + " " + item["content"],
                "fact_type": item["type"],
                "explicit": True,
                "via": "move_self_identity_to_persona",
                "fact_id": item["id"],
                "reason": "这是「助手自己是谁」,归人格页(/persona);事实层不再留一份",
            },
        )
        print("  已停用事实 #%d(仍可在 /memory 的 L3 恢复)" % item["id"])
    print("")
    print("完成。打开 /persona 就能看到她带着这个名字了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
