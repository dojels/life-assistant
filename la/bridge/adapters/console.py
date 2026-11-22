"""终端适配器:用来验证桥接层本身,也是断网时的逃生通道。

它证明了一件重要的事:**桥接层确实与微信无关** ——
换成微信,要改的只有这个文件(150 行左右),bridge.py 一行都不用动。
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from ..bridge import Bridge, render_for_chat
from ..types import Attachment, Inbound, SendAction

BANNER = """桥接层 · 终端适配器
直接打字就是"从渠道来的一条消息";它会走和微信完全相同的桥接路径。
  /img 照片.png   假装用户发来一张照片(走一遍"拍照录错题"那条链路)
  退出: Ctrl+C 或输入 /exit
"""


class ConsoleTransport:
    """把出站动作打到终端,并在末尾画一条分隔线。"""

    name = "console"

    def __init__(self) -> None:
        self.sent: list[SendAction] = []
        self._lock = asyncio.Lock()

    async def send(self, action: SendAction) -> None:
        async with self._lock:  # 串行写,避免两条消息的字混在一起
            self.sent.append(action)
            sys.stdout.write("\n" + action.text + "\n\n")
            sys.stdout.flush()

    async def typing(self, thread_id: str) -> None:
        sys.stdout.write("· ")
        sys.stdout.flush()


class ConsoleAdapter:
    def __init__(self, *, thread_id: str = "console", channel: str = "console") -> None:
        self.channel = channel
        self.thread_id = thread_id
        self.transport = ConsoleTransport()
        self.bridge = Bridge(transport=self.transport)
        #: 正在处理的消息(一条一个任务)。理由同微信适配器:提问回环要求
        #: "下一行"能被读到 —— 串行 await 会让那一行永远排不到。
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> None:
        """把一条消息派成独立任务;异常要响,不能静默。"""
        task = asyncio.create_task(coro)
        self._tasks.add(task)

        def _done(item: asyncio.Task) -> None:
            self._tasks.discard(item)
            if item.cancelled():
                return
            error = item.exception()
            if error is not None:
                sys.stdout.write("\n这条消息处理失败:" + type(error).__name__
                                 + " " + str(error)[:160] + "\n")
                sys.stdout.flush()

        task.add_done_callback(_done)

    async def _on_message(self, text: str, seq: int) -> None:
        # `/img 路径` = "假装用户发来一张照片"。终端里也能走一遍手机那条链路
        # (素材层 -> 视觉识别 -> 直接录入),不必真去掏手机。
        attachments: list[Attachment] = []
        body = text
        if text.lower().startswith("/img "):
            from pathlib import Path

            target = Path(text[5:].strip().strip('"'))
            body = ""
            if not target.exists():
                sys.stdout.write("找不到图片:" + str(target) + "\n")
                sys.stdout.flush()
                return
            from ...domain.media import sniff_media_type

            data = target.read_bytes()
            attachments.append(
                Attachment(
                    kind="image", data=data, name=target.name,
                    media_type=sniff_media_type(data),
                    external_id="console:" + str(seq) + ":0",
                )
            )
        inbound = Inbound(
            channel=self.channel,
            user_id=self.thread_id,
            thread_id=self.thread_id,
            text=body,
            external_id=self.channel + ":" + str(seq),
            attachments=attachments,
        )
        thread = self.bridge.thread(self.thread_id)
        events: list[dict[str, Any]] = []
        async for event in thread.handle(inbound):
            events.append(event)
        body = render_for_chat(events)
        # ⭐ 终端没有"发文件"这回事,就把路径打出来。顺带证明了产物事件**不依赖微信**
        #    (桥接层与渠道无关那条纪律,一在这里,一在微信那侧)。
        files = [event["data"] for event in events if event.get("event") == "file"]
        if files:
            listing = "\n".join(
                "📎 " + str(item.get("name") or "") + "  " + str(item.get("path") or "")
                for item in files
            )
            body = (body + "\n\n" if body else "") + listing
        if body:
            await self.transport.send(SendAction(text=body))

    async def serve(self) -> int:
        sys.stdout.write(BANNER)
        sys.stdout.flush()
        seq = 0
        while True:
            try:
                line = await asyncio.to_thread(input, "你: ")
            except (EOFError, KeyboardInterrupt):
                sys.stdout.write("\n")
                break
            line = line.strip()
            if line in ("/exit", "/quit", "/q"):
                break
            if not line:
                continue
            seq += 1
            # ⚠️ 不 await:见 _spawn。回答提问的那一行必须能被读到。
            self._spawn(self._on_message(line, seq))
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        self.bridge.close()
        return 0


async def run_console() -> int:
    return await ConsoleAdapter().serve()
