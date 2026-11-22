"""D112:发不出去的回复**不该消失**。

实测背景(2026-10-13 21:32–21:53):本机到微信服务端的网络断了一截 ——
日志里是 ReadTimeout → ConnectError → `getaddrinfo failed`,中间那条
"照片录好了"的回复**发失败就没了**,用户那边看到的是"连续无响应"。
当时进程还重启过一次,所以待发的东西必须**落盘**。

这一组钉四件事:
  1. 发送失败 → 这条(以及它后面还没发的)进待发队列,并落盘;
  2. 网络恢复 / 用户再来一句话 → 补发,而且**明确标注是补发的**;
  3. 额度用完(ret=-2)同样进队列,等新 token 带来额度再补;
  4. 太旧的(超过 TTL)不再补 —— 隔半小时收到旧回复只会让人困惑。
"""

from __future__ import annotations

import time
import unittest
from pathlib import Path

from la import config
from la.bridge import SendAction
from la.bridge.adapters import weixin_store
from la.bridge.adapters.weixin_ilink import OUTBOX_TTL_S, ILinkTransport


class _FlakyClient:
    """前 fail_times 次发送抛网络错误,之后成功(与真实客户端同一份契约:失败抛异常)。"""

    def __init__(self, *, fail_times: int = 1, exc: Exception | None = None) -> None:
        self.fail_times = fail_times
        self.exc = exc or ConnectionError("All connection attempts failed")
        self.sent: list[str] = []
        self.calls = 0

    async def send_text(self, to_user: str, text: str, context_token: str) -> dict:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc
        self.sent.append(text)
        return {"ret": 0, "remaining": 9}


class OutboxCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self._original = config.DATA_DIR
        config.DATA_DIR = Path(self._tmp.name)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        config.DATA_DIR = self._original
        self._tmp.cleanup()

    def _transport(self, client) -> tuple[ILinkTransport, object]:
        transport = ILinkTransport(client=client)  # type: ignore[arg-type]
        conv = transport.conversation("u1")
        conv.context_token = "tok"
        return transport, conv

    async def test_failed_send_is_held_and_persisted(self) -> None:
        client = _FlakyClient(fail_times=1)
        transport, _conv = self._transport(client)
        await transport.send(SendAction(text="照片录好了,认出了 2 道题"))
        self.assertEqual(len(transport.outbox), 1, "发失败的那条必须留下")
        self.assertIn("照片录好了", transport.outbox[0]["text"])
        self.assertIn("ConnectionError", transport.last_error)
        on_disk = weixin_store.load_outbox()
        self.assertEqual(len(on_disk), 1, "待发队列要落盘 —— 进程重启也不能丢")
        self.assertEqual(on_disk[0]["text"], transport.outbox[0]["text"])

    async def test_flush_sends_it_later_with_a_note(self) -> None:
        client = _FlakyClient(fail_times=1)
        transport, _conv = self._transport(client)
        await transport.send(SendAction(text="补录好了,2 道题"))
        self.assertEqual(await transport.flush_outbox(), 1, "网络好了就该补上")
        self.assertEqual(len(transport.outbox), 0)
        self.assertIn("补录好了", client.sent[-1])
        self.assertIn("补发", client.sent[-1], "要标出来是补发的,不然用户以为串台了")
        self.assertEqual(weixin_store.load_outbox(), [])

    async def test_a_long_reply_keeps_the_remaining_chunks(self) -> None:
        """⚠️ 以前 `return` 一走了之:长回复说了一半就没了,后面几段彻底消失。"""
        client = _FlakyClient(fail_times=1)
        transport, _conv = self._transport(client)
        long_text = "第一段。" + "字" * 3000 + "。\n\n" + "第二段。" + "词" * 3000
        await transport.send(SendAction(text=long_text))
        self.assertGreaterEqual(len(transport.outbox), 1)
        held = " ".join(item["text"] for item in transport.outbox)
        self.assertIn("第二段", held)

    async def test_quota_exhaustion_also_queues_until_a_new_token(self) -> None:
        from la.bridge.adapters.weixin_protocol import RateLimited

        class _Limited:
            def __init__(self) -> None:
                self.calls = 0

            async def send_text(self, to_user: str, text: str, context_token: str) -> dict:
                self.calls += 1
                # ⚠️ ILinkError 的签名是 (ret, errcode, errmsg) —— 第一个位置参数就是 ret
                raise RateLimited(-2, None, "rate limited")

        client = _Limited()
        transport, conv = self._transport(client)
        await transport.send(SendAction(text="今天这道题…"))
        self.assertEqual(len(transport.outbox), 1, "额度用完也要留下,不是丢掉")
        self.assertEqual(conv.remaining_quota, 0)
        # 额度见底时补发是白试:一次请求都不该发
        self.assertEqual(await transport.flush_outbox(), 0)
        self.assertEqual(client.calls, 1)
        # 用户再说一句 = 新 token = 新额度
        conv.remaining_quota = 10
        client.calls = 0

        class _Ok:
            def __init__(self) -> None:
                self.sent: list[str] = []

            async def send_text(self, to_user: str, text: str, context_token: str) -> dict:
                self.sent.append(text)
                return {"ret": 0}

        good = _Ok()
        transport.client = good  # type: ignore[assignment]
        self.assertEqual(await transport.flush_outbox(), 1)
        self.assertIn("今天这道题", good.sent[-1])

    async def test_too_old_items_are_dropped(self) -> None:
        client = _FlakyClient(fail_times=99)
        transport, _conv = self._transport(client)
        await transport.send(SendAction(text="很久以前那条"))
        transport.outbox[0]["at"] = time.time() - OUTBOX_TTL_S - 1
        self.assertEqual(await transport.flush_outbox(), 0)
        self.assertEqual(transport.outbox, [], "过期的补发只会更让人困惑")
        self.assertEqual(weixin_store.load_outbox(), [])


if __name__ == "__main__":
    unittest.main()
