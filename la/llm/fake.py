"""离线假客户端:无密钥也能跑通端到端验证与单元测试。

它不是玩具:
  * M0 的验收(运行时上下文每轮注入)在没有 API 密钥时必须可重复验证;
  * M1 的验收(S1 记住偏好、S2 重启后仍记得)同样先用它跑通;
  * M4 起它还能**发出工具调用**(tool_script),所以"模型调工具"这条链路也能离线验收。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence  # noqa: F401  (Sequence 给协议签名用)

from .client import ChatResult, ToolCall, Usage

#: 事实准入裁决的暗号(定义在 la/memory/fact_adjudicator.py)。
#: ⚠️ 从这里 import 会绕成环(store -> config,memory -> llm),所以**复制一份常量**,
#:    并在测试里断言两边一致(tests/test_m14_fact_gate.py)。
PROMPT_MARKER = "【事实准入裁决】"

_QUESTION_HINTS = ("什么", "谁", "哪", "多少", "吗", "?", "？", "几")

_EXISTING_RE = re.compile(r"^#(\d+) \[([^\]]+)\] (.+)$", re.M)
_CANDIDATE_RE = re.compile(r"待审候选:\n(.+?)(?:\n\n|$)", re.S)


def _normalize(text: str) -> str:
    cleaned = re.sub(r"[\s,。，.;；;!！?？、:：]", "", str(text or ""))
    return cleaned.removeprefix("用户")


def _fake_fact_verdict(prompt: str) -> str:
    """离线裁决:完全同名 -> duplicate;一个是另一个的真子串 -> refines;否则 new。

    ⚠️ 这里**故意不做语义判断** —— 它是确定性替身,不是模型。真正的语义判断
       (尤其"喜欢/不喜欢"这种极性相反的情况)由真人模型做,并且
       `fact_adjudicator._police_polarity` 还会兜一道。
    """
    body = _CANDIDATE_RE.search(prompt)
    candidate = _normalize(body.group(1)) if body else ""
    if not candidate:
        return '{"relationship":"review"}'
    for fact_id, _kind, content in _EXISTING_RE.findall(prompt):
        known = _normalize(content)
        if not known:
            continue
        if known == candidate:
            return '{"relationship":"duplicate","target_id":' + fact_id + "}"
        if known in candidate or candidate in known:
            # "细化"后的说法取**候选**(它更完整),不是取旧的那条
            merged = body.group(1).strip().replace('"', "'") if body else candidate
            return (
                '{"relationship":"refines","target_id":' + fact_id
                + ',"text":"' + merged + '"}'
            )
    return '{"relationship":"new"}'


@dataclass
class FakeClient:
    """确定性回复器。

    scripted     —— 按脚本依次返回纯文本回复;
    tool_script  —— 按脚本依次返回**工具调用**;用完后回落到普通回复。
    """

    model: str = "fake-echo"
    scripted: list[str] = field(default_factory=list)
    tool_script: list[list[ToolCall]] = field(default_factory=list)
    # 思考脚本:与 scripted 一一对应(缺省则没有思考)。前端与验收脚本靠它验证流式思考。
    reasoning_script: list[str] = field(default_factory=list)
    calls: list[list[dict[str, Any]]] = field(default_factory=list)
    tool_catalogs: list[list[dict[str, Any]]] = field(default_factory=list)

    async def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        on_delta: Callable[[str], None] | None = None,
        on_reasoning: Callable[[str], None] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        reasoning_effort: str | None = None,  # noqa: ARG002 - 假客户端不看思考档位
    ) -> ChatResult:
        snapshot = [dict(message) for message in messages]
        self.calls.append(snapshot)
        self.tool_catalogs.append([dict(item) for item in (tools or [])])

        prompt_chars = sum(len(str(m.get("content") or "")) for m in snapshot)
        usage = Usage(
            prompt_tokens=prompt_chars // 4 + 4,
            completion_tokens=8,
            total_tokens=prompt_chars // 4 + 12,
        )

        # 离线也走一遍"思考 -> 正文":这么写是为了让流式思考的链路**无密钥也能验收**
        thought = self.reasoning_script.pop(0) if self.reasoning_script else ""
        if thought and on_reasoning is not None:
            for character in thought:
                on_reasoning(character)

        if self.tool_script:
            calls = self.tool_script.pop(0)
            return ChatResult(
                text="",
                reasoning=thought,
                model=self.model,
                finish_reason="tool_calls",
                usage=usage,
                tool_calls=list(calls),
            )

        text = self.scripted.pop(0) if self.scripted else self._compose(snapshot)
        if on_delta is not None:
            for character in text:
                on_delta(character)
        usage.completion_tokens = len(text) // 4 + 4
        usage.total_tokens = usage.prompt_tokens + usage.completion_tokens
        return ChatResult(
            text=text, reasoning=thought, model=self.model,
            finish_reason="stop", usage=usage,
        )

    def _compose(self, messages: Sequence[dict[str, Any]]) -> str:
        system = "\n".join(
            str(m.get("content") or "") for m in messages if m.get("role") == "system"
        )
        user = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                user = str(message.get("content") or "")
                break

        # 事实准入裁决:离线也要走**同一条**链路,否则 fake 模式下测的是另一套代码。
        # 判决由确定性规则给出(完全相同 -> duplicate;一个是另一个的子串 -> refines)。
        if PROMPT_MARKER in user:
            return _fake_fact_verdict(user)

        # 工具结果已经回来了:直接复述它(离线模式下这就是"模型读到了工具输出")
        tool_outputs = [m for m in messages if m.get("role") == "tool"]
        if tool_outputs:
            latest = str(tool_outputs[-1].get("content") or "")
            return "工具返回:" + latest[:200]

        evidence_lines = re.findall(r"^- \[([^\]]+)\] \(([^)]+)\) (.+)$", system, re.M)
        conflict = re.search(r"<conflict-note>([^<]+)</conflict-note>", system)

        if "<evidence-status>found</evidence-status>" in system:
            parts = []
            if evidence_lines:
                timestamp, _source, text = evidence_lines[-1]
                parts.append("我查到的记录是(" + timestamp + "):" + text)
            if conflict:
                parts.append(conflict.group(1))
            if parts:
                return "\n".join(parts)

        if any(keyword in user for keyword in ("几点", "今天几号")):
            match = re.search(r"当前时间:\s*([^\n]+)", system)
            return "现在是 " + match.group(1).strip() + "。" if match else "我看不到当前时间。"

        if any(hint in user for hint in _QUESTION_HINTS):
            if "<memory-status>hit</memory-status>" in system:
                hot = re.search(r"<hot-memories>\n-\s*([^\n]+)", system)
                if hot:
                    return "根据记忆:" + hot.group(1).strip()
                fact = re.search(r"<facts>\n-\s*([^\n]+)", system)
                if fact:
                    return "我记得:" + fact.group(1).strip()
                return "我检索到了相关记忆,但内容为空。"
            if "<evidence-status>none</evidence-status>" in system:
                return "记忆里没有相关记录,我翻过档案了。"
            if "<memory-status>miss</memory-status>" in system:
                return "记忆里没有关于这件事的记录。"

        if "记住" in user or "记一下" in user:
            return "好的,我记下了。"

        return "(离线假客户端)收到:" + user
