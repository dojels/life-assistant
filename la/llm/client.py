"""DeepSeek(OpenAI 兼容)客户端:流式 + 重试 + 用量统计。

为什么不直接用 openai SDK:
  * 我们只需要 /chat/completions 的 SSE,httpx 已经够用,少一层依赖;
  * 重试策略、超时、用量(含 KV 前缀缓存命中数)与假客户端注入都由我们掌控。

⚠️ 两条与 DeepSeek 有关的实测约束(学习路线 §15 第 18、19 条):
  * 带 tools 开 thinking 时,**轮内那条"结果还没被消费"的 assistant 消息必须带
    reasoning_content 字段** —— 缺了 API 直接 400("The reasoning_content in the thinking
    mode must be passed back to the API")。⭐ 但**内容可以是空字符串**:字段"在"就够,
    不必是真实思考;跨轮历史(请求以 user 结尾)则整个字段都可以不带。
    ⭐ 可复现:`scripts/verify_reasoning_replay.py`(真实模型 5 项)。
    ⚠️ 这条以前被记成"必须**完整**回传所有历史 assistant 消息" —— 2026-10-14 实测更正;
  * JSON 模式偶尔返回空内容,必须由代码兜底重试。
"""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence

import httpx

from .. import config


def _payload_tokens(payload: dict[str, Any]) -> int:
    """这次请求大概多少 token(**只用于"值不值得重试"的判断**,不求精确)。

    口径与投影器一致(4 字符 ≈ 1 token):数 messages 的 content + reasoning_content
    + tool_calls 参数 + tools 清单。思考内容也算 —— 真机上它常常是大头
    (云朵实测:12 次请求里思考 136,701 字符,带 tools 时它进 prompt_tokens)。
    """
    chars = 0
    for message in payload.get("messages") or []:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            chars += len(content)
        elif content:
            chars += len(json.dumps(content, ensure_ascii=False, default=str))
        if isinstance(message, dict):
            reasoning = message.get("reasoning_content")
            if isinstance(reasoning, str):
                chars += len(reasoning)
            for call in message.get("tool_calls") or []:
                chars += len(json.dumps(call, ensure_ascii=False, default=str))
    if payload.get("tools"):
        chars += len(json.dumps(payload["tools"], ensure_ascii=False, default=str))
    return chars // 4


class LLMError(RuntimeError):
    """LLM 调用失败的基类。"""


class LLMAuthError(LLMError):
    """密钥缺失或无效 —— 不可重试。"""


class LLMTransportError(LLMError):
    """网络错误 / 429 / 5xx —— 可重试。"""


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    #: 思维链占用的 completion token(**已经包含在 completion_tokens 里**)。
    #: 2026-10-25 加:裁决被截断那次,正是它把 200 的额度吃光了(见 D170)。
    reasoning_tokens: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }

    def merge(self, other: "Usage") -> "Usage":
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cache_hit_tokens=self.cache_hit_tokens + other.cache_hit_tokens,
            cache_miss_tokens=self.cache_miss_tokens + other.cache_miss_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
        )


def _usage_from_raw(raw: Any) -> Usage:
    """把 SSE 最后一块的 usage 转成账。

    ⚠️ `completion_tokens` **包含** `completion_tokens_details.reasoning_tokens`;
       两者不是并列关系,别再相加。2026-10-25 加这个字段,是为了让
       "思考把额度吃光"能直接从账上看出来(见 D170)。
    """
    if not isinstance(raw, dict):
        return Usage()
    details = raw.get("completion_tokens_details")
    details = details if isinstance(details, dict) else {}
    return Usage(
        prompt_tokens=int(raw.get("prompt_tokens") or 0),
        completion_tokens=int(raw.get("completion_tokens") or 0),
        total_tokens=int(raw.get("total_tokens") or 0),
        cache_hit_tokens=int(raw.get("prompt_cache_hit_tokens") or 0),
        cache_miss_tokens=int(raw.get("prompt_cache_miss_tokens") or 0),
        reasoning_tokens=int(
            details.get("reasoning_tokens") or raw.get("reasoning_tokens") or 0
        ),
    )


