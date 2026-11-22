"""按魔数认"这是什么" —— 基础层里**唯一一份**类型识别实现。

为什么提到这里:图片(素材/视觉)、文件读取、账单导入三处都要认类型。
以前只有 domain/media.py 一份,基础层要用就得反过来 import 域 —— 依赖方向就错了。
现在 domain/media.py 变成 import 这里(方向:域 → 基础)。

⚠️ 一律**按内容**判断,不信文件名后缀(与 D82 微信媒体"解密结果要验货"同一条纪律)。
"""

from __future__ import annotations

import io
import zipfile

#: 容器/格式 → 给用户看的说法(错误信息里用它,不要甩 MIME 类型给用户)
KIND_LABELS = {
    "image": "图片",
    # ⭐ M14d:听力题要靠它。以前只认图片/PDF,于是听写生成的 mp3 被登记成
    #    "application/octet-stream",页面的内联器认不出它是音频 —— **听力题就没声音**。
    "audio": "音频",
    "pdf": "PDF",
    "docx": "Word 文档",
    "xlsx": "Excel 表格",
    "pptx": "PPT 演示文稿",
    "zip": "压缩包",
    "ole": "旧版 Office 文件(doc/xls/ppt)",
    "text": "文本",
    "binary": "二进制文件",
}


def sniff_media_type(data: bytes) -> str:
    """按魔数认 MIME。认不出返回 application/octet-stream(不猜)。"""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data.startswith(b"%PDF"):
        return "application/pdf"
    # --- 音频(听力题)---------------------------------------------------
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data.startswith(b"ID3"):
        return "audio/mpeg"
    # MPEG 帧同步:11 个 1 开头(0xFF 后高 3 位全 1)且层/版本字段合法
    if len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "audio/mpeg"
    if data.startswith(b"OggS"):
        return "audio/ogg"
    if len(data) > 12 and data[4:8] == b"ftyp" and data[8:12] in (b"M4A ", b"mp42", b"isom"):
        return "audio/mp4"
    # 微信语音的两种容器,都不是标准 MP3:AMR(#!AMR)与 SILK(#!SILK_V3,前面可能带一个 0x02)。
    # ⭐ 认出来只为"标签/抽不抽得出文字"这两件事 —— 这两种格式这一版**转写不了**(D139)。
    if data.startswith(b"#!AMR"):
        return "audio/amr"
    if b"SILK" in data[:16]:
        return "audio/silk"
    return "application/octet-stream"


def office_flavor(data: bytes) -> str | None:
    """ZIP 里装的到底是 Word/Excel/PPT:看目录名(docx/xlsx/pptx 都是 zip)。

    认证不出来返回 None —— 那就是个普通压缩包,别硬说成 Office 文件。
    """
    if not data.startswith(b"PK\x03\x04"):
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
    except (zipfile.BadZipFile, OSError):
        return None
    if any(name.startswith("word/") for name in names):
        return "docx"
    if any(name.startswith("xl/") for name in names):
        return "xlsx"
    if any(name.startswith("ppt/") for name in names):
        return "pptx"
    return None


def looks_like_text(data: bytes, *, sample: int = 4096) -> bool:
    """能不能当文本读:没有 NUL 字节、且能按 utf-8 或 gbk 解出来、且没有控制字符。

    ⚠️ 只抽样前 4KB —— 大文件不必为了判断类型全读一遍。
    """
    chunk = data[:sample]
    if not chunk:
        return True
    if b"\x00" in chunk:
        return False
    text = None
    for encoding in ("utf-8", "gbk"):
        try:
            text = chunk.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return False
    return all(char.isprintable() or char in "\r\n\t" for char in text)


def detect_kind(data: bytes) -> str:
    """粗分类:image / pdf / docx / xlsx / pptx / zip / ole / text / binary。

    只用来决定"走哪条路"(看图 / 抽文本 / 拒绝),不用来当安全判据。
    """
    media = sniff_media_type(data)
    if media.startswith("image/"):
        return "image"
    if media.startswith("audio/"):
        return "audio"
    if media == "application/pdf":
        return "pdf"
    flavor = office_flavor(data)
    if flavor:
        return flavor
    if data.startswith(b"PK\x03\x04"):
        return "zip"
    # 旧版 doc/xls/ppt 的 OLE 复合文档魔数
    if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "ole"
    if looks_like_text(data):
        return "text"
    return "binary"


def kind_label(kind: str) -> str:
    return KIND_LABELS.get(kind, kind)
