"""命令行 REPL。

它是命令行界面,也是长期存在的"逃生通道":
即使以后 Web 界面出问题,CLI 也必须能读写数据。
"""

from __future__ import annotations

import asyncio
import sys
from typing import Callable

from typing import Any

from .. import config

BANNER = """家庭助手 · 命令行
命令: /help 帮助 · /context 查看本轮投影 · /usage 用量 · /clear 清空对话 · /exit 退出
离线模式:把 LA_LLM_PROVIDER 设为 fake 即可在没有密钥时使用。
"""

HELP = """/help     显示本帮助
/context  打印本轮将发给模型的投影(角色、token 估算、前 80 字预览)
/usage    累计 token 用量(含 KV 前缀缓存命中数)
/clear    清空当前对话历史(不影响落盘数据)
/exit     退出
"""


def configure_stdio() -> None:
    """Windows 控制台兜底:编码不换,只把不可编码字符替换掉,避免中文崩栈。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


class CLI:
    def __init__(self, loop: Any, *, out: Callable[[str], None] = print) -> None:
        # ⚠️ 生产传进来的是 MemorySession(带阶段/工具/记忆),不是 M0 的 AgentLoop。
        #    这里刻意用 Any:CLI 只依赖 respond/projection_preview/total_usage/reset
        #    这四件事,两代实现都满足 —— 以前标的 AgentLoop 是错的。

        self.loop = loop
        self.out = out
        self._streaming = False

    def _make_delta_printer(self, prefix: str) -> Callable[[str], None]:
        state = {"started": False}

        def on_delta(piece: str) -> None:
            if not state["started"]:
                sys.stdout.write(prefix)
                state["started"] = True
            sys.stdout.write(piece)
            sys.stdout.flush()

        return on_delta

    async def run_once(
        self,
        text: str,
        *,
        echo_user: bool = True,
        prefix: str = "助手: ",
    ) -> str:
        if echo_user:
            self.out("你: " + text)
        result = await self.loop.respond(text, on_delta=self._make_delta_printer(prefix))
        if prefix:
            sys.stdout.write("\n")
            sys.stdout.flush()
        return result.text

    async def repl(self) -> int:
        # configure_stdio() 已由入口(__main__.main)调用一次;这里不再重复。
        self.out(BANNER)
        while True:
            try:
                line = await asyncio.to_thread(input, "你: ")
            except (EOFError, KeyboardInterrupt):
                self.out("")
                break
            line = line.strip()
            if not line:
                continue
            if line.startswith("/"):
                if self._handle_command(line):
                    break
                continue
            try:
                await self.run_once(line, echo_user=False)
            except Exception as exc:  # 一轮失败不该让整个进程退出
                self.out("[出错] " + type(exc).__name__ + ": " + str(exc))
            self.out("")
        return 0

    # -- 命令 -------------------------------------------------------------
    def _handle_command(self, line: str) -> bool:
        command = line.split()[0].lower()
        if command in ("/exit", "/quit", "/q"):
            return True
        if command == "/help":
            self.out(HELP)
        elif command == "/context":
            self._print_projection()
        elif command == "/usage":
            self.out(str(self.loop.total_usage.as_dict()))
        elif command == "/clear":
            self.loop.reset()
            self.out("对话历史已清空。")
        else:
            self.out("未知命令:" + command + "(用 /help 查看)")
        return False

    def _print_projection(self) -> None:
        # 只预览,不推进轮次计数
        messages, total = self.loop.projection_preview()
        self.out("—— 本轮投影(" + str(len(messages)) + " 条,约 " + str(total)
                 + " token,压缩触发 " + str(config.BUDGET_COMPACT_TRIGGER) + ")——")
        for index, message in enumerate(messages):
            content = str(message.get("content") or "").replace("\n", " ")
            preview = content[:80] + ("…" if len(content) > 80 else "")
            self.out(
                "  [" + str(index) + "] " + str(message.get("role")).ljust(9)
                + " " + str(config.estimate_tokens(content)).rjust(5) + " tok  " + preview
            )