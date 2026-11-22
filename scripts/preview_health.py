"""预览健康页(四个页签)并截图。

跑法:
    .venv/Scripts/python.exe scripts/preview_health.py --out .tmp-health
"""

from __future__ import annotations

import argparse
import random
import re
import sqlite3
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.dont_write_bytecode = True

from fastapi.testclient import TestClient  # noqa: E402

from la import config  # noqa: E402
from la.base.shot import find_chrome  # noqa: E402
from la.domain.services.health_service import HealthService  # noqa: E402
from la.storage import db  # noqa: E402
from la.storage.migrations import MIGRATIONS_ASSISTANT, MIGRATIONS_MEMORY  # noqa: E402
from la.web import health_sync  # noqa: E402
from la.web.app import create_app  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")


def _inline_css(html: str) -> str:
    def swap(match: re.Match[str]) -> str:
        name = match.group(1).rsplit("/", 1)[-1].split("?", 1)[0]
        path = BASE_DIR / "la" / "web" / "static" / name
        if not path.exists():
            return match.group(0)
        return "<style>\n" + path.read_text(encoding="utf-8") + "\n</style>"

    # ⚠️ base.html 的 app.css 带 ?v=… 缓存串,正则必须认它,否则整页没有布局。
    return re.sub(r'<link[^>]+href="([^"]+\.css[^"]*)"[^>]*>', swap, html)


