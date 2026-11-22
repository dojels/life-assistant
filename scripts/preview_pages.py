"""看健康页 / 素材页长什么样:导出**可直接截图**的单文件 HTML。

与 preview_ledger.py 同一套路:CSS 内联(否则 file:// 打不开 /static/),
再用无头 Chrome 截图就是所见即所得。

跑法:
    .venv/Scripts/python.exe scripts/preview_pages.py --out .tmp-pages
"""

from __future__ import annotations

import argparse
import io
import random
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.dont_write_bytecode = True

from fastapi.testclient import TestClient  # noqa: E402

from la import config  # noqa: E402
from la.domain.artifacts import ArtifactStore  # noqa: E402
from la.domain.media import MediaStore  # noqa: E402
from la.domain.materials import MaterialService  # noqa: E402
from la.domain.services.health_service import HealthService  # noqa: E402
from la.storage import db  # noqa: E402
from la.storage.migrations import MIGRATIONS_ASSISTANT, MIGRATIONS_MEMORY  # noqa: E402
from la.web.app import create_app  # noqa: E402


def _inline_css(html: str) -> str:
    def swap(match: re.Match[str]) -> str:
        name = match.group(1).rsplit("/", 1)[-1]
        path = BASE_DIR / "la" / "web" / "static" / name
        if not path.exists():
            return match.group(0)
        return "<style>\n" + path.read_text(encoding="utf-8") + "\n</style>"

    return re.sub(r'<link[^>]+href="([^"]+\.css)"[^>]*>', swap, html)


def _seed_health(health: HealthService, conn: sqlite3.Connection) -> None:
    from datetime import date, timedelta

    health.update_profile(sex="男", birth_year=1990, height_cm=175, activity_level="moderate")
    today = date.today()
    rng = random.Random(7)
    for offset in range(27, -1, -1):
        day = today - timedelta(days=offset)
        base = 74.2 - (27 - offset) * 0.09          # 缓慢下降
        value = round(base + rng.uniform(-0.6, 0.6), 1)
        health.log_measurement({
            "metric_code": "weight", "value": value, "source": "user",
            "measured_at": day.isoformat() + "T07:20:00+08:00",
        }, actor="user", receipt=False)
    workouts = [("跑步", 32), ("快走", 45), ("力量", 40), ("游泳", 30),
                ("骑行", 55), ("瑜伽", 35), ("跑步", 28)]
    for index, (kind, minutes) in enumerate(workouts):
        day = today - timedelta(days=index)
        health.log_workout({
            "activity": kind, "duration_min": minutes,
            "ts": day.isoformat() + "T18:30:00+08:00",
        }, actor="user")
    # 吃的:直接落 app_food_logs(预览不需要走完整模型链)
    food = conn.execute(
        "INSERT INTO app_foods(name, unit, kcal_per_unit, source, cached_at)"
        " VALUES ('家常菜', '份', 620, 'preview', datetime('now'))"
    ).lastrowid
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        for kcal in (520, 760, 610):
            conn.execute(
                "INSERT INTO app_food_logs(ts, local_date, meal_type, food_id, amount, kcal,"
                " note, created_at) VALUES (?, ?, 'meal', ?, 1, ?, NULL, datetime('now'))",
                (day.isoformat() + "T12:00:00+08:00", day.isoformat(), food, kcal),
            )
    conn.commit()


def _png_bytes(color: tuple[int, int, int]) -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (640, 480), color)
    draw = ImageDraw.Draw(image)
    for index in range(6):
        draw.rectangle([40 + index * 90, 120 + (index % 3) * 60,
                        110 + index * 90, 190 + (index % 3) * 60],
                       outline=(255, 255, 255), width=3)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _seed_materials(conn: sqlite3.Connection) -> dict[int, bytes]:
    materials = MaterialService(conn, media=MediaStore(conn, root=config.MEDIA_DIR))
    # ⚠️ 给够数量(>24),否则截图上根本看不到翻页条 —— 而翻页正是这一版要看的东西
    samples = [
        ("数学-第3页.png", (90, 127, 199)),
        ("英语-听写本.png", (232, 154, 96)),
        ("语文-作文.png", (140, 196, 160)),
        ("错题-物理.png", (186, 140, 210)),
        ("错题-化学.png", (232, 120, 120)),
        ("账单-九月.pdf", (150, 150, 160)),
    ]
    for index in range(24):
        shade = (90 + index * 6) % 200
        samples.append(("练习-%02d.png" % (index + 1), (shade, 150, 210 - index * 3)))
    ids = []
    # 截图时图片得能看见:/materials/<id>/file 要登录态,静态导出打不开 ——
    # 所以把 PNG 的字节直接编成 data URI 塞回页面里(仅预览用)。
    thumbs: dict[int, bytes] = {}
    for name, color in samples:
        payload = _png_bytes(color) if name.endswith(".png") else b"%PDF-1.4 preview"
        material, _fresh = materials.ingest_bytes(payload, channel="web", original_name=name)
        ids.append(material.id)
        if name.endswith(".png"):
            thumbs[material.id] = payload
    # 状态做点区分:前两个已录进错题本,第四个识别失败
    for index, status in ((0, "recorded"), (1, "recorded"), (2, "pending"), (3, "failed")):
        conn.execute("UPDATE app_materials SET handled = ? WHERE id = ?", (status, ids[index]))
    conn.commit()

    artifacts = ArtifactStore(conn, root=config.ARTIFACTS_DIR)
    artifacts.register_text(
        "<html><body><h1>今日错题卷</h1></body></html>",
        filename="错题卷-1018.html", label="今日错题卷 · 8 道",
    )
    artifacts.register_text(
        "日期,科目,题目\n10-18,数学,分数加减\n",
        filename="错题清单-1018.csv", label="错题清单 · CSV",
    )
    for index in range(11):
        artifacts.register_text(
            "第 %d 份\n" % index, filename="weekly-%02d.txt" % index,
            label="每周错题汇总 · 第 %d 周" % (index + 1),
        )
    return thumbs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="preview_pages")
    parser.add_argument("--out", default=".tmp-pages")
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
    _seed_health(health, conn)
    thumbs = _seed_materials(conn)
    conn.close()

    client = TestClient(create_app())
    client.post("/setup", data={"pin": "123456", "pin2": "123456"})
    pages = {
        "health.html": "/health",
        "health-compose.html": "/health?compose=workout",
        "materials.html": "/materials",
        "materials-upload.html": "/materials?upload=1",
    }
    import base64

    for name, path in pages.items():
        response = client.get(path)
        if response.status_code != 200:
            print("FAIL", path, response.status_code)
            return 1
        html = response.text
        for material_id, payload in thumbs.items():
            html = html.replace(
                'src="/materials/' + str(material_id) + '/file"',
                'src="data:image/png;base64,' + base64.b64encode(payload).decode() + '"',
            )
        (out / name).write_text(_inline_css(html), encoding="utf-8")
        print("ok", path, "->", out / name)
    client.close()
    for key, value in patched.items():
        setattr(config, key, value)
    tmp.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
