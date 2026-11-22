"""D105:工具超时 / 自动重试 / 整轮墙钟的护栏。

实测背景(见 docs/决策记录.md D105):
  * 52 个工具里 **46 个**的 handler 从头到尾没有一个 `await` —— 它们是对同步 service 的
    async 包装,`asyncio.wait_for` **抢不动**它们:标称超时是摆设,而且它们跑在事件循环里,
    跑多久整个网页端/桥接就冻多久(实测:218KB docx 用 1.97s,50ms 的限时没拦住,心跳 0 次);
  * 生产日志里**从来没有过一条工具超时** —— 说明这些数从来没被校准过。

这一组用例钉住四件事:
  1. 瞬时错误(超时/连接类)**只对只读工具**自动重试一次(照云朵 _TRANSIENT_RETRY=1);
  2. 标称超时 ≥8s 的工具:要么 handler 里真有 await,要么必须显式标 `blocking=True`;
  3. 一层轮墙钟到底会收回工具、并在账本里说明原因;
  4. LLM 重试的上下文规模闸:大 prompt 不重试。
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from la import config
from la.base.phases import CircuitBreaker, PHASE_ANSWER
from la.agent.session import MemorySession
from la.llm.client import DeepSeekClient, LLMTransportError
from la.llm.fake import FakeClient
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT, MIGRATIONS_MEMORY
from la.tools.registry import (  # noqa: SLF001
    RISK_LOW, ToolInputError, ToolRegistry, ToolSpec, _is_transient,
)

ANY = "any"


def _spec(name: str, handler, *, timeout_ms: int = 100, writes: bool = False,
          blocking: bool = False) -> ToolSpec:
    return ToolSpec(
        name=name, description="探针工具", phase=ANY, risk=RISK_LOW,
        timeout_ms=timeout_ms, handler=handler, required=(), writes=writes,
        blocking=blocking, params={"type": "object", "properties": {}},
    )


def _handler_awaits(handler) -> bool:
    """handler 里有没有 await(去掉 docstring 与注释再数,免得被文字误导)。"""
    try:
        source = inspect.getsource(handler)
    except Exception:
        return True   # 拿不到源码就不当它是"同步重活"(宁可漏判)
    source = re.sub(r'"""[\s\S]*?"""', "", source)
    source = re.sub(r"#[^\n]*", "", source)
    return bool(re.search(r"\bawait\b|\basync with\b|\basync for\b", source))


class TransientRetryCase(unittest.IsolatedAsyncioTestCase):
    """P0-B:瞬时错误自动重试一次(照云朵)。"""

    async def test_timeout_is_retried_once_for_read_only_tools(self):
        calls = {"n": 0}

        async def flaky(params):
            calls["n"] += 1
            if calls["n"] == 1:
                await asyncio.sleep(0.3)      # 第一次必超时(限 50ms)
            return {"ok": True, "attempt": calls["n"]}

        registry = ToolRegistry()
        registry.register(_spec("flaky_read", flaky, timeout_ms=50))
        result = await registry.execute(
            "flaky_read", {}, breaker=CircuitBreaker(phase=PHASE_ANSWER)
        )
        self.assertTrue(result.ok, result.message)
        self.assertEqual(calls["n"], 2, "第一次超时后必须自动重试一次")
        self.assertIn("自动重试", str(result.data), "结果里要说清这是重试拿到的")

    async def test_write_tools_are_never_retried(self):
        calls = {"n": 0}

        async def flaky(params):
            calls["n"] += 1
            await asyncio.sleep(0.3)
            return {"ok": True}

        registry = ToolRegistry(direct_write=True)
        registry.register(_spec("flaky_write", flaky, timeout_ms=50, writes=True))
        result = await registry.execute(
            "flaky_write", {}, breaker=CircuitBreaker(phase=PHASE_ANSWER)
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "TIMEOUT")
        self.assertEqual(calls["n"], 1, "写类工具绝不自动重试 —— 它可能已经写进去了")

    async def test_connection_error_is_retried_but_logic_error_is_not(self):
        counters = {"conn": 0, "logic": 0}

        async def conn(params):
            counters["conn"] += 1
            if counters["conn"] == 1:
                raise ConnectionError("connection reset by peer")
            return {"ok": True}

        async def logic(params):
            counters["logic"] += 1
            raise ValueError("缺少必填参数: amount")

        registry = ToolRegistry()
        registry.register(_spec("conn_probe", conn))
        registry.register(_spec("logic_probe", logic))
        breaker = CircuitBreaker(phase=PHASE_ANSWER)
        self.assertTrue((await registry.execute("conn_probe", {}, breaker=breaker)).ok)
        self.assertEqual(counters["conn"], 2)
        failed = await registry.execute("logic_probe", {}, breaker=breaker)
        self.assertFalse(failed.ok)
        self.assertEqual(counters["logic"], 1, "业务错误不重试(照云朵:宁可漏判不可误判)")

    async def test_input_error_maps_to_bad_params_and_is_not_retried(self):
        """参数写错是 BAD_PARAMS,不是 HANDLER_ERROR;也不该重试同一份坏参数。"""
        calls = {"n": 0}

        async def bad(params):
            calls["n"] += 1
            raise ToolInputError("无法识别的周期格式: 2026-11-16")

        registry = ToolRegistry()
        registry.register(_spec("input_probe", bad))
        result = await registry.execute(
            "input_probe", {}, breaker=CircuitBreaker(phase=PHASE_ANSWER)
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "BAD_PARAMS")
        self.assertEqual(calls["n"], 1, "参数错误不自动重试")
        self.assertIn("无法识别的周期格式", result.message)
        self.assertNotIn("ToolInputError", result.message, "给模型看的不该是异常类名")

    async def test_user_wait_and_subagent_tools_are_never_retried(self):
        """等用户的工具、以及一次跑 4 个子代理的 spawn:超时也不重试。

        前者:超时的含义是"用户没回答",重试等于把同一个问题再弹一次,还白等一轮;
        后者:重试 = 白花一倍的钱,而且它内部已经按代理做了失败隔离。
        """
        calls = {"n": 0}

        async def slow(params):
            calls["n"] += 1
            await asyncio.sleep(0.3)
            return {"ok": True}

        registry = ToolRegistry()
        for name in ("ask_user", "category_create", "spawn_sub_agents"):
            registry.register(_spec(name, slow, timeout_ms=50))
        for name in ("ask_user", "category_create", "spawn_sub_agents"):
            result = await registry.execute(
                name, {}, breaker=CircuitBreaker(phase=PHASE_ANSWER)
            )
            self.assertFalse(result.ok, name)
            self.assertEqual(result.error_code, "TIMEOUT", name)
        self.assertEqual(calls["n"], 3, "三个名字各跑一次,谁都不许重试")

    def test_classifier_is_conservative(self):
        self.assertTrue(_is_transient(ConnectionError("connection reset")))
        self.assertTrue(_is_transient(RuntimeError("请求超时")))
        self.assertTrue(_is_transient(OSError("network is unreachable")))
        self.assertFalse(_is_transient(ValueError("金额必须大于 0")))
        self.assertFalse(_is_transient(KeyError("category")))


class BlockingInventoryCase(unittest.IsolatedAsyncioTestCase):
    """P0-A:把"抢不动"标出来,并且**加新工具时拦得住**。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self._patched = {
            key: getattr(config, key)
            for key in ("DATA_DIR", "ARCHIVE_DIR", "MEDIA_DIR", "ARTIFACTS_DIR",
                        "ASSISTANT_DB", "MEMORY_DB")
        }
        self._env = {key: os.environ.get(key) for key in ("LA_LLM_PROVIDER", "DEEPSEEK_API_KEY")}
        os.environ["LA_LLM_PROVIDER"] = "fake"
        os.environ["DEEPSEEK_API_KEY"] = ""
        config.DATA_DIR = self.tmp
        config.ARCHIVE_DIR = self.tmp / "archive"
        config.MEDIA_DIR = self.tmp / "media"
        config.ARTIFACTS_DIR = self.tmp / "artifacts"
        config.ASSISTANT_DB = self.tmp / "assistant.db"
        config.MEMORY_DB = self.tmp / "memory.db"
        config._settings = None
        self.session = MemorySession(
            FakeClient(), config.get_settings(), session_id="timeout-probe",
            assistant_conn=db.connect_assistant(), direct_write=True,
        )

    def tearDown(self) -> None:
        for conn in (self.session.assistant_conn, getattr(self.session.store, "conn", None)):
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
        for key, value in self._patched.items():
            setattr(config, key, value)
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config._settings = None
        self._tmp.cleanup()

    def test_slow_tools_are_either_async_or_marked_blocking(self):
        """标称超时 ≥8s 的工具:要么 handler 真有 await,要么显式标 blocking。

        没有第三条路 —— 否则它就是一个"看起来有超时保护、实际抢不动"的重活。
        """
        offenders = [
            spec.name for spec in self.session.registry.specs()
            if spec.timeout_ms >= 8000
            and not spec.blocking
            and not _handler_awaits(spec.handler)
        ]
        self.assertEqual(
            offenders, [],
            "这些工具标称超时 ≥8s 却抢不动(wait_for 对它无效),必须标 blocking=True:"
            + str(offenders),
        )

    def test_the_known_sync_heavy_tools_are_marked(self):
        """⭐ 这是一份**显式清单**:新增的同步重活必须在这里登记,否则会悄悄变成
        "标称有超时、实际抢不动"的那一类。

        实测依据:docx 218KB = 1.97s(D105);网页截图是 Chrome 子进程,
        冷启动 + 渲染 2~5 秒(D140,内部另有按 PID 杀树的硬超时)。
        """
        marked = {spec.name for spec in self.session.registry.specs() if spec.blocking}
        self.assertEqual(
            marked, {"artifact_write", "file", "html_page", "expense_import", "page_view"},
            "已知的同步重活就这五个;清单变了要连着改这里",
        )


