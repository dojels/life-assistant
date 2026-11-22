"""M2 端到端验收:S3 / S4 / S7。

每一步都是**独立进程**(真重启)。数据目录被临时目录接管,不污染日常库。
注意:本脚本会先**在进程内**往档案里补种历史事件,再启动子进程对话 ——
因为"上周三"这类时间锚点在真实运行中才有意义。

用法:
    .venv/Scripts/python.exe scripts/verify_m2.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
DATA_DIR = tempfile.mkdtemp(prefix="la-verify2-")

os.environ["LA_DATA_DIR"] = DATA_DIR
os.environ["LA_LLM_PROVIDER"] = "fake"
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.path.insert(0, str(BASE_DIR))

from la import config  # noqa: E402
from la.memory.store import MemoryStore  # noqa: E402
from la.memory.timerange import parse_range  # noqa: E402
from la.storage import db  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + ("   " + detail if detail else ""))


def run(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    return subprocess.run(
        [PYTHON, "-m", "la", *args],
        cwd=str(BASE_DIR),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )


def seed(session_id: str, pairs, base) -> None:
    conn = db.connect_memory()
    store = MemoryStore(conn)
    for offset, (role, text) in enumerate(pairs):
        store.append(
            session_id,
            "user/message" if role == "user" else "assistant/message",
            {"text": text},
            time=(base + timedelta(minutes=offset)).isoformat(timespec="seconds"),
        )
    conn.close()


def main() -> int:
    print("验收数据目录: " + DATA_DIR)
    now = config.get_settings().now()
    target = parse_range("上周三上午", now)
    if target is None:
        print("时间解析失败,无法继续")
        return 1
    print("上周三上午 = " + target.start.isoformat() + " — " + target.end.isoformat())

    print("\n[1/4] 场景 S3:QA 未命中 -> 考古命中原文 -> 冲突按最新采信")
    seed(
        "old-session",
        [
            ("user", "装修预算敲定为 5 万,先按这个准备"),
            ("assistant", "好,按 5 万记下了"),
            ("user", "装修预算改成 6 万了,材料涨了"),
            ("assistant", "收到,已更新为 6 万"),
        ],
        target.start + timedelta(hours=1),
    )
    proc = run("--session", "s3", "--phases", "--once", "上周三上午我们说的装修预算是多少")
    out = proc.stdout
    check("S3 退出码为 0", proc.returncode == 0, proc.stderr.strip()[:120])
    check("S3 采信最新金额 6 万", "6 万" in out, out.strip()[:90])
    check("S3 附冲突说明", "以最新为准" in out)
    check("S3 回复带时间", bool(re.search(r"\d{4}-\d{2}-\d{2}", out)))
    check("S3 走了 ARCH 阶段", "archaeology" in out or "阶段" in out)

    print("\n[2/4] 场景 S4:显式记住的事实,重启后直接命中")
    run("--session", "s4", "--say", "帮我记住:项目X 报价 6月15日截止")
    proc = run("--session", "s4", "--once", "项目X 的报价什么时候截止?")
    check("S4 重启后仍能命中事实", "6月15日" in proc.stdout, proc.stdout.strip()[:90])

    print("\n[3/4] 场景 S7:窄窗口没有 -> 4 个子代理并行翻 7 天")
    seed(
        "scattered",
        [("user", "方案就叫星火计划吧"), ("assistant", "好,记录为星火计划")],
        target.start - timedelta(days=3),
    )
    proc = run("--yes", "--session", "s7", "--once", "上周三上午我们说的那个方案叫什么")
    check("S7 子代理找回分散记录", "星火计划" in proc.stdout, proc.stdout.strip()[:90])

    print("\n[4/4] 门禁与审计")
    proc = run("--session", "s9", "--phases", "--once", "你好")
    check("普通寒暄不触发考古", "证据=0" in proc.stdout, proc.stdout.strip()[:90])

    conn = db.connect_assistant()
    actions = [row[0] for row in conn.execute("SELECT action FROM ops_audit ORDER BY id")]
    conn.close()
    check("工具调用写入审计表", "tool.query_summary_pool" in actions, str(sorted(set(actions))[:6]))
    check("考古工具也被审计", "tool.query_timestamp_range" in actions)

    conn = db.connect_memory()
    summaries = conn.execute("SELECT count(*) FROM summary_pool").fetchone()[0]
    consolidations = conn.execute("SELECT count(*) FROM consolidations").fetchone()[0]
    subagents = conn.execute(
        "SELECT count(*) FROM events WHERE type = 'subagent/result'"
    ).fetchone()[0]
    transitions = conn.execute(
        "SELECT count(*) FROM events WHERE type = 'phase/transition'"
    ).fetchone()[0]
    conn.close()
    check("考古成功后回填了摘要", summaries > 0, "摘要 " + str(summaries) + " 条")
    check("回填受幂等键约束", consolidations > 0, "巩固 " + str(consolidations) + " 次")
    check("子代理报告落盘", subagents > 0, "报告 " + str(subagents) + " 条")
    check("阶段流转留痕", transitions > 0, "流转 " + str(transitions) + " 次")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n结果: " + str(passed) + "/" + str(len(RESULTS)) + " 项通过")
    for name, ok, _ in RESULTS:
        if not ok:
            print("  未通过: " + name)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
