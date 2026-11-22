"""看图:校验 + 尺寸 + 交给视觉模型(M12)。

三条硬约束(都有出处,别改):
  1. **类型按魔数判**,不信后缀(la/base/sniff.py);后缀与内容不符时以**内容**为准;
  2. **能力闸失败关闭**:拿不到"这个模型支持图片输入"的明确声明,就走描述式兜底,
     **绝不静默丢图**(云朵 turn_images.py 的边界:把图片块发给不认它的端点必然 400);
  3. **尺寸只按头部读**,不引 Pillow —— 我们只需要宽高来判断"是不是超限"。

⚠️ DeepSeek 视觉接口的既有事实(见执行计划 §M4.3b / D76):图片会被缩到约 1300×1300、
   单图约 1024 tokens。所以**整页小字、公式、表格**别指望看图 —— 那要走 file(action=read) 抽文本。
"""

from __future__ import annotations

import struct

from .. import config
from .sniff import sniff_media_type

#: 认得的图片类型(与 DSH ImageMediaType 同一份清单)
IMAGE_MEDIA_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")


def image_dimensions(data: bytes, media_type: str) -> tuple[int, int] | None:
    """从文件头读宽高。读不出返回 None(**不要瞎猜**,后面按"无法校验"处理)。"""
    try:
        if media_type == "image/png" and len(data) >= 24:
            return struct.unpack(">II", data[16:24])
        if media_type == "image/gif" and len(data) >= 10:
            return struct.unpack("<HH", data[6:10])
        if media_type == "image/webp":
            return _webp_size(data)
        if media_type == "image/jpeg":
            return _jpeg_size(data)
    except (struct.error, IndexError, ValueError):
        return None
    return None


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    """扫 JPEG 段,找 SOFn(0xC0-0xCF 里除 C4/C8/CC 之外的那些)。"""
    index = 2
    end = len(data)
    while index + 9 < end:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        if index + 4 > end:
            return None
        length = struct.unpack(">H", data[index + 2:index + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[index + 5:index + 9])
            return int(width), int(height)
        index += 2 + max(length, 2)
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    chunk = data[12:16]
    if chunk == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if chunk == b"VP8 ":
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width, height
    if chunk == b"VP8L":
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


def check_image(data: bytes, *, name: str = "") -> tuple[str, str | None]:
    """校验一张图。返回 (media_type, 拒绝原因或 None)。

    ⚠️ 拒绝理由要说清**数字**(多少 MB、多少像素),而不是一句"太大了"。
    """
    if not data:
        return "", "这张图是空的"
    media_type = sniff_media_type(data)
    if media_type not in IMAGE_MEDIA_TYPES:
        suffix = ("(文件名是 " + name + ",但内容不是图片)") if name else ""
        return media_type, "这不是" + "图片" + suffix + " —— 按内容判断,它更像别的格式"
    if len(data) > config.IMAGE_MAX_BYTES:
        return media_type, ("图太大:" + str(round(len(data) / (1 << 20), 1)) + "MB,上限 "
                            + str(config.IMAGE_MAX_BYTES // (1 << 20)) + "MB")
    size = image_dimensions(data, media_type)
    if size is None:
        return media_type, ""
    width, height = size
    if width > config.IMAGE_MAX_DIMENSION or height > config.IMAGE_MAX_DIMENSION:
        return media_type, ("图太大:" + str(width) + "×" + str(height) + ",单边上限 "
                            + str(config.IMAGE_MAX_DIMENSION) + "px")
    if width * height > config.IMAGE_MAX_PIXELS:
        return media_type, ("图太大:" + str(width) + "×" + str(height) + " 超过 "
                            + str(config.IMAGE_MAX_PIXELS // 1_000_000) + " 万像素上限")
    return media_type, ""
