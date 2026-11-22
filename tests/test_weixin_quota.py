"""iLink 出站额度的失败语义(2026-10-13 补)。

社区实测口径(多份独立报告一致):一个 `context_token` 只能发 **10 条**,
超了服务端回 **HTTP 200 + `ret=-2`("rate limited")**,需要用户再说一句话才恢复额度;
`ret=-2` 也可能表示这个 token 已经作废。来源见 docs/决策记录.md D98 的补充说明。

本组测试钉三件事:

  1. `ret != 0` 必须被当成**失败** —— 以前只看 `errcode=-14`,于是限流被当成发送成功,
     本地额度照减、日志一个字都没有,而用户什么都没收到(与 D87 同一类静默失效);
  2. `ret=-2` 要把本地额度归零,并给出一句"照着做就行"的原因;
  3. `ret=0` 或没有 `ret` 字段的响应**不能**误判成失败(否则会把正常发送也静音)。

运行:
    .venv/Scripts/python.exe -m unittest tests.test_weixin_quota -v
"""

from __future__ import annotations

import unittest

from la.bridge import SendAction
from la.bridge.adapters.weixin_ilink import ILinkTransport
from la.bridge.adapters.weixin_protocol import ILinkError, RateLimited, _check


class CheckCase(unittest.TestCase):
    def test_ok_payloads_pass_through(self) -> None:
        self.assertEqual(_check({"ret": 0})["ret"], 0)
        self.assertEqual(_check({})["ret"] if "ret" in _check({}) else 0, 0)
        self.assertEqual(_check({"errcode": 0, "errmsg": ""}).get("errcode"), 0)

    def test_negative_ret_is_a_failure(self) -> None:
        with self.assertRaises(RateLimited) as caught:
            _check({"ret": -2, "errmsg": "rate limited"})
        self.assertEqual(caught.exception.ret, -2)
        with self.assertRaises(ILinkError):
            _check({"ret": -1, "errmsg": "prepare failed"})

    def test_session_expired_still_wins(self) -> None:
        from la.bridge.adapters.weixin_protocol import SessionExpired

        with self.assertRaises(SessionExpired):
            _check({"ret": -2, "errcode": -14, "errmsg": "session timeout"})


class _Client:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = 0

    async def send_text(self, to_user: str, text: str, context_token: str) -> dict:
        # ⚠️ 与真实 ILinkClient 同一份契约:失败要在这里变成异常(_check),
        #    而不是把一个 ret=-2 的 payload 当成功返回给传输层。
        self.calls += 1
        return _check(dict(self.payload))


class SendCase(unittest.IsolatedAsyncioTestCase):
    def _transport(self, payload: dict) -> tuple[ILinkTransport, _Client]:
        client = _Client(payload)
        transport = ILinkTransport(client=client)  # type: ignore[arg-type]
        conv = transport.conversation("u1")
        conv.context_token = "tok"
        return transport, client

    async def test_rate_limited_send_is_reported_and_zeroes_the_quota(self) -> None:
        transport, client = self._transport({"ret": -2, "errmsg": "rate limited"})
        conv = transport.conversation("u1")

        await transport.send(SendAction(text="你好"))
        self.assertEqual(conv.remaining_quota, 0, "限流之后本地额度必须归零")
        self.assertIn("额度", transport.last_error)
        self.assertIn("再发一条消息", transport.last_error, "要给一句能照做的话")

        # 第二次:不再打服务端(白试),原因保持清楚
        await transport.send(SendAction(text="还在吗"))
        self.assertEqual(client.calls, 1)
        self.assertIn("额度", transport.last_error)

    async def test_normal_send_calibrates_from_the_server(self) -> None:
        transport, _ = self._transport({"ret": 0, "remaining": 7})
        conv = transport.conversation("u1")
        await transport.send(SendAction(text="你好"))
        self.assertEqual(conv.remaining_quota, 7, "服务端给了 remaining 就以它为准")
        self.assertEqual(transport.last_error, "")

    async def test_normal_send_without_remaining_trusts_the_local_counter(self) -> None:
        transport, _ = self._transport({"ret": 0})
        conv = transport.conversation("u1")
        before = conv.remaining_quota
        await transport.send(SendAction(text="你好"))
        self.assertEqual(conv.remaining_quota, before - 1, "没有 remaining 就本地自减")
        self.assertEqual(transport.last_error, "")
