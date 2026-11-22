"""M11 验收测试:素材层、产物层、微信媒体(下载解密/上传加密)、图片直达错题。

运行:
    .venv/Scripts/python.exe -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from la import config
from la.bridge import Inbound
from la.domain.receipts import DOMAIN_ARTIFACT
from la.domain.artifacts import (
    ArtifactError,
    ArtifactStore,
    artifact_id_for,
    safe_filename,
)
from la.domain.materials import (
    CHANNEL_WEIXIN,
    HANDLED_FAILED,
    HANDLED_RECORDED,
    HANDLED_STORED,
    MaterialService,
    kind_of,
)
from la.domain.media import MediaStore
from la.domain.services.study_service import StudyError, StudyService
from la.domain.study.intake import IntakeService, render_ingest_result
from la.llm.client import ChatResult
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT, MIGRATIONS_MEMORY

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 8, 20, 0, tzinfo=TZ)

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)

VISION_REPLY = json.dumps(
    {
        "is_problem": True,
        "reason": "",
        "problems": [
            {
                "subject": "数学",
                "stem_text": "3/4 + 1/6 = ?",
                "answer_text": "11/12",
                "knowledge_points": ["异分母分数加减", "通分"],
                "confidence": 0.9,
            }
        ],
    },
    ensure_ascii=False,
)
NOT_A_PROBLEM_REPLY = json.dumps(
    {"is_problem": False, "reason": "这是一张风景照", "problems": []}, ensure_ascii=False
)


class StubVision:
    model = "stub-vision"

    def __init__(self, reply: str = VISION_REPLY) -> None:
        self.reply = reply
        self.calls = 0

    async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        return ChatResult(text=self.reply, model=self.model)


# ==========================================================================
# 假 CDN:入站给密文,出站收密文并要求 aes_key 是 base64(hex)
# ==========================================================================
class _CDNHandler(BaseHTTPRequestHandler):
    payload = b""
    uploaded = b""
    last_headers: dict[str, str] = {}

    def log_message(self, *args):  # noqa: ANN002 - 静音
        return

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/c2c/download"):
            body = type(self).payload
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        type(self).uploaded = body
        type(self).last_headers = dict(self.headers)
        self.send_response(200)
        self.send_header("x-encrypted-param", "PARAM123")
        self.send_header("Content-Length", "0")
        self.end_headers()


class FakeCDN:
    """本地假 CDN。起在随机端口上,测试结束就关。"""

    def __init__(self) -> None:
        self.server = HTTPServer(("127.0.0.1", 0), _CDNHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return "http://127.0.0.1:" + str(self.port) + "/c2c"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


# ==========================================================================
class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.conn = db.connect(self.tmp / "assistant.db")
        db.migrate(self.conn, MIGRATIONS_ASSISTANT)
        self.media = MediaStore(self.conn, root=self.tmp / "media")
        self.materials = MaterialService(self.conn, media=self.media, now_provider=lambda: NOW)
        self.artifacts = ArtifactStore(
            self.conn, root=self.tmp / "artifacts", now_provider=lambda: NOW
        )
        self.study = StudyService(self.conn, media=self.media, now_provider=lambda: NOW)

    def tearDown(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
        self._tmp.cleanup()


# ==========================================================================
# 微信媒体:加解密
# ==========================================================================
class MediaCryptTestCase(unittest.TestCase):
    def test_aes_key_four_shapes(self):
        from la.bridge.adapters.weixin_media import encode_aes_key, parse_aes_key
        import base64

        key = bytes(range(16))
        self.assertEqual(parse_aes_key(key), key)
        self.assertEqual(parse_aes_key(key.hex()), key)
        self.assertEqual(parse_aes_key(base64.b64encode(key).decode()), key)
        self.assertEqual(parse_aes_key(encode_aes_key(key)), key)
        # 官方插件发出去的那一种必须是 base64(32 位 hex) —— 错了对方看到灰框
        self.assertEqual(
            encode_aes_key(key), base64.b64encode(key.hex().encode()).decode()
        )

    def test_round_trip(self):
        from la.bridge.adapters.weixin_media import aes_ecb_decrypt, aes_ecb_encrypt

        key = os.urandom(16)
        for size in (1, 15, 16, 17, 1024, 65536):
            data = os.urandom(size)
            self.assertEqual(aes_ecb_decrypt(aes_ecb_encrypt(data, key), key), data)

    def test_bad_key_is_reported(self):
        from la.bridge.adapters.weixin_media import MediaError, parse_aes_key

        with self.assertRaises(MediaError):
            parse_aes_key("这不是密钥")

    def test_missing_crypto_library_says_what_to_do(self):
        """没装 pycryptodome 时,报错必须是"照着做就行"的一句话,而不是 ImportError。"""
        import sys

        from la.bridge.adapters import weixin_media

        # ⚠️ 要把 Crypto **整棵子树**都摘掉,不能只塞 sys.modules["Crypto"] = None:
        #    只要之前有任何一个用例 import 过 Crypto.Cipher,它就留在 sys.modules 里,
        #    "from Crypto.Cipher import AES" 照样成功 —— 这条用例会**静默变成空断言**
        #    (它一开始就踩了这个坑:是否通过取决于别的用例有没有先用到 AES)。
        saved = {
            key: value
            for key, value in sys.modules.items()
            if key == "Crypto" or key.startswith("Crypto.")
        }
        for key in list(saved):
            sys.modules.pop(key, None)
        sys.modules["Crypto"] = None  # type: ignore[assignment]
        try:
            with self.assertRaises(weixin_media.MediaError) as ctx:
                weixin_media._aes()
            self.assertIn("pip install", str(ctx.exception))
        finally:
            sys.modules.pop("Crypto", None)
            sys.modules.update(saved)


class MediaRefTestCase(unittest.TestCase):
    def test_extracts_image_item(self):
        from la.bridge.adapters.weixin_media import media_refs_from_items

        items = [
            {"type": 1, "text_item": {"text": "看这道题"}},
            {"type": 2, "image_item": {"encrypt_query_param": "abc", "aes_key": "00" * 16,
                                       "md5": "d41d8cd98f00b204e9800998ecf8427e"}},
        ]
        refs = media_refs_from_items(items)
        self.assertEqual(len(refs), 1, "文本项不该被当成媒体")
        self.assertEqual(refs[0].kind, "image")
        self.assertIn("/c2c/download?encrypted_query_param=abc", refs[0].urls()[0])

    def test_extracts_cdn_url_shape_too(self):
        """协议里有两种写法,cnd_url 那一种也必须认(不然真机上会静默收不到)。"""
        from la.bridge.adapters.weixin_media import media_refs_from_items

        refs = media_refs_from_items(
            [{"type": 2, "image_item": {"cdn_url": "https://example.com/a.jpg", "md5": "x"}}]
        )
        self.assertEqual(refs[0].urls(), ["https://example.com/a.jpg"])

    def test_file_item_keeps_name(self):
        from la.bridge.adapters.weixin_media import media_refs_from_items

        refs = media_refs_from_items(
            [{"type": 4, "file_item": {"encrypt_query_param": "p", "file_name": "卷子.pdf"}}]
        )
        self.assertEqual(refs[0].kind, "file")
        self.assertEqual(refs[0].file_name, "卷子.pdf")

    def test_voice_item_carries_transcript(self):
        """⭐ 语音的转写文本在 voice_item.text 上。

        有它 = 内容已经以正文形式进了这一轮;下载原声只会在素材区多一条读不了的音频,
        并在回话里多一句"有个附件"(2026-10-17 真机,D139)。
        """
        from la.bridge.adapters.weixin_media import media_refs_from_items

        refs = media_refs_from_items(
            [{"type": 3, "voice_item": {
                "text": "今天我一共花了多少钱",
                "media": {"encrypt_query_param": "p", "aes_key": "0" * 32},
            }}]
        )
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].kind, "voice")
        self.assertTrue(refs[0].transcribed)
        self.assertEqual(refs[0].transcript, "今天我一共花了多少钱")
        self.assertTrue(refs[0].downloadable, "有没有转写文本,与能不能下载是两件事")
        self.assertTrue(refs[0].as_dict()["transcribed"])

    def test_voice_item_without_transcript_is_not_transcribed(self):
        from la.bridge.adapters.weixin_media import media_refs_from_items

        refs = media_refs_from_items(
            [{"type": 3, "voice_item": {"media": {"encrypt_query_param": "p"}}}]
        )
        self.assertEqual(refs[0].kind, "voice")
        self.assertFalse(refs[0].transcribed)

    def test_no_url_means_not_downloadable(self):
        from la.bridge.adapters.weixin_media import media_refs_from_items

        refs = media_refs_from_items([{"type": 2, "image_item": {"media_id": "m1"}}])
        self.assertFalse(refs[0].downloadable)


class VoiceMessageTestCase(unittest.TestCase):
    """语音消息:正文用转写文字,原声不当附件(D139)。"""

    def _raw(self, items):
        return {
            "from_user_id": "wxid_me",
            "message_id": "m-voice-1",
            "context_token": "ctx",
            "item_list": items,
        }

    def test_voice_transcript_becomes_the_message_text(self):
        from la.bridge.adapters.weixin_protocol import parse_incoming

        message = parse_incoming(self._raw([
            {"type": 3, "voice_item": {"text": "今天我一共花了多少钱",
                                       "media": {"encrypt_query_param": "p"}}},
        ]))
        self.assertIsNotNone(message)
        self.assertEqual(message.text, "今天我一共花了多少钱")
        self.assertEqual(message.voice_text, "今天我一共花了多少钱")
        self.assertEqual(message.voice_transcribed, 1)
        self.assertEqual(len(message.media), 1)
        self.assertTrue(message.media[0].transcribed)

    def test_image_plus_voice_keeps_both(self):
        """⚠️ 回归:以前只读 items[0],「一张图 + 一段语音」里的语音转写**整段丢掉**。"""
        from la.bridge.adapters.weixin_protocol import parse_incoming

        message = parse_incoming(self._raw([
            {"type": 2, "image_item": {"media": {"encrypt_query_param": "img"}}},
            {"type": 3, "voice_item": {"text": "这道题是英语的"}},
        ]))
        self.assertEqual(message.text, "这道题是英语的")
        self.assertEqual(message.voice_transcribed, 1)
        self.assertEqual([ref.kind for ref in message.media], ["image", "voice"])

    def test_unknown_item_type_is_not_an_attachment(self):
        """认不出类型的内容(表情/位置/名片)不该变成"有个附件"(D139 同一条纪律)。"""
        from la.bridge.adapters.weixin_protocol import parse_incoming

        message = parse_incoming(self._raw([
            {"type": 1, "text_item": {"text": "在吗"}},
            {"type": 7, "location_item": {"poi": "某处"}},
        ]))
        self.assertEqual(message.text, "在吗")
        self.assertEqual(message.media, [])
        self.assertEqual(message.unknown_item_types, [7])

    def test_voice_without_transcript_has_no_text(self):
        from la.bridge.adapters.weixin_protocol import parse_incoming

        message = parse_incoming(self._raw([
            {"type": 3, "voice_item": {"media": {"encrypt_query_param": "p"}}},
        ]))
        self.assertEqual(message.text, "")
        self.assertEqual(message.voice_transcribed, 0)
        self.assertFalse(message.media[0].transcribed)


class MediaDownloadTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.cdn = FakeCDN()
        self._saved_base = None

    def tearDown(self) -> None:
        from la.bridge.adapters import weixin_media

        if self._saved_base is not None:
            weixin_media.CDN_BASE = self._saved_base
        self.cdn.close()

    def _patch_base(self):
        from la.bridge.adapters import weixin_media

        self._saved_base = weixin_media.CDN_BASE
        weixin_media.CDN_BASE = self.cdn.base

    def test_downloads_and_decrypts(self):
        from la.bridge.adapters.weixin_media import (
            MediaRef,
            aes_ecb_encrypt,
            download_media,
        )

        key = os.urandom(16)
        _CDNHandler.payload = aes_ecb_encrypt(PNG_1PX, key)
        self._patch_base()
        ref = MediaRef(item_type=2, kind="image", encrypt_query_param="q", aes_key=key.hex())
        data = asyncio.run(download_media(ref))
        self.assertEqual(data, PNG_1PX, "下载回来必须是解密后的原图")

    def test_plain_payload_without_key_still_works(self):
        from la.bridge.adapters.weixin_media import MediaRef, download_media

        _CDNHandler.payload = PNG_1PX
        self._patch_base()
        ref = MediaRef(item_type=2, kind="image", encrypt_query_param="q")
        self.assertEqual(asyncio.run(download_media(ref)), PNG_1PX)

    def test_missing_url_is_explicit(self):
        from la.bridge.adapters.weixin_media import MediaError, MediaRef, download_media

        with self.assertRaises(MediaError):
            asyncio.run(download_media(MediaRef(item_type=2, kind="image")))


class MediaUploadTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.cdn = FakeCDN()
        _CDNHandler.uploaded = b""

    def tearDown(self) -> None:
        self.cdn.close()

    def test_upload_encrypts_and_returns_sendable_media(self):
        import httpx

        from la.bridge.adapters.weixin_media import (
            ITEM_IMAGE,
            build_media_item,
            encode_aes_key,
            upload_media,
        )

        captured: dict = {}

        async def fake_get_upload_url(body):   # 与 ILinkClient.get_upload_url 同签名:只收请求体
            captured.update(body)
            return {"ret": 0, "upload_full_url": self.cdn.base + "/upload?filekey=x"}

        async def run():
            async with httpx.AsyncClient() as http:
                return await upload_media(
                    fake_get_upload_url, http,
                    data=PNG_1PX, item_type=ITEM_IMAGE, to_user_id="wxid_me",
                )

        media = asyncio.run(run())
        self.assertEqual(media["encrypt_query_param"], "PARAM123")
        self.assertEqual(captured["rawsize"], len(PNG_1PX))
        self.assertEqual(captured["aeskey"], captured["aeskey"].lower())
        self.assertEqual(len(captured["aeskey"]), 32, "上传时给的是 16 字节的 hex")
        # CDN 收到的必须是密文,而不是原图
        self.assertNotEqual(_CDNHandler.uploaded, PNG_1PX)
        self.assertEqual(len(_CDNHandler.uploaded) % 16, 0)
        # 发出去的那条媒体项里,aes_key 必须是 base64(hex)
        item = build_media_item(ITEM_IMAGE, {**media, "file_name": "题.png"})
        self.assertEqual(item["type"], 2)
        self.assertIn("image_item", item)
        # ⚠️ 媒体字段在 media 里,不在 image_item 顶层(D87)
        self.assertEqual(
            item["image_item"]["media"]["aes_key"],
            encode_aes_key(bytes.fromhex(captured["aeskey"])),
        )
        self.assertEqual(item["image_item"]["media"]["encrypt_type"], 1)
        # 图片给的是**密文**大小
        self.assertEqual(item["image_item"]["mid_size"], len(_CDNHandler.uploaded))

    def test_file_item_shape_matches_the_protocol(self):
        """形状要**逐字段**对得上 —— 这次真机翻车就是形状错了(扁平 vs 嵌 media)。

        依据是三份独立来源:真机入站报文、官方插件源码分析、weixin-ilink SDK 的
        file_item() 构造。任何一个字段名/类型写错,表现都是"服务端拒掉 / 对方看到灰框",
        而不是报错 —— 所以这里用**整体相等**把它钉死。
        """
        from la.bridge.adapters.weixin_media import (
            ITEM_FILE,
            ITEM_IMAGE,
            build_media_item,
        )

        self.assertEqual(
            build_media_item(ITEM_FILE, {
                "encrypt_query_param": "PQ", "aes_key": "QUJD",
                "size": "32", "raw_size": "17", "file_name": "周报.docx",
            }),
            {
                "type": 4,
                "file_item": {
                    "media": {"encrypt_query_param": "PQ", "aes_key": "QUJD",
                              "encrypt_type": 1},
                    "file_name": "周报.docx",
                    "len": "17",            # 明文大小,而且是**字符串**
                },
            },
        )
        self.assertEqual(
            build_media_item(ITEM_IMAGE, {
                "encrypt_query_param": "PQ", "aes_key": "QUJD", "size": "32", "raw_size": "17",
            }),
            {
                "type": 2,
                "image_item": {
                    "media": {"encrypt_query_param": "PQ", "aes_key": "QUJD",
                              "encrypt_type": 1},
                    "mid_size": 32,          # 密文大小,int
                },
            },
        )


class OutboundFileTestCase(unittest.TestCase):
    """出站文件:\`ILinkTransport.send_file\` 必须和**真实客户端的签名**对上。

    ⚠️ 这个用例是补的,它值一段说明。原来只有 \`upload_media\` 的假回调测试,
    而那个假回调是照着 \`(path, body)\` 两个参数写的 —— 真实客户端
    \`ILinkClient.get_upload_url\` 只收一个 body。两端各自"看着都对",
    合起来 100% 抛 TypeError,测试却全绿。
    所以这里**故意继承真的 ILinkClient**,只把出网的那两步换掉:
    签名错一点,这个用例就会红。
    """

    def setUp(self) -> None:
        _CDNHandler.uploaded = b""
        self.cdn = FakeCDN()
        self.addCleanup(self.cdn.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "本周小结.docx"
        self.payload = b"PK\x03\x04" + b"docx-body" * 64
        self.path.write_bytes(self.payload)

    def _client(self, upload_url: str, sent: list, bodies: list):
        from la.bridge.adapters.weixin_protocol import ILinkClient

        class RecordingClient(ILinkClient):
            """**签名保持真实**:get_upload_url(body) 一个参数,send_items 三个位置参数。"""

            def __init__(self) -> None:
                super().__init__(token="t")

            async def get_upload_url(self, body):
                bodies.append(body)
                return {"ret": 0, "upload_full_url": upload_url}

            async def send_items(self, to_user, items, context_token):
                sent.append({"to_user": to_user, "items": items, "token": context_token})
                # ⚠️ 刻意**不带 remaining**:带了的话服务端数字会覆盖本地计数,
                #    额度用例就没法断言"到底发了几条"了。
                return {"ret": 0}

        client = RecordingClient()
        self.addCleanup(lambda: asyncio.run(client.aclose()))
        return client

    def test_docx_reaches_sendmessage_as_file_item(self):
        from la.bridge.adapters.weixin_ilink import ILinkTransport
        from la.bridge.types import OutboundFile

        sent: list = []
        bodies: list = []
        client = self._client(self.cdn.base + "/upload?filekey=x", sent, bodies)
        transport = ILinkTransport(client=client)
        conv = transport.conversation("wxid_me")
        conv.context_token = "CTX"

        ok = asyncio.run(
            transport.send_file(
                OutboundFile(path=str(self.path), kind="file", name="本周小结.docx"), conv
            )
        )

        self.assertTrue(ok, transport.last_error)
        self.assertEqual(transport.last_error, "")
        # 要上传地址这一步:文件档 media_type=3,报的是**明文**大小
        self.assertEqual(bodies[0]["media_type"], 3)
        self.assertEqual(bodies[0]["rawsize"], len(self.payload))
        # CDN 收到的必须是密文,且长度是 16 的整数倍(PKCS7)
        self.assertNotEqual(_CDNHandler.uploaded, self.payload)
        self.assertEqual(len(_CDNHandler.uploaded) % 16, 0)
        # 到了 sendmessage 的那一项:type=4 = 文件
        self.assertEqual(len(sent), 1)
        item = sent[0]["items"][0]
        self.assertEqual(item["type"], 4)
        self.assertEqual(item["file_item"]["file_name"], "本周小结.docx")
        # ⚠️ 媒体字段必须嵌在 media 里 —— 扁平写法会被服务端拒掉(D87 的真机翻车)
        self.assertIn("media", item["file_item"])
        self.assertTrue(item["file_item"]["media"]["encrypt_query_param"])
        self.assertEqual(item["file_item"]["media"]["encrypt_type"], 1)
        # 文件的大小字段叫 len、给**明文**、而且是字符串
        self.assertEqual(item["file_item"]["len"], str(len(self.payload)))
        self.assertEqual(sent[0]["to_user"], "wxid_me")
        self.assertEqual(sent[0]["token"], "CTX")
        self.assertEqual(conv.remaining_quota, 9, "发一条媒体要扣一格额度")

    def test_quota_keeps_a_slot_for_the_text(self):
        """⚠️ 文件先发,但不能把额度吃光 —— 否则用户收到一堆文件却没有任何说明。"""
        from la.bridge.adapters.weixin_ilink import ILinkTransport
        from la.bridge.types import OutboundFile, SendAction

        sent: list = []
        bodies: list = []
        client = self._client(self.cdn.base + "/upload?filekey=x", sent, bodies)
        transport = ILinkTransport(client=client)
        conv = transport.conversation("wxid_me")
        conv.context_token = "CTX"
        conv.remaining_quota = 2          # 只剩两格

        asyncio.run(transport.send(SendAction(
            text="都在这儿了。",
            files=[OutboundFile(path=str(self.path), kind="file", name="周报.docx")
                   for _ in range(3)],
        )))

        # 2 格额度 = 1 个文件 + 1 条正文(正文那格必须留住)
        self.assertEqual(len(sent), 2, "文件和正文加起来只该发两条")
        self.assertEqual(sent[0]["items"][0]["type"], 4, "第一条是文件")
        self.assertEqual(sent[1]["items"][0]["type"], 1, "第二条是正文")
        self.assertEqual(conv.remaining_quota, 0)
        self.assertIn("少发了", transport.last_error, "少发了要如实说,不能悄悄丢")

    def test_missing_file_is_reported_not_raised(self):
        from la.bridge.adapters.weixin_ilink import ILinkTransport
        from la.bridge.types import OutboundFile

        client = self._client(self.cdn.base + "/upload?filekey=x", [], [])
        transport = ILinkTransport(client=client)
        conv = transport.conversation("wxid_me")
        conv.context_token = "CTX"

        ok = asyncio.run(
            transport.send_file(OutboundFile(path=str(self.path) + ".nope"), conv)
        )
        self.assertFalse(ok)
        self.assertIn("不存在", transport.last_error)

    def test_no_context_token_means_no_file(self):
        """没有 context_token 时**不能**白传一遍 CDN —— 传了也发不出去。"""
        from la.bridge.adapters.weixin_ilink import ILinkTransport
        from la.bridge.types import OutboundFile

        bodies: list = []
        client = self._client(self.cdn.base + "/upload?filekey=x", [], bodies)
        transport = ILinkTransport(client=client)
        conv = transport.conversation("wxid_me")

        ok = asyncio.run(
            transport.send_file(OutboundFile(path=str(self.path), kind="file"), conv)
        )
        self.assertFalse(ok)
        self.assertEqual(bodies, [], "连上传地址都不该去要")
        self.assertIn("context_token", transport.last_error)


# ==========================================================================
# 素材层
# ==========================================================================
class MaterialTestCase(Fixture):
    def test_ingest_stores_bytes_and_provenance(self):
        material, fresh = self.materials.ingest_bytes(
            PNG_1PX, channel=CHANNEL_WEIXIN, external_id="wx:1",
            sender="wxid_me", thread_id="wxid_me", original_name="错题.png",
        )
        self.assertTrue(fresh)
        self.assertEqual(material.kind, "image")
        self.assertEqual(material.media_type, "image/png")
        self.assertEqual(material.channel, CHANNEL_WEIXIN)
        self.assertTrue(self.materials.path_of(material).exists())
        self.assertEqual(self.materials.read_bytes(material), PNG_1PX)

    def test_same_channel_message_is_idempotent(self):
        first, _ = self.materials.ingest_bytes(PNG_1PX, channel=CHANNEL_WEIXIN, external_id="wx:1")
        second, fresh = self.materials.ingest_bytes(
            b"other bytes", channel=CHANNEL_WEIXIN, external_id="wx:1"
        )
        self.assertFalse(fresh, "同一条渠道消息不能入两次素材")
        self.assertEqual(first.id, second.id)
        self.assertEqual(self.materials.stats()["total"], 1)

    def test_local_uploads_without_external_id_never_collide(self):
        for _ in range(3):
            self.materials.ingest_bytes(PNG_1PX, channel="web")
        self.assertEqual(self.materials.stats()["total"], 3)

    def test_same_bytes_are_stored_once(self):
        one, _ = self.materials.ingest_bytes(PNG_1PX, channel="web")
        two, _ = self.materials.ingest_bytes(PNG_1PX, channel="cli")
        self.assertEqual(one.sha256, two.sha256)
        assets = self.conn.execute("SELECT count(*) FROM app_assets").fetchone()[0]
        self.assertEqual(assets, 1, "字节仓是内容寻址的:同一张照片只存一份")

    def test_mark_records_outcome(self):
        material, _ = self.materials.ingest_bytes(PNG_1PX, channel="web")
        marked = self.materials.mark(material.id, handled=HANDLED_RECORDED,
                                     result={"problem_id": 7})
        self.assertEqual(marked.handled, HANDLED_RECORDED)
        self.assertEqual(marked.result["problem_id"], 7)

    def test_unknown_handled_is_refused(self):
        material, _ = self.materials.ingest_bytes(PNG_1PX, channel="web")
        with self.assertRaises(Exception):
            self.materials.mark(material.id, handled="瞎写的")

    def test_kind_detection(self):
        self.assertEqual(kind_of("image/jpeg"), "image")
        self.assertEqual(kind_of("application/pdf"), "file")
        self.assertEqual(kind_of(""), "file")

    def test_empty_bytes_refused(self):
        with self.assertRaises(Exception):
            self.materials.ingest_bytes(b"", channel="web")


# ==========================================================================
# 产物层
# ==========================================================================
class ArtifactTestCase(Fixture):
    def test_register_writes_file_and_row(self):
        artifact = self.artifacts.register_text(
            "错题卷\n1. 3/4 + 1/6 = ?", filename="今日错题卷.md", label="今日错题卷"
        )
        self.assertTrue(self.artifacts.path_of(artifact).exists())
        self.assertEqual(artifact.kind, "text")
        self.assertIn("text/markdown", artifact.media_type)
        self.assertEqual(self.artifacts.read_bytes(artifact).decode("utf-8")[:3], "错题卷")

    def test_handle_algorithm_is_stable(self):
        artifact = self.artifacts.register_text("x", filename="a.txt")
        self.assertEqual(artifact.id, artifact_id_for(artifact.rel_path))
        self.assertTrue(self.artifacts.exists(artifact.id))

    def test_same_path_re_export_keeps_one_row_and_merges_meta(self):
        first = self.artifacts.register_path(
            self._write("same.txt"), rel_path="2026/same.txt", meta={"version": 1},
        )
        second = self.artifacts.register_path(
            self._write("same.txt"), rel_path="2026/same.txt", meta={"extra": 2},
        )
        self.assertEqual(first.id, second.id)
        rows = self.conn.execute("SELECT count(*) FROM app_artifacts").fetchone()[0]
        self.assertEqual(rows, 1, "同一路径只登记一行")
        meta = json.loads(
            self.conn.execute("SELECT meta FROM app_artifacts").fetchone()[0]
        )
        self.assertEqual(meta, {"version": 1, "extra": 2}, "meta 要合并,不能把上次的抹掉")

    def test_outside_root_is_refused(self):
        outside = self.tmp / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        with self.assertRaises(ArtifactError):
            self.artifacts.register_path(outside)

    def test_forget_missing_only_looks_at_existence(self):
        artifact = self.artifacts.register_text("x", filename="gone.txt")
        self.assertEqual(self.artifacts.forget_missing(), 0)
        self.artifacts.path_of(artifact).unlink()
        self.assertEqual(self.artifacts.forget_missing(), 1)
        self.assertEqual(self.artifacts.list(), [])

    def test_safe_filename_strips_paths(self):
        self.assertNotIn("/", safe_filename("../../etc/passwd"))
        self.assertNotIn("\\", safe_filename("..\\..\\windows\\x.txt"))
        self.assertTrue(safe_filename("今日错题卷.md").endswith(".md"))

    def _write(self, name: str) -> Path:
        target = self.artifacts.root / "2026" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
        return target


# ==========================================================================
# 一张照片 -> 错题(手机那条主路径)
# ==========================================================================
class IngestPipelineTestCase(Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.vision = StubVision()
        self.intake = IntakeService(
            self.conn, study=self.study, media=self.media,
            client_factory=lambda: self.vision, now_provider=lambda: NOW,
        )

    def _material(self, **kwargs):
        material, _ = self.materials.ingest_bytes(
            PNG_1PX, channel=CHANNEL_WEIXIN, external_id=kwargs.pop("external_id", "wx:9"),
            sender="wxid_me", thread_id="wxid_me", **kwargs,
        )
        return material

    def test_photo_becomes_problem_automatically(self):
        material = self._material()
        outcome = self.intake.ingest_material(material.id, actor="user")
        self.assertEqual(outcome["handled"], HANDLED_RECORDED)
        self.assertEqual(len(outcome["recorded"]), 1)
        problem_id = outcome["recorded"][0]["problem_id"]
        problem = self.study.get_problem(problem_id)
        self.assertEqual(problem.subject, "数学")
        self.assertEqual(problem.knowledge_points, ["异分母分数加减", "通分"])
        self.assertEqual(problem.asset_ids, [material.asset_id], "照片要跟着错题走")
        self.assertEqual(outcome["recorded"][0]["first_due_local"] > NOW.date().isoformat(),
                         True)
        # 素材上留痕:哪个渠道、最后变成了什么
        refreshed = self.materials.get(material.id)
        self.assertEqual(refreshed.handled, HANDLED_RECORDED)
        self.assertEqual(refreshed.result["recorded"][0]["problem_id"], problem_id)
        self.assertIn("已录进错题本", outcome["message"])
        self.assertIn("/撤销", outcome["message"])
        # 回话里必须有题干 —— 否则用户看不出录进去的是哪道题
        self.assertIn("3/4 + 1/6", outcome["message"])

    def test_accept_study_error_is_caught_not_nameerror(self):
        """自动接受失败时必须报告真实领域错误,不能因为 StudyError 未导入抛 NameError。"""
        material = self._material()
        original = self.intake.accept

        def boom(*args, **kwargs):
            raise StudyError("知识点不存在")

        self.intake.accept = boom
        try:
            outcome = self.intake.ingest_material(material.id, actor="user")
        finally:
            self.intake.accept = original
        self.assertIn("知识点不存在", outcome["error"])
        self.assertNotIn("NameError", outcome["error"])

    def test_not_a_problem_is_stored_not_recorded(self):
        self.vision.reply = NOT_A_PROBLEM_REPLY
        material = self._material()
        outcome = self.intake.ingest_material(material.id, actor="user")
        self.assertEqual(outcome["handled"], HANDLED_STORED)
        self.assertEqual(self.study.list_problems({}), [], "不是题目就不该进错题本")
        self.assertIn("看着不是题目", outcome["message"])
        self.assertIn("风景照", outcome["message"])

    def test_offline_mode_keeps_the_photo(self):
        service = IntakeService(self.conn, study=self.study, media=self.media, client_factory=None)
        material = self._material()
        outcome = service.ingest_material(material.id, actor="user")
        self.assertTrue(outcome["offline"])
        self.assertEqual(outcome["handled"], HANDLED_FAILED)
        self.assertTrue(self.materials.path_of(material).exists(), "原图一份都不能丢")
        self.assertIn("没有可用的模型", outcome["message"])

    def test_non_image_is_only_archived(self):
        material, _ = self.materials.ingest_bytes(
            b"%PDF-1.4 fake", channel=CHANNEL_WEIXIN, external_id="wx:pdf",
            original_name="卷子.pdf",
        )
        outcome = self.intake.ingest_material(material.id)
        self.assertEqual(outcome["handled"], HANDLED_STORED)
        self.assertIn("不是图片", outcome["message"])
        self.assertEqual(self.vision.calls, 0, "不是图片就不要浪费一次模型调用")

    def test_render_message_shapes(self):
        self.assertIn("照片收到", render_ingest_result(
            {"material_id": 1, "offline": True, "message": "照片收到了,但现在没有可用的模型。"}
        ))
        self.assertIn("不是题目", render_ingest_result(
            {"material_id": 2, "is_problem": False, "reason": "一只猫"}
        ))
        self.assertIn("看着像题目", render_ingest_result(
            {"material_id": 3, "is_problem": True, "error": "拍糊了"}
        ))


# ==========================================================================
# 桥接层:照片从渠道进来 -> 自动录入 -> 回话
# ==========================================================================
class BridgeAttachmentTestCase(Fixture):
    """走真正的 Bridge/Thread —— 只把"视觉客户端"换成一个假的。"""

    def setUp(self) -> None:
        super().setUp()
        from la import config as _config

        self._config = _config
        self._patched = {
            key: getattr(_config, key)
            for key in ("DATA_DIR", "ARCHIVE_DIR", "MEDIA_DIR", "ARTIFACTS_DIR",
                        "LOGS_DIR", "ASSISTANT_DB", "MEMORY_DB", "RUNTIME_DIRS")
        }
        _config.DATA_DIR = self.tmp
        _config.ARCHIVE_DIR = self.tmp / "archive"
        _config.MEDIA_DIR = self.tmp / "media"
        _config.ARTIFACTS_DIR = self.tmp / "artifacts"
        _config.LOGS_DIR = self.tmp / "logs"
        _config.ASSISTANT_DB = self.tmp / "assistant.db"
        _config.MEMORY_DB = self.tmp / "memory.db"
        _config.RUNTIME_DIRS = (
            self.tmp, _config.ARCHIVE_DIR, _config.MEDIA_DIR,
            _config.ARTIFACTS_DIR, _config.LOGS_DIR,
        )
        os.environ["LA_LLM_PROVIDER"] = "fake"
        _config._settings = None
        self._bridges: list = []

    def tearDown(self) -> None:
        # ⚠️ 桥接线程各自持有两个 sqlite 连接,不关掉的话 Windows 上临时目录删不掉
        #    (报 WinError 32 文件被占用)。
        for bridge in self._bridges:
            try:
                bridge.close()
            except Exception:
                pass
        for key, value in self._patched.items():
            setattr(self._config, key, value)
        self._config._settings = None
        super().tearDown()

    def _bridge(self):
        from la.bridge import Bridge, SendAction

        class Collector:
            name = "collector"

            def __init__(self) -> None:
                self.actions: list[SendAction] = []

            async def send(self, action: SendAction) -> None:
                self.actions.append(action)

            async def typing(self, thread_id: str) -> None:
                return

        transport = Collector()
        bridge = Bridge(settings=self._config.get_settings(reload=True), transport=transport)
        self._bridges.append(bridge)
        return bridge, transport

    def _drain(self, thread, inbound):
        return asyncio.run(_collect(thread, inbound))

    def test_photo_only_message_records_problem_and_replies(self):
        from la.bridge import Attachment, Inbound

        bridge, transport = self._bridge()
        thread = bridge.thread("wxid_me")
        # 只把视觉客户端换成假的:其余全是真的(素材层、流水线、回执)
        thread.session.intake._client_factory = lambda: StubVision()
        inbound = Inbound(
            channel="weixin", user_id="wxid_me", thread_id="wxid_me", text="",
            external_id="ilink:m1",
            attachments=[Attachment(kind="image", data=PNG_1PX, name="错题.png",
                                    media_type="image/png", external_id="ilink:m1:0")],
        )
        events = self._drain(thread, inbound)
        kinds = [event["event"] for event in events]
        self.assertIn("text", kinds)
        body = " ".join(str(event["data"]) for event in events if event["event"] == "text")
        self.assertIn("已录进错题本", body)
        self.assertIn("通分", body)
        # 只发了一张图 —— 不该起一轮对话(省一次模型调用)
        self.assertNotIn("done_x", kinds)
        turns = [e for e in thread.session.store.events("bridge:wxid_me") if e.type == "turn/start"]
        self.assertEqual(turns, [], "只有附件时不该起对话轮次")
        # 素材与错题都真的落库了
        rows = self.conn.execute("SELECT count(*) FROM app_materials").fetchone()[0]
        self.assertEqual(rows, 1)
        self.assertEqual(thread.session.assistant_conn.execute(
            "SELECT count(*) FROM app_problems").fetchone()[0], 1)
        # ⭐ 这件事必须进对话事件:否则用户下一句"刚才那道题呢"会得到"没记录"
        events_log = thread.session.store.events("bridge:wxid_me")
        texts = [json.dumps(e.data, ensure_ascii=False) for e in events_log]
        self.assertTrue(
            any("发来 1 个附件" in text for text in texts),
            "只发附件也要在对话里留一条(带附件摘要)",
        )
        self.assertTrue(
            any("已录进错题本" in text for text in texts),
            "自动录入的结果要进对话历史,模型下一轮才接得上",
        )

    def test_duplicate_delivery_does_not_record_twice(self):
        from la.bridge import Attachment, Inbound

        bridge, transport = self._bridge()
        thread = bridge.thread("wxid_me")
        thread.session.intake._client_factory = lambda: StubVision()
        attachment = Attachment(kind="image", data=PNG_1PX, name="错题.png",
                                media_type="image/png", external_id="ilink:m9:0")
        first = Inbound(channel="weixin", user_id="wxid_me", thread_id="wxid_me",
                        external_id="ilink:m9", attachments=[attachment])
        self._drain(thread, first)
        second = Inbound(channel="weixin", user_id="wxid_me", thread_id="wxid_me",
                         external_id="ilink:m9", attachments=[attachment])
        events = self._drain(thread, second)
        self.assertTrue(any(e["event"] == "duplicate" for e in events))
        self.assertEqual(thread.session.assistant_conn.execute(
            "SELECT count(*) FROM app_problems").fetchone()[0], 1)

    def test_undo_command_really_undoes(self):
        from la.bridge import Inbound

        bridge, transport = self._bridge()
        thread = bridge.thread("wxid_me")
        record = thread.session.study.add_problem(
            {"subject": "数学", "stem_text": "1+1"}, actor="user"
        )
        events = self._drain(thread, Inbound(
            channel="weixin", user_id="wxid_me", thread_id="wxid_me",
            text="/撤销", external_id="ilink:u1",
        ))
        body = " ".join(str(e["data"]) for e in events if e["event"] == "text")
        self.assertIn("已撤销", body)
        self.assertEqual(
            thread.session.study.get_problem(record.id).status, "archived",
            "撤销错题 = 归档,不是删行",
        )

    def test_voice_without_transcript_does_not_promise_reading(self):
        """语音(没有转写)只留档,并且**不许**说"你可以直接让我读它的内容"。

        2026-10-17 真机:每条语音都多一条"已存成素材 #9(附件),你可以直接让我读它的内容",
        模型照着去读、读不出来,回话里就一直挂着一句"有个我不认识的附件"(D139)。
        """
        from la.bridge import Attachment, Inbound

        bridge, _transport = self._bridge()
        thread = bridge.thread("wxid_me")
        events = self._drain(thread, Inbound(
            channel="weixin", user_id="wxid_me", thread_id="wxid_me", text="",
            external_id="ilink:voice1",
            attachments=[Attachment(
                kind="voice", data=b"#!AMR\n" + b"\x3c" * 40,
                media_type="audio/amr", external_id="ilink:voice1:0",
            )],
        ))
        body = " ".join(str(event["data"]) for event in events if event["event"] == "text")
        self.assertIn("已存成素材", body)
        self.assertIn("读不出", body)
        self.assertNotIn("你可以直接让我读它的内容", body, "音频读不了,不许这么承诺")
        # 归宿如实记成"已留档",不是"待处理"
        row = self.conn.execute("SELECT kind, handled FROM app_materials").fetchone()
        self.assertEqual(tuple(row), ("voice", HANDLED_STORED))

    def test_failed_attachment_is_reported_not_swallowed(self):
        from la.bridge import Attachment, Inbound

        bridge, transport = self._bridge()
        thread = bridge.thread("wxid_me")
        events = self._drain(thread, Inbound(
            channel="weixin", user_id="wxid_me", thread_id="wxid_me", text="",
            external_id="ilink:m3",
            attachments=[Attachment(kind="image", error="下载失败: HTTP 403")],
        ))
        body = " ".join(str(e["data"]) for e in events if e["event"] == "text")
        self.assertIn("没取回来", body)
        self.assertIn("403", body)


    # -- 出站文件:手机的"发我一份 Word"就是走这条 --------------------------
    def _fake_turn_writing(
        self, thread, name: str, body: str = "# 周报\n\n本周小结", data: bytes | None = None
    ) -> None:
        """把这一轮伪装成"模型调了 artifact_write"。

        ⚠️ 用的是 artifact_write **真正的那两个组件**(产物登记 + 产物回执),
        只绕开模型 —— 桥接层要的判据本来就是**回执**,不是模型的话。
        """
        session = thread.session

        async def fake_respond(text, **kwargs):
            artifact = (
                session.artifacts.register_bytes(data, filename=name, label=name)
                if data is not None
                else session.artifacts.register_text(body, filename=name, label=name)
            )
            session.receipts.record(
                kind="artifact.file", domain=DOMAIN_ARTIFACT, label="产物:" + name,
                detail={"artifact_id": artifact.id, "name": name, "bytes": artifact.byte_size},
                undo_plan={"type": "artifact", "artifact_id": artifact.id, "name": name},
            )
            session.receipts.conn.commit()
            return ChatResult(text="已生成《" + name + "》。", model="stub")

        session.respond = fake_respond

    def test_turn_that_writes_a_file_emits_file_event(self):
        bridge, _transport = self._bridge()
        thread = bridge.thread("wxid_me")
        self._fake_turn_writing(thread, "本周小结.md")
        events = self._drain(thread, Inbound(
            channel="weixin", user_id="wxid_me", thread_id="wxid_me",
            text="把本周小结做成文件", external_id="ilink:f1",
        ))
        files = [event["data"] for event in events if event["event"] == "file"]
        self.assertEqual(len(files), 1, [event["event"] for event in events])
        self.assertEqual(files[0]["name"], "本周小结.md")
        self.assertEqual(files[0]["kind"], "file")
        self.assertTrue(Path(files[0]["path"]).exists(), "给出去的路径必须真的有文件")
        self.assertEqual(events[-1]["event"], "done")
        self.assertEqual(events[-1]["data"]["files"], 1)

    def _fake_turn_speaking(self, thread, text: str) -> None:
        """把这一轮伪装成"模型调了 tts_audio"。

        ⚠️ 走的是 **tts_audio 真正的那段代码**(引擎用离线的 SAPI 假 runner,
        不碰网络),只绕开模型 —— 要验的正是"录音能不能作为文件到达聊天窗口"。
        """
        import io
        import wave

        from la.domain.study.tts import TtsEngine
        from la.domain.tools import build_study_tools

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(b"\x00\x00" * 3200)
        payload = buffer.getvalue()

        class Runner:                      # 假 SAPI:写一段真 WAV 就返回
            stderr = b""

            def __call__(self, command):
                script = " ".join(command)
                marker = "SetOutputToWaveFile('"
                start = script.index(marker) + len(marker)
                Path(script[start:script.index("'", start)]).write_bytes(payload)
                return self

        session = thread.session
        engine = TtsEngine(
            provider="sapi", api_key="", cache_dir=self.tmp / "tts", runner=Runner(), rate=0,
        )
        handler = next(
            item for item in build_study_tools(session.study, tts_engine=engine)
            if item.name == "tts_audio"
        ).handler

        async def fake_respond(user_text, **kwargs):
            result = await handler({"text": text, "mode": "dictation"})
            self.assertTrue(result["delivered"], "没有回执 = 录音到不了微信")
            return ChatResult(text="录音做好了。", model="stub")

        session.respond = fake_respond

    def test_turn_that_speaks_emits_an_audio_file_event(self) -> None:
        """⭐ 真机报的 bug(2026-10-15):助手说"录音发你了",用户手里一个文件都没有。

        桥接层只认 **artifact 域回执** —— tts_audio 以前只登记产物、不写回执。
        这一条从工具一路走到"这一轮要发哪个文件",顺便钉住用户看到的名字。
        """
        bridge, _transport = self._bridge()
        thread = bridge.thread("wxid_me")
        self._fake_turn_speaking(thread, "Monday、Tuesday")
        events = self._drain(thread, Inbound(
            channel="weixin", user_id="wxid_me", thread_id="wxid_me",
            text="把这两个单词读一遍发我", external_id="ilink:tts1",
        ))
        files = [event["data"] for event in events if event["event"] == "file"]
        self.assertEqual(len(files), 1, [event["event"] for event in events])
        self.assertTrue(str(files[0]["name"]).endswith(".mp3"), files[0]["name"])
        self.assertTrue(Path(files[0]["path"]).exists(), "给出去的路径必须真的有文件")
        self.assertEqual(events[-1]["data"]["files"], 1)

    def test_artifact_from_an_earlier_turn_is_not_resent(self):
        """⚠️ 只发**这一轮**新写的:否则用户每说一句话都会再收到一堆旧文件。"""
        bridge, _transport = self._bridge()
        thread = bridge.thread("wxid_me")
        self._fake_turn_writing(thread, "第一次.md")
        first = self._drain(thread, Inbound(
            channel="weixin", user_id="wxid_me", thread_id="wxid_me",
            text="做一份", external_id="ilink:f10",
        ))
        self.assertEqual(len([e for e in first if e["event"] == "file"]), 1)

        thread.session.respond = _plain_reply("好的。")
        second = self._drain(thread, Inbound(
            channel="weixin", user_id="wxid_me", thread_id="wxid_me",
            text="再说一句", external_id="ilink:f11",
        ))
        self.assertEqual([e for e in second if e["event"] == "file"], [])
        self.assertEqual(second[-1]["data"]["files"], 0)

    def test_undone_artifact_is_not_sent(self):
        """撤销掉的产物文件已经被删掉了,不能再发出去。"""
        from la.domain.receipts import Receipt

        bridge, _transport = self._bridge()
        thread = bridge.thread("wxid_me")
        self._fake_turn_writing(thread, "要撤的.md")
        self._drain(thread, Inbound(
            channel="weixin", user_id="wxid_me", thread_id="wxid_me",
            text="做一份", external_id="ilink:f20",
        ))
        live = thread.session.receipts.since(0)
        self.assertEqual(len(thread._outbound_files(live)), 1, "没撤销时应该发")

        undone = Receipt(
            id=live[-1].id, ts="", actor="agent", kind="artifact.file",
            domain=DOMAIN_ARTIFACT, label=live[-1].label, detail=live[-1].detail,
            undone_at="2026-01-01T00:00:00+00:00",
        )
        self.assertEqual(thread._outbound_files([undone]), [])

    def test_file_registration_gone_is_not_fatal(self):
        """回执在、产物登记没了(被清理过)—— 如实不发,而不是把这一轮搞崩。"""
        from la.domain.receipts import Receipt

        bridge, _transport = self._bridge()
        thread = bridge.thread("wxid_me")
        ghost = Receipt(
            id=1, ts="", actor="agent", kind="artifact.file", domain=DOMAIN_ARTIFACT,
            label="产物:早就没了.md", detail={"artifact_id": "f0000000000"},
        )
        self.assertEqual(thread._outbound_files([ghost]), [])

    def test_weixin_adapter_uploads_and_sends_the_file(self):
        """整整一条链路:微信来一句话 → 助手写出文件 → 文件真的进了 sendmessage。

        ⚠️ 这一条是"接上"的验收:上面几条只证明桥接层**报出**了文件,
        能不能真的送到微信,要看微信适配器这一段。
        """
        from la.bridge.adapters.weixin_ilink import ILinkAdapter
        from la.bridge.adapters.weixin_protocol import ILinkClient

        _CDNHandler.uploaded = b""
        cdn = FakeCDN()
        self.addCleanup(cdn.close)
        sent: list = []

        class RecordingClient(ILinkClient):
            def __init__(self) -> None:
                super().__init__(token="stub")

            async def get_upload_url(self, body):
                return {"ret": 0, "upload_full_url": cdn.base + "/upload?filekey=x"}

            async def send_items(self, to_user, items, context_token):
                sent.append({"to_user": to_user, "items": items, "token": context_token})
                return {"ret": 0}

        from la.base.docx_writer import build_docx

        bridge, _transport = self._bridge()
        thread = bridge.thread("wxid_me")
        # ⭐ 用**真的 Word 文件**,不是假装一个 .docx 后缀
        payload, _facts = build_docx("# 本周小结\n\n就这样", fallback_title="本周小结")
        # ⭐ 用户要的名字是"本周小结.docx";落盘时会加时间戳前缀,发出去的必须还是原名
        self._fake_turn_writing(thread, "本周小结.docx", data=payload)

        client = RecordingClient()
        self.addCleanup(lambda: asyncio.run(client.aclose()))
        adapter = ILinkAdapter(bridge=bridge, client=client)
        asyncio.run(adapter._handle_raw({
            "message_id": "m-file-1", "from_user_id": "wxid_me", "context_token": "CTX",
            "item_list": [{"type": 1, "text_item": {"text": "把本周小结做成 Word"}}],
        }))

        kinds = [entry["items"][0]["type"] for entry in sent]
        self.assertIn(4, kinds, "必须有一条是文件(type=4)")
        self.assertIn(1, kinds, "正文也要送到")
        file_item = next(
            entry["items"][0] for entry in sent if entry["items"][0]["type"] == 4
        )["file_item"]
        self.assertEqual(file_item["file_name"], "本周小结.docx", "发出去的必须是用户要的名字")
        self.assertTrue(_CDNHandler.uploaded, "CDN 上必须真的传了密文")
        # 传上去的必须是**密文**,不是原样的 docx
        self.assertNotEqual(_CDNHandler.uploaded, payload)
        self.assertEqual(file_item["media"]["encrypt_type"], 1)
        self.assertIn("media", file_item, "媒体字段嵌在 media 里,不是扁平的")
        self.assertEqual(file_item["len"], str(len(payload)), "文件给的是明文大小")

    # -- 入口感知:桥接层必须把渠道原样传下去 ------------------------------
    def test_bridge_passes_the_channel_through_as_surface(self):
        """真正的接线:一条微信消息进来,系统提示里必须写着"手机微信"。"""
        bridge, _transport = self._bridge()
        thread = bridge.thread("wxid_me")
        self._drain(thread, Inbound(
            channel="weixin", user_id="wxid_me", thread_id="wxid_me",
            text="你好", external_id="ilink:s1",
        ))
        self.assertEqual(thread.session.surface, "weixin")
        messages = thread.session.projection_preview()[0]
        # ⭐ D122:入口这类每轮都变的信息挪到了**末尾的系统快照**里;头部的稳定系统提示
        #    必须保持逐字节不变,否则前缀缓存从第一个 token 起就失效。
        joined = "\n".join(str(message.get("content") or "") for message in messages)
        self.assertIn("手机微信", joined)
        self.assertNotIn("手机微信", str(messages[0].get("content") or ""))


def _plain_reply(text: str):
    async def fake_respond(_text, **kwargs):
        return ChatResult(text=text, model="stub")

    return fake_respond


class AdapterImportTestCase(unittest.TestCase):
    """每个渠道适配器都要能被 import。

    ⚠️ 这条是**补的**:console.py 曾被改出一个语法错误(字符串里的换行写成了真换行),
    而整套测试全绿 —— 因为没有一条用例 import 过它。终端适配器是"断网时的逃生通道",
    正是那种"坏了没人发现"的文件。
    """

    def test_every_adapter_module_imports(self):
        import importlib

        from la.bridge import adapters

        root = Path(adapters.__file__).parent
        names = sorted(
            path.stem for path in root.glob("*.py")
            if not path.stem.startswith("__")
        )
        self.assertIn("console", names)
        self.assertIn("weixin_ilink", names)
        for name in names:
            with self.subTest(module=name):
                importlib.import_module("la.bridge.adapters." + name)


class SurfaceTestCase(unittest.TestCase):
    """入口感知(提示词层):模型必须知道用户是在手机上还是在电脑前。

    ⚠️ 刻意**不继承** BridgeAttachmentTestCase —— 那会把整套桥接用例再跑一遍。
    "桥接层到底有没有把渠道传下来"是桥接自己的用例,见
    BridgeAttachmentTestCase.test_bridge_passes_the_channel_through_as_surface。
    """

    def setUp(self) -> None:
        from la import config

        self._config = config
        self._saved = config._settings
        config._settings = None

    def tearDown(self) -> None:
        self._config._settings = self._saved

    def test_known_surfaces_translate(self):
        from la.agent.prompts import surface_lines

        weixin = surface_lines("weixin")
        web = surface_lines("web")
        cli = surface_lines("console")      # 渠道名是 console,入口叫 cli(别名收敛)
        self.assertIn("手机微信", weixin[0])
        self.assertIn("电脑网页", web[0])
        self.assertIn("电脑终端", cli[0])
        self.assertNotEqual(
            weixin[1], web[1], "两个入口的约束必须真的不一样,否则等于没注入"
        )

    def test_unknown_surface_is_reported_not_guessed(self):
        from la.agent.prompts import surface_lines

        self.assertEqual(surface_lines("telegram"), ["对话入口: telegram"])
        self.assertEqual(surface_lines(""), [])
        self.assertEqual(surface_lines(None), [])

    def test_runtime_context_carries_the_surface(self):
        from la.agent.prompts import build_runtime_context

        settings = self._config.get_settings(reload=True)
        text = build_runtime_context(settings, turn=1, surface="weixin")
        self.assertIn("对话入口: 手机微信", text)
        self.assertIn("入口约束:", text)
        self.assertNotIn("入口约束:", build_runtime_context(settings, turn=1))

async def _collect(thread, inbound):
    return [event async for event in thread.handle(inbound)]


class WebMaterialTestCase(Fixture):
    """Web 侧:素材页、聊天传图、产物下载。走真正的 app(路径按 temp 隔离)。"""

    def setUp(self) -> None:
        super().setUp()
        from la import config as _config

        self._config = _config
        self._patched = {
            key: getattr(_config, key)
            for key in ("DATA_DIR", "ARCHIVE_DIR", "MEDIA_DIR", "ARTIFACTS_DIR",
                        "LOGS_DIR", "ASSISTANT_DB", "MEMORY_DB", "RUNTIME_DIRS")
        }
        _config.DATA_DIR = self.tmp
        _config.ARCHIVE_DIR = self.tmp / "archive"
        _config.MEDIA_DIR = self.tmp / "media"
        _config.ARTIFACTS_DIR = self.tmp / "artifacts"
        _config.LOGS_DIR = self.tmp / "logs"
        _config.ASSISTANT_DB = self.tmp / "assistant.db"
        _config.MEMORY_DB = self.tmp / "memory.db"
        _config.RUNTIME_DIRS = (
            self.tmp, _config.ARCHIVE_DIR, _config.MEDIA_DIR,
            _config.ARTIFACTS_DIR, _config.LOGS_DIR,
        )
        self._env = {key: os.environ.get(key) for key in ("LA_LLM_PROVIDER", "DEEPSEEK_API_KEY")}
        # 让 build_intake 认为"有视觉能力",但实际拿到的是假的客户端
        os.environ["LA_LLM_PROVIDER"] = "deepseek"
        os.environ["DEEPSEEK_API_KEY"] = "stub-key-for-tests"
        _config._settings = None
        from la.web import deps as web_deps

        self._web_deps = web_deps
        self._real_factory = web_deps.build_model_client
        self.stub = StubVision()
        web_deps.build_model_client = lambda settings: self.stub

    def tearDown(self) -> None:
        self._web_deps.build_model_client = self._real_factory
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for key, value in self._patched.items():
            setattr(self._config, key, value)
        self._config._settings = None
        super().tearDown()

    def _client(self):
        from fastapi.testclient import TestClient

        from la.web.app import create_app

        return TestClient(create_app())

    def test_materials_page_renders(self):
        client = self._client()
        response = client.get("/materials")
        self.assertEqual(response.status_code, 200)
        self.assertIn("素材", response.text)

    def test_upload_then_serve_file(self):
        client = self._client()
        response = client.post(
            "/materials/upload",
            files=[("files", ("错题.png", PNG_1PX, "image/png"))],
            data={"_idem": "m1"}, follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        rows = self.conn.execute("SELECT id, media_type FROM app_materials").fetchall()
        self.assertEqual(len(rows), 1)
        served = client.get("/materials/" + str(rows[0]["id"]) + "/file")
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.content, PNG_1PX)
        self.assertEqual(client.get("/materials/9999/file").status_code, 404)

    def test_chat_attach_records_problem(self):
        """聊天页传图 = 与微信同一条流水线:素材 -> 识别 -> 直接录成错题。"""
        client = self._client()
        response = client.post(
            "/chat/attach", files=[("files", ("错题.png", PNG_1PX, "image/png"))]
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(len(payload["recorded"]), 1)
        self.assertIn("已录进错题本", payload["message"])
        self.assertIsNotNone(payload["receipt"], "上传后要能一键撤销")
        rows = self.conn.execute("SELECT count(*) FROM app_problems").fetchone()[0]
        self.assertEqual(rows, 1)

    def test_sheet_becomes_an_artifact_and_downloads(self):
        client = self._client()
        self.study.add_problem(
            {"subject": "数学", "stem_text": "3/4 + 1/6 = ?", "answer_text": "11/12",
             "knowledge_points": ["通分"]}
        )
        # 把首次到期提前到今天,才有一份有内容的错题卷
        today = self.study._current_time().date().isoformat()  # noqa: SLF001
        self.conn.execute("UPDATE app_review_units SET due_date_local = ?", (today,))
        self.conn.commit()
        posted = client.post("/study/sheet", data={"_idem": "sheet1"}, follow_redirects=False)
        self.assertEqual(posted.status_code, 303)
        artifacts = self.artifacts.list()
        self.assertTrue(artifacts, "错题卷要登记成产物")
        downloaded = client.get("/artifacts/" + artifacts[0].id)
        self.assertEqual(downloaded.status_code, 200)
        body = downloaded.content.decode("utf-8")
        self.assertIn("错题卷", body)
        self.assertIn("3/4 + 1/6", body)
        self.assertIn("答案", body)
        self.assertEqual(client.get("/artifacts/nope").status_code, 404)


class SheetTestCase(Fixture):
    def test_sheet_contains_items_and_answers(self):
        service = StudyService(
            self.conn, media=self.media, now_provider=lambda: NOW, artifacts=self.artifacts
        )
        service.add_problem({
            "subject": "数学", "stem_text": "3/4 + 1/6 = ?", "answer_text": "11/12",
            "knowledge_points": ["通分"],
        })
        result = service.make_sheet(today=(NOW + __import__("datetime").timedelta(days=2)).date().isoformat())
        self.assertEqual(result["items"], 1)
        text = result["text"]
        self.assertIn("# ", text)
        self.assertIn("3/4 + 1/6", text)
        self.assertIn("11/12", text)
        self.assertTrue((self.artifacts.root / result["artifact"]["rel_path"]).exists())

    def test_sheet_without_artifact_store_is_explicit(self):
        from la.domain.services.study_service import StudyError

        with self.assertRaises(StudyError):
            StudyService(self.conn, media=self.media, now_provider=lambda: NOW).make_sheet()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
