"""M10 端到端验收:拍照录入(识别 → 候选 → 确认)、学生档案、复习周期、每日推送。

    .venv/Scripts/python.exe scripts/verify_m10.py

⭐ 全程**不需要密钥**:视觉那一环用一个假客户端验证"图片 -> 结构化字段 -> 候选区"的
   完整链路;真实模型的一环单独标出来,想看就自己跑一次(命令在最后打印)。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
DATA_DIR = tempfile.mkdtemp(prefix="la-verify-m10-")
PHOTO_DIR = tempfile.mkdtemp(prefix="la-photos-m10-")

os.environ["LA_DATA_DIR"] = DATA_DIR
os.environ["LA_LLM_PROVIDER"] = "fake"
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.path.insert(0, str(BASE_DIR))

TZ = ZoneInfo("Asia/Shanghai")
RESULTS: list[tuple[str, bool, str]] = []
FENCE = chr(96) * 3

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)
PNG2 = PNG[:-12] + b"\x00\x00\x00\x00\x00\x00\x00\x00\x49\x45\x4e\x44\xae\x42\x60\x82"

VISION_REPLY = json.dumps(
    {
        "problems": [
            {
                "subject": "初中数学",
                "grade": "五年级",
                "stem_text": "3/4 + 1/6 = ?",
                "answer_text": "11/12",
                "knowledge_points": ["数学 异分母分数加减", "通分"],
                "confidence": 0.93,
            },
            {
                "subject": "数学",
                "stem_text": "2/3 - 1/4 = ?",
                "knowledge_points": ["异分母分数加减"],
                "confidence": 0.85,
            },
        ]
    },
    ensure_ascii=False,
)


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + ("   " + detail if detail else ""))


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, "-m", "la", *args], cwd=str(BASE_DIR), env=dict(os.environ),
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )


class StubVision:
    """假的视觉客户端:固定回一段 JSON,并记下收到的消息形状。"""

    model = "stub-vision"

    def __init__(self) -> None:
        self.messages: list[list[dict]] = []

    async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        from la.llm.client import ChatResult

        self.messages.append([dict(item) for item in messages])
        return ChatResult(text=VISION_REPLY, model=self.model)


LONG_STEM = "题目" * 800


def main() -> int:  # noqa: C901 - 验收脚本本来就是一条流水线
    from la import config
    from la.domain.media import MediaStore
    from la.domain.schemas.study import ProblemQuery
    from la.domain.services.study_service import StudyService
    from la.domain.study.intake import IntakeService
    from la.storage import db
    from la.storage.migrations import MIGRATIONS_ASSISTANT, MIGRATIONS_MEMORY
    from la.tasks.dispatcher import KIND_STUDY, Dispatcher

    print("验收数据目录: " + DATA_DIR)
    now = datetime.now(TZ)
    tomorrow = now + timedelta(days=1)

    conn = db.connect_assistant()
    media = MediaStore(conn, root=Path(DATA_DIR) / "media")
    service = StudyService(conn, media=media)

    print("\n[1/7] 迁移:学生档案 / 推送设置 / 错题归属")
    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    columns = {row[1] for row in conn.execute("PRAGMA table_info(app_problems)")}
    versions = [row[0] for row in conn.execute("SELECT version FROM sys_schema_version")]
    check("新增 app_students 与 app_study_settings", {"app_students", "app_study_settings"} <= tables)
    check("app_problems 挂了 student_id", "student_id" in columns)
    check("迁移版本到 10", 10 in versions, "已应用:" + str(len(versions)) + " 个版本")
    check("重复迁移是空操作", db.migrate(conn, MIGRATIONS_ASSISTANT) == [])

    print("\n[2/7] 学生:错题的对象是学生,不是「本人」")
    default = service.default_student()
    xiaoming = service.ensure_student("小明", grade="五年级")
    check("默认学生现场兜底可建", default.name != "", default.name)
    check("同名复用同一个学生", service.ensure_student("小明").id == xiaoming.id)
    service.set_default_student(xiaoming.id)
    check("换默认学生落库", service.settings().student_id == xiaoming.id,
          str(service.settings().student_name))

    print("\n[3/7] 拍照录入:照片 -> 候选区 -> 识别 -> 确认")
    stub = StubVision()
    intake = IntakeService(
        conn, study=service, media=media, client_factory=lambda: stub,
        now_provider=lambda: now,
    )
    session_id = intake.start_session(student_id=xiaoming.id)
    candidate = intake.add_image(session_id, PNG, original_name="错题.png")
    check("照片按内容寻址存下来", media.exists(candidate.asset_id))
    check("候选区是 pending,还没落成错题", candidate.decision == "pending"
          and service.list_problems(ProblemQuery()) == [])
    report = intake.recognize_session(session_id)
    check("视觉识别链路走通", report["recognized"] == 1 and report["problems"] == 2,
          "识别 " + str(report["problems"]) + " 道题")
    check("图片是以 user 消息的 image_url 块发出去的",
          stub.messages[0][0]["content"][1]["type"] == "image_url")
    candidates = intake.candidates(session_id)
    check("一张照片两道题 -> 两条候选(拆题不丢题)", len(candidates) == 2)
    check("学科归到闭集里的「数学」", candidates[0].payload["subject"] == "数学",
          str(candidates[0].payload["subject"]))
    check("知识点被规范化(去掉学科前缀)", candidates[0].payload["knowledge_points"]
          == ["异分母分数加减", "通分"], str(candidates[0].payload["knowledge_points"]))
    outcome = intake.accept(candidates[0].id, actor="user")
    problem = service.get_problem(outcome["problem_id"])
    check("确认后才是错题,且带上照片", problem.status == "pending"
          and problem.asset_ids == [candidate.asset_id])
    check("⭐ 一个知识点一个复习单元(1 原题 + 2 知识点)",
          len(problem.review_units) == 3,
          str([(u.ord, u.knowledge_point) for u in problem.review_units]))
    check("⭐ 首次复习排在明天,不是立刻", all(
        u.due_date_local > now.date().isoformat() for u in problem.review_units
    ), problem.review_units[0].due_date_local)
    check("归属跟着录入会话走(小明)", problem.student_name == "小明",
          str(problem.student_name))
    check("候选区记录已入库的错题号", intake.get_candidate(candidates[0].id).problem_id
          == problem.id)
    intake.reject(candidates[1].id, note="重复题")
    check("丢弃的候选不再进错题本", len(service.list_problems(ProblemQuery())) == 1)
    check("同一张图重复上传只存一份", media.get_by_sha256(
        media.get(candidate.asset_id).sha256).id == candidate.asset_id)

    print("\n[4/7] 识别失败 / 离线时也必须能用")
    broken = IntakeService(conn, study=service, media=media, client_factory=None)
    offline_session = broken.start_session()
    offline_candidate = broken.add_image(offline_session, PNG2)
    offline_report = broken.recognize_session(offline_session)
    check("离线模式如实说「没有模型」", offline_report["offline"] is True)
    manual = broken.accept(offline_candidate.id,
                           overrides={"subject": "数学", "stem_text": "手抄的题干"})
    check("离线也能手工录进去", service.get_problem(manual["problem_id"]).stem_text == "手抄的题干")

    print("\n[5/7] 复习周期与推送设置(界面可设,模型也可以设)")
    proc = run("--set-push-time", "晚上7点半", "--push")
    check("CLI 认「晚上7点半」", "19:30" in proc.stdout,
          proc.stdout.strip().replace("\n", " ")[:100])
    settings = service.settings()
    check("推送时间落库", settings.push_time_local == "19:30", settings.push_time_local)
    proc = run("--study-settings")
    check("设置页能打印复习周期依据", "固定间隔" in proc.stdout)
    early = now.replace(hour=7, minute=0)
    check("没到点不推送", service.push_digest(now=early) is None)
    digest = service.push_digest(now=tomorrow.replace(hour=20, minute=0))
    check("到点给出复习清单正文", bool(digest) and "错题复习" in (digest or ""),
          (digest or "").splitlines()[0][:60])
    service.update_settings({"daily_limit": 1})
    limited = service.push_digest(now=tomorrow.replace(hour=20, minute=0)) or ""
    check("每日上限真的限制条数", "(1 道" in limited, limited.splitlines()[0][:60])
    service.update_settings({"daily_limit": 10})

    print("\n[6/7] 确定性调度器:一天只推一次,重启也不会重发")
    memory = db.connect_memory()
    db.migrate(memory, MIGRATIONS_MEMORY)
    dispatcher = Dispatcher(
        memory, study_digest=lambda moment: service.push_digest(now=moment)
    )
    first = dispatcher.deliver_study_push(tomorrow.replace(hour=20, minute=0))
    second = dispatcher.deliver_study_push(tomorrow.replace(hour=21, minute=0))
    check("第一次推送成功", first is not None and first.kind == KIND_STUDY)
    check("同一天第二次被抢占挡住", second is None)
    check("推送内容留在库里可回看",
          dispatcher.study_push_today(tomorrow.replace(hour=22, minute=0)) is not None)
    check("第二天可以再推一次",
          dispatcher.deliver_study_push(tomorrow.replace(hour=20) + timedelta(days=1))
          is not None)
    memory.close()

    print("\n[7/7] 界面:上传 -> 确认识别 -> 设置(读取路径不经过模型)")
    from fastapi.testclient import TestClient

    from la.web.app import create_app

    client = TestClient(create_app())
    check("错题页打得开", client.get("/study").status_code == 200)
    check("详情页打得开",
          client.get("/study/problem/" + str(problem.id)).status_code == 200,
          "错题 #" + str(problem.id))
    asset_response = client.get("/assets/" + str(candidate.asset_id))
    check("图片能被浏览器取到", asset_response.status_code == 200
          and asset_response.headers["content-type"].startswith("image/"))
    check("没登记过的资产 id 一律 404", client.get("/assets/999999").status_code == 404)
    upload = client.post(
        "/study/upload",
        files=[("files", ("新照片.png", PNG, "image/png"))],
        data={"_idem": "verify-upload"}, follow_redirects=False,
    )
    check("上传后跳到确认页", upload.status_code == 303
          and "/study/intake/" in upload.headers.get("location", ""),
          str(upload.headers.get("location"))[:60])
    new_session = int(upload.headers["location"].split("/study/intake/")[1].split("?")[0])
    check("确认页把图片与表单都渲染出来",
          client.get("/study/intake/" + str(new_session)).status_code == 200)
    setup = client.post(
        "/study/settings",
        data={"push_time": "06:45", "enabled": "1", "daily_limit": "4",
              "student_name": "小红", "_idem": "verify-settings"},
        follow_redirects=False,
    )
    check("界面改推送时间生效", setup.status_code == 303
          and service.settings().push_time_local == "06:45",
          service.settings().push_time_local)
    check("界面改学生生效(新建并设为默认)",
          service.settings().student_name == "小红",
          str(service.settings().student_name))
    archived = client.post("/study/problem/" + str(problem.id) + "/status",
                           data={"action": "archive"}, follow_redirects=False)
    check("归档后离开复习清单", archived.status_code == 303
          and service.due_items(today=(tomorrow + timedelta(days=3)).date().isoformat()) != []
          and all(item["problem_id"] != problem.id
                  for item in service.due_items(
                      today=(tomorrow + timedelta(days=3)).date().isoformat())),
          "归档 = 撤出复习,记录保留")
    check("归档的题仍然可查(带 include_archived)",
          len(service.list_problems(ProblemQuery(include_archived=True))) >= 2)

    conn.close()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n结果: " + str(passed) + "/" + str(len(RESULTS)) + " 项通过")
    for name, ok, _ in RESULTS:
        if not ok:
            print("  未通过: " + name)
    print("\n想看真实视觉模型的一轮(会联网、会花钱,几秒钟):")
    print("  .venv/Scripts/python.exe -m la --intake-image 你的错题照片.png")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
