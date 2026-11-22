"""M4.3c 端到端验收:排程、复习日志、孩子端两按钮、FSRS 五个坑。

    .venv/Scripts/python.exe scripts/verify_m4_3c.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
DATA_DIR = tempfile.mkdtemp(prefix="la-verify43c-")

os.environ["LA_DATA_DIR"] = DATA_DIR
os.environ["LA_LLM_PROVIDER"] = "fake"
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.path.insert(0, str(BASE_DIR))

TZ = ZoneInfo("Asia/Shanghai")
UTC = ZoneInfo("UTC")
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + ("   " + detail if detail else ""))


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, "-m", "la", *args], cwd=str(BASE_DIR), env=dict(os.environ),
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )


def main() -> int:
    from la.domain.media import MediaStore
    from la.domain.schemas.study import ProblemCreate
    from la.domain.services.study_service import StudyService
    from la.domain.study.scheduler import (
        CHILD_RATINGS,
        FIXED_INTERVALS_DAYS,
        MAX_INTERVAL_DAYS,
        RATING_AGAIN,
        RATING_GOOD,
        FsrsPolicy,
        UnitState,
        build_policy,
    )
    from la.storage import db

    print("验收数据目录: " + DATA_DIR)
    now = datetime.now(TZ)

    print("\n[1/5] FSRS 落地的五个坑,逐条验证")
    policy = FsrsPolicy()
    check("坑之二:maximum_interval = 180,不是默认的 36500",
          policy.scheduler.maximum_interval == MAX_INTERVAL_DAYS,
          str(policy.scheduler.maximum_interval))
    check("坑之五:没有加载任何个性化参数(第一年不碰 optimizer)",
          tuple(policy.scheduler.parameters) == tuple(type(policy.scheduler)().parameters),
          "用的是官方默认参数")

    conn = db.connect_assistant()
    service = StudyService(conn, media=MediaStore(conn, root=Path(DATA_DIR) / "media"),
                           now_provider=lambda: now, srs_engine="fixed")
    record = service.add_problem(ProblemCreate(
        subject="数学", stem_text="5/6 - 1/4 = ?", answer_text="7/12",
        knowledge_points=["异分母分数加减", "通分"],
    ))
    check("坑之三:新建单元不立刻到期,首次在明天",
          all(u.due_date_local == (now + timedelta(days=1)).date().isoformat()
              for u in record.review_units))
    row = conn.execute("SELECT due_utc, due_date_local FROM app_review_units LIMIT 1").fetchone()
    check("坑之一:due_utc 与 due_date_local 两个都存",
          "+00:00" in row["due_utc"] and len(row["due_date_local"]) == 10,
          row["due_utc"][:19])
    check("坑之四:孩子端只有两个按钮", CHILD_RATINGS == (1, 3), str(CHILD_RATINGS))

    print("\n[2/5] 固定间隔:1 → 3 → 7 → 15 → 30")
    unit_id = record.review_units[0].id
    ladder: list[float] = []
    for _ in range(len(FIXED_INTERVALS_DAYS)):
        result = service.record_review(unit_id, RATING_GOOD)
        ladder.append(round(result.interval_days, 2))
    check("走完 1/3/7/15/30 五档", ladder == [1.0, 3.0, 7.0, 15.0, 30.0], str(ladder))
    capped = service.record_review(unit_id, RATING_GOOD)
    check("到顶后停在 30 天", round(capped.interval_days, 2) == 30.0)

    print("\n[3/5] 复习日志:只增不改,且能看出进步")
    history = service.review_history(unit_id)
    check("每次复习各留一条,不覆盖", len(history) == 6, str(len(history)) + " 条")
    check("相邻两次的间隔能对比(进步了没)",
          history[0]["last_interval_days"] == history[1]["interval_days"],
          str(history[0]["last_interval_days"]) + " vs " + str(history[1]["interval_days"]))
    check("日志字段齐全(未来训练个人参数的唯一依据)",
          all(key in history[0] for key in
              ("interval_days", "last_interval_days", "elapsed_ms", "state_before",
               "state_after", "source")))

    print("\n[4/5] 答错与当天不重复")
    before_lapses = service.list_units(record.id)[0].lapses
    forgot = service.record_review(unit_id, RATING_AGAIN)
    after = service.list_units(record.id)[0]
    check("答错后很快再见(回到第一档)", round(forgot.interval_days, 2) == 1.0)
    check("lapses 只在答错时增加", after.lapses == before_lapses + 1)

    today = now.date().isoformat()
    conn.execute("UPDATE app_review_units SET due_date_local = ?", (today,))
    conn.commit()
    queue_before = service.review_queue(today=today)
    reviewed = queue_before[0]["unit_id"]
    service.record_review(reviewed, RATING_GOOD)
    queue_after = service.review_queue(today=today)
    # ⚠️ 2026-10-17 审计:旧断言是"总数少 1",而一道题可能有**多个复习单元**
    #    (原题一个 + 每个知识点各一个),复习掉其中一个,总数不一定减一。
    #    要守的是"刚复习的那一个当天不再出现",所以直接认那个 unit_id。
    check("⭐ 复习过的那个单元当天不再出现在清单里",
          reviewed not in [unit["unit_id"] for unit in queue_after],
          "复习前 " + str(len(queue_before)) + " -> 复习后 " + str(len(queue_after)))

    print("\n[5/5] FSRS 引擎与 CLI")
    # ⚠️ FSRS 的稳定性增长依赖"距上次复习过了多久" —— 固定时钟下它会一直给同一个间隔。
    # 所以这里用一个**会走的时钟**,模拟"到点了就去复习"的真实节奏。
    clock = {"now": now}
    fsrs_service = StudyService(conn, media=MediaStore(conn, root=Path(DATA_DIR) / "media"),
                                now_provider=lambda: clock["now"], srs_engine="fsrs")
    fsrs_record = fsrs_service.add_problem(ProblemCreate(
        subject="英语", stem_text="He ___ to school yesterday.", answer_text="went",
        knowledge_points=["一般过去时"],
    ))
    fsrs_unit = fsrs_record.review_units[0].id
    intervals: list[float] = []
    for _ in range(4):
        result = fsrs_service.record_review(fsrs_unit, RATING_GOOD)
        intervals.append(round(result.interval_days, 2))
        clock["now"] = clock["now"] + timedelta(days=max(result.interval_days, 0.01))
    check("⭐ FSRS 间隔随「真实保持」增长(时钟必须往前走)", intervals[-1] > intervals[1],
          str(intervals))
    check("FSRS 任何一次都不超过 180 天", all(i <= MAX_INTERVAL_DAYS + 1 for i in intervals))
    check("引擎可切换", build_policy("fsrs").name == "fsrs" and build_policy("fixed").name == "fixed")

    # 固定时钟下 FSRS 不会增长 —— 这是它的工作原理,不是 bug。留一条断言把这个认知钉住。
    frozen = StudyService(conn, media=MediaStore(conn, root=Path(DATA_DIR) / "media"),
                          now_provider=lambda: now, srs_engine="fsrs")
    frozen_record = frozen.add_problem(ProblemCreate(subject="语文", stem_text="冻结时钟"))
    frozen_unit = frozen_record.review_units[0].id
    frozen_first = frozen.record_review(frozen_unit, RATING_GOOD)
    frozen_second = frozen.record_review(frozen_unit, RATING_GOOD)
    check("固定时钟下 FSRS 间隔不变(证明它确实在看「过了多久」)",
          abs(frozen_first.interval_days - frozen_second.interval_days) < 1e-6,
          str(round(frozen_first.interval_days, 3)) + " vs " + str(round(frozen_second.interval_days, 3)))
    conn.close()

    proc = run("--review", "5")
    check("--review 能取到清单", "要复习" in proc.stdout or "没有要复习" in proc.stdout,
          proc.stdout.strip().replace("\n", " ")[:90])

    proc = run("--record-review", str(fsrs_unit), "--rating", "3")
    check("--record-review 能记下并给出下次日期", "下次复习" in proc.stdout,
          proc.stdout.strip()[:90])
    check("--record-review 明确说下次是哪天", "2026-" in proc.stdout or "2027-" in proc.stdout)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n结果: " + str(passed) + "/" + str(len(RESULTS)) + " 项通过")
    for name, ok, _ in RESULTS:
        if not ok:
            print("  未通过: " + name)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())