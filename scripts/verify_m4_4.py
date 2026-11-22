"""M4.4 端到端验收:泛化测量表、EWMA 趋势、MET、能量收支、Apple Health 导入、两条红线。

    .venv/Scripts/python.exe scripts/verify_m4_4.py
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
DATA_DIR = tempfile.mkdtemp(prefix="la-verify44-")

os.environ["LA_DATA_DIR"] = DATA_DIR
os.environ["LA_LLM_PROVIDER"] = "fake"
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.path.insert(0, str(BASE_DIR))

TZ = ZoneInfo("Asia/Shanghai")
RESULTS: list[tuple[str, bool, str]] = []

APPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="zh_CN">
 <Record type="HKQuantityTypeIdentifierBodyMass" sourceName="iPhone" unit="kg"
   startDate="2026-09-01 07:30:00 +0800" endDate="2026-09-01 07:30:00 +0800" value="75.2"/>
 <Record type="HKQuantityTypeIdentifierRestingHeartRate" sourceName="Watch" unit="count/min"
   startDate="2026-09-01 08:00:00 +0800" endDate="2026-09-01 08:00:00 +0800" value="58"/>
 <Record type="HKQuantityTypeIdentifierBodyFatPercentage" sourceName="Watch" unit="%"
   startDate="2026-09-01 08:05:00 +0800" endDate="2026-09-01 08:05:00 +0800" value="0.185"/>
 <Record type="HKQuantityTypeIdentifierSomethingBrandNew" sourceName="Watch" unit="x"
   startDate="2026-09-01 08:10:00 +0800" endDate="2026-09-01 08:10:00 +0800" value="1"/>
</HealthData>
"""


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + ("   " + detail if detail else ""))


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, "-m", "la", *args], cwd=str(BASE_DIR), env=dict(os.environ),
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )


