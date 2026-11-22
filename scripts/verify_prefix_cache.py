"""前缀缓存验收(**真打模型**,四个小请求):易变段放在哪,决定跨轮能不能命中。

为什么要有这个脚本(2026-10-14,D120-① / D122):

  * D120 那份复核量出"跨轮 KV 缓存基本没吃到"(一轮只请求一次模型的那些轮,命中率 **1.6%**),
    根因是**秒级时间戳拼在系统提示里** —— 它排在整个历史**前面**,于是每轮的历史全部重算;
  * 改法:把每轮都变的段(运行时 / 记忆 / 证据)推到消息列表**末尾**(历史之后、这一句 user 之前),
    头部系统提示只留稳定段。与 DSH 同形(它的动态上下文以"历史里的一条快照"追加);
  * ⭐ 这件事**必须实测**:它靠的是"两次请求从头逐字节相同",推理讲得再顺也可能被真实
    tokenizer / 服务端缓存粒度否掉 —— 所以这个脚本用**真实的装配器**(Projector)造两轮
    连续请求,直接看 API 返回的 prompt_cache_hit_tokens。

对照实验(两边都用同一套真实历史,只是易变段的位置不同):

    A. 旧形状:系统提示 = 稳定段 + 时间戳;然后 memory;然后历史   ← 对照组(改之前的样子)
    B. 新形状:系统提示 = 稳定段;然后历史;然后 运行时 / 记忆 / 证据 / 这一句 user  ← D122

⚠️ 会打真实模型、花一点点钱(四个小请求,每个约 2~3k token)。没有真实凭据就跳过;
   **不进测试套件** —— 测试套件必须离线、零成本。

运行:
    .venv/Scripts/python.exe scripts/verify_prefix_cache.py
"""
from __future__ import annotations

import asyncio
import datetime
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.dont_write_bytecode = True

from la import config  # noqa: E402
from la.llm.client import ChatResult, DeepSeekClient  # noqa: E402
from la.memory.projector import Projector  # noqa: E402
from la.ui.cli import configure_stdio  # noqa: E402

configure_stdio()

PASS = 0
FAIL = 0


def check(label: str, ok: bool) -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  PASS  " + label)
    else:
        FAIL += 1
        print("  FAIL  " + label)


def filler(tag: str, times: int) -> str:
    return "(" + tag + ")" + "今天杭州晴,18 到 27 度,东北风微风,早晚偏凉,适合出门走走。" * times


def build_history(tag: str, turns: int) -> list[dict[str, str]]:
    history: list[dict[str, str]] = []
    for index in range(turns):
        history.append({"role": "user", "content": tag + " 第 " + str(index) + " 问:" + filler(tag, 3)})
        history.append({"role": "assistant", "content": tag + " 第 " + str(index) + " 答:" + filler(tag, 3)})
    history.append({"role": "user", "content": tag + " 最后一句:" + filler(tag, 3)})
    return history


def legacy_messages(
    *, system: str, runtime: str, memory: str, history: list[dict[str, str]]
) -> list[dict[str, str]]:
    """**对照组**:D122 之前的装配顺序(易变段排在历史前面)。只用来对比,不参与生产。"""
    return (
        [{"role": "system", "content": system + "\n\n" + runtime}]
        + [{"role": "system", "content": memory}]
        + [dict(message) for message in history]
    )


async def measure(client: DeepSeekClient, tag: str, messages: list[dict[str, str]]):
    result: ChatResult = await client.chat(messages, max_tokens=8)
    usage = result.usage
    total = max(usage.prompt_tokens, 1)
    print("  %-26s prompt=%6d 命中=%6d 未命中=%6d 命中率=%5.1f%%" % (
        tag, usage.prompt_tokens, usage.cache_hit_tokens, usage.cache_miss_tokens,
        100.0 * usage.cache_hit_tokens / total))
    return result, usage


