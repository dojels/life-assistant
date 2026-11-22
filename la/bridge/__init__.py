"""渠道桥接层:让同一个助手出现在微信/终端/任何能收发文本的地方。

**边界**:这一层不做任何"更强的能力",它只做搬运 ——
渠道消息进来 -> 已有的 MemorySession 跑一轮 -> 正文 + 写入回执发回去。
记忆、工具、账本、护栏全部复用,不在这里重写。
"""

from __future__ import annotations

from .bridge import Bridge, Thread, render_for_chat
from .types import Attachment, Inbound, OutboundFile, SendAction, Transport

__all__ = [
    "Attachment",
    "Bridge",
    "Inbound",
    "OutboundFile",
    "SendAction",
    "Thread",
    "Transport",
    "render_for_chat",
]
