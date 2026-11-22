"""思考预算的回归测试(2026-10-25 真机,D168/D170)。

钉住三件事:

1. `reasoning_effort=""` 在**真正发出去的 payload** 上是 `"none"`,
   而不是"这个字段不出现" —— 服务端默认 thinking=enabled / effort=high,
   不发等于没关(D168 第一版就是这么错的)。
2. 裁决调用端到端:`max_tokens` 够放一条完整判决,且这一次不思考。
3. 被 `max_tokens` 截断时,`finish_reason=length` 要变成一句可诊断的
   "被截断",不能再伪装成"模型输出不是 JSON"(那次查错了方向)。

跑法:.venv/Scripts/python.exe -m unittest tests.test_llm_reasoning_budget -v
"""

from __future__ import annotations

import unittest

from la.llm.client import ChatResult, DeepSeekClient, Usage, _usage_from_raw
from la.memory.fact_adjudicator import (
    REL_NEW,
    REL_REVIEW,
    VERDICT_MAX_TOKENS,
    arbitrate,
)


class PayloadCase(unittest.IsolatedAsyncioTestCase):
    """`reasoning_effort` 的三态必须在 wire 上成立,不能只在 kwargs 上成立。"""

    def client(self, effort: str | None = "high") -> DeepSeekClient:
        return DeepSeekClient(
            api_key="sk-test", model="probe", reasoning_effort=effort, max_retries=0
        )

    async def capture(self, client: DeepSeekClient, **kwargs) -> dict:
        seen: dict = {}

        async def fake_stream(payload, on_delta, on_reasoning=None):
            seen.clear()
            seen.update(payload)
            return ChatResult(
                text='{"relationship":"new"}', model=payload["model"], finish_reason="stop"
            )

        client._stream_once = fake_stream  # noqa: SLF001 - 换掉网络那一层
        await client.chat([{"role": "user", "content": "x"}], **kwargs)
        return seen

    async def test_empty_override_really_turns_thinking_off(self):
        payload = await self.capture(self.client(), max_tokens=64, reasoning_effort="")
        self.assertEqual(
            payload.get("reasoning_effort"), "none",
            "空串必须发 none;不发字段 = 服务端默认 high 思考",
        )

    async def test_none_override_keeps_client_default(self):
        payload = await self.capture(self.client("high"), max_tokens=64)
        self.assertEqual(payload.get("reasoning_effort"), "high")

    async def test_explicit_off_also_maps_to_none(self):
        payload = await self.capture(self.client("high"), max_tokens=64, reasoning_effort="none")
        self.assertEqual(payload.get("reasoning_effort"), "none")

    async def test_explicit_level_wins(self):
        payload = await self.capture(self.client("high"), max_tokens=64, reasoning_effort="low")
        self.assertEqual(payload.get("reasoning_effort"), "low")

    async def test_client_level_empty_means_off(self):
        """配置层也允许 `""`(不思考),不能悄悄退回服务端默认 high。"""
        payload = await self.capture(self.client(""), max_tokens=64)
        self.assertEqual(payload.get("reasoning_effort"), "none")


class AdjudicatorWireCase(unittest.IsolatedAsyncioTestCase):
    """裁决的**真实请求**必须不思考,且额度留够。"""

    async def test_arbitrate_sends_no_thinking_and_enough_room(self):
        client = DeepSeekClient(
            api_key="sk-test", model="probe", reasoning_effort="high", max_retries=0
        )
        seen: dict = {}

        async def fake_stream(payload, on_delta, on_reasoning=None):
            seen.update(payload)
            return ChatResult(
                text='{"relationship":"new"}', model=payload["model"], finish_reason="stop"
            )

        client._stream_once = fake_stream  # noqa: SLF001
        verdict = await arbitrate(client, "用户养了一只猫", [])

        self.assertEqual(verdict.relationship, REL_NEW)
        self.assertEqual(
            seen.get("reasoning_effort"), "none",
            "裁决在 wire 上必须是关闭思考,不是缺字段",
        )
        self.assertGreaterEqual(
            int(seen.get("max_tokens") or 0), VERDICT_MAX_TOKENS,
            "400 是给合并后的说法留的余量,不能又缩回 200",
        )


class TruncationCase(unittest.IsolatedAsyncioTestCase):
    """截断要自己说出来,不要让人再去查 prompt。"""

    async def test_length_is_reported_as_truncation(self):
        class Truncated:
            model = "truncated"

            async def chat(self, messages, **kwargs):
                return ChatResult(
                    text='{"relationship":"refines","target_id":8,"text":"助手的名字是',
                    model="truncated",
                    finish_reason="length",
                )

        verdict = await arbitrate(Truncated(), "助手的名字是小默", [])
        self.assertEqual(verdict.relationship, REL_REVIEW)
        self.assertIn("截断", verdict.reason)
        self.assertIn("length", verdict.reason)

    async def test_other_non_stop_reasons_are_named(self):
        """`content_filter` / `aborted` 也不该伪装成"模型判不准"。"""

        class Filtered:
            model = "filtered"

            async def chat(self, messages, **kwargs):
                return ChatResult(text="", model="filtered", finish_reason="content_filter")

        verdict = await arbitrate(Filtered(), "用户养了一只猫", [])
        self.assertEqual(verdict.relationship, REL_REVIEW)
        self.assertIn("content_filter", verdict.reason)

    async def test_truncated_but_valid_json_is_still_accepted(self):
        """极端情况:JSON 已经完整,只是尾部被 length 截掉空白 —— 能解析就用。"""

        class TruncatedButValid:
            model = "ok"

            async def chat(self, messages, **kwargs):
                return ChatResult(
                    text='{"relationship":"new"}', model="ok", finish_reason="length"
                )

        verdict = await arbitrate(TruncatedButValid(), "用户养了一只猫", [])
        self.assertEqual(verdict.relationship, REL_NEW, "能解析的判决不该被误降级")


class UsageCase(unittest.TestCase):
    """`reasoning_tokens` 是 completion 的一部分,要能读到、能累加。"""

    def test_usage_reads_reasoning_tokens(self):
        usage = _usage_from_raw({
            "prompt_tokens": 10,
            "completion_tokens": 200,
            "total_tokens": 210,
            "prompt_cache_hit_tokens": 6,
            "prompt_cache_miss_tokens": 4,
            "completion_tokens_details": {"reasoning_tokens": 160},
        })
        self.assertEqual(usage.reasoning_tokens, 160)
        self.assertEqual(usage.completion_tokens, 200, "completion 已含思维链,不再相加")
        self.assertEqual(usage.as_dict()["reasoning_tokens"], 160)

    def test_merge_and_missing_details_are_safe(self):
        one = _usage_from_raw({"completion_tokens": 5})
        two = _usage_from_raw({
            "completion_tokens": 7, "completion_tokens_details": {"reasoning_tokens": 3},
        })
        merged = one.merge(two)
        self.assertEqual(merged.completion_tokens, 12)
        self.assertEqual(merged.reasoning_tokens, 3)
        self.assertEqual(_usage_from_raw(None).reasoning_tokens, 0)

    def test_chat_result_exposes_truncation(self):
        self.assertTrue(ChatResult(text="x", finish_reason="length").truncated)
        self.assertFalse(ChatResult(text="x", finish_reason="stop").truncated)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
