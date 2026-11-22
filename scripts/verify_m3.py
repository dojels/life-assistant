"""M3 端到端验收:S5(目标跨会话) / S6(每日播报) / 提醒 / 技能。

每一步都是**独立进程**(真重启)。数据目录被临时目录接管,不污染日常库。
时间由脚本在进程内推进(把 now 传成"明天 9:00"),否则"次日续跑"无法在当天验证。

用法:
    .venv/Scripts/python.exe scripts/verify_m3.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
DATA_DIR = tempfile.mkdtemp(prefix="la-verify3-")

os.environ["LA_DATA_DIR"] = DATA_DIR
os.environ["LA_LLM_PROVIDER"] = "fake"
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.path.insert(0, str(BASE_DIR))

from la import config  # noqa: E402
from la.memory.store import MemoryStore  # noqa: E402
from la.skills import SkillLibrary  # noqa: E402
from la.storage import db  # noqa: E402
from la.tasks.dispatcher import KIND_BRIEFING, KIND_REMINDER  # noqa: E402
from la.tasks.goals import GoalStore  # noqa: E402
from la.tasks.schedules import ScheduleStore  # noqa: E402
from la.tasks.todos import TodoStore  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + ("   " + detail if detail else ""))


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, "-m", "la", *args],
        cwd=str(BASE_DIR),
        env=dict(os.environ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )


def open_db():
    conn = db.connect_memory()
    return conn, MemoryStore(conn)


def main() -> int:
    from la.tasks.dispatcher import Dispatcher

    print("验收数据目录: " + DATA_DIR)
    now = config.get_settings().now()
    tomorrow_9 = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    print("当前: " + now.isoformat(timespec="seconds") + " | 次日 9:00 = "
          + tomorrow_9.isoformat(timespec="seconds"))

    print("\n[1/4] 场景 S5:建立一个目标,次日重开会话能接续")
    # ⚠️ 这里不再用「--say 帮我定个目标:减重 5kg」建立目标。那条路径是确定性正则
    #    (D20 / la/tasks/intents.py),已随 D91 删除 —— 现在显式指令由**模型的工具
    #    调用**落地,而本脚本跑在 fake 提供方下,假客户端不会自己发工具调用。
    #    所以直接调工具背后的 store(模型点下去的就是同一个方法),把验收的力气
    #    留给真正的语义:「次日重开会话能不能接续」。
    conn, store = open_db()
    goals = GoalStore(conn)
    todos = TodoStore(conn)
    schedules = ScheduleStore(conn)
    dispatcher = Dispatcher(
        conn, settings=config.get_settings(), todos=todos, goals=goals,
        schedules=schedules, summaries_between=store.summaries_between,
    )
    # ⚠️ 先把"今天"的简报消费掉:CLI 每次开会话都会自动派发简报(每天一次),
    #    而"派发成功 = 推进一轮"。不先消费的话,下面第一条 CLI 命令就会替新目标
    #    白推一轮,后面所有轮次断言都会差 1（实测踩过）。
    dispatcher.deliver_briefing(now)
    goal = goals.create("减重 5kg")
    check("目标能建立", bool(goal.id), goal.id)
    proc = run("--session", "s5", "--goals")
    check("--goals 能列出目标", "减重 5kg" in proc.stdout, proc.stdout.strip()[:80])
    check("目标初始轮次为 0", "轮次 0" in proc.stdout, proc.stdout.strip()[:80])

    delivered = dispatcher.deliver_briefing(tomorrow_9)
    check("次日到点派发了简报", delivered is not None)
    goal = goals.list(open_only=True)[0]
    check("轮次由代码推进", goal.rounds_started == 1, "轮次=" + str(goal.rounds_started))
    again = dispatcher.deliver_briefing(tomorrow_9 + timedelta(hours=3))
    check("同一天不重复播报", again is None)
    conn.close()

    proc = run("--session", "s5", "--dump")
    check("重启后投影带 <goals>", "<goals>" in proc.stdout)
    check("重启后投影带轮次进度", "轮次 1" in proc.stdout, proc.stdout.strip()[-60:])

    print("\n[2/4] 场景 S6:每日播报内容来自待办清单 + 昨日概要")
    conn, store = open_db()
    TodoStore(conn).add("交周报")
    store.upsert_summary(
        session_id="s6",
        start_seq=1,
        end_seq=4,
        start_time=(now - timedelta(days=1)).replace(hour=20).isoformat(timespec="seconds"),
        end_time=(now - timedelta(days=1)).replace(hour=21).isoformat(timespec="seconds"),
        text="<compacted-summary>- 事项:昨天确认了体检安排在周五</compacted-summary>",
    )
    conn.close()
    proc = run("--session", "s6", "--briefing")
    check("播报含待办", "交周报" in proc.stdout)
    check("播报含昨日概要", "体检安排" in proc.stdout)
    check("播报含日期与星期", "星期" in proc.stdout and now.strftime("%Y-%m-%d") in proc.stdout)

    print("\n[3/4] 提醒:到点被捡起,**但不许**在没有送达渠道的地方被认领")
    conn, store = open_db()
    schedules = ScheduleStore(conn)
    goals = GoalStore(conn)
    todos = TodoStore(conn)
    made = schedules.create(
        "开会", kind="at", at=tomorrow_9.isoformat(timespec="seconds"), receipt=False
    )
    dispatcher = Dispatcher(
        conn, settings=config.get_settings(), todos=todos, goals=goals,
        schedules=schedules, summaries_between=store.summaries_between,
    )
    due = dispatcher.schedules.due(tomorrow_9 + timedelta(minutes=1))
    check("到点后该提醒是 due 的", len(due) == 1, str(len(due)) + " 条")
    pending = dispatcher.deliver_pending(tomorrow_9 + timedelta(minutes=1))
    check("⭐ 派发简报时不会顺手吃掉提醒",
          all(item.kind != KIND_REMINDER for item in pending),
          str([item.kind for item in pending]))
    check("提醒仍未被认领(留着给推送环)",
          schedules.get(made.id).last_fired_at is None)
    conn.close()
    # 真正的认领在 la/bridge/reminder_push.py:先送达、后认领。它的单测在
    # tests/test_reminder_push.py,端到端在微信进程里(需要活的 context_token)。

    print("\n[4/4] 技能包与守门")
    proc = run("--session", "s6", "--dump")
    check("技能目录常驻系统提示词", "<skills>" in proc.stdout)
    check("目录含三个技能", all(name in proc.stdout for name in ("scheduling", "memory-protocol", "goal-tracking")))
    check("正文不占常驻 token", "## 默认值" not in proc.stdout)
    library = SkillLibrary()
    check("正文可按需加载", "300 秒" in library.load("scheduling").body)

    before = run("--session", "s6", "--todos").stdout
    run("--session", "s6", "--once", "今天天气不错")
    after = run("--session", "s6", "--todos").stdout
    check("普通寒暄不会凭空建事项", before.strip() == after.strip())

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n结果: " + str(passed) + "/" + str(len(RESULTS)) + " 项通过")
    for name, ok, _ in RESULTS:
        if not ok:
            print("  未通过: " + name)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
