"""D118 / D119 验收:**轨迹页** —— 规则说得对,"上一次对话"看到的就是**发出去的那一份**,
而三个页签切来切去都在该在的地方。

用户 2026-10-13 的原话:*"我希望在个人生活助手界面上加一个【轨迹】的菜单,在这里面,
可以看到当前上下文的装配规则,并能显示上一次对话的完整上下文。"*
用户 2026-10-14 的原话:*"优化轨迹界面视觉效果,让用户获取更清晰的信息,可以采用页标签切换"*
—— 于是拆成三个页签(上下文 / 规则 / 历史)+ 一张按同一把刻度画的占用图(D119)。

为什么要独立起服务器 + 独立数据目录(与 verify_persona_page 同一条纪律):
  真实 data/ 里是这个家自己的助手,验收脚本只许在**副本**上写 —— 所以这里在项目内建一个
  临时数据目录,并且**先断言它不是真实目录**再继续。

⚠️ 两处刻意的写法:
  * 不用 `tempfile.mkdtemp`:它以 0o700 建目录,带文件沙箱的环境会因此拒绝后续写入;
  * 全程 `LA_LLM_PROVIDER=fake` 且**不配任何密钥**:这一页属于纯读界面,模型欠费、断网都得打得开。

八件事(每一件都直接打真实 HTTP 请求,不 mock):
  1. 九个页面(含轨迹)在没有密钥时全部 200,而且**每一页**的导航里都有轨迹入口;
  2. 还没聊过时,三个页签都在(规则页本来就与有没有聊过无关),六段装配规则一条不少,
     空状态说清"为什么是空的、怎么让它有内容",认不出来的 `?tab=` 回默认页;
  3. 真聊一轮(POST /chat/stream,SSE 全读完)→ 「上下文」上出现那一轮:
     用户那句话、系统提示(含身份与固定尾巴)、每一条消息的段标签;
  4. 页面渲染的**就是落盘的那一份**(逐字),而且两条刻度(投影 / 工具 schema)分开画,
     六段规则在**规则页**上、段名与装配器一致;
  5. 敏感事实在这一页上**仍然只是占位符** —— 端到端插一条 confidential 事实再聊一轮;
  6. 页签 / 过滤器 / 翻页切来切去都对:`?i=1` 翻回上一轮(老链接落在上下文页)、
     历史页能点进某一轮、"只看对话/只看系统"真的只给那一半,认不出来的参数一律不报错;
  7. 轨迹落在**独立数据目录**里(不在真实 data/),而且不带工具 schema 全文;
  8. 重启服务后轨迹还在 —— 它是**落盘**的,不是进程内存。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.dont_write_bytecode = True

from la.ui.cli import configure_stdio  # noqa: E402  (Windows 控制台默认 GBK:中文会把它打崩)

configure_stdio()

PORT = 8794
BASE = "http://127.0.0.1:" + str(PORT)
failures: list[str] = []
checks = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if ok:
        print("  PASS  " + label)
    else:
        print("  FAIL  " + label + ("  <- " + detail if detail else ""))
        failures.append(label)


def start_server(env: dict) -> subprocess.Popen:
    (BASE_DIR / ".tmp-verify").mkdir(parents=True, exist_ok=True)
    log = open(BASE_DIR / ".tmp-verify" / "trace-server.log", "a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "la", "--web", "127.0.0.1:" + str(PORT)],
        cwd=str(BASE_DIR), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    proc._la_log = log  # type: ignore[attr-defined]  # noqa: SLF001 - 收尾时关掉
    return proc


def wait_ready(client) -> bool:
    for _ in range(80):
        try:
            if client.get("/healthz").status_code == 200:
                return True
        except Exception:
            time.sleep(0.25)
    return False


def stop_server(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
    log = getattr(proc, "_la_log", None)
    if log is not None:
        log.close()


def chat(client, text: str) -> str:
    """真打一次 /chat/stream 并把 SSE 全读完(离线 fake:不联网、不花钱)。"""
    with client.stream("POST", "/chat/stream", data={"text": text}) as response:
        return "".join(response.iter_text())


def sse_frames(body: str) -> list[tuple[str, dict]]:
    frames: list[tuple[str, dict]] = []
    for block in body.split("\n\n"):
        lines = [line for line in block.splitlines() if line.strip()]
        name = ""
        payload = ""
        for line in lines:
            if line.startswith("event: "):
                name = line[7:].strip()
            elif line.startswith("data: "):
                payload += line[6:]
        if not name or not payload:
            continue
        try:
            frames.append((name, json.loads(payload)))
        except json.JSONDecodeError:
            continue
    return frames


def main() -> int:
    work = BASE_DIR / ".tmp-verify" / ("trace-" + uuid.uuid4().hex[:8])
    (work / "data").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "LA_DATA_DIR": str(work / "data"),
        "LA_LLM_PROVIDER": "fake",
        "LA_DIRECT_WRITE": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    env.pop("DEEPSEEK_API_KEY", None)
    env.pop("DASHSCOPE_API_KEY", None)
    os.environ.update(env)   # 子进程用 env,脚本自己用 os.environ —— 两边都要是临时目录

    from la import config

    config.reload_paths()
    real = (BASE_DIR / "data").resolve()
    assert not str(Path(config.DATA_DIR).resolve()).startswith(str(real)), "数据目录没有隔离,拒绝运行"
    assert str(Path(config.DATA_DIR).resolve()).startswith(str(work.resolve())), "数据目录跑到外面去了"

    from la.agent.context_trace import read_records, trace_path
    from la.agent.prompts import IDENTITY, PERSONA_TAIL
    from la.web.trace_view import SEGMENT_BUDGET_SUM, SEGMENT_RULES

    proc = start_server(env)
    try:
        import httpx

        client = httpx.Client(base_url=BASE, timeout=60.0, follow_redirects=False)
        ready = wait_ready(client)
        check("服务器起来了(独立数据目录 + 无密钥)", ready, str(work))
        if not ready:
            return 1

        print("\n[1/8] 九个页面都能打开,导航里处处有轨迹入口")
        for path, needle in (
            ("/chat", "聊天"), ("/ledger", "账本"), ("/calendar", "日历"),
            ("/health", "健康"), ("/study", "错题"), ("/materials", "素材"),
            ("/memory", "记忆"), ("/persona", "人格"), ("/trace", "轨迹"),
        ):
            response = client.get(path)
            check(path + " 200", response.status_code == 200, str(response.status_code))
            check(path + " 渲染出标题", needle in response.text)
            check(path + " 导航里有轨迹", 'href="/trace"' in response.text)

        print("\n[2/8] 还没聊过:三个页签都在,规则一条不少")
        page = client.get("/trace").text
        check("空状态有说明", "还没有任何一轮对话" in page and "/chat" in page)
        check("三个页签都在", all(label in page for label in ("上下文", "规则", "历史")))
        check("空着的时候也能切到规则页", client.get("/trace?tab=rules").status_code == 200)
        rules = client.get("/trace?tab=rules").text
        check("装配规则一条不少", all(str(rule.budget) in rules for rule in SEGMENT_RULES))
        check("硬上限与触发线写在规则页上", str(config.BUDGET_HARD_MAX) in rules
              and str(config.BUDGET_COMPACT_TRIGGER) in rules)
        check("规则表讲清了 '谁来填'", "prompts.IDENTITY" in rules)
        # ⭐ 用户 2026-10-14 的追问:工具 schema 到底在哪、模型怎么拿到它、是不是分两次调用
        check("规则页讲清了工具 schema 在哪",
              "一次请求长什么样" in rules and "同一次请求" in rules
              and "模型两样都收得到" in rules and "再发一次" in rules)
        check("'单独发出'这种会被误读的说法已经撤掉", "单独发出" not in rules)
        check("这一页没有画崩(不是错误页)", "这一页没能画出来" not in page)
        check("'现在跑在什么上面' 那一栏在", "跑在什么上面" in rules)
        check("历史页空着也不报错", client.get("/trace?tab=history").status_code == 200)
        check("认不出来的 tab 回默认页(上下文)",
              'href="/trace?tab=context" class="on"' in client.get("/trace?tab=nope").text)
        # ⭐ 用户 2026-10-14 的追问:"到多少就压缩?" —— 2026-10-17 起只剩三条线(总锁已删除)。
        trigger_value = config.BUDGET_COMPACT_TRIGGER
        check("规则页讲清了三条线各管什么",
              "三条线各管什么" in rules and "只有前两条会真的触发动作" in rules
              and "回复发出去之后" in rules)
        # ⭐ D121:模型窗口(1M)也要在那条线上 —— 它回答"怎么这么快就压缩"。
        #    占比**现算**:触发线会随预算重定标变(2.2% → 11.2%),页面与验收都不该写死。
        share = "%.1f%%" % (100.0 * trigger_value / config.CONTEXT_WINDOW_TOKENS)
        check("模型窗口(1M)与触发线的占比写在页面上",
              str(config.CONTEXT_WINDOW_TOKENS) in rules and share in rules)
        # ⚠️ 2026-10-17:总锁已从页面与代码里删除 —— 它不参与任何判定,留着只会被读成"到它就压"。
        check("触发线与硬上限两个数都在页面上",
              str(trigger_value) in rules and str(config.BUDGET_HARD_MAX) in rules)
        check("页面不再出现总锁", "总锁" not in rules)
        # ⚠️ 2026-10-17(D141)之后总锁已从页面与代码里删除,"到总锁就压缩"这个误解不复存在,
        #    所以原先那条"当场否掉它"的检查失去了对象。改成钉住现在真正要说清的事:
        #    触发线是一个**写死的数**,不是从任何比例推出来的(它为什么写死 = 成本与延迟)。
        check("说清触发线是写死的数,不是从比例推出来的",
              "写死的数" in rules and "不再由任何比例" in rules
              and "成本与延迟" in rules)
        check("六段之和与含工具 schema 的七段之和分开说",
              str(SEGMENT_BUDGET_SUM) in rules
              and str(SEGMENT_BUDGET_SUM + config.BUDGET_TOOLS) in rules)
        check("如实说明设计里那条强制压缩回环尚未实现", "没有实现" in rules)
        # ⭐ D130:用户 2026-10-16 点名"要按实际的排序顺序放" —— 用具名锚点验渲染出来的先后,
        #    比找数字稳(数字会随重定标变)。工具必须在**对话之前**。
        import re as _re

        anchors = [
            ("①", "系统提示(身份"),
            ("②", "工具 schema"),
            ("③", "压缩检查点"),
            ("④", "运行时上下文"),
            ("⑤", "记忆块"),
            ("⑥", "对话投影"),
            ("⑦", "考古证据块"),
        ]
        positions = []
        for order, name in anchors:
            # ⚠️ 用"序号 + 段名"连在一起找:盘面上"工具 schema"这种词在正文里也出现过,
            #    单找段名会命中解释段落里的那一处,order 就假绿了。
            found = _re.search(
                _re.escape(order) + r"[\s\S]{0,200}?" + _re.escape(name), rules
            )
            positions.append(found.start() if found else -1)
            check("规则表里有 " + order + " " + name, found is not None)
        check("渲染顺序就是真实请求的顺序(系统 → 工具 → 其余)",
              positions == sorted(positions) and positions[1] < positions[5],
              str(positions))
        check("工具 schema 的预算 = 实测预留额,不是旧的 2500",
              "工具 schema" in rules and str(config.BUDGET_TOOLS) in rules
              and str(config.BUDGET_TOOLS) != 2500)

        print("\n[3/8] 真聊一轮(离线 fake)-> 页面上出现那一轮")
        body = chat(client, "我想看看这一轮到底发了什么")
        frames = sse_frames(body)
        check("/chat/stream 有步骤帧(执行反馈)", any(name == "step" for name, _ in frames))
        check("/chat/stream 正常收尾", any(name == "done" for name, _ in frames))
        records = read_records(path=trace_path())
        check("轨迹落了 1 条", len(records) == 1, str(len(records)))
        page = client.get("/trace").text
        check("用户那句话在里面", "我想看看这一轮到底发了什么" in page)
        check("系统提示在里面(身份)", IDENTITY[:12] in page)
        check("固定尾巴在里面", PERSONA_TAIL.replace(">", "&gt;") in page)
        check("逐条消息那一段在", "逐条消息" in page)
        check("段标签是装配器给的", '系统提示' in page and '对话' in page)
        check("工具 schema 单列显示", str(records[0]["tools"]) in page)

        print("\n[4/8] 页面渲染的 == 落盘的那一份(逐字,不是重算)")
        import html as html_lib

        record = read_records(path=trace_path())[0]
        page = client.get("/trace").text
        check("每条消息的正文(转义后)都能在页面上找到",
              all(html_lib.escape(str(item.get("content") or "")[:40]) in page
                  for item in record["messages"]))
        check("落盘时间显示在页面上", str(record.get("at") or "") in page)
        check("每一段的实际占用都渲染出来了",
              all(str(value) in page for value in record["segments"].values()),
              str(record["segments"]))
        # ⚠️ 段名里有 "<memory>" 这种尖括号,进 HTML 会被转义 —— 按转义后的样子比
        check("七段规则与段内说明都在规则页上",
              all(html_lib.escape(rule.name) in client.get("/trace?tab=rules").text
                  for rule in SEGMENT_RULES))
        # D119:一张占用图 + 两条刻度线(上下文一条、工具 schema 另一条)
        check("占用图在页面上", 'class="trace-stack"' in page and "trace-tick" in page)
        check("两条刻度是分开画的(投影 / 工具 schema)",
              page.count('class="trace-stack"') == 2)
        check("消息条数与页面上说的一致",
              ("共 " + str(len(record["messages"])) + " 条消息") in page)
        check("记录里每一段都带得动(没有空正文)",
              all(str(item.get("content") or "").strip() for item in record["messages"]))

        print("\n[5/8] 敏感事实:轨迹页上仍然只是占位符")
        import sqlite3

        secret = "身份证号 110101199001011234"
        conn = sqlite3.connect(str(config.MEMORY_DB))
        conn.execute(
            "INSERT INTO facts(session_id, type, category, content, keywords, confidential,"
            " active, created_at, updated_at) VALUES ('default','identity','permanent',?,'[]',1,1,"
            "'2026-10-13T00:00:00Z','2026-10-13T00:00:00Z')",
            (secret,),
        )
        conn.commit()
        conn.close()
        chat(client, "我的身份证号是多少")
        page = client.get("/trace").text
        check("占位符出现在页面上(说明这一轮真的注入了)", config.SECRET_PLACEHOLDER in page)
        check("敏感原文没有出现在页面上", "110101199001011234" not in page)
        check("敏感原文也没有落进轨迹文件",
              "110101199001011234" not in trace_path().read_text(encoding="utf-8"))

        print("\n[6/8] 页签、过滤器与翻页:切来切去都对")
        chat(client, "第三句了")
        page = client.get("/trace").text
        check("默认显示最新那轮", "第三句了" in page)
        older = client.get("/trace?i=1").text
        check("?i=1 翻回上一轮", "我想看看这一轮到底发了什么" in older)
        check("老链接(?i=)落在上下文页", 'href="/trace?tab=context' in older)
        history = client.get("/trace?tab=history").text
        check("历史页列出每一轮", "看这一轮" in history)
        # ⚠️ 这一行的 href 是模板里的**字面量**(不是 Python 传进来的),Jinja 只转义 {{ }} ——
        #    所以这里的 & 是原样的,别写成 &amp;(页签那一排才是 Python 拼的、会被转义)。
        check("历史页能点进某一轮", 'href="/trace?tab=context&i=1"' in history)
        talk = client.get("/trace?only=talk").text
        check("只看对话:用户那句话在", "第三句了" in talk)
        # ⚠️ 2026-10-17 审计:这里原来写死"四条不可违反的边界",而 D145 把它改成了"五条" ——
        #    于是 :317 从"红线"退化成**永远为真**(系统提示真漏进对话视图也不会报),
        #    :319 则一直红。改成认 IDENTITY 里**不会随计数变化**的那一句。
        marker = "运行在主人本机的私人助理"
        check("只看对话:系统提示不在", marker not in talk)
        check("只看系统:系统提示在", marker in client.get("/trace?only=system").text)
        for query in ("?i=abc", "?i=99", "?i=-2", "?tab=nope", "?only=nope", "?tab=history&i=99"):
            check("/trace" + query + " 不报错", client.get("/trace" + query).status_code == 200)

        print("\n[7/8] 轨迹落在独立数据目录里,而且不带工具 schema 全文")
        check("文件在临时数据目录", str(trace_path()).startswith(str(work)))
        # ⚠️ 真实 data/ 里可能已经有用户自己的轨迹(重启服务之后就会有),
        #    所以这里只断言"这一轮的敏感原文没有漏进去",不要求那个目录不存在。
        real_trace = BASE_DIR / "data" / "trace" / "context.jsonl"
        leaked = real_trace.exists() and "110101199001011234" in real_trace.read_text(
            encoding="utf-8"
        )
        check("这一轮的敏感原文没有漏进真实 data/", not leaked)
        check("记录里没有工具 schema 正文",
              all("tool_catalog" not in json.dumps(item, ensure_ascii=False)
                  for item in read_records(path=trace_path())))
        check("默认只看最近几轮(有上限)", "保留" in page or "最多" in page)

        print("\n[8/8] 重启服务后轨迹还在(它是落盘的)")
        stop_server(proc)
        proc = start_server(env)
        ready = wait_ready(client)
        check("服务器重启成功", ready)
        page = client.get("/trace").text
        check("重启后最新那轮还在", "第三句了" in page)
        check("重启后仍能翻回上一轮", "我想看看这一轮到底发了什么" in client.get("/trace?i=1").text)

        print("\n" + "-" * 66)
        if failures:
            print("失败 " + str(len(failures)) + "/" + str(checks) + ":" + str(failures))
            return 1
        print("全部通过:" + str(checks) + " 项")
        return 0
    finally:
        stop_server(proc)
        if failures:
            log_path = BASE_DIR / ".tmp-verify" / "trace-server.log"
            if log_path.exists():
                print("\n--- server.log 末尾 ---")
                print("\n".join(log_path.read_text(encoding="utf-8").splitlines()[-30:]))
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())