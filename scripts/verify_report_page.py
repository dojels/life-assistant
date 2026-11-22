# -*- coding: utf-8 -*-
"""使用报表(D136)的真机验收:页面能开、五档时段都在、数字与库里一致。

⚠️ 它测的是**渲染出来的那一页**(TestClient,不联网、不花钱),而不是聚合函数的返回值 ——
   那一条由 tests/test_usage_report.py 管。两者分开:一个防"算错",一个防"没渲染出来"。

跑法:.venv/Scripts/python.exe scripts/verify_report_page.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from la import config  # noqa: E402
from la.web import report_view  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        PASSED.append(label)
        print("  PASS  " + label)
    else:
        FAILED.append(label + (("  <- " + detail) if detail else ""))
        print("  FAIL  " + label + (("  <- " + detail) if detail else ""))


def main() -> int:
    os.environ.setdefault("DEEPSEEK_API_KEY", "")
    from fastapi.testclient import TestClient

    from la.web.app import create_app

    print("[1/4] 没有密钥也能打开,而且时段选择齐全")
    client = TestClient(create_app())
    page = client.get("/report")
    check("/report 200", page.status_code == 200, str(page.status_code))
    body = page.text
    for label in ("按天", "按周", "按月", "按年", "自定义"):
        check("时段入口在:" + label, label in body)
    for need in ("对话轮次", "输入 token", "缓存命中率", "工具调用", "技能调用"):
        check("报表项在:" + need, need in body)

    print("[2/4] 五档都能开,认不出来的值收敛而不是报错")
    for query in (
        "?period=day", "?period=week", "?period=month", "?period=year",
        "?period=custom&start=2026-10-11&end=2026-10-16",
        "?period=custom&start=bad&end=", "?period=nope", "",
    ):
        response = client.get("/report" + query)
        check("/report" + (query or "(默认)") + " 200", response.status_code == 200,
              str(response.status_code))

    print("[3/4] 页面上的数字与库里的账对得上")
    conn = sqlite3.connect(str(config.MEMORY_DB))
    conn.row_factory = sqlite3.Row
    report = report_view.build_usage_report(
        conn, period=report_view.PERIOD_DAY, now=datetime.now()
    )
    conn.close()
    check("轮次数字渲染出来了", str(report.totals.asks) in body, str(report.totals.asks))
    if report.usage_available:
        check("输入 token 渲染出来了(千分位)",
              "{:,}".format(report.totals.prompt_tokens) in body,
              "{:,}".format(report.totals.prompt_tokens))
        check("命中率渲染出来了", ("%.1f%%" % (100 * report.totals.cache_rate)) in body,
              "%.1f%%" % (100 * report.totals.cache_rate))
        check("页面说清了 token 账从什么时候开始有",
              "token 账从" in body or report.usage_first_at in body)
    else:
        check("没有 token 账时页面明说原因(而不是只显示 0)",
              "一条 token 账都没有" in body)
    check("工具表与技能表都在页面上", "成功率" in body)
    # ⭐ D148:联网的钱单列(Search / Fetch 按成功请求计费,不在 token 账里)
    check("分时段表有「联网(搜/抓)」与「≈花费」两列",
          "联网(搜/抓)" in body and "≈花费" in body)
    check("总览有「联网花费」这一张卡", "联网花费" in body)
    check("联网花费的数字与库里数的一致",
          ("￥%.4f" % report.totals.web_cost) in body, "￥%.4f" % report.totals.web_cost)

    print("[3b/4] 工具表是注册表全集(用户 2026-10-16:工具要求全列)") 
    from la.agent.session import INTERNAL_TOOLS
    from la.web.report_view import INTERNAL_TOOL_NAMES
    check("两份内部工具名单一致(不许各抄一份)", set(INTERNAL_TOOLS) == set(INTERNAL_TOOL_NAMES))
    check("标题写明注册表有几个工具、而且是全列", "个,全列" in body)
    cells = body.split("<td><code>")
    shown = set(part.split("</code>")[0] for part in cells[1:])
    check("注册表全体工具都出现在页面上(除框架内部那几个)",
          len(shown) >= len(INTERNAL_TOOLS) + 20,
          "页面上出现了 %d 个工具名" % len(shown)) 
    check("框架内部工具在页面上单独说明", "框架自己发起" in body)
    check("没调过的工具单独点出来", "从未调用" in body)
    print("[4/4] 它是轨迹页那一支的入口,而且口径写在脸上")
    trace = client.get("/trace").text
    check("轨迹页页签里有「使用报表」", "使用报表" in trace and 'href="/report"' in trace)
    check("报表页能回到轨迹", 'href="/trace"' in body)
    check("数据来源写在页面上", "llm/usage" in body and "tool/result" in body)
    check("纯读这条纪律写在页面上", "只读" in body)
    check("金额口径写在页面上(高峰/空闲分档 + 联网单价)",
          "高峰" in body and "空闲" in body and "0.005" in body and "估算" in body)

    print("\n" + "-" * 66)
    if FAILED:
        print("失败 %d/%d:" % (len(FAILED), len(FAILED) + len(PASSED)))
        for item in FAILED:
            print("  - " + item)
        return 1
    print("全部通过:%d 项" % len(PASSED))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
