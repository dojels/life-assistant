"""M0 时代的 Agent 主循环(⚠️ 生产路径已不用它)。

生产入口(CLI / Web / 微信)全部走 agent/session.py 的 MemorySession:它带阶段门禁、
工具、记忆与压缩。AgentLoop 只保留给 test_m0 当"最小回路"的基线,以及离线脚本
对比"加了记忆之后差在哪"。**不要在新代码里用它** —— 它没有工具、没有记忆、
没有回执,行为与线上不一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .. import config
from ..config import Settings
from ..llm.client import ChatResult, LLMClient, Usage
from .prompts import build_system_message


@dataclass
class TurnRecord:
    index: int
    user_text: str
    assistant_text: str
    projected_tokens: int
    projected_messages: int
    usage: Usage = field(default_factory=Usage)


class AgentLoop:
    """单主循环 + 单一工具命名空间(能力域 §10.8:不要给每个域派子代理)。"""

    def __init__(
        self,
        client: LLMClient,
        settings: Settings,
        *,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.client = client
        self.settings = settings
        self.history: list[dict[str, str]] = []
        self.turns: list[TurnRecord] = []
        self.turn_index = 0
        self.total_usage = Usage()
        self.last_active: str | None = None
        self._on_event = on_event

    # -- 事件(前端"执行可见性"的挂钩点,见能力域 §6.7.3)------------------
    def emit(self, kind: str, **data: Any) -> None:
        if self._on_event is not None:
            self._on_event(kind, data)

    # -- 投影装配(M0 版)--------------------------------------------------
    def build_messages(self) -> list[dict[str, str]]:
        """system(含运行时上下文) + 最近对话。M1 会在这里加记忆热摘要块。"""
        self.turn_index += 1
        system = build_system_message(
            self.settings, turn=self.turn_index, last_active=self.last_active
        )
        return [system, *self._trim_history()]

    def _trim_history(self) -> list[dict[str, str]]:
        """按 BUDGET_CONVERSATION 裁剪最近的对话投影。

        成对丢弃,避免留下孤立的 assistant 消息(真正的配对裁剪在 M1 的投影器里做)。
        """
        kept = list(self.history[-config.MAX_HISTORY_MESSAGES:])
        while kept and config.estimate_messages_tokens(kept) > config.BUDGET_CONVERSATION:
            if len(kept) >= 2:
                del kept[:2]
            else:
                kept.clear()
        return kept

    def projection_preview(self) -> tuple[list[dict[str, str]], int]:
        """给 /context 用:算一遍投影,但不推进轮次计数。"""
        system = build_system_message(
            self.settings, turn=self.turn_index + 1, last_active=self.last_active
        )
        messages = [system, *self._trim_history()]
        return messages, config.estimate_messages_tokens(messages)

    # -- 一轮对话 ---------------------------------------------------------
    async def respond(
        self,
        user_text: str,
        *,
        on_delta: Callable[[str], None] | None = None,
    ) -> ChatResult:
        previous_active = self.last_active
        self.history.append({"role": "user", "content": user_text})

        messages = self.build_messages()
        projected_tokens = config.estimate_messages_tokens(messages)
        self.emit(
            "projection",
            messages=len(messages),
            tokens=projected_tokens,
            budget=config.BUDGET_CONVERSATION,
        )

        result = await self.client.chat(messages, on_delta=on_delta)

        self.history.append({"role": "assistant", "content": result.text})
        self.last_active = config.format_local(self.settings.now())
        self.total_usage = self.total_usage.merge(result.usage)
        self.turns.append(
            TurnRecord(
                index=self.turn_index,
                user_text=user_text,
                assistant_text=result.text,
                projected_tokens=projected_tokens,
                projected_messages=len(messages),
                usage=result.usage,
            )
        )
        self.emit("usage", **result.usage.as_dict())
        del previous_active  # 保留给 M1 的记忆层
        return result

    def reset(self) -> None:
        self.history.clear()
        self.turns.clear()
        self.turn_index = 0
