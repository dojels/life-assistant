"""思维链回传规则验收(**真打模型**,5 个小请求):字段必须"在",内容不必"有"。

为什么要有这个脚本(2026-10-14,D122 之后用户追问:*"本地 dsh 和云朵辅助智能体历史回传思维链吗?"*):

  * 我们代码里一直写着"带 tools 时 reasoning_content 必须**完整回传**,否则 API 400"
    (见 llm/client.py 与 session.conversation_history 的注释,来源是 2026-09 的实测);
  * 但"回传字段"与"回传思考"是两件事。这一次把它测清楚:
      ① 请求**以 tool 结果结尾**(= 轮内下一次请求的形状)时,最后那条带 tool_calls 的
         assistant 消息**缺字段 → 400**(错误原文:The reasoning_content in the thinking
         mode must be passed back to the API);
      ② **空字符串可以** —— 即"字段在"就够,内容不必是真实思考;
      ③ 请求**以 user 消息结尾**(跨轮历史的形状)时,整个字段都可以不带,API 不校验;
      ④ 多轮工具往返里,历史那些带 tool_calls 的 assistant 消息全部填空字符串也 OK。
  * ⭐ 这条结论决定了"**跨轮历史要不要把思维链带回去**":D122 之后我们的历史段里
    思维链是正文的 2.3 倍,而照 ① ② 的口径,跨轮完全可以只留"空字段"甚至不留。

⚠️ 会打真实模型、花一点点钱(5 个极小请求)。没有真实凭据就跳过;**不进测试套件**。

运行:
    .venv/Scripts/python.exe scripts/verify_reasoning_replay.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.dont_write_bytecode = True

from la import config  # noqa: E402
from la.llm.client import DeepSeekClient  # noqa: E402
from la.ui.cli import configure_stdio  # noqa: E402

configure_stdio()

PASS = 0
FAIL = 0

TOOLS = [{
    "type": "function",
    "function": {
        "name": "ping",
        "description": "测试用工具:回显一个值",
        "parameters": {
            "type": "object",
            "properties": {"x": {"type": "string"}},
            "required": ["x"],
        },
    },
}]


def check(label: str, ok: bool) -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  PASS  " + label)
    else:
        FAIL += 1
        print("  FAIL  " + label)


def assistant(call_id: str, reasoning, *, with_key: bool = True) -> dict:
    message = {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": call_id, "type": "function",
                        "function": {"name": "ping", "arguments": '{"x": "1"}'}}],
    }
    if with_key:
        message["reasoning_content"] = reasoning
    return message


def tool(call_id: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": '{"ok": true}'}


SYSTEM = [{"role": "system", "content": "你是测试助手,回答尽量短。"},
          {"role": "user", "content": "查一下。"}]


async def call(client: DeepSeekClient, messages: list[dict]) -> tuple[str, str]:
    """返回 (结果, 错误原文)。结果 ∈ {ok, 400, 其他}。"""
    try:
        await client.chat(messages, tools=TOOLS, max_tokens=8)
        return "ok", ""
    except Exception as exc:  # noqa: BLE001 - 探针要的就是错误本身
        text = str(exc).replace("\n", " ")
        return ("400" if "400" in text else "其他"), text


async def main() -> int:
    settings = config.get_settings()
    print("== 思维链回传规则验收(真实模型)==")
    print("provider=%s model=%s effort=%s" % (
        settings.provider, settings.model, settings.reasoning_effort))
    if settings.provider == "fake" or not settings.api_key:
        print("\n没有真实凭据 —— 跳过(这条结论本来就只有真实服务端能回答)。")
        return 2

    client = DeepSeekClient(
        api_key=settings.api_key, base_url=settings.base_url, model=settings.model,
        timeout_s=60.0, max_retries=0, reasoning_effort=settings.reasoning_effort,
    )

    print("\n== 请求以 tool 结果结尾(轮内下一次请求的形状)==")
    outcome, error = await call(client, SYSTEM + [assistant("c1", None, with_key=False), tool("c1")])
    check("缺字段 → 400,且错误里点名 reasoning_content",
          outcome == "400" and "reasoning_content" in error)
    print("        " + (error[:150] or "(无错误)"))
    outcome, _ = await call(client, SYSTEM + [assistant("c1", ""), tool("c1")])
    check("★ 空字符串 → OK(**字段在**就够,内容不必是真实思考)", outcome == "ok")
    outcome, _ = await call(client, SYSTEM + [assistant("c1", "我该调 ping 试试。"), tool("c1")])
    check("真实思考全文 → OK(我们现在就是这么发的)", outcome == "ok")

    print("\n== 请求以 user 消息结尾(跨轮历史的形状)==")
    outcome, _ = await call(client, SYSTEM + [
        assistant("c1", None, with_key=False), tool("c1"),
        {"role": "assistant", "content": "好了。"},
        {"role": "user", "content": "第三句。"}])
    check("★ 历史那条**整个字段都不带** → OK(API 不校验历史)", outcome == "ok")

    print("\n== 多轮工具往返,历史全部填空字符串 ==")
    outcome, _ = await call(client, SYSTEM + [
        assistant("c1", ""), tool("c1"), assistant("c2", ""), tool("c2")])
    check("空字符串 x 2 → OK", outcome == "ok")

    print("\n== 结论 ==")
    print("  * 「回传字段」≠「回传思考」:轮内**待消费**的那条 assistant 必须带这个字段,填空即可;")
    print("  * 跨轮历史连字段都可以不带 —— 所以「历史里要不要留思维链」是一个")
    print("    **可以被我们自己决定**的取舍(云朵选择不留;DSH 选择全文留),不是 API 逼的。")
    print("  * 记分牌:scripts/analyze_conversation_budget.py 第 3b 节(历史不回传思维链 -> 同一份")
    print("    预算能装下的条数 x2.8)。")
    print("-" * 66)
    print("通过 %d 项,失败 %d 项" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
