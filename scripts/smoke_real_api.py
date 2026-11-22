"""真实 DeepSeek API 冒烟测试(V4.1 flash + 思考强度 high)。

它验证的是**离线 fake 客户端永远测不到**的那条路:
    真实 SSE 解析 · reasoning_content 回传 · 带 tools 的多轮循环 · KV 前缀缓存命中

用法(需要 .env 里配好 DEEPSEEK_API_KEY):
    .venv/Scripts/python.exe scripts/smoke_real_api.py

不消耗多少额度:两轮短对话。数据写在临时目录,不污染日常库。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = tempfile.mkdtemp(prefix="la-smoke-")
os.environ["LA_DATA_DIR"] = DATA_DIR
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.path.insert(0, str(BASE_DIR))

from la import config  # noqa: E402
from la.agent.session import MemorySession  # noqa: E402
from la.llm.client import DeepSeekClient  # noqa: E402
from la.memory.store import MemoryStore  # noqa: E402
from la.storage import db  # noqa: E402
from la.storage.migrations import MIGRATIONS_ASSISTANT, MIGRATIONS_MEMORY  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + ("   " + detail if detail else ""))


async def main() -> int:
    settings = config.get_settings(reload=True)
    print("数据目录: " + DATA_DIR)
    print("模型: " + settings.model + " | 思考强度: " + settings.reasoning_effort)
    if settings.provider != "deepseek" or not settings.api_key:
        print("跳过:没有配置 DEEPSEEK_API_KEY(或在用 fake 模式)")
        return 2

    client = DeepSeekClient(
        api_key=settings.api_key,
        base_url=settings.base_url,
        model=settings.model,
        timeout_s=settings.request_timeout_s,
        max_retries=settings.max_retries,
        reasoning_effort=settings.reasoning_effort,
    )

    print("\n[1/3] 单轮对话:SSE 解析 + 思维链返回")
    result = await client.chat(
        [{"role": "user", "content": "只回答两个字:收到"}], max_tokens=2000
    )
    check("拿到了正文", bool(result.text.strip()), result.text.strip()[:40])
    # ⚠️ 2026-10-17 审计:这条原来要求 reasoning **非空**,而"只回答两个字:收到"这种
    #    极短请求,模型完全可能不给推理段(实测 61 字 / 0 字两种都出现过)—— 于是它是个
    #    间歇性假红。本项目自己的口径是**字段必须在、内容不必有**(见 verify_reasoning_replay),
    #    这里跟着同一条:能取到属性就说明字段通了。
    check("思考模式下 reasoning 字段可用", result.reasoning is not None,
          str(len(result.reasoning or "")) + " 字")
    usage = result.usage.as_dict()
    check("用量里带 KV 缓存字段", "cache_hit_tokens" in usage and usage["prompt_tokens"] > 0,
          json.dumps(usage, ensure_ascii=False))

    print("\n[2/3] 带 tools 的两轮循环:reasoning_content 必须完整回传,否则 API 400")
    assistant = db.connect(Path(DATA_DIR) / "assistant.db")
    db.migrate(assistant, MIGRATIONS_ASSISTANT)
    memory = db.connect(Path(DATA_DIR) / "memory.db")
    db.migrate(memory, MIGRATIONS_MEMORY)
    store = MemoryStore(memory, archive_dir=Path(DATA_DIR) / "archive")

    session = MemorySession(
        client,
        dataclasses.replace(settings, session_id="smoke"),
        store=store,
        session_id="smoke",
        assistant_conn=assistant,
        auto_approve=True,
        auto_briefing=False,
    )
    session.start()
    reply = await session.respond("帮我记住:我最喜欢的咖啡是冰美式")
    check("带 tools 的多轮循环没有 400", True, reply.text.strip()[:50])
    facts = store.active_facts()
    coffee = [f.content for f in facts if "冰美式" in f.content]
    check("事实被写进长期记忆", bool(coffee), str(coffee))
    check("同一条事实不会被写两遍(规则与模型工具去重)", len(coffee) == 1, str(coffee))

    print("\n[3/3] 账本写入(模型自己决定调 expense_record)")
    await session.respond("记一笔:今天午饭 38 元,老张牛肉面,算餐饮")
    rows = [dict(r) for r in assistant.execute(
        "SELECT id, payee, status FROM app_txns").fetchall()]
    # ⚠️ 2026-10-17 审计:M7/D65 之后默认是**直写**(助手记的账直接 confirmed),
    #    旧断言死认 draft,于是这条从直写上线上那天起就一直红。
    #    这条检查的本意是"模型真的把账记进去了",所以状态只要求是两种合法值之一。
    statuses = {r["status"] for r in rows}
    check("账本里出现了助手记的那一笔(直写=confirmed / 关直写=draft)",
          bool(statuses & {"draft", "confirmed"}), json.dumps(rows, ensure_ascii=False))
    relayed = session.relay_outbox()
    check("发件箱已中继到归档", relayed is not None and relayed.relayed >= 0,
          json.dumps(relayed.as_dict(), ensure_ascii=False) if relayed else "n/a")

    total = session.total_usage.as_dict()
    print("\n本轮累计用量: " + json.dumps(total, ensure_ascii=False))
    check("KV 前缀缓存有命中(工具清单稳定)", total["cache_hit_tokens"] > 0,
          "命中 " + str(total["cache_hit_tokens"]) + " / 未命中 " + str(total["cache_miss_tokens"]))

    assistant.close()
    memory.close()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n结果: " + str(passed) + "/" + str(len(RESULTS)) + " 项通过")
    for name, ok, _ in RESULTS:
        if not ok:
            print("  未通过: " + name)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