@dataclass
class ToolCall:
    """模型请求的一次工具调用。arguments 是 JSON **字符串**(可能被流式切碎)。"""

    id: str
    name: str
    arguments: str = ""

    def parsed_arguments(self) -> dict[str, Any]:
        try:
            loaded = json.loads(self.arguments or "{}")
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def as_message(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments or "{}"},
        }


@dataclass
class ChatResult:
    text: str
    reasoning: str = ""
    model: str = ""
    finish_reason: str = ""
    usage: Usage = field(default_factory=Usage)
    raw: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[ToolCall] = field(default_factory=list)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)

    @property
    def truncated(self) -> bool:
        """服务端因为 max_tokens / 上下文上限停下 —— 正文可能只是半句。"""
        return self.finish_reason == "length"

    def assistant_message(self, *, include_reasoning: bool = False) -> dict[str, Any]:
        """回传给模型的那条 assistant 消息。

        ⚠️ **带 tools 时,这条消息一旦带着"结果还没被消费"的 tool_calls,就必须带
        reasoning_content 字段** —— 缺了整个请求 400("The reasoning_content in the
        thinking mode must be passed back to the API")。

        ⭐ 但**内容不必是真实思考**:空字符串照样通过(实测 2026-10-14,
        `scripts/verify_reasoning_replay.py` 5 项全通过)。所以"要不要把思维链带回去"
        是一个**质量/成本取舍**,不是 API 逼的 —— 我们轮内带真实思考(同轮质量),
        跨轮历史不带(见 session.conversation_history)。
        """
        message: dict[str, Any] = {"role": "assistant", "content": self.text or ""}
        if include_reasoning and self.reasoning:
            message["reasoning_content"] = self.reasoning
        if self.tool_calls:
            message["tool_calls"] = [call.as_message() for call in self.tool_calls]
        return message


class LLMClient(Protocol):
    """任何实现了 chat() 的对象都能当客户端用(测试用假客户端)。"""

    model: str

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
        reasoning_effort: str | None = None,
    ) -> ChatResult: ...


