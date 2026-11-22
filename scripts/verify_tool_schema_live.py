# -*- coding: utf-8 -*-
"""真机验收:把**整份工具清单**发给真实 API,确认 schema 被接受。

为什么必须单独有这个脚本(2026-10-15 的真实事故):
  离线测试全绿、`estimate` 也对,但整份 catalog 一发出去就是 **HTTP 400**:
      Invalid schema for function 'query_summary_pool':
      "string" is not of types "boolean", "object"
  根因:wire 最小化器把数组的 `items` 塌成了裸类型 `"items": "string"`。
  **离线 fake 客户端永远不会碰 schema 的合法性** —— 只有真端点会。
  所以每次动 `la/llm/tools.py` 的装配逻辑、或新增工具之后,都该跑一遍这个。

用法(需要 .env 里配好 DEEPSEEK_API_KEY):
    .venv/Scripts/python.exe scripts/verify_tool_schema_live.py

成本:一次请求、几乎全是缓存命中,prompt 约 1.2 万 token(实测 ¥0.00x 量级)。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.path.insert(0, str(BASE_DIR))

import httpx  # noqa: E402

from la import config  # noqa: E402
from la.agent.session import MemorySession  # noqa: E402
from la.llm.client import DeepSeekClient  # noqa: E402
from la.storage import db  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + ("   " + detail if detail else ""))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="la-schema-live-"))
    for sub in ("archive", "media", "artifacts"):
        (tmp / sub).mkdir(parents=True, exist_ok=True)
    config.DATA_DIR = tmp
    config.ARCHIVE_DIR = tmp / "archive"
    config.MEDIA_DIR = tmp / "media"
    config.ARTIFACTS_DIR = tmp / "artifacts"
    config.ASSISTANT_DB = tmp / "assistant.db"
    config.MEMORY_DB = tmp / "memory.db"
    config._settings = None

    settings = config.get_settings()          # ⚠️ 先读真 key,再切 fake
    if not settings.api_key:
        print("  没有 DEEPSEEK_API_KEY —— 跳过(见 .env)")
        return 0
    os.environ["LA_LLM_PROVIDER"] = "fake"

    session = MemorySession(
        DeepSeekClient(settings), settings, session_id="schema-live",
        assistant_conn=db.connect_assistant(), auto_briefing=False,
    )
    catalog = session.tool_catalog()
    check("工具清单装得出来", bool(catalog), str(len(catalog)) + " 个工具")

    # 结构自查:所有"必须是 schema 对象"的位置都不能是裸值(与离线测试同一条判据)
    bare: list[str] = []
    for item in catalog:
        params = item["function"]["parameters"]
        for key in ("items",):
            for node in _walk(params):
                if key in node and not isinstance(node[key], dict):
                    bare.append(item["function"]["name"] + "." + key)
    check("items 都是 schema 对象", not bare, ", ".join(bare[:5]))

    body = {
        "model": settings.model,
        "messages": [{"role": "user", "content": "只回答两个字:收到"}],
        "max_tokens": 4, "temperature": 0, "stream": False,
        "tools": catalog,
    }
    response = httpx.post(
        settings.base_url + "/chat/completions",
        headers={"Authorization": "Bearer " + settings.api_key,
                 "Content-Type": "application/json"},
        json=body, timeout=180,
    )
    detail = ""
    if response.status_code != 200:
        try:
            detail = response.json()["error"]["message"][:160]
        except Exception:  # noqa: BLE001
            detail = response.text[:160]
    check("整份清单被真实端点接受", response.status_code == 200,
          detail or ("HTTP " + str(response.status_code)))
    if response.status_code == 200:
        usage = response.json().get("usage") or {}
        check("用量里能看到缓存命中", bool(usage.get("prompt_cache_hit_tokens")),
              "prompt=" + str(usage.get("prompt_tokens"))
              + " hit=" + str(usage.get("prompt_cache_hit_tokens")))

    failed = [name for name, ok, _ in RESULTS if not ok]
    print()
    print("小结: %d/%d 通过" % (len(RESULTS) - len(failed), len(RESULTS)))
    return 0 if not failed else 1


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


if __name__ == "__main__":
    raise SystemExit(main())