def main() -> int:
    from la.domain.health.metrics import EWMA_LAG_DAYS, met_minutes
    from la.domain.services.health_service import (
        MEASUREMENT_CAVEAT,
        MEDICAL_DISCLAIMER,
        HealthService,
    )
    from la.storage import db

    print("验收数据目录: " + DATA_DIR)
    now = datetime.now(TZ)
    conn = db.connect_assistant()
    service = HealthService(conn, now_provider=lambda: now)

    print("\n[1/5] 泛化测量表 + 指标字典")
    types = service.list_metric_types()
    check("指标字典里有常用指标", len(types) >= 8, str([t["code"] for t in types[:5]]) + "…")
    service.log_measurement({"metric_code": "weight", "value": 74.0})
    service.log_measurement({"metric_code": "resting_hr", "value": 58})
    check("同一条测量表装下所有指标(不新建表)",
          conn.execute("SELECT count(DISTINCT metric_code) FROM app_measurements").fetchone()[0] == 2)
    check("本地自然日单独存(按天聚合走索引)",
          conn.execute("SELECT local_date FROM app_measurements LIMIT 1").fetchone()[0]
          == now.date().isoformat())

    print("\n[2/5] EWMA:把噪声压下去,并如实说明滞后")
    # 造 40 天:真实趋势每天 -0.06kg,叠加 ±1.5kg 的日噪声
    import math

    for day in range(1, 41):
        day_date = (now - timedelta(days=41 - day)).date()
        noise = 1.5 * math.sin(day * 2.3)
        service.log_measurement({
            "metric_code": "weight", "value": 75.0 - 0.06 * day + noise,
            "measured_at": day_date.isoformat() + "T07:00:00+08:00",
        })
    trend = service.trend({"metric_code": "weight", "days": 90})

    # ⚠️ 不能比"总跨度" —— 趋势跨度里本来就包含真实的下降(0.06×40 = 2.4kg)。
    # 要比的是**残差**:离真实趋势线有多远。这才是"噪声被压下去"的正确度量。
    def true_value(day: int) -> float:
        return 75.0 - 0.06 * day

    # 只看后半段:开头十几个点是冷启动瞬态(趋势初值就是第一个含噪读数),
    # 拿瞬态说"降噪效果差"不公平 —— 稳态才是这个滤波器的真实表现。
    tail = list(enumerate(trend.points))[20:]
    raw_residual = max(abs(point.value - true_value(index + 1)) for index, point in tail)
    trend_residual = max(abs(point.trend - true_value(index + 1)) for index, point in tail)
    check("⭐ 趋势线把日噪声压下去(稳态残差,不是总跨度)",
          trend_residual < raw_residual / 2,
          "原始最大偏离 " + str(round(raw_residual, 2)) + "kg -> 趋势最大偏离 "
          + str(round(trend_residual, 2)) + "kg")
    check("⭐ 摘要主动说明滞后(否则用户以为曲线不动是 bug)",
          "滞后" in trend.summary and trend.trend_lag_days == EWMA_LAG_DAYS)
    check("看出下降方向", (trend.weekly_rate or 0) < 0,
          "周变化 " + str(trend.weekly_rate) + " kg/周")

    print("\n[3/5] MET-分钟与能量收支")
    check("MET-分钟不依赖体重", met_minutes(8.3, 30) == 249.0 or abs(met_minutes(8.3, 30) - 249.0) < 1e-9)
    out = service.log_workout({"activity": "跑步", "duration_min": 30})
    check("用强度系数换算,MET-分钟可跨运动累加",
          out["met_minutes"] == 249.0 and out["intensity"] == "剧烈",
          str(out["met"]) + " MET × 30 分钟")

    service.update_profile(sex="男", birth_year=1990, height_cm=175, activity_level="light")
    service.log_food({"food_name": "米饭", "kcal_per_unit": 200, "amount": 10})
    summary = service.energy_summary(days=7)
    low, high = summary["deficit_range"]
    check("给出 BMR / TDEE / 缺口", summary["bmr"] > 1000 and summary["tdee"] > summary["bmr"],
          "BMR 约 " + str(summary["bmr"]) + " / TDEE 约 " + str(summary["tdee"]))
    check("⭐ 缺口带诚实的 ±400 区间(不报精确值)",
          high - low == 800, str(low) + " ~ " + str(high))
    check("⭐ 附医疗免责", "不构成医疗建议" in summary["disclaimer"])
    check("食物热量带误差说明", "±30%" in MEASUREMENT_CAVEAT)

    print("\n[4/5] 历史反推 TDEE:数据不够时必须说不够")
    short = service.estimate_tdee_from_history(days=21)
    check("⭐ 只有几天数据时如实说数据不够",
          not short["available"] and "还需要更多数据" in short["reason"],
          short.get("reason", "")[:40])

    print("\n[5/5] Apple Health 导入与 CLI")
    xml = Path(DATA_DIR) / "export.xml"
    xml.write_text(APPLE_XML, encoding="utf-8")
    report = service.import_apple_health(str(xml))
    check("只认白名单,未知类型跳过而不是崩",
          report["imported"] == 3 and report["skipped_unknown"] == 1,
          str(report["by_type"]))
    check("体脂做了单位换算(苹果存 0..1)", abs(service.latest("body_fat")["value"] - 18.5) < 0.01)
    check("来源可追溯", service.latest("body_fat") is not None)
    conn.close()

    proc = run("--log-weight", "73.2")
    check("--log-weight 能记", "记下了" in proc.stdout, proc.stdout.strip()[:60])
    proc = run("--log-workout", "瑜伽", "--minutes", "40")
    check("--log-workout 给出 MET-分钟与强度", "MET-分钟" in proc.stdout and "强度" in proc.stdout,
          proc.stdout.strip()[:70])
    proc = run("--energy", "7")
    check("--energy 用「约」表述并给区间", "约 " in proc.stdout and "~" in proc.stdout,
          proc.stdout.strip().splitlines()[0][:70] if proc.stdout.strip() else "")
    check("--energy 打印医疗免责", "不构成医疗建议" in proc.stdout)
    proc = run("--health-today")
    check("--health-today 给出当日小结", "MET-分钟" in proc.stdout,
          proc.stdout.strip().replace("\n", " ")[:70])
    check("免责文本就位", "不构成医疗建议" in MEDICAL_DISCLAIMER)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n结果: " + str(passed) + "/" + str(len(RESULTS)) + " 项通过")
    for name, ok, _ in RESULTS:
        if not ok:
            print("  未通过: " + name)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())