def _seed(health: HealthService, conn: sqlite3.Connection) -> None:
    health.update_profile(sex="男", birth_year=1990, height_cm=175, activity_level="moderate")
    today = datetime.now(TZ).date()
    rng = random.Random(11)
    days = []
    for offset in range(29, -1, -1):
        day = today - timedelta(days=offset)
        steps = int(6200 + rng.uniform(-1600, 2600) + (900 if day.weekday() < 5 else -400))
        days.append({
            "date": day.isoformat(),
            "steps": max(1200, steps),
            "distance_km": round(max(1.0, steps / 1350.0), 2),
            "active_kcal": int(max(120, steps * 0.045 + rng.uniform(-30, 60))),
            "basal_kcal": 1520,
            "exercise_min": int(rng.choice([0, 12, 22, 31, 45, 58])),
            "flights": int(rng.choice([2, 4, 6, 9, 12])),
            "stand_hours": int(rng.choice([8, 10, 11, 12, 13])),
        })
    # 今天的数据单独补满,让三个环都在动
    days[-1].update({"steps": 7320, "active_kcal": 386, "exercise_min": 24,
                     "distance_km": 5.4, "flights": 7, "stand_hours": 9})
    health.sync_push({"days": days, "workouts": []})

    for offset in range(13, -1, -1):
        day = today - timedelta(days=offset)
        hours = round(7.1 + rng.uniform(-0.8, 0.7), 1)
        wake = day.isoformat() + "T07:05:00+08:00"
        health.log_measurement({
            "metric_code": "sleep_hours", "value": hours, "source": "device:apple",
            "measured_at": wake, "note": "23:20→07:05",
        }, actor="import", receipt=False)
        health.log_measurement({
            "metric_code": "sleep_midpoint", "value": round(3.1 + rng.uniform(-0.6, 0.6), 2),
            "source": "device:apple", "measured_at": wake,
        }, actor="import", receipt=False)

    for offset in range(28, -1, -2):
        day = today - timedelta(days=offset)
        health.log_measurement({
            "metric_code": "weight", "value": round(74.2 - (28 - offset) * 0.035 + rng.uniform(-0.35, 0.35), 1),
            "source": "device:apple", "measured_at": day.isoformat() + "T07:20:00+08:00",
        }, actor="import", receipt=False)

    for offset in range(7, -1, -1):
        day = today - timedelta(days=offset)
        health.log_measurement({
            "metric_code": "resting_hr", "value": 57 + rng.randint(0, 4),
            "source": "device:apple", "measured_at": day.isoformat() + "T08:00:00+08:00",
        }, actor="import", receipt=False)

    for offset, kind, minutes, distance, kcal in (
        (0, "跑步", 31, 5.1, 298), (1, "快走", 46, 3.8, 168),
        (2, "力量训练", 42, None, 210), (4, "骑车", 55, 16.2, 365),
        (5, "跑步", 28, 4.6, 270), (6, "瑜伽", 35, None, 120),
    ):
        day = today - timedelta(days=offset)
        health.sync_push({"workouts": [{
            "activity": kind, "duration_min": minutes,
            "start": day.isoformat() + "T18:30:00+08:00",
            "distance_km": distance, "kcal": kcal,
            "external_id": "seed|" + kind + "|" + day.isoformat(),
        }]})
    # ⭐ 2026-11-21(D188):一条**只有个数、没有时长**的运动 —— 预览图要能看见
    #    「第二个维度」与「没记时长就不算消耗」长什么样(实测截图也靠它)。
    health.log_workout({"activity": "仰卧起坐", "reps": 40}, actor="user")
    conn.execute(
        "INSERT INTO app_health_syncs(ts, source, items, days, note, ip, ua)"
        " VALUES (?, 'shortcut', 7, 1, 'iPhone', '192.168.1.9', 'Shortcuts/1120')",
        (datetime.now(TZ).replace(microsecond=0).isoformat(),),
    )
    conn.commit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="preview_health")
    parser.add_argument("--out", default=".tmp-health")
    parser.add_argument("--no-shots", action="store_true")
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    root = Path(tmp.name)
    patched = {key: getattr(config, key) for key in (
        "DATA_DIR", "ARCHIVE_DIR", "ARTIFACTS_DIR", "MEDIA_DIR",
        "ASSISTANT_DB", "MEMORY_DB", "LOGS_DIR",
    )}
    config.DATA_DIR = root
    config.ARCHIVE_DIR = root / "archive"
    config.ARTIFACTS_DIR = root / "artifacts"
    config.MEDIA_DIR = root / "media"
    config.ASSISTANT_DB = root / "assistant.db"
    config.MEMORY_DB = root / "memory.db"
    config.LOGS_DIR = root / "logs"
    config.ensure_runtime_dirs()

    conn = sqlite3.connect(str(config.ASSISTANT_DB))
    conn.row_factory = sqlite3.Row
    db.migrate(conn, MIGRATIONS_ASSISTANT)
    db.migrate(conn, MIGRATIONS_MEMORY)
    health = HealthService(conn)
    _seed(health, conn)

    client = TestClient(create_app())
    client.post("/setup", data={"pin": "123456", "pin2": "123456"})
    row = conn.execute("SELECT id FROM app_members ORDER BY id LIMIT 1").fetchone()
    health_sync.rotate_token(conn, int(row["id"]) if row else None)
    conn.close()

    pages = {
        "health-today.html": "/health",
        "health-trend.html": "/health?tab=trend&metric=steps&days=30",
        "health-energy.html": "/health?tab=energy",
        "health-sync.html": "/health?tab=sync",
        # ⭐ 2026-11-21(D188):「记运动」的三个输入框(时长 / 距离 / 个数)要能看见
        #    —— 这张图就是"两个维度"的入口,改表单时先看它。
        "health-workout.html": "/health?compose=workout",
    }
    for name, path in pages.items():
        response = client.get(path)
        if response.status_code != 200:
            print("FAIL", path, response.status_code)
            return 1
        (out / name).write_text(_inline_css(response.text), encoding="utf-8")
        print("ok", path, "->", out / name)
    client.close()

    if not args.no_shots:
        chrome = find_chrome()
        shots = [
            ("health-today.html", "health-today-desktop.png", 1280, 1080),
            ("health-trend.html", "health-trend-desktop.png", 1280, 900),
            ("health-energy.html", "health-energy-desktop.png", 1280, 1080),
            ("health-sync.html", "health-sync-desktop.png", 1280, 1500),
            ("health-workout.html", "health-workout-desktop.png", 1280, 900),
            # ⚠️ Chrome 的窗口有最小宽度(实测 512):写 430 会得到"布局按 512、截图按 430",
            #    右边被切掉。用 512 才是真正的手机宽度(媒体查询仍是 max-width:820)。
            ("health-today.html", "health-today-mobile.png", 512, 1300),
            ("health-trend.html", "health-trend-mobile.png", 512, 900),
        ]
        for page, png, width, height in shots:
            subprocess.run([
                chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                "--force-device-scale-factor=2",
                "--window-size=" + str(width) + "," + str(height),
                "--screenshot=" + str((out / png).resolve()),
                (out / page).resolve().as_uri(),
            ], check=False, timeout=60)
            print("shot", png)

    for key, value in patched.items():
        setattr(config, key, value)
    tmp.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
