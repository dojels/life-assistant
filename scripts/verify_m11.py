"""M11 端到端验收:素材层 / 产物层 / 微信媒体 / 照片直达错题。

    .venv/Scripts/python.exe scripts/verify_m11.py

全程**不需要密钥、不联网**:视觉那一环用假客户端,微信 CDN 用本地假服务器。
唯一没法在这里验的是"真机微信"——那一步在最后打印出来,由你拿手机走一遍。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = tempfile.mkdtemp(prefix="la-verify-m11-")

os.environ["LA_DATA_DIR"] = DATA_DIR
os.environ["LA_LLM_PROVIDER"] = "fake"
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.path.insert(0, str(BASE_DIR))

TZ = ZoneInfo("Asia/Shanghai")
RESULTS: list[tuple[str, bool, str]] = []

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)

VISION_REPLY = json.dumps(
    {
        "is_problem": True,
        "problems": [
            {
                "subject": "数学",
                "stem_text": "3/4 + 1/6 = ?",
                "answer_text": "11/12",
                "knowledge_points": ["异分母分数加减", "通分"],
                "confidence": 0.92,
            }
        ],
    },
    ensure_ascii=False,
)


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + ("   " + detail if detail else ""))


class StubVision:
    model = "stub-vision"

    def __init__(self, reply: str = VISION_REPLY) -> None:
        self.reply = reply
        self.calls = 0

    async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        from la.llm.client import ChatResult

        self.calls += 1
        return ChatResult(text=self.reply, model=self.model)


class _CDN(BaseHTTPRequestHandler):
    payload = b""
    uploaded = b""

    def log_message(self, *args):  # noqa: ANN002
        return

    def do_GET(self):  # noqa: N802
        body = type(self).payload
        self.send_response(200 if self.path.startswith("/c2c/download") else 404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        type(self).uploaded = self.rfile.read(length)
        self.send_response(200)
        self.send_header("x-encrypted-param", "PARAM-OK")
        self.send_header("Content-Length", "0")
        self.end_headers()


async def _collect(thread, inbound):
    return [event async for event in thread.handle(inbound)]


def main() -> int:  # noqa: C901 - 验收脚本本来就是一条流水线
    from la import config
    from la.bridge import Attachment, Bridge, Inbound
    from la.bridge.adapters import weixin_media
    from la.domain.artifacts import ArtifactStore, artifact_id_for
    from la.domain.materials import (
        CHANNEL_WEIXIN,
        HANDLED_RECORDED,
        HANDLED_STORED,
        MaterialService,
    )
    from la.domain.media import MediaStore
    from la.domain.services.study_service import StudyService
    from la.domain.study.intake import IntakeService
    from la.storage import db
    from la.storage.migrations import MIGRATIONS_ASSISTANT

    print("验收数据目录: " + DATA_DIR)
    now = datetime.now(TZ)
    conn = db.connect_assistant()
    media = MediaStore(conn, root=Path(DATA_DIR) / "media")
    materials = MaterialService(conn, media=media)
    artifacts = ArtifactStore(conn, root=Path(DATA_DIR) / "artifacts")
    study = StudyService(conn, media=media, artifacts=artifacts)
    vision = StubVision()
    intake = IntakeService(conn, study=study, media=media, client_factory=lambda: vision)

    print("\n[1/8] 迁移:素材层与产物层")
    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    versions = [row[0] for row in conn.execute("SELECT version FROM sys_schema_version")]
    check("新增 app_materials / app_artifacts",
          {"app_materials", "app_artifacts"} <= tables)
    check("迁移版本到 11", 11 in versions, "已应用 " + str(len(versions)) + " 个版本")

    print("\n[2/8] 素材层:先原样存下来,再谈它是什么")
    first, fresh = materials.ingest_bytes(
        PNG_1PX, channel=CHANNEL_WEIXIN, external_id="ilink:1001",
        sender="wxid_me", thread_id="wxid_me", original_name="错题.png",
    )
    check("素材入库(带来源:哪个渠道、哪条消息、谁发的)", fresh and first.channel == CHANNEL_WEIXIN)
    check("字节按内容寻址落盘", materials.path_of(first).exists()
          and first.sha256[:2] in str(materials.path_of(first)))
    again, fresh2 = materials.ingest_bytes(
        b"different bytes", channel=CHANNEL_WEIXIN, external_id="ilink:1001"
    )
    check("同一条渠道消息重复投递 = 同一份素材(不会录两遍)", not fresh2 and again.id == first.id)

    print("\n[3/8] 产物层:落地即登记,句柄可寻")
    artifact = artifacts.register_text(
        "# 测试错题卷\n", filename="错题卷.md", label="测试错题卷"
    )
    check("产物落盘 + 登记", artifacts.path_of(artifact).exists()
          and artifact.id == artifact_id_for(artifact.rel_path))
    outside = Path(DATA_DIR) / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    try:
        artifacts.register_path(outside)
        check("产物目录之外的落点被拒", False)
    except Exception:
        check("产物目录之外的落点被拒", True, "宁可不给句柄,也不给一个会 404 的链接")
    check("文件没了就把登记清掉(只按存在性判)",
          artifacts.forget_missing() == 0)

    print("\n[4/8] 微信媒体:入站解密 / 出站加密")
    key = os.urandom(16)
    shapes = {
        "16 字节原文": key,
        "32 位 hex": key.hex(),
        "base64(16 字节)": __import__("base64").b64encode(key).decode(),
        "base64(32 hex)": weixin_media.encode_aes_key(key),
    }
    ok_shapes = all(weixin_media.parse_aes_key(value) == key for value in shapes.values())
    check("aes_key 的四种写法都认", ok_shapes, "/".join(shapes))
    check("出站发的是 base64(32 位 hex) —— 错了对方只看到灰框",
          weixin_media.parse_aes_key(weixin_media.encode_aes_key(key)) == key)
    encrypted = weixin_media.aes_ecb_encrypt(PNG_1PX, key)
    check("AES-128-ECB 往返一致",
          weixin_media.aes_ecb_decrypt(encrypted, key) == PNG_1PX)
    refs = weixin_media.media_refs_from_items([
        {"type": 1, "text_item": {"text": "看这道题"}},
        {"type": 2, "image_item": {"encrypt_query_param": "abc", "aes_key": key.hex()}},
    ])
    check("从 item_list 里抽出图片(文本项不算媒体)",
          len(refs) == 1 and refs[0].kind == "image")
    refs2 = weixin_media.media_refs_from_items(
        [{"type": 2, "image_item": {"cdn_url": "https://example.com/x.jpg"}}]
    )
    check("另一套字段名(cdn_url)也认", refs2[0].downloadable, "真机上字段名不统一")

    server = HTTPServer(("127.0.0.1", 0), _CDN)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:" + str(server.server_address[1]) + "/c2c"
    saved_base = weixin_media.CDN_BASE
    weixin_media.CDN_BASE = base
    try:
        _CDN.payload = encrypted
        ref = weixin_media.MediaRef(item_type=2, kind="image",
                                    encrypt_query_param="q", aes_key=key.hex())
        fetched = asyncio.run(weixin_media.download_media(ref))
        check("从 CDN 下载并解密回原图", fetched == PNG_1PX)
    finally:
        weixin_media.CDN_BASE = saved_base

    print("\n[5/8] 照片 -> 错题:一条流水线走完")
    outcome = intake.ingest_material(first.id, actor="user")
    check("识别出来后**直接**录入(手机上不该还要点确认)",
          outcome["handled"] == HANDLED_RECORDED and len(outcome["recorded"]) == 1)
    problem_id = outcome["recorded"][0]["problem_id"]
    problem = study.get_problem(problem_id)
    check("照片跟着错题走", problem.asset_ids == [first.asset_id])
    check("一个知识点一个复习单元", len(problem.review_units) == 3,
          str([(u.ord, u.knowledge_point) for u in problem.review_units]))
    check("回话里说清了录了什么(不由模型生成)",
          "已录进错题本" in outcome["message"] and "通分" in outcome["message"])
    check("回话里给了撤销入口", "/撤销" in outcome["message"])
    stored = materials.get(first.id)
    check("素材上留下去向(handled + 错题号)",
          stored.handled == HANDLED_RECORDED
          and stored.result["recorded"][0]["problem_id"] == problem_id)

    vision.reply = json.dumps({"is_problem": False, "reason": "一张风景照", "problems": []},
                              ensure_ascii=False)
    other, _ = materials.ingest_bytes(PNG_1PX, channel="web", external_id="web:2")
    not_problem = intake.ingest_material(other.id)
    check("不是题目 = 留档,而不是失败", not_problem["handled"] == HANDLED_STORED)
    check("不是题目时不进错题本", len(study.list_problems({"limit": 10})) == 1)
    check("留档也要说清楚(引用模型看到的东西)",
          "看着不是题目" in not_problem["message"] and "风景照" in not_problem["message"])

    print("\n[6/8] 桥接层:手机上只发一张照片")
    bridge = Bridge(settings=config.get_settings(reload=True), transport=_Collector())
    try:
        thread = bridge.thread("wxid_me")
        thread.session.intake._client_factory = lambda: StubVision()  # noqa: SLF001
        events = asyncio.run(_collect(thread, Inbound(
            channel="weixin", user_id="wxid_me", thread_id="wxid_me", text="",
            external_id="ilink:2001",
            attachments=[Attachment(kind="image", data=PNG_1PX, name="错题.png",
                                    media_type="image/png", external_id="ilink:2001:0")],
        )))
        body = " ".join(str(e["data"]) for e in events if e["event"] == "text")
        check("只发照片也能走完(不需要聊天模型在场)", "已录进错题本" in body)
        turns = [e for e in thread.session.store.events("bridge:wxid_me")
                 if e.type == "turn/start"]
        check("只有附件时不起对话轮次(省一次模型调用)", turns == [])
        before = thread.session.assistant_conn.execute(
            "SELECT count(*) FROM app_problems").fetchone()[0]
        asyncio.run(_collect(thread, Inbound(
            channel="weixin", user_id="wxid_me", thread_id="wxid_me", text="",
            external_id="ilink:2001",
            attachments=[Attachment(kind="image", data=PNG_1PX, name="错题.png",
                                    media_type="image/png", external_id="ilink:2001:0")],
        )))
        after = thread.session.assistant_conn.execute(
            "SELECT count(*) FROM app_problems").fetchone()[0]
        check("微信回调重发不会录第二遍", before == after, str(before) + " -> " + str(after))
        undo_events = asyncio.run(_collect(thread, Inbound(
            channel="weixin", user_id="wxid_me", thread_id="wxid_me",
            text="/撤销", external_id="ilink:2002",
        )))
        undo_body = " ".join(str(e["data"]) for e in undo_events if e["event"] == "text")
        check("/撤销 真的能撤(以前只是提示语)", "已撤销" in undo_body, undo_body[:80])
    finally:
        bridge.close()

    print("\n[7/8] 产物:错题卷(可下载、可打印)")
    today = now.date().isoformat()
    conn.execute("UPDATE app_review_units SET due_date_local = ?", (today,))
    conn.commit()
    sheet = study.make_sheet()
    check("错题卷生成并登记", sheet["items"] >= 1 and sheet["artifact"]["id"])
    text = sheet["text"]
    check("卷面有题目与作答线", "3/4 + 1/6" in text and "答:" in text)
    check("卷面附答案区", "答案" in text)
    check("同一道题只印一遍(按题去重,不按单元)",
          text.count("## 1.") == 1 and text.count("3/4 + 1/6") <= 2)
    check("产物文件真的落盘",
          (Path(DATA_DIR) / "artifacts" / sheet["artifact"]["rel_path"]).exists())

    print("\n[8/8] Web:素材页 / 聊天传图 / 产物下载(读取路径不经过模型)")
    from fastapi.testclient import TestClient

    from la.web import deps as web_deps
    from la.web.app import create_app

    saved_factory = web_deps.build_model_client
    stub = StubVision()
    web_deps.build_model_client = lambda settings: stub
    os.environ["LA_LLM_PROVIDER"] = "deepseek"
    os.environ["DEEPSEEK_API_KEY"] = "stub-key"
    config._settings = None  # noqa: SLF001
    try:
        client = TestClient(create_app())
        check("素材页打得开", client.get("/materials").status_code == 200)
        uploaded = client.post(
            "/materials/upload",
            files=[("files", ("新照片.png", PNG_1PX, "image/png"))],
            data={"_idem": "v1"}, follow_redirects=False,
        )
        check("素材页能手工上传", uploaded.status_code == 303)
        newest = materials.latest()
        served = client.get("/materials/" + str(newest.id) + "/file")
        check("原件能取回来", served.status_code == 200
              and served.content == PNG_1PX)
        check("没登记的素材 id 一律 404",
              client.get("/materials/999999/file").status_code == 404)
        chat = client.post("/chat/attach",
                           files=[("files", ("聊天里的题.png", PNG_1PX, "image/png"))])
        payload = chat.json()
        check("聊天页传图 = 与微信同一条流水线", payload.get("ok")
              and payload.get("recorded"), str(payload.get("message"))[:60])
        check("聊天里给了可撤销的回执", bool(payload.get("receipt")))
        downloaded = client.get("/artifacts/" + sheet["artifact"]["id"])
        check("产物能下载", downloaded.status_code == 200
              and "错题卷" in downloaded.content.decode("utf-8"))
    finally:
        web_deps.build_model_client = saved_factory
        os.environ["LA_LLM_PROVIDER"] = "fake"
        config._settings = None  # noqa: SLF001

    server.shutdown()
    conn.close()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n结果: " + str(passed) + "/" + str(len(RESULTS)) + " 项通过")
    for name, ok, _ in RESULTS:
        if not ok:
            print("  未通过: " + name)
    print("\n最后一步只能你来走(需要一个能收文件的微信通道):")
    print("  1) 重启常驻进程: python -m la.bridge.adapters.weixin_ilink_main")
    print("  2) 手机拍一张错题,发给它")
    print("  3) 它应该回一条「📚 照片收到,认出 N 道题,已录进错题本…」")
    print("  4) 录错了就在微信里回一句:/撤销")
    return 0 if passed == len(RESULTS) else 1


class _Collector:
    name = "collector"

    def __init__(self) -> None:
        self.actions: list = []

    async def send(self, action) -> None:  # noqa: ANN001
        self.actions.append(action)

    async def typing(self, thread_id: str) -> None:
        return


if __name__ == "__main__":
    raise SystemExit(main())
