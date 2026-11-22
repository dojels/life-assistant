"""M1 端到端验收脚本:S1 与 S2。

关键点:**每一步都是独立进程** —— 这样"S2 重启后仍记得"才是真的重启验证,
而不是同一个进程里的内存残留。

用法:
    .venv/Scripts/python.exe scripts/verify_m1.py
数据目录被临时目录接管(LA_DATA_DIR),不会污染日常使用的 data/。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PYTHON = sys.executable

CHITCHAT = [
    "今天天气不错",
    "晚上吃了碗面",
    '我们刚才聊的那家餐厅叫"老张牛肉面",下周三中午想去',
    "地铁上的人真多",
    "下午开了个短会",
    "电脑有点卡",
    "楼下新开了家咖啡店",
    "周末想去看场电影",
    "手机充电器找不到了",
    "阳台的花该浇水了",
    "同事推荐了一本书",
    "快递放到了驿站",
    "明天要早起",
    "空调温度调低了一点",
    "鞋子有点磨脚",
    "想换个键盘",
    "冰箱里的牛奶快过期了",
    "晚上散步走了两圈",
    "窗户关好了",
    "睡前一小时不看手机",
]

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + ("   " + detail if detail else ""))


def run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
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


def main() -> int:
    data_dir = tempfile.mkdtemp(prefix="la-verify-")
    env = dict(os.environ)
    env["LA_LLM_PROVIDER"] = "fake"
    env["LA_DATA_DIR"] = data_dir
    env["PYTHONIOENCODING"] = "utf-8"
    print("验收数据目录: " + data_dir)

    print("\n[1/3] 自检 --check")
    proc = run(env, "--check")
    check("自检退出码为 0", proc.returncode == 0, proc.stderr.strip()[:120])
    check("检索索引探测为 trigram", "trigram" in proc.stdout)
    check("记忆库已就绪", "memory.db" in proc.stdout)

    print("\n[2/3] 场景 S1:说一次名字,重启后投影仍带着它")
    proc = run(env, "--session", "s1", "--say", "我的英文名是 Kevin,叫我就行",
               "--say", "今天天气怎么样?")
    check("S1 第一进程正常退出", proc.returncode == 0)
    proc = run(env, "--session", "s1", "--dump")
    check("S1 重启后投影含 <facts> 块", "<facts>" in proc.stdout)
    check("S1 重启后投影含 Kevin", "Kevin" in proc.stdout)
    check("S1 事实落在长期记忆而非对话历史", "用户的英文名是 Kevin" in proc.stdout)

    print("\n[3/3] 场景 S2:20 轮闲聊 -> 压缩 -> 重启 -> 检索")
    args: list[str] = ["--session", "s2"]
    for line in CHITCHAT:
        args += ["--say", line]
    proc = run(env, *args)
    check("S2 20 轮落盘成功", proc.returncode == 0)

    proc = run(env, "--session", "s2", "--compact")
    check("压缩产出了摘要", "已生成摘要" in proc.stdout, proc.stdout.strip()[:80])

    proc = run(env, "--session", "s2", "--once", "我们刚才聊的餐厅叫什么")
    check("S2 重启后命中摘要", "老张牛肉面" in proc.stdout, proc.stdout.strip()[:80])
    check("S2 命中带锚点时间", bool(re.search(r"\d{4}-\d{2}-\d{2}", proc.stdout)))

    proc = run(env, "--session", "s2", "--once", "我上次说的那个航班号是多少")
    check("S2 未命中时诚实回答", "记忆里没有" in proc.stdout, proc.stdout.strip()[:80])

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n结果: " + str(passed) + "/" + str(total) + " 项通过")
    for name, ok, _ in RESULTS:
        if not ok:
            print("  未通过: " + name)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