async def main() -> int:
    settings = config.get_settings()
    print("== 前缀缓存验收(真实模型)==")
    print("provider=%s model=%s base_url=%s" % (
        settings.provider, settings.model, settings.base_url))
    if settings.provider == "fake" or not settings.api_key:
        print("\n没有真实凭据(provider=fake 或缺 DEEPSEEK_API_KEY)—— 跳过,四项都不算通过。")
        print("这是**故意**的:这个脚本验的是真实服务端的缓存行为,拿假客户端跑没有意义。")
        return 2

    client = DeepSeekClient(
        api_key=settings.api_key, base_url=settings.base_url, model=settings.model,
        timeout_s=90.0, max_retries=0, reasoning_effort=None,
    )
    projector = Projector(settings)

    # ⚠️ 稳定文本里必须带一个**本次运行的唯一串**:否则上一轮探针留下的缓存会让
    #    "第一次请求"也报出高命中率,整个对照实验就废了(实测踩过)。
    nonce = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    stable_a = "你是稳定系统提示(A/" + nonce + ")。 " + filler("A", 30)
    stable_b = "你是稳定系统提示(B/" + nonce + ")。 " + filler("B", 30)
    runtime_a1 = "<runtime-context>\n当前时间: 2026-10-14 13:20:07\n会话: web\n</runtime-context>"
    runtime_a2 = "<runtime-context>\n当前时间: 2026-10-14 13:24:41\n会话: web\n</runtime-context>"
    runtime_b1 = "<runtime-context>\n当前时间: 2026-10-14 14:20:07\n会话: web\n</runtime-context>"
    runtime_b2 = "<runtime-context>\n当前时间: 2026-10-14 14:24:41\n会话: web\n</runtime-context>"
    memory = "<memory><memory-status>hit</memory-status><facts>- 主人喝拿铁</facts></memory>"

    # -- A:旧形状(对照组)------------------------------------------------
    history_a = build_history("A", 8)
    print("\n== A. 旧形状:易变段在系统提示里(排在全部历史之前) ==")
    _r1, usage_a1 = await measure(client, "A 第 1 轮", legacy_messages(
        system=stable_a, runtime=runtime_a1, memory=memory, history=history_a))
    reply_a = "A 的最后一句答:明天多云。"
    _r2, usage_a2 = await measure(client, "A 第 2 轮(时间变了)", legacy_messages(
        system=stable_a, runtime=runtime_a2, memory=memory,
        history=history_a + [{"role": "assistant", "content": reply_a},
                             {"role": "user", "content": "A 下一句:" + filler("A", 3)}]))
    old_rate = 100.0 * usage_a2.cache_hit_tokens / max(usage_a2.prompt_tokens, 1)

    # -- B:新形状(D122,生产上跑的就是它)--------------------------------
    history_b = build_history("B", 8)
    head_b = history_b[:-1]          # 真实路径:装配器会把最后那一句 user 挪到末尾
    last_b = history_b[-1]
    print("\n== B. 新形状(D122):系统提示只留稳定段,易变段在历史之后 ==")
    turn1 = projector.build(
        system_text=stable_b, runtime_text=runtime_b1, memory_text=memory,
        history=history_b, evidence_text="",
    )
    check("第 1 轮的顺序 = 系统提示 → 历史 → 运行时 → 记忆 → 这一句 user",
          [message["role"] for message in turn1.messages]
          == ["system"] + ["user", "assistant"] * 8 + ["system", "system", "user"])
    check("最后一条是这一句 user(模型该回的那一句)",
          turn1.messages[-1]["content"] == last_b["content"])
    check("稳定系统提示里没有时间戳",
          "当前时间" not in turn1.messages[0]["content"])
    r1, usage_b1 = await measure(client, "B 第 1 轮", list(turn1.messages))
    reply_b = r1.text or "B 的最后一句答:明天多云。"

    turn2 = projector.build(
        system_text=stable_b, runtime_text=runtime_b2, memory_text=memory,
        history=history_b + [{"role": "assistant", "content": reply_b},
                             {"role": "user", "content": "B 下一句:" + filler("B", 3)}],
        evidence_text="",
    )
    check("第 2 轮的前缀与第 1 轮**逐字节相同**(一直到历史结束)",
          [dict(message) for message in turn2.messages[:len(turn1.messages) - 3]]
          == [dict(message) for message in turn1.messages[:len(turn1.messages) - 3]])
    _r2b, usage_b2 = await measure(client, "B 第 2 轮(时间变了)", list(turn2.messages))
    new_rate = 100.0 * usage_b2.cache_hit_tokens / max(usage_b2.prompt_tokens, 1)

    print("\n== 结论 ==")
    print("  旧形状跨轮命中: %5.1f%%   新形状(D122): %5.1f%%" % (old_rate, new_rate))
    # ⚠️ 先验"第一轮确实是全新前缀":否则高命中可能来自上一轮探针留下的缓存
    check("两个形状的**第 1 轮**都是冷启动(命中率 < 10%)",
          usage_a1.cache_hit_tokens / max(usage_a1.prompt_tokens, 1) < 0.10
          and usage_b1.cache_hit_tokens / max(usage_b1.prompt_tokens, 1) < 0.10)
    check("新形状的跨轮命中率 > 80%(历史整段被复用)", new_rate > 80.0)
    check("新形状明显优于旧形状(差值 > 30 个百分点)", new_rate - old_rate > 30.0)
    # ---- C:连打 N 轮(2026-10-16 用户问"10 轮对话缓存是多少")------------------
    # ⭐ 为什么单列一段:上面 A/B 比的是"两种装配形状",这一段量的是**一条真实对话
    #    连打 10 轮**时每轮的命中 —— 报的命中量含 **tools 字段**(35 个工具的 schema
    #    每轮都一样),所以它天然占大头。
    # ⚠️ 这一段的稳定系统提示带**新的 nonce**:否则上一段的缓存会让第 1 轮也"命中"。
    rounds = int((os.getenv("LA_PREFIX_ROUNDS") or "10").strip() or "10")
    nonce_c = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-C")
    stable_c = "你是稳定系统提示(C/" + nonce_c + ")。 " + filler("C", 30)
    print("\n== C. 连打 %d 轮(真实装配形状:系统提示 → 历史 → 运行时/记忆 → 这一句 user)==" % rounds)
    history_c = build_history("C", 2)
    rates: list[float] = []
    hits_per_round: list[int] = []
    for index in range(1, rounds + 1):
        turn = projector.build(
            system_text=stable_c,
            runtime_text="<runtime-context>\n当前时间: 2026-10-14 15:%02d:00\n会话: web\n</runtime-context>" % index,
            memory_text=memory,
            history=history_c,
            evidence_text="",
        )
        reply, usage = await measure(client, "C 第 %d 轮" % index, list(turn.messages))
        rates.append(100.0 * usage.cache_hit_tokens / max(usage.prompt_tokens, 1))
        hits_per_round.append(int(usage.cache_hit_tokens))
        history_c = history_c + [
            {"role": "assistant", "content": reply.text or "第 %d 轮的回答" % index},
            {"role": "user", "content": "C 第 %d 问:" % (index + 3) + filler("C", 3)},
        ]
    warm = rates[1:] or rates
    hits = [hits_per_round[i] for i in range(rounds)]
    print("  冷启动第 1 轮: %5.1f%%   第 2~%d 轮平均: %5.1f%%   最低: %5.1f%%"
          % (rates[0], rounds, sum(warm) / len(warm), min(warm)))
    # ⚠️ 这些阈值的来历(别照着别的场景改数字):
    #    这一段**不带 tools**(比生产少 7,841 token 的固定前缀),历史也短,
    #    所以"可复用比例"天然比生产低;要验的是**机制**:① 第一轮真冷;
    #    ② 从第二轮起每一轮都复用到明显一截(> 60%);③ 命中量**逐轮变多**
    #    (历史在长,复用的前缀跟着长)—— 第 ③ 条才是"跨轮复用真的发生"的直接证据。
    check("第 1 轮是冷启动(命中率 < 10%)", rates[0] < 10.0)
    check("第 2 轮起每一轮都 > 60%(跨轮真的在复用)", min(warm) > 60.0)
    # ⚠️ 2026-10-17 审计:原来要求**严格递增**,而服务端的缓存按 128 token 的粒度记账
    #    (实测 896,1024,1152,1152,1280,1408,1408…),某两轮命中量相同完全正常。
    #    这条要守的是"前缀在随历史一起变长",所以改成**单调不减**。
    check("命中量逐轮不减少(复用的前缀随历史一起变长) " + str(hits),
          all(hits[i] <= hits[i + 1] for i in range(len(hits) - 1)))
    print("\n  记分牌:改完之后跑 scripts/analyze_context_cost.py 看第 2 张表第 1 组.")
    print("-" * 66)
    print("通过 %d 项,失败 %d 项" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