class TurnDeadlineCase(unittest.IsolatedAsyncioTestCase):
    """P1-B:一轮的墙钟到底 → 收回工具 + 账本写明原因。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self._patched = {
            key: getattr(config, key)
            for key in ("DATA_DIR", "ARCHIVE_DIR", "MEDIA_DIR", "ARTIFACTS_DIR",
                        "ASSISTANT_DB", "MEMORY_DB")
        }
        self._env = {key: os.environ.get(key) for key in ("LA_LLM_PROVIDER", "DEEPSEEK_API_KEY")}
        os.environ["LA_LLM_PROVIDER"] = "fake"
        os.environ["DEEPSEEK_API_KEY"] = ""
        config.DATA_DIR = self.tmp
        config.ARCHIVE_DIR = self.tmp / "archive"
        config.MEDIA_DIR = self.tmp / "media"
        config.ARTIFACTS_DIR = self.tmp / "artifacts"
        config.ASSISTANT_DB = self.tmp / "assistant.db"
        config.MEMORY_DB = self.tmp / "memory.db"
        config._settings = None

    def tearDown(self) -> None:
        for key, value in self._patched.items():
            setattr(config, key, value)
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config._settings = None
        self._tmp.cleanup()

    async def test_deadline_takes_the_tools_away_and_says_why(self):
        from la.llm.client import ToolCall
        import json

        steps: list = []

        async def tick(params):
            await asyncio.sleep(0.04)      # 每轮 40ms:几轮下来必然超过 1e-9 的墙钟
            return {"ok": True}

        rounds = [
            [ToolCall(id="t" + str(i), name="__tick", arguments="{}")] for i in range(4)
        ]
        client = FakeClient(tool_script=rounds, scripted=["收尾了,按现有结果。"])
        session = MemorySession(
            client, config.get_settings(), session_id="deadline-probe",
            assistant_conn=db.connect_assistant(), direct_write=True,
            auto_briefing=False, on_step=lambda kind, data: steps.append(data),
        )
        session.registry.register(_spec("__tick", tick, timeout_ms=5000))
        session.start()
        with mock.patch.object(config, "AGENT_MAX_TURN_SEC", 1e-9):
            reply = await session.respond("随便做点需要好几步的事")

        self.assertTrue(
            any(catalog == [] for catalog in client.tool_catalogs),
            "墙钟到底之后必须收回工具:" + str([len(c) for c in client.tool_catalogs]),
        )
        self.assertTrue(
            any("时长上限" in str(row.get("detail") or "") for row in steps),
            "账本里要写明是被时长叫停的:" + str(steps[-3:]),
        )
        self.assertEqual(reply.text, "收尾了,按现有结果。")
        session.assistant_conn.close()


class LLMRetryGuardCase(unittest.IsolatedAsyncioTestCase):
    """P0-C:大 prompt 不重试(照云朵 STREAM_RETRY_MIN_CTX)。"""

    def _client(self, max_retries: int = 1):
        client = DeepSeekClient(api_key="probe-key", model="probe", timeout_s=1.0,
                                max_retries=max_retries)
        calls = {"n": 0}

        async def boom(payload, on_delta, on_reasoning=None):
            calls["n"] += 1
            raise LLMTransportError("HTTP 500: 上游错误")

        client._stream_once = boom      # noqa: SLF001 - 直接换掉网络那一层
        return client, calls

    async def test_large_context_gives_up_immediately(self):
        client, calls = self._client(max_retries=3)
        big = "x" * (config.STREAM_RETRY_MIN_CTX * 4 + 400)
        with self.assertRaises(LLMTransportError) as caught:
            await client.chat([{"role": "user", "content": big}])
        self.assertEqual(calls["n"], 1, "大上下文不该重试(重试要重新 prefill 一遍)")
        self.assertIn("不重试", str(caught.exception))

    async def test_small_context_still_retries(self):
        client, calls = self._client(max_retries=1)
        with self.assertRaises(LLMTransportError):
            await client.chat([{"role": "user", "content": "你好"}])
        self.assertEqual(calls["n"], 2, "小上下文照旧重试一次")