@dataclass
class DeepSeekClient:
    """OpenAI 兼容的流式客户端。"""

    api_key: str
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    timeout_s: float = 120.0
    max_retries: int = 3
    reasoning_effort: str | None = "high"

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
        reasoning_effort: str | None = None,
    ) -> ChatResult:
        """⭐ reasoning_effort: 这一次调用的思考强度覆盖。

        None(默认)= 用客户端配置;"" = 这一次**关闭思考**(机判/抽取这类
        "只需要一个 JSON"的调用);其它值 = 用这个档位。

        ⚠️ 为什么需要它(2026-10-25 真机):事实准入裁决带着默认的 high 思考去要
           200 token 的 JSON —— 思考把额度吃光了,正文被截在半个字符串上
           (形如 relationship=refines,target_id=8,text 只剩"助手的名字是"),
           解析失败 → 每一条候选都降级成"待人工放行"。判关系这种事不需要思考。

        ⚠️⚠️ 空串**必须发 `reasoning_effort="none"`,不能只是"这个字段不出现"**:
           思考模式默认就是打开的、effort 默认 high(官方参数表),不发字段
           等于什么都没关 —— D168 第一版就是这么错的,只把 200 改成 400,
           把"必现截断"降成了"偶发截断"(见 D170)。
        """
        if not self.api_key:
            raise LLMAuthError(
                "模型密钥还没配置。打开 设置 → 模型与语音 填一个(保存即生效,不用重启),"
                "或把 LA_LLM_PROVIDER 设为 fake 走离线模式。"
            )

        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": [dict(message) for message in messages],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        # ⚠️ effort 的三态:没传(None)= 用配置;传空串 = **这一次关掉思考**;
        #    传别的 = 这一次用那个档位。
        # ⚠️ 空串不能"整段不出现":思考模式默认打开、effort 默认 high,不发字段
        #    等于仍然 high。官方值 "none" 才是关闭(D170)。
        effort = self.reasoning_effort if reasoning_effort is None else reasoning_effort
        if effort == "":
            effort = "none"
        if effort:
            # 思考模式默认打开且默认为 high;显式声明,避免服务端改默认值时行为突变
            payload["reasoning_effort"] = effort
        if tools:
            # 清单顺序由 build_tool_catalog 保证稳定(KV 前缀缓存的前提)
            payload["tools"] = list(tools)
            payload["tool_choice"] = "auto"

        # 上下文规模闸(D105,照云朵 config.STREAM_RETRY_MIN_CTX=40000 与 DSH 的取向):
        # 大 prompt 上的重试要**重新 prefill 一遍**,既慢又大概率同样卡住 ——
        # 与其白等几分钟,不如如实报错,让用户决定是等、是拆小任务、还是新开会话。
        context_tokens = _payload_tokens(payload)
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return await self._stream_once(payload, on_delta, on_reasoning)
            except LLMTransportError as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                limit = config.STREAM_RETRY_MIN_CTX
                if limit > 0 and context_tokens >= limit:
                    raise LLMTransportError(
                        "上下文约 " + str(context_tokens) + " tokens(≥" + str(limit)
                        + "),不重试 —— 重试要重新 prefill 一遍大 prompt,大概率同样失败。"
                        + "原始错误: " + str(exc)
                    ) from exc
                # 指数退避 + 抖动,避免重试风暴
                delay = min(8.0, 0.5 * (2 ** attempt)) * (0.5 + random.random() / 2)
                await asyncio.sleep(delay)
        raise LLMTransportError(f"重试 {self.max_retries} 次后仍失败: {last_error}")

    async def _stream_once(
        self,
        payload: dict[str, Any],
        on_delta: Callable[[str], None] | None,
        on_reasoning: Callable[[str], None] | None = None,
    ) -> ChatResult:
        url = self.base_url + "/chat/completions"
        headers = {
            "Authorization": "Bearer " + self.api_key,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_slots: dict[int, dict[str, str]] = {}
        usage = Usage()
        finish_reason = ""

        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as http:
                async with http.stream("POST", url, headers=headers, json=payload) as response:
                    if response.status_code in (401, 403):
                        body = (await response.aread()).decode("utf-8", "replace")
                        raise LLMAuthError(f"鉴权失败 HTTP {response.status_code}: {body[:300]}")
                    if response.status_code == 429 or response.status_code >= 500:
                        body = (await response.aread()).decode("utf-8", "replace")
                        raise LLMTransportError(f"HTTP {response.status_code}: {body[:300]}")
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", "replace")
                        raise LLMError(f"HTTP {response.status_code}: {body[:500]}")

                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue

                        choices = chunk.get("choices") or []
                        if choices:
                            choice = choices[0]
                            delta = choice.get("delta") or {}
                            piece = delta.get("content")
                            if piece:
                                text_parts.append(piece)
                                if on_delta is not None:
                                    on_delta(piece)
                            thought = delta.get("reasoning_content")
                            if thought:
                                reasoning_parts.append(thought)
                                # 思考也流式下发(M7):前端能看见"它在想什么",而不是干等
                                if on_reasoning is not None:
                                    on_reasoning(thought)
                            # 流式 tool_calls:按 index 归并,arguments 会分片到达
                            for raw_call in delta.get("tool_calls") or []:
                                slot_index = int(raw_call.get("index") or 0)
                                slot = tool_slots.setdefault(
                                    slot_index, {"id": "", "name": "", "arguments": ""}
                                )
                                if raw_call.get("id"):
                                    slot["id"] = str(raw_call["id"])
                                function = raw_call.get("function") or {}
                                if function.get("name"):
                                    slot["name"] = str(function["name"])
                                if function.get("arguments"):
                                    slot["arguments"] += str(function["arguments"])
                            if choice.get("finish_reason"):
                                finish_reason = choice["finish_reason"]

                        raw_usage = chunk.get("usage")
                        if raw_usage:
                            usage = _usage_from_raw(raw_usage)
        except httpx.TimeoutException as exc:
            raise LLMTransportError(f"请求超时: {exc}") from exc
        except httpx.HTTPError as exc:
            raise LLMTransportError(f"网络错误: {exc}") from exc

        tool_calls = [
            ToolCall(
                id=slot["id"] or ("call_" + str(index)),
                name=slot["name"],
                arguments=slot["arguments"],
            )
            for index, slot in sorted(tool_slots.items())
            if slot["name"]
        ]
        return ChatResult(
            text="".join(text_parts),
            reasoning="".join(reasoning_parts),
            model=payload["model"],
            finish_reason=finish_reason,
            usage=usage,
            tool_calls=tool_calls,
        )
