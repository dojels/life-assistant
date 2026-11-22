"""思考预算验收(真实模型):空串必须真的不思考,裁决必须能完整解析。

2026-10-25 的事故(D168/D170):裁决调用要 200 token 的 JSON,却继承默认的
high 思考;思考与正文共用 completion 预算,判决被截在半个字符串上。这个脚本
用真实服务端确认两件事:

  1. `reasoning_effort=""` 在 wire 上是 `"none"`,服务端真的不产生思维链;
  2. 裁决调用(关思考 + 400 token)能拿到一条完整、可解析的判决。

跑法:
    .venv/Scripts/python.exe scripts/verify_reasoning_budget.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.dont_write_bytecode = True

from la import config  # noqa: E402
from la.llm.client import DeepSeekClient  # noqa: E402
from la.memory.fact_adjudicator import VERDICT_MAX_TOKENS, arbitrate  # noqa: E402


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("  [OK] " if ok else "  [FAIL] ") + label + (("  — " + detail) if detail else ""))
    return ok


async def main() -> int:
    settings = config.get_settings()
    print("== 思考预算验收(真实模型)==")
    print("provider=%s model=%s default_effort=%s" % (
        settings.provider, settings.model, settings.reasoning_effort))
    if settings.provider == "fake" or not settings.api_key:
        print("\n没有真实凭据(provider=fake 或缺 DEEPSEEK_API_KEY)—— 跳过。")
        print("这是**故意**的:这条结论只有真实服务端能回答。")
        return 2

    client = DeepSeekClient(
        api_key=settings.api_key, base_url=settings.base_url, model=settings.model,
        timeout_s=60.0, max_retries=0, reasoning_effort="high",
    )

    print("\n[1/2] 单次调用:reasoning_effort=\"\" 应当关闭思考")
    result = await client.chat(
        [
            {"role": "system", "content": "只输出一个 JSON,不要解释。"},
            {"role": "user", "content": '输出 {"ok":true}'},
        ],
        temperature=0.0, max_tokens=64, reasoning_effort="",
    )
    checks = [
        check("思维链为空", not result.reasoning,
              "reasoning_chars=" + str(len(result.reasoning))),
        check("usage.reasoning_tokens = 0", result.usage.reasoning_tokens == 0,
              str(result.usage.as_dict())),
        check("没有被 max_tokens 截断", result.finish_reason != "length",
              "finish_reason=" + repr(result.finish_reason)),
    ]

    print("\n[2/2] 完整裁决:同一类'助手名字'候选应当拿到结构化判决")
    verdict = await arbitrate(
        client,
        "助手的名字是小默,不是小莫",
        [SimpleNamespace(id=8, type="identity", content="助手的名字是「小默」,默是沉默的默")],
        capacity_note="1/20",
    )
    checks.append(check(
        "判决可解析", verdict.ok,
        verdict.relationship + " / target_id=" + str(verdict.target_id)
        + " / text=" + verdict.text[:40],
    ))
    checks.append(check(
        "裁决额度 >= 400", VERDICT_MAX_TOKENS >= 400, str(VERDICT_MAX_TOKENS),
    ))

    print("\n%d/%d 项通过" % (sum(checks), len(checks)))
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
