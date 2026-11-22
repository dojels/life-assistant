"""看账本页长什么样:把 SSR 结果导出成**可直接截图**的单文件 HTML。

为什么要有它(2026-10-24 D164):账本页做了一次大改版,改完之后
"好不好看"这件事没法用断言表达 —— 只能真的渲染出来看一眼。
这个脚本把 CSS 内联进 HTML(否则 file:// 打不开 /static/),再用无头 Chrome
截图就是所见即所得。

跑法:
    .venv/Scripts/python.exe scripts/preview_ledger.py --out .tmp-shots
"""

from __future__ import annotations

import argparse
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
from la.domain.schemas.ledger import TxnCreate  # noqa: E402
from la.domain.services.ledger_service import LedgerService  # noqa: E402
from la.storage import db  # noqa: E402
from la.storage.migrations import MIGRATIONS_ASSISTANT  # noqa: E402
from la.web.app import create_app  # noqa: E402

#: ⚠️ 这份种子数据是照**生产库的真实形状**抄的(2026-10-24):
#: 14 笔、只有 3 个支出分类 —— 分类很少时布局最容易露馅,所以拿它当样板。
SEED = [
    ("expense", 3800, "老张牛肉面", "餐饮", "2026-10-24T12:10:00+08:00"),
    ("expense", 2650, "星巴克", "餐饮", "2026-10-24T09:05:00+08:00"),
    ("expense", 1200, "地铁", "汽车", "2026-10-24T08:20:00+08:00"),
    ("expense", 2500, "便利店", "购物", "2026-10-23T21:40:00+08:00"),
    ("expense", 3200, "美团买菜", "餐饮", "2026-10-23T18:30:00+08:00"),
    ("expense", 340000, "修车", "汽车", "2026-10-20T10:00:00+08:00"),
    ("expense", 19900, "健身房", "餐饮", "2026-10-18T19:00:00+08:00"),
    ("expense", 4500, "药店", "餐饮", "2026-10-17T15:12:00+08:00"),
    ("expense", 6800, "出租车", "汽车", "2026-10-16T22:03:00+08:00"),
    ("expense", 12000, "咖啡", "餐饮", "2026-10-15T10:00:00+08:00"),
    ("expense", 9000, "外卖", "餐饮", "2026-10-14T19:30:00+08:00"),
    ("expense", 7600, "早餐", "餐饮", "2026-10-13T08:10:00+08:00"),
    ("expense", 5000, "停车", "汽车", "2026-10-12T20:00:00+08:00"),
    ("income", 50000, "兼职", "兼职", "2026-10-12T14:00:00+08:00"),
]


def _inline_css(html: str) -> str:
    def swap(match: re.Match[str]) -> str:
        href = match.group(1)
        path = BASE_DIR / "la" / "web" / "static" / href.rsplit("/", 1)[-1]
        if not path.exists():
            return match.group(0)
        return "<style>\n" + path.read_text(encoding="utf-8") + "\n</style>"

    return re.sub(r'<link[^>]+href="([^"]+\.css)"[^>]*>', swap, html)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="preview_ledger")
    parser.add_argument("--out", default=".tmp-shots")
    parser.add_argument("--many", action="store_true",
                        help="铺 12 个分类,用来看「分类排行」那个固定窗口会不会滚")
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
    ledger = LedgerService(conn)
    # 生产库里"汽车""兼职"是用户自己建的分类,种子数据先把它建出来(幂等)
    ledger.create_category(name="汽车", kind="expense", reason="预览")
    ledger.create_category(name="兼职", kind="income", reason="预览")
    for direction, cents, payee, category, ts in SEED:
        ledger.record_expense(
            TxnCreate(direction=direction, amount_cents=cents, payee=payee,
                      category=category, ts=ts),
            status="confirmed", actor="user",
        )
    if args.many:
        names = ["宠物", "健身", "房租", "医疗", "教育", "娱乐", "人情",
                 "交通", "通讯", "水电", "旅行"]
        for index, name in enumerate(names):
            ledger.create_category(name=name, kind="expense", reason="预览")
            ledger.record_expense(
                TxnCreate(amount_cents=(index + 1) * 1234, payee=name + "支出",
                          category=name, ts="2026-10-0" + str(index % 9 + 1) + "T12:00:00+08:00"),
                status="confirmed", actor="user",
            )
    conn.close()

    client = TestClient(create_app())
    client.post("/setup", data={"pin": "123456", "pin2": "123456"})
    pages = {
        "ledger-expense.html": "/ledger?view=expense&period=2026-10",
        "ledger-income.html": "/ledger?view=income&period=2026-10",
        "ledger-compose.html": "/ledger?view=expense&period=2026-10&compose=1",
        "ledger-categories.html": "/ledger/categories?kind=expense",
    }
    for name, path in pages.items():
        response = client.get(path)
        if response.status_code != 200:
            print("FAIL", path, response.status_code)
            return 1
        (out / name).write_text(_inline_css(response.text), encoding="utf-8")
        print("ok", path, "->", out / name)
    client.close()
    for key, value in patched.items():
        setattr(config, key, value)
    tmp.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
