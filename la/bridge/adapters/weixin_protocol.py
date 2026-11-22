"""微信官方 iLink Bot 协议(自己实现,不引第三方 SDK)。

依据:协议是**腾讯官方公开**的,多个独立实现可交叉验证
(weilink / wechat-openclaw-sdk / lib-x/ilink(Go) / Whale(JS))。
本项目只依赖 httpx,所以这里不装 SDK —— 协议本身很薄,自己实现反而好控制。

⚠️ **改这个文件之前先读这段**,下面每一条都是"错了就静默失效"的:

* `X-WECHAT-UIN` 每次请求都要**重新随机**(服务端用它防重放);
* `iLink-App-ClientVersion` 是 uint32 编码 `0x00MMNNPP`,不是版本字符串;
* 回复必须**原样回传 `context_token`**,否则消息关联不到会话;
* **一个 context_token 最多只能发 10 条**(CONTEXT_TOKEN_QUOTA)。
  这就是"主动提醒推不动"的机制根源:窗口内额度用完就得等用户再说一句。
* 会话过期返回 `errcode = -14`,必须重新扫码;
* 文本上限 16384 字节(UTF-8),按**字节**不是按字符。
* **失败不一定走 HTTP 状态码**:HTTP 200 + `ret != 0` 也是失败。
  `ret = -2` 社区口径是"限流 / 这个 context_token 已作废"(见 `_check`)。
* ⚠️ **未核实**:`sendtyping`(以及 `getconfig`)是否也消耗 context_token 的额度。
  社区有报告说"额度被 typing 吃掉、回复随即被拒(2026-10,QwenPaw #6696)",
  本项目的额度计数**只算真正发出去的消息**;若真机会复现,再把它算进去。
* ⚠️ **未核实**:发送响应里的 `remaining` 字段。代码用它校准本地计数,
  但**官方文档里没核到过这个字段名** —— 取不到就退回本地自减(见 weixin_ilink.py)。
"""

from __future__ import annotations

import base64
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

BASE_URL = "https://ilinkai.weixin.qq.com"

EP_QR_CODE = "/ilink/bot/get_bot_qrcode"
EP_QR_STATUS = "/ilink/bot/get_qrcode_status"
EP_GET_UPDATES = "/ilink/bot/getupdates"
EP_SEND_MESSAGE = "/ilink/bot/sendmessage"
EP_GET_CONFIG = "/ilink/bot/getconfig"
EP_SEND_TYPING = "/ilink/bot/sendtyping"
EP_GET_UPLOAD_URL = "/ilink/bot/getuploadurl"

#: 登录时申请的 bot 类型。3 = 微信 ClawBot(个人号插件)通道。
BOT_TYPE = 3
#: 渠道版本,会编码进 iLink-App-ClientVersion 头并放进 base_info
CHANNEL_VERSION = "1.0.2"
#: 服务端长轮询 hold 上限(秒)
LONGPOLL_TIMEOUT = 35.0
#: 会话过期
SESSION_EXPIRED = -14
#: 限流 / context_token 已作废(额度用完或窗口失效)。
#: 依据:社区多份实测报告(hermes-agent #35713 / QwenPaw #6696)——
#: 服务端回 HTTP 200 但 `ret=-2`,errmsg 常见 "rate limited"。
RATE_LIMITED = -2
#: 单个 context_token 的出站额度
CONTEXT_TOKEN_QUOTA = 10
#: 主动消息只能在"用户最后一次说话起 24 小时内"发(微信官方 FAQ 口径)。
#: ⚠️ 过了窗口,服务端不一定先说"过期",而是回 ret=-2 prepare failed —— 所以
#:    本地必须自己按时间判,否则会拿着一个六天前的 token 打一整晚。
WINDOW_S = 24 * 3600.0
#: 文本上限(UTF-8 字节)
TEXT_BYTE_LIMIT = 16384

# item_list 里的类型码
ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

# typing 状态
TYPING_START = 1
TYPING_STOP = 2


