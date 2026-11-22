"""微信媒体:入站下载解密 + 出站加密上传。

⭐ 这一层的事实全部来自**多份独立实现的交叉验证**(Rust ilink-hub 的类型文档、
Python wechat-bridge 的 media.py/crypto.py),不是猜的:

  * 入站:item_list 里的 image_item / file_item 带 `encrypt_query_param`(首选)
    或 `full_url`,以及 `aes_key` / `md5` / `file_name`;
  * CDN:`https://novac2c.cdn.weixin.qq.com/c2c`
      下载 `GET /download?encrypted_query_param=<urlencoded>`
      上传 `upload_full_url`(优先)或 `/upload?encrypted_query_param=..&filekey=..`
  * 加密:**AES-128-ECB + PKCS7**;上传后 CDN 在响应头 `x-encrypted-param` 里给下载参数;
  * `aes_key` 有 4 种写法:16 字节原文 / 32 位 hex / base64(16 字节) / base64(32 位 hex)。
    官方插件发出去的是 **base64(32 位 hex)** —— 这条最容易错,错了表现为"对方看到灰框"。

⚠️ 两条工程纪律:
  1. `Crypto`(pycryptodome)**延迟导入**。没有它时,整个应用照常跑,只有微信媒体这一步
     会给出"装一下 pycryptodome"的明确报错 —— 而不是 import 期就炸掉整个进程。
  2. 下载优先用 `encrypt_query_param`:实测 `full_url` 的数据在部分消息类型上
     解不开(视频必踩),所以两个都试,先成的先用。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"

#: 单个媒体的上限。与素材层一致:超过它就不下载,只记一条"太大,没取回来"。
MAX_MEDIA_BYTES = 32 << 20

ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

#: getuploadurl 的 media_type 编号与 item 类型**不是一套**(这是协议里的坑)
UPLOAD_IMAGE = 1
UPLOAD_VIDEO = 2
UPLOAD_FILE = 3
UPLOAD_VOICE = 4

ITEM_KIND = {
    ITEM_IMAGE: "image",
    ITEM_VOICE: "voice",
    ITEM_FILE: "file",
    ITEM_VIDEO: "file",
}
ITEM_MEDIA_FIELD = {
    ITEM_IMAGE: "image_item",
    ITEM_VOICE: "voice_item",
    ITEM_FILE: "file_item",
    ITEM_VIDEO: "video_item",
}
UPLOAD_TYPE = {ITEM_IMAGE: UPLOAD_IMAGE, ITEM_FILE: UPLOAD_FILE, ITEM_VIDEO: UPLOAD_VIDEO}

CRYPTO_HINT = (
    "微信媒体的 AES 解密需要一个加密库:请执行 "
    ".venv/Scripts/python.exe -m pip install pycryptodome"
)


class MediaError(RuntimeError):
    """媒体收发失败。"""


# -- 加解密 ---------------------------------------------------------------
def _aes():
    """延迟导入 pycryptodome。没有就抛一句**能照做**的话。"""
    try:
        from Crypto.Cipher import AES  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise MediaError(CRYPTO_HINT) from exc
    return AES


def _pkcs7_pad(data: bytes) -> bytes:
    pad = 16 - len(data) % 16
    return data + bytes([pad]) * pad


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        raise MediaError("解密结果为空")
    pad = data[-1]
    if pad < 1 or pad > 16 or data[-pad:] != bytes([pad]) * pad:
        raise MediaError("AES 解密 padding 校验失败(密钥可能不对)")
    return data[:-pad]


def aes_ecb_encrypt(data: bytes, key: bytes) -> bytes:
    return _aes().new(key, _aes().MODE_ECB).encrypt(_pkcs7_pad(data))


def aes_ecb_decrypt(data: bytes, key: bytes) -> bytes:
    return _pkcs7_unpad(_aes().new(key, _aes().MODE_ECB).decrypt(data))


def _is_hex(raw: bytes) -> bool:
    return bool(raw) and all(
        (48 <= c <= 57) or (97 <= c <= 102) or (65 <= c <= 70) for c in raw
    )


def parse_aes_key(value: Any) -> bytes:
    """把 aes_key 收敛成 16 字节。四种写法都认,认不出就明确报错。"""
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    else:
        raw = str(value or "").encode("utf-8")
    if len(raw) == 16:
        return raw
    if len(raw) == 32 and _is_hex(raw):
        return bytes.fromhex(raw.decode("ascii"))
    for altchars in (None, b"-_"):
        text = raw.strip()
        padded = text + b"=" * ((-len(text)) % 4)
        try:
            decoded = base64.b64decode(padded, altchars=altchars, validate=True)
        except Exception:  # noqa: BLE001 - 换一种编码接着试
            continue
        if len(decoded) == 32 and _is_hex(decoded):
            return bytes.fromhex(decoded.decode("ascii"))
        if len(decoded) == 16:
            return decoded
    raise MediaError("aes_key 解析不了(len=" + str(len(raw)) + ")")


def encode_aes_key(key: bytes) -> str:
    """出站用的写法:**32 位 hex 字符串再 base64**。

    ⚠️ 这一条错了不会报错,只会让对方看到一个灰框(实测多份实现都踩过)。
    """
    return base64.b64encode(key.hex().encode("ascii")).decode("ascii")


# -- 描述符 ---------------------------------------------------------------
@dataclass
class MediaRef:
    """从一条入站消息里抽出来的"有个媒体要取"的描述。

    ⚠️ 字段名不统一是**协议本身**的样子:image_item 给 `cdn_url/md5/media_id`,
    另一些实现里给 `encrypt_query_param/full_url/aes_key`。所以这里全都要,
    取的时候逐个试,谁有就用谁 —— 猜单一字段名会在真机上静默失败。
    """

    item_type: int
    kind: str
    encrypt_query_param: str = ""
    full_url: str = ""
    cdn_url: str = ""
    aes_key: str = ""
    md5: str = ""
    file_name: str = ""
    file_size: int = 0
    media_id: str = ""
    #: 语音项自带的转写文本(微信服务端已经转好,不用自己做 ASR)。
    #  ⭐ **有它就说明内容已经在正文里了** —— 原声不必下载,更不能当成"待读的附件"
    #  报给模型(2026-10-17 真机:每条语音都回"有个我不认识的附件",见 D139)。
    transcript: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def downloadable(self) -> bool:
        return bool(self.encrypt_query_param or self.full_url or self.cdn_url)

    @property
    def transcribed(self) -> bool:
        """语音内容已经在正文里(渠道替我们转写过)。"""
        return bool(self.transcript.strip())

    def decrypt_key(self) -> bytes | None:
        if not self.aes_key:
            return None
        try:
            return parse_aes_key(self.aes_key)
        except MediaError:
            return None

    def urls(self) -> list[str]:
        """按"最可能成功"排序。

        ⚠️ 顺序在 2026-10-11 **调转过**,原因是真机报文:
        服务端给的 `full_url` 里带着 `&taskid=...`,而我们自己用 `encrypt_query_param`
        拼出来的地址**没有 taskid,实测直接 400**。官方插件的做法与 weixin-ilink SDK 的
        `download_and_decrypt` 也都是**优先 full_url**。
        D82 当时记的是"full_url 在部分类型上解不开",所以两条都留着不删 ——
        调用方本来就是逐个试,谁先成先用。
        """
        found: list[str] = []
        for candidate in (self.full_url, self.cdn_url):
            if candidate and candidate not in found:
                found.append(candidate)
        if self.encrypt_query_param:
            built = (
                CDN_BASE + "/download?encrypted_query_param="
                + quote(self.encrypt_query_param, safe="")
            )
            if built not in found:
                found.append(built)
        return found

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_type": self.item_type,
            "kind": self.kind,
            "file_name": self.file_name,
            "file_size": self.file_size,
            "md5": self.md5,
            "has_key": bool(self.aes_key),
            "urls": len(self.urls()),
            # 只报"有没有转写",不把转写文本塞进来 —— 这段要进事件日志,
            # 而正文里已经有那份文字了,重复记一遍只会让归档变肥。
            "transcribed": self.transcribed,
        }


def _pick(payload: dict[str, Any], *names: str) -> str:
    for name in names:
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def media_refs_from_items(items: list[Any]) -> list[MediaRef]:
    """从 item_list 里抽出所有媒体描述。文本项自然被跳过。"""
    refs: list[MediaRef] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        try:
            item_type = int(item.get("type") or ITEM_TEXT)
        except (TypeError, ValueError):
            continue
        if item_type == ITEM_TEXT:
            continue
        if item_type not in ITEM_KIND:
            # 不认识的内容类型(表情 / 位置 / 名片 / 小程序 / 通话记录…)。
            # ⚠️ 以前照样抽成一条"没有可下载地址"的媒体,于是用户会收到
            #    「⚠️ 有个附件没取回来」—— 又一次"凭空多出一个附件"(与 D139 同一个坑)。
            #    看不清的东西**当成没发**,只在日志里留个类型号,不拿它去打扰人。
            continue
        field_name = ITEM_MEDIA_FIELD.get(item_type)
        payload = item.get(field_name) if field_name else None
        if not isinstance(payload, dict):
            # 兜底:有些实现把媒体字段直接平铺在 item 上
            payload = item
        # ⚠️ 真机上媒体字段是**嵌在 media 里**的:
        #     image_item.media.{encrypt_query_param, aes_key, full_url}
        # 2026-10-11 之前只认"扁平写法"(字段直接挂在 image_item 上),于是手机发来的
        # 每一张照片都报"这条媒体没有可下载的地址" —— 见 data/logs/wechat-raw-*.json
        # 里那份真机报文。扁平写法是早期第三方实现的描述,留着做兼容,不删。
        inner = payload.get("media")
        if not isinstance(inner, dict):
            inner = {}
        # ⭐ 语音的转写文本挂在 voice_item.text 上(**不在 media 里**,别去 inner 找)。
        #    有这段文字 = 这句话已经以正文的形式进了这一轮,音频只是原件。
        transcript = _pick(payload, "text") if item_type == ITEM_VOICE else ""
        ref = MediaRef(
            item_type=item_type,
            kind=ITEM_KIND.get(item_type, "file"),
            encrypt_query_param=(
                _pick(inner, "encrypt_query_param", "encrypted_query_param")
                or _pick(payload, "encrypt_query_param", "encrypted_query_param")
            ),
            full_url=_pick(inner, "full_url") or _pick(payload, "full_url"),
            cdn_url=_pick(inner, "cdn_url", "url") or _pick(payload, "cdn_url", "url"),
            # media.aes_key 是 base64;image_item.aeskey(顶层)是 hex —— 两种都认,
            # parse_aes_key 本来就支持这两种写法。
            aes_key=_pick(inner, "aes_key", "aeskey") or _pick(payload, "aes_key", "aeskey"),
            md5=_pick(payload, "md5"),
            file_name=_pick(payload, "file_name", "filename", "name"),
            file_size=int(payload.get("file_size") or payload.get("rawsize") or 0),
            media_id=_pick(payload, "media_id"),
            transcript=transcript,
            raw=dict(payload),
        )
        refs.append(ref)
    return refs


# -- 网络 ----------------------------------------------------------------
async def download_media(
    ref: MediaRef,
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float = 120.0,
) -> bytes:
    """下载并(必要时)解密。返回明文图片/文件字节。"""
    if not ref.downloadable:
        raise MediaError("这条媒体没有可下载的地址(只有 media_id 或字段缺失)")
    own_client = client is None
    http = client or httpx.AsyncClient(timeout=timeout, follow_redirects=True)
    key = ref.decrypt_key()
    errors: list[str] = []
    try:
        for url in ref.urls():
            try:
                # ⚠️ 边下边计数(2026-10-18 审计修):以前是 http.get() 把整包读进内存
                #    之后才比 MAX_MEDIA_BYTES —— 上限等于没有,一个异常大的文件就能把
                #    常驻的桥接进程打爆。先看 Content-Length,再看流式累计。
                async with http.stream("GET", url) as response:
                    if response.status_code != 200:
                        errors.append(
                            "HTTP " + str(response.status_code) + " @ " + url[:60]
                        )
                        continue
                    declared = response.headers.get("content-length") or "0"
                    try:
                        if int(declared) > MAX_MEDIA_BYTES:
                            raise MediaError(
                                "媒体超过大小上限("
                                + str(MAX_MEDIA_BYTES // (1 << 20)) + "MB),没有下载"
                            )
                    except ValueError:
                        pass
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_MEDIA_BYTES:
                            raise MediaError(
                                "媒体超过大小上限("
                                + str(MAX_MEDIA_BYTES // (1 << 20)) + "MB),已中断下载"
                            )
                        chunks.append(chunk)
                    raw = b"".join(chunks)
            except MediaError:
                raise
            except Exception as exc:  # noqa: BLE001 - 换下一个地址接着试
                errors.append(type(exc).__name__ + " @ " + url[:60])
                continue
            # ⭐ 三种可能,谁先"看着像一个真文件"就用谁:
            #    ① 这份媒体是加密的 -> 解密结果应该能认出文件头;
            #    ② 这份媒体本来就是明文 -> 原始字节就能认出来;
            #    ③ 认不出来(冷门格式)-> 有密钥就信解密结果,否则给原始字节。
            #    这层兜底不是画蛇添足:真机上字段名与加密与否在几种实现之间并不统一。
            candidates: list[bytes] = []
            if key is not None:
                try:
                    candidates.append(aes_ecb_decrypt(raw, key))
                except Exception as exc:  # noqa: BLE001
                    errors.append("解密失败:" + str(exc)[:50])
            candidates.append(raw)
            for candidate in candidates:
                if sniff_kind(candidate) != "unknown":
                    return candidate
            if len(candidates) > 1:
                return candidates[0]      # 有密钥就信解密结果
            return raw
        raise MediaError("媒体取不回来:" + "; ".join(errors[:3]))
    finally:
        if own_client:
            await http.aclose()


def sniff_kind(data: bytes) -> str:
    """按魔数认一下"这看着像什么"。只用来判断"要不要再解密 / 这份字节是不是完整文件"。"""
    if not data:
        return "unknown"
    if data.startswith(b"\xff\xd8\xff"):
        return "image"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image"
    if data.startswith(b"GIF8"):
        return "image"
    if data.startswith(b"BM") and len(data) > 14:
        return "image"
    if data.startswith(b"%PDF"):
        return "file"
    if data.startswith(b"PK\x03\x04"):
        return "file"      # docx/xlsx/pptx 都是 zip
    if data.startswith(b"\xd0\xcf\x11\xe0"):
        return "file"      # 旧版 doc/xls
    # 微信语音的两种容器(都不是标准 MP3):AMR 与 SILK。
    # ⚠️ 认出来只是为了"解密结果是不是个真文件"这一层判断,以及素材列表上的标签 ——
    #    这一层不转写,音频内容本来就读不出来(D139)。
    if data.startswith(b"#!AMR"):
        return "audio"
    if b"SILK" in data[:16]:
        return "audio"
    # 文本兜底:能整段按 UTF-8 解出来、且没有控制字符,就当成文本
    sample = data[:4096]
    try:
        text = sample.decode("utf-8")
    except UnicodeDecodeError:
        return "unknown"
    if all(char.isprintable() or char in "\r\n\t" for char in text):
        return "text"
    return "unknown"


async def upload_media(
    get_upload_url,
    http_client: httpx.AsyncClient,
    *,
    data: bytes,
    item_type: int,
    to_user_id: str,
) -> dict[str, str]:
    """加密 + 要上传地址 + 传到 CDN。返回可直接塞进 sendmessage 的媒体描述。

    ⚠️ 顺序是刻意的:**先上传(易失败、用户看不见),成功之后再发消息**。
    反过来的话,上传重试会让对方收到两条配文 + 一个灰框。

    ⚠️ `get_upload_url` 的签名**就是** `ILinkClient.get_upload_url(body)`:只收请求体,
    端点由回调自己负责。这里一度写成 `(path, body)` 两个参数,而真实客户端只有一个 ——
    测试里的假回调照着错签名写,于是**测试全绿、真机上 100% 抛 TypeError**。
    真机实测过一次才暴露(见 D85)。别再往这一层传路径。
    """
    if len(data) > MAX_MEDIA_BYTES:
        raise MediaError("文件超过 " + str(MAX_MEDIA_BYTES // (1 << 20)) + "MB,发不出去")
    key = os.urandom(16)
    filekey = os.urandom(16).hex()
    cipher = aes_ecb_encrypt(data, key)
    response = await get_upload_url(
        {
            "filekey": filekey,
            "media_type": UPLOAD_TYPE.get(item_type, UPLOAD_FILE),
            "to_user_id": to_user_id,
            "rawsize": len(data),
            "rawfilemd5": hashlib.md5(data).hexdigest(),  # noqa: S324 - 协议要求,不是安全用途
            "filesize": len(cipher),
            "no_need_thumb": True,
            "aeskey": key.hex(),
            "base_info": {"channel_version": "1.0.2"},
        }
    )
    if response.get("ret") not in (0, None):
        raise MediaError("getuploadurl 失败: " + str(response)[:200])
    upload_url = str(response.get("upload_full_url") or "").strip()
    if not upload_url:
        param = str(response.get("upload_param") or "").strip()
        if not param:
            raise MediaError("getuploadurl 没给上传地址: " + str(response)[:200])
        upload_url = (
            CDN_BASE + "/upload?encrypted_query_param=" + quote(param, safe="")
            + "&filekey=" + quote(filekey, safe="")
        )
    last_error = ""
    for attempt in range(3):
        try:
            sent = await http_client.post(
                upload_url, content=cipher,
                headers={"Content-Type": "application/octet-stream"},
                timeout=300.0,
            )
            if sent.status_code != 200:
                raise MediaError("CDN 上传返回 " + str(sent.status_code))
            download_param = sent.headers.get("x-encrypted-param") or ""
            if not download_param:
                raise MediaError("CDN 响应缺少 x-encrypted-param")
            return {
                "encrypt_query_param": download_param,
                "aes_key": encode_aes_key(key),
                "size": str(len(cipher)),
                "raw_size": str(len(data)),
            }
        except Exception as exc:  # noqa: BLE001 - 网络抖动重试三次
            last_error = type(exc).__name__ + ": " + str(exc)[:120]
            # ⚠️ 必须是 await 的 sleep:这里是常驻服务的事件循环。原来的 time.sleep
            #    会把整个微信渠道**阻塞住**(重试三次最坏 4.5 秒,期间长轮询与别的会话全停)。
            await asyncio.sleep(1.5 * (attempt + 1))
    raise MediaError("CDN 上传失败: " + last_error)


#: CDN 媒体的加密方式。协议里是个显式字段,**不发它对方解不开**。
ENCRYPT_TYPE_AES_ECB = 1

#: 每种媒体各自的大小字段名 —— 名字和类型都不一样,这是协议本来的样子:
#:   图片 mid_size / 视频 video_size 是**密文**字节数(int);
#:   文件 len 是**明文**字节数,而且是**字符串**。
ITEM_SIZE_FIELD = {ITEM_IMAGE: "mid_size", ITEM_VIDEO: "video_size"}


def build_media_item(item_type: int, media: dict[str, str]) -> dict[str, Any]:
    """拼出 sendmessage 里的媒体项。

    ⚠️ **媒体字段必须嵌在 `media` 里**。这一条错了的代价是"整条消息被服务端拒掉、
    而且用户什么都看不到" —— 我们就是这么踩的(2026-10-11,见 D87)。

    形状以三份独立来源交叉确认为准(真机入站报文 + 官方插件源码分析 +
    weixin-ilink SDK 的 file_item/image_item 构造):

        {"type": 4, "file_item": {
            "media": {"encrypt_query_param": .., "aes_key": base64(hex), "encrypt_type": 1},
            "file_name": "周报.docx",
            "len": "39751",                    # 明文大小,**字符串**
        }}

    而图片是 mid_size(密文,int)。别再改回"扁平写法"。
    """
    field_name = ITEM_MEDIA_FIELD.get(item_type, "file_item")
    payload: dict[str, Any] = {
        "media": {
            "encrypt_query_param": media.get("encrypt_query_param", ""),
            "aes_key": media.get("aes_key", ""),
            "encrypt_type": ENCRYPT_TYPE_AES_ECB,
        }
    }
    name = str(media.get("file_name") or "")
    if name:
        payload["file_name"] = name
    if item_type == ITEM_FILE:
        # 文件:明文大小,字符串
        raw = media.get("raw_size") or media.get("size") or ""
        if raw != "":
            payload["len"] = str(raw)
    else:
        # 图片/视频:密文大小,int
        cipher_size = media.get("size")
        if cipher_size not in (None, ""):
            payload[ITEM_SIZE_FIELD.get(item_type, "mid_size")] = int(cipher_size)
    return {"type": item_type, field_name: payload}
