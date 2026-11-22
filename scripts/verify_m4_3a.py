"""M4.3a 端到端验收:拍照录入、知识点拆分、复习单元、双视角、列表统计。

    .venv/Scripts/python.exe scripts/verify_m4_3a.py
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
DATA_DIR = tempfile.mkdtemp(prefix="la-verify43a-")
PHOTO_DIR = tempfile.mkdtemp(prefix="la-photos-")

os.environ["LA_DATA_DIR"] = DATA_DIR
os.environ["LA_LLM_PROVIDER"] = "fake"
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.path.insert(0, str(BASE_DIR))

TZ = ZoneInfo("Asia/Shanghai")
RESULTS: list[tuple[str, bool, str]] = []

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)
PNG2 = PNG[:-12] + b"\x00\x00\x00\x00\x00\x00\x00\x00\x49\x45\x4e\x44\xae\x42\x60\x82"


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + ("   " + detail if detail else ""))


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, "-m", "la", *args], cwd=str(BASE_DIR), env=dict(os.environ),
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )


def main() -> int:
    from la import config
    from la.domain.media import MediaStore
    from la.domain.schemas.study import ProblemCreate, ProblemQuery
    from la.domain.services.study_service import StudyService
    from la.storage import db

    print("验收数据目录: " + DATA_DIR)
    now = datetime.now(TZ)

    photo_a = Path(PHOTO_DIR) / "题1.png"
    photo_a.write_bytes(PNG)
    photo_b = Path(PHOTO_DIR) / "题2.png"
    photo_b.write_bytes(PNG2)

    print("\n[1/5] 拍照 + 手工补录(能力域 §5.4 的录入形态,此时还没有 OCR)")
    proc = run("--add-problem", "数学", "--stem", "3/4 + 1/6 = ?", "--answer", "11/12",
               "--kp", "异分母分数加减", "--kp", "通分", "--image", str(photo_a))
    check("录入命令正常退出", proc.returncode == 0, proc.stderr.strip()[:100])
    check("照片已存并内容寻址", "内容寻址" in proc.stdout, proc.stdout.strip().replace("\n", " ")[:110])
    check("建了原题 + 每个知识点各一个单元", "已建复习单元 3 个" in proc.stdout,
          proc.stdout.strip().replace("\n", " ")[:110])

    conn = db.connect_assistant()
    media = MediaStore(conn, root=Path(DATA_DIR) / "media")
    service = StudyService(conn, media=media)
    items = service.list_problems(ProblemQuery())
    record = items[0]
    check("图片文件真的落在媒体目录", media.exists(record.asset_ids[0]))
    asset = media.get(record.asset_ids[0])
    schema = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'app_assets'"
    ).fetchone()[0]
    check("库里只存 sha256 与**相对**路径,不存 BLOB",
          not Path(asset.rel_path).is_absolute() and "BLOB" not in schema.upper(),
          asset.rel_path)

    print("\n[2/5] 一题多单元 + 首次到期不轰炸")
    check("复习单元 = 原题 1 个 + 知识点 2 个", len(record.review_units) == 3,
          str([u.ord for u in record.review_units]))
    expected_due = (now + timedelta(days=1)).date().isoformat()
    check("⭐ 首次到期设在明天,不是立刻(否则一次传 20 道题就被 20 道题轰炸)",
          all(u.due_date_local == expected_due for u in record.review_units),
          expected_due)
    row = conn.execute(
        "SELECT due_utc, due_date_local FROM app_review_units LIMIT 1"
    ).fetchone()
    check("UTC 与本地自然日**都**存(FSRS 只认 UTC,今日复习要按本地日索引)",
          "+00:00" in row["due_utc"] and len(row["due_date_local"]) == 10)

    print("\n[3/5] 同一张照片再传一次应当去重")
    second = service.add_problem(ProblemCreate(
        subject="数学", stem_text="另一个孩子拍的同一张", image_paths=[str(photo_a)]
    ))
    check("内容寻址自动去重", second.asset_ids == record.asset_ids,
          str(second.asset_ids) + " vs " + str(record.asset_ids))
    total_assets = conn.execute("SELECT count(*) FROM app_assets").fetchone()[0]
    check("库里的资产数没变", total_assets == 1, "资产 " + str(total_assets) + " 个")

    service.add_problem(ProblemCreate(
        subject="英语", stem_text="He ___ to school yesterday.", answer_text="went",
        knowledge_points=["一般过去时"], image_paths=[str(photo_b)],
    ))

    print("\n[4/5] 列表、过滤与统计")
    proc = run("--problems", "10")
    check("--problems 能列出来", "数学" in proc.stdout and "英语" in proc.stdout,
          proc.stdout.strip().replace("\n", " ")[:100])
    check("列表显示知识点", "异分母分数加减" in proc.stdout)

    math_only = service.list_problems(ProblemQuery(subject="数学"))
    check("按学科过滤", len(math_only) == 2, str(len(math_only)))
    by_kp = service.list_problems(ProblemQuery(knowledge_point="通分"))
    check("按知识点过滤", len(by_kp) == 1)

    stats = service.stats()
    check("统计按学科分组", stats.by_subject.get("数学") == 2 and stats.by_subject.get("英语") == 1,
          str(stats.by_subject))
    check("今天没有到期的(首次在明天)", stats.due_today == 0)
    proc = run("--weak-points")
    check("--weak-points 输出薄弱知识点", "薄弱知识点" in proc.stdout or "通分" in proc.stdout,
          proc.stdout.strip().replace("\n", " ")[:110])

    print("\n[5/5] 双视角:孩子端不许出现家长视角的评价")
    unit_id = record.review_units[1].id
    child = service.render_card(unit_id, perspective="child")
    parent = service.render_card(unit_id, perspective="parent")
    check("孩子端有题干与两个按钮",
          child["stem_text"] == "3/4 + 1/6 = ?"
          and [b["label"] for b in child["buttons"]] == ["忘了", "会了"])
    check("⭐ 孩子端没有答案 / 解析 / 知识点 / 错题次数",
          all(key not in child for key in ("answer_text", "explanation", "knowledge_point", "lapses")))
    check("家长端有全部信息",
          parent["answer_text"] == "11/12"
          and parent["knowledge_point"] in ("通分", "异分母分数加减")
          and "fsrs_state" in parent,
          "知识点=" + str(parent.get("knowledge_point")))
    proc = run("--child-card", str(unit_id))
    check("CLI 渲染孩子卡不带答案", "11/12" not in proc.stdout and "忘了" in proc.stdout,
          proc.stdout.strip().replace("\n", " ")[:110])

    conn.close()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n结果: " + str(passed) + "/" + str(len(RESULTS)) + " 项通过")
    for name, ok, _ in RESULTS:
        if not ok:
            print("  未通过: " + name)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())