class ILinkError(RuntimeError):
    """iLink 调用失败。"""

    def __init__(self, ret: int = 0, errcode: int | None = None, errmsg: str = "") -> None:
        self.ret = ret
        self.errcode = errcode
        self.errmsg = errmsg
        super().__init__("iLink ret=" + str(ret) + " errcode=" + str(errcode) + " " + errmsg)


class RateLimited(ILinkError):
    """`ret=-2`:限流,或者这个 `context_token` 的额度已经用完 / 已作废。

    ⚠️ 与 SessionExpired(-14)的区别:-14 要**重新扫码**;-2 只要等用户再说一句话,
    下一个 context_token 带来的就是新额度(见 weixin_ilink.py 的处理)。
    """


class SessionExpired(ILinkError):
    """会话过期(-14),必须重新扫码。"""


def encode_client_version(version: str = CHANNEL_VERSION) -> str:
    """把 "1.0.2" 编码成 uint32 的十进制字符串(0x00MMNNPP)。"""
    parts = [int(piece) for piece in version.split(".")]
    while len(parts) < 3:
        parts.append(0)
    return str((parts[0] << 16) | (parts[1] << 8) | parts[2])


def random_uin() -> str:
    """每次请求都重新生成的防重放头。"""
    return base64.b64encode(str(random.randint(0, 0xFFFFFFFF)).encode()).decode()


