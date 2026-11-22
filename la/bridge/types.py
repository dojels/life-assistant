"""桥接层的公共类型。

这一层的职责只有一句话:**把"某个渠道来的一条消息"变成一轮 Agent 对话,
再把这一轮产生的"回复 + 写入回执 + 产物"交回渠道。**

它不认识微信,也不认识 httpx —— 渠道细节全部落在 adapters/ 里。
这么做是为了让"接微信"这件事退化成一个适配器,而不是又一次改动 Agent 内核。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class Attachment:
    """渠道 -> 助手。一条消息里带的媒体(照片 / 文件 / 语音)。

    ⚠️ 与 Inbound.text 的关系是**并列**而不是替代:微信允许"一张图 + 一句话",
    也可能只发一张图(那时 text 是空的)。桥接层必须两种都收。
    """

    #: image | file | voice
    kind: str = "file"
    #: 已经取回来的字节。取不回来时是 None,而 error 里会说明为什么
    data: bytes | None = None
    name: str | None = None
    media_type: str | None = None
    #: 渠道内这条附件自己的 id(下载后的内容哈希 / 消息里的 media_id),用于幂等
    external_id: str | None = None
    #: 取不回来时如实说明原因 —— 界面与回话都要用,不允许静默丢失
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.data) and not self.error

    def as_dict(self) -> dict[str, Any]:
        """给日志/事件用的摘要。**不含字节** —— 塞进事件里会把归档撑爆。"""
        return {
            "kind": self.kind,
            "name": self.name,
            "media_type": self.media_type,
            "bytes": len(self.data) if self.data else 0,
            "external_id": self.external_id,
            "error": self.error,
        }


@dataclass
class OutboundFile:
    """助手 -> 渠道。要发出去的一个文件(产物层的东西)。"""

    path: str
    #: image | file
    kind: str = "file"
    name: str | None = None
    caption: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "kind": self.kind, "name": self.name,
                "caption": self.caption}


@dataclass
class Inbound:
    """渠道 -> 助手。一条用户消息。"""

    #: 渠道标识:wechat / stdin / webhook ...
    channel: str
    #: 渠道内的会话/联系人标识(微信里就是那个人的 wxid)
    user_id: str
    #: 文本。语音由渠道转写成文字后进来;图片走 attachments(不再假装"图片转成了文字")
    text: str = ""
    #: 同一段对话的键。默认取 user_id;群里可以是 room_id
    thread_id: str = ""
    #: 渠道给的幂等键(微信回调会重试,必须靠它去重)
    external_id: str | None = None
    #: 附件:照片、文件、语音。⭐ 手机上最常用的一条路 —— 拍照直接发过来。
    attachments: list[Attachment] = field(default_factory=list)
    #: 原样保留的渠道原始报文,便于排障
    raw: dict[str, Any] = field(default_factory=dict)

    def key(self) -> str:
        return self.thread_id or self.user_id

    def summary(self) -> str:
        """一句话描述这条消息有什么(事件日志与排障用)。"""
        parts = []
        if self.text.strip():
            parts.append("文本 " + str(len(self.text)) + " 字")
        if self.attachments:
            parts.append("附件 " + str(len(self.attachments)) + " 个")
        return " / ".join(parts) or "(空消息)"


@dataclass
class SendAction:
    """助手 -> 渠道。要发出去的东西。"""

    text: str
    #: 微信侧的"正在输入"。能支持的适配器把它翻成真实动作,不支持的忽略
    typing: bool = False
    #: 引用/回复某条消息(适配器按自身能力决定是否实现)
    reply_to: str | None = None
    #: 要一并发出去的文件(产物层)。不支持的渠道忽略它,正文照发。
    files: list[OutboundFile] = field(default_factory=list)


@runtime_checkable
class Transport(Protocol):
    """渠道适配器要实现的两件事:发消息、显示"正在输入"。"""

    name: str

    async def send(self, action: SendAction) -> None: ...

    async def typing(self, thread_id: str) -> None: ...