def build_headers(token: str | None = None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": random_uin(),
        "iLink-App-Id": "bot",
        "iLink-App-ClientVersion": encode_client_version(),
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    return headers


def _check(payload: dict[str, Any]) -> dict[str, Any]:
    """把服务端的失败**变成异常** —— 包括"HTTP 200 但 ret 非 0"这种。

    ⚠️ 2026-10-13 之前这里只看 `errcode == -14`,于是 `ret=-2`(限流/额度用完)
    会被当成**发送成功**:本地额度照减、日志里一个字都没有,而用户什么都没收到。
    与 D87("出站失败必须响")是同一类静默失效。
    """
    if payload.get("errcode") == SESSION_EXPIRED:
        raise SessionExpired(
            ret=payload.get("ret", 0), errcode=SESSION_EXPIRED,
            errmsg=payload.get("errmsg", "session timeout"),
        )
    ret = payload.get("ret")
    if isinstance(ret, int) and ret != 0:
        if ret == RATE_LIMITED:
            raise RateLimited(
                ret=ret, errcode=payload.get("errcode"),
                errmsg=str(payload.get("errmsg") or "rate limited"),
            )
        raise ILinkError(
            ret=ret, errcode=payload.get("errcode"),
            errmsg=str(payload.get("errmsg") or ""),
        )
    return payload


def split_text(text: str, limit: int = TEXT_BYTE_LIMIT) -> list[str]:
    """按 **UTF-8 字节** 切分,不切断多字节字符。

    ⚠️ 不能按字符切:一个中文字符 3 字节,按 16384 个字符切会超限被服务端拒。
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return [text] if text else []
    pieces: list[str] = []
    buffer: list[str] = []
    size = 0
    for char in text:
        width = len(char.encode("utf-8"))
        if size + width > limit:
            pieces.append("".join(buffer))
            buffer, size = [], 0
        buffer.append(char)
        size += width
    if buffer:
        pieces.append("".join(buffer))
    return pieces


@dataclass
class BotCredentials:
    """扫码换来的凭据。"""

    bot_id: str = ""
    token: str = ""
    base_url: str = BASE_URL
    user_id: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"bot_id": self.bot_id, "token": self.token,
                "base_url": self.base_url, "user_id": self.user_id}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "BotCredentials":
        return cls(
            bot_id=str(raw.get("bot_id") or ""),
            token=str(raw.get("token") or ""),
            base_url=str(raw.get("base_url") or BASE_URL),
            user_id=str(raw.get("user_id") or ""),
        )


class ILinkClient:
    """iLink 协议客户端。线程内使用,别跨 asyncio 任务共享。"""

    def __init__(
        self,
        *,
        token: str = "",
        base_url: str = BASE_URL,
        timeout_s: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=timeout_s, transport=transport,
            headers={"User-Agent": "life-assistant/0.1 (ilink bot)"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- 登录 -------------------------------------------------------------
    async def request_qrcode(self) -> dict[str, Any]:
        resp = await self._client.get(
            self.base_url + EP_QR_CODE,
            params={"bot_type": str(BOT_TYPE)},
            headers=build_headers(),
            timeout=15.0,
        )
        resp.raise_for_status()
        return resp.json()

    async def poll_qrcode(
        self, qrcode: str, *, timeout_s: float = 40.0
    ) -> dict[str, Any]:
        """查一次扫码状态。

        ⚠️ 默认 40 秒是**长轮询**(服务端把请求挂到状态变化才回)。
           网页轮询必须传一个短超时(3 秒),否则一个请求就能把线程池占住。
        """
        resp = await self._client.get(
            self.base_url + EP_QR_STATUS,
            params={"qrcode": qrcode},
            headers=build_headers(),
            timeout=float(timeout_s),
        )
        resp.raise_for_status()
        return resp.json()

    # -- 收 ---------------------------------------------------------------
    async def get_updates(self, cursor: str = "") -> dict[str, Any]:
        body = {
            "get_updates_buf": cursor,
            "base_info": {"channel_version": CHANNEL_VERSION},
        }
        resp = await self._client.post(
            self.base_url + EP_GET_UPDATES,
            json=body,
            headers=build_headers(self.token),
            timeout=LONGPOLL_TIMEOUT + 10.0,
        )
        resp.raise_for_status()
        return _check(resp.json())

    # -- 发 ---------------------------------------------------------------
    async def send_items(
        self, to_user: str, items: list[dict[str, Any]], context_token: str
    ) -> dict[str, Any]:
        """发一条消息(一个或多个 item:文本 / 图片 / 文件)。

        ⚠️ 媒体必须先经 getuploadurl + CDN 上传拿到 `encrypt_query_param` 才能进来,
        这里只负责把它塞进 item_list。
        """
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user,
                "client_id": "la:" + str(int(time.time() * 1000)) + "-" + uuid.uuid4().hex[:8],
                "message_type": 2,   # BOT
                "message_state": 2,  # FINISH
                "context_token": context_token,
                "item_list": list(items),
            },
            "base_info": {"channel_version": CHANNEL_VERSION},
        }
        resp = await self._client.post(
            self.base_url + EP_SEND_MESSAGE,
            json=body,
            headers=build_headers(self.token),
            timeout=30.0,
        )
        resp.raise_for_status()
        return _check(resp.json())

    async def send_text(self, to_user: str, text: str, context_token: str) -> dict[str, Any]:
        """发一条文本。超过 16KiB 会被拒,调用方应先 split_text。"""
        return await self.send_items(
            to_user, [{"type": ITEM_TEXT, "text_item": {"text": text}}], context_token
        )

    @property
    def http(self) -> httpx.AsyncClient:
        """内部的 httpx 客户端 —— 媒体下载/上传复用它(连接池、超时都在这里)。"""
        return self._client

    async def get_upload_url(self, body: dict[str, Any]) -> dict[str, Any]:
        """要一个 CDN 上传地址(媒体出站的第一步)。"""
        resp = await self._client.post(
            self.base_url + EP_GET_UPLOAD_URL,
            json=body,
            headers=build_headers(self.token),
            timeout=30.0,
        )
        resp.raise_for_status()
        return _check(resp.json())

    async def send_typing(self, user_id: str, typing_ticket: str, status: int) -> dict[str, Any]:
        body = {
            "ilink_user_id": user_id,
            "typing_ticket": typing_ticket,
            "status": status,
            "base_info": {"channel_version": CHANNEL_VERSION},
        }
        resp = await self._client.post(
            self.base_url + EP_SEND_TYPING,
            json=body,
            headers=build_headers(self.token),
            timeout=10.0,
        )
        resp.raise_for_status()
        return _check(resp.json())

    async def get_config(self, user_id: str, context_token: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "ilink_user_id": user_id,
            "base_info": {"channel_version": CHANNEL_VERSION},
        }
        if context_token:
            body["context_token"] = context_token
        resp = await self._client.post(
            self.base_url + EP_GET_CONFIG,
            json=body,
            headers=build_headers(self.token),
            timeout=10.0,
        )
        resp.raise_for_status()
        return _check(resp.json())


@dataclass
class IncomingMessage:
    """从 iLink 原始报文里解析出来的入站消息。"""

    message_id: str
    from_user: str
    text: str
    context_token: str
    create_time_ms: int = 0
    item_type: int = ITEM_TEXT
    #: 语音消息自带的转写文本(服务端已经转好,不用自己做 ASR)
    voice_text: str = ""
    #: 这条消息里**带转写文字**的语音项有几条。>0 就说明语音内容已经在 text 里,
    #: 原声不必下载(见 D139:以前会多存一份读不了的音频,回话里还带一句"有个附件")。
    voice_transcribed: int = 0
    #: 这条消息里**认不出类型**的项(表情/位置/名片…)。只用来写一行日志;
    #: 它们不会被当成附件 —— 否则用户会收到"有个附件没取回来"(D139)。
    unknown_item_types: list[int] = field(default_factory=list)
    #: 这条消息里带的媒体(照片/文件/视频)。**空列表 = 纯文本消息**。
    media: list[Any] = field(default_factory=list)
    raw: dict[str, Any] | None = None

    def dedup_key(self) -> str:
        """幂等键。

        ⚠️ 用 message_id 而不是 context_token:同一个 context_token 会伴随
        用户的多条消息,拿它去重会把后面的真消息一起吞掉。
        """
        if self.message_id:
            return "ilink:" + self.message_id
        return "ilink:" + str(self.create_time_ms) + ":" + self.from_user


def parse_incoming(raw: dict[str, Any]) -> IncomingMessage | None:
    """解析一条原始消息。解析不出来(没有发送者)返回 None。"""
    sender = str(raw.get("from_user_id") or "")
    if not sender:
        return None
    text = ""
    item_type = ITEM_TEXT
    voice_text = ""
    voice_transcribed = 0
    unknown_types: set[int] = set()
    items = raw.get("item_list") or []
    if items:
        first = items[0] or {}
        item_type = int(first.get("type") or ITEM_TEXT)
        # ⚠️ 以前只读 items[0]:于是"一张图 + 一段语音"这种消息里的语音转写**整段丢掉**,
        #    手机那边表现为"说了话它没听见"。现在把每一项的文字都收进来 ——
        #    纯文本消息的结果与以前完全一致(只有一项)。
        pieces: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                kind = int(item.get("type") or ITEM_TEXT)
            except (TypeError, ValueError):
                continue
            if kind == ITEM_TEXT:
                pieces.append(str((item.get("text_item") or {}).get("text") or ""))
            elif kind == ITEM_VOICE:
                # 微信侧已经把语音转成文字了 —— 白捡的能力
                heard = str((item.get("voice_item") or {}).get("text") or "").strip()
                if heard:
                    voice_transcribed += 1
                    voice_text = (voice_text + "\n" + heard) if voice_text else heard
                    pieces.append(heard)
            elif kind not in (ITEM_IMAGE, ITEM_FILE, ITEM_VIDEO):
                unknown_types.add(kind)
        text = "\n".join(piece for piece in pieces if piece.strip())
    # ⭐ 媒体项(image/file/video)由 weixin_media 统一抽描述,这里只负责带上。
    #    抽成独立的 dataclass 而不是就地读字段,是因为入站字段名在几种实现之间不一致
    #    (cdn_url / full_url / encrypt_query_param 三种都见过)。
    try:
        from .weixin_media import media_refs_from_items
    except Exception:  # noqa: BLE001 - 协议解析不该因为媒体模块缺失而整个失败
        media = []
    else:
        media = media_refs_from_items(list(items))
    return IncomingMessage(
        message_id=str(raw.get("message_id") or ""),
        from_user=sender,
        text=text,
        context_token=str(raw.get("context_token") or ""),
        create_time_ms=int(raw.get("create_time_ms") or 0),
        item_type=item_type,
        voice_text=voice_text,
        voice_transcribed=voice_transcribed,
        unknown_item_types=sorted(unknown_types),
        raw=raw,
        media=media,
    )
