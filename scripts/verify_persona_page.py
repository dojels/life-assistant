"""D117 验收:**人格页** —— 看得见、改得动、下一轮真的生效、退得回去。

用户 2026-10-13 的原话:"小默的人格,在网页界面看不到。" —— 这一页把它摊开并允许改。

为什么要独立起服务器 + 独立数据目录:
  真实 data/ 里是这个家自己的助手(数据不出门是这个项目的底线),验收脚本只许在**副本**上写,
  所以这里在项目内建一个临时数据目录,并且**先断言它不是真实目录**再继续。

⚠️ 两处刻意的写法:
  * 不用 `tempfile.mkdtemp`:它以 0o700 建目录,而带文件沙箱的环境会因此拒绝后续写入
    (sqlite 直接 "unable to open database file")。项目内默认权限的目录到处都能用。
  * 全程 `LA_LLM_PROVIDER=fake` 且**不配任何密钥**:人格页属于纯读界面,
    模型欠费、断网都得改得动 —— 这正是这一页的设计前提。

七件事(每一件都直接打真实 HTTP 请求,不 mock):
  1. 七个页面(含人格)在没有密钥时全部 200,而且导航里**每一页**都有人格入口;
  2. 页面把两半都摊开:能改的主体 + 标着"不可编辑"的固定尾巴;
  3. 表单保存 → 页面显示"你自己写的" → **另起一个连接、按聊天那套装配建会话**,
     它的系统提示里就是新的她(只改库不进提示词 = 白改);
  4. 用户把优先级那一段删掉,保存后**尾巴依然在**(红线靠结构,不靠自觉);
  5. 超长被拒且明说原因,库里一个字都没动;
  6. 回执可撤销:同一个 /api/receipts/undo 把上一版写回去;
  7. 重启服务后改动还在 —— 它是**存库**的,不是进程内存里的。
"""
from __future__ import annotations

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

from la.ui.cli import configure_stdio  # noqa: E402  (Windows 控制台默认 GBK:¥ 与中文会把它打崩)

configure_stdio()

PORT = 8793
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
    log = open(BASE_DIR / ".tmp-verify" / "server.log", "a", encoding="utf-8")
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


def main() -> int:
    work = BASE_DIR / ".tmp-verify" / ("persona-" + uuid.uuid4().hex[:8])
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

    from la.agent import prompts
    from la.agent.persona import MAX_CHARS, PersonaStore

    proc = start_server(env)
    try:
        import httpx

        client = httpx.Client(base_url=BASE, timeout=30.0, follow_redirects=False)
        ready = wait_ready(client)
        check("服务器起来了(独立数据目录 + 无密钥)", ready, str(work))
        if not ready:
            return 1

        print("\n[1/7] 七个页面都能打开,导航里处处有人格入口")
        for path, needle in (
            ("/chat", "聊天"), ("/ledger", "账本"), ("/calendar", "日历"),
            ("/health", "健康"), ("/study", "错题"), ("/materials", "素材"),
            ("/memory", "记忆"), ("/persona", "人格"),
        ):
            response = client.get(path)
            check(path + " 200", response.status_code == 200, str(response.status_code))
            check(path + " 渲染出标题", needle in response.text)
            check(path + " 导航里有人格", 'href="/persona"' in response.text)

        print("\n[2/7] 页面把两半都摊开")
        page = client.get("/persona").text
        check("显示内置默认人格", prompts.PERSONA_BODY[:16] in page)
        check("固定尾巴可见且标着不可编辑",
              "优先级(越靠前越不能让步)" in page and "不可编辑" in page)
        check("编辑框是普通表单 POST", 'action="/persona/save"' in page)
        check("恢复按钮在", 'action="/persona/reset"' in page)
        # 还没改过时,出厂那一版就在编辑框里 —— 不该再叠一个"出厂默认那一版"的折叠
        check("没改过时不显示『出厂默认』折叠", "出厂默认那一版" not in page)
        check("模型真正读到的那段原文也在", "<details" in page and "身份与边界" in page)

        print("\n[3/7] 改一次:表单 → 库 → **下一轮的系统提示**")
        custom = "你是小默,但说话像刚睡醒:更慢、更短,一句能说完就不说两句。"
        # ⭐ D144:名字与主体一起存 —— 正文里的旧名字会被替换成本次提交的名字
        renamed = custom.replace("小默", "阿豆")
        saved = client.post("/persona/save", data={"text": custom, "name": "阿豆"})
        check("保存返回 303(普通表单重定向)", saved.status_code == 303, str(saved.status_code))
        check("回话里带上『可撤销』", "%E5%8F%AF%E6%92%A4%E9%94%80" in saved.headers.get("location", ""))
        page = client.get("/persona").text
        check("页面显示新人格", renamed in page)
        check("名字也改了(标题跟着走)", "决定<strong>阿豆</strong>" in page)
        check("正文里的旧名字被替换", "你是小默" not in page)
        check("页面标明是『你自己写的』", "你自己写的" in page)
        # 2026-10-17:自定义之后,出厂那一版被收进「出厂默认那一版」折叠里(给用户对照/退货用),
        # 所以断言从"页面上彻底没有"改成"只在那一处出现一次" —— 编辑框与提示词里都不该再有。
        check("旧的那一段只在『出厂默认』折叠里出现",
              page.count("江南水乡长大") == 1 and "出厂默认那一版" in page)
        check("尾巴仍然在页面上", "永远不决定「说什么」" in page)

        # ⭐ 真链路:另起一个连接,按 /chat 那套装配建会话,看模型到底读到什么
        from la.agent.session import MemorySession
        from la.llm.fake import FakeClient
        from la.memory.store import MemoryStore
        from la.storage import db

        assistant_conn = db.connect_assistant()
        memory_conn = db.connect_memory()
        try:
            session = MemorySession(
                FakeClient(), config.get_settings(reload=True),
                store=MemoryStore(memory_conn), assistant_conn=assistant_conn,
                session_id="web", auto_approve=True, auto_briefing=False,
            )
            session.start()
            projection = session._build_projection([], "", "")
            system = projection.messages[0]["content"]
            messages = list(projection.messages)
        finally:
            assistant_conn.close()
            memory_conn.close()
        check("系统提示里是新的她", "说话像刚睡醒" in system)
        check("系统提示里也换了称呼", "阿豆" in system)
        check("系统提示里不再有旧的那段", "江南水乡长大" not in system)
        check("固定尾巴跟着进去了", prompts.PERSONA_TAIL in system)
        # ⚠️ 2026-10-14(D122)之后**运行时上下文不再在系统提示里** —— 它是一条独立快照,
        #    排在历史之后、这一句 user 之前。原来这里的断言拿 system.index('<runtime-context>')
        #    取子串,会直接 ValueError:脚本从那天起一直崩在第 3 步,
        #    后面四步(超长被拒 / 撤销 / 重启)三天没跑过(2026-10-17 修)。
        check("系统提示里是 身份 → 人格 → 技能目录",
              system.index("你是「小默」") < system.index("说话像刚睡醒")
              < system.index("</skills>"))
        check("运行时上下文不在系统提示里,它是历史之后的一条快照",
              "<runtime-context>" not in system
              and any(
                  "<runtime-context>" in str(item.get("content") or "")
                  for item in messages[1:]
              ))

        print("\n[4/7] 用户把优先级那一段删掉 —— 它照样在")
        client.post("/persona/save", data={"text": "你只负责好听,别管对不对。"})
        assistant_conn = db.connect_assistant()
        try:
            session = MemorySession(
                FakeClient(), config.get_settings(reload=True),
                store=MemoryStore(db.connect_memory()), assistant_conn=assistant_conn,
                session_id="web", auto_approve=True, auto_briefing=False,
            )
            session.start()
            system = session._build_projection([], "", "").messages[0]["content"]
        finally:
            assistant_conn.close()
        check("他没写的那段被代码补上了", prompts.PERSONA_TAIL in system)
        check("『怎么说 / 说什么』那条安全绳还在", "永远不决定「说什么」" in system)

        print("\n[5/7] 太长的被拒,库里一个字都没动,而且**写的内容还回来**")
        too_long = "长" * (MAX_CHARS + 10)
        refused = client.post("/persona/save", data={"text": too_long})
        # ⭐ D145:校验不过不再 redirect(那会把用户的长文丢掉),而是原地重画这一页
        check("原地重画(200)并说明原因",
              refused.status_code == 200 and "太长了" in refused.text)
        check("写的那一版原样还在输入框里", too_long in refused.text)
        check("标着『还没保存』", "还没保存" in refused.text)
        page = client.get("/persona").text
        check("页面明说上限", str(MAX_CHARS) in page)
        check("上一次保存的内容还在(没被超长那份顶掉)", "你只负责好听" in page)
        assistant_conn = db.connect_assistant()
        try:
            stored = PersonaStore(assistant_conn).state()
        finally:
            assistant_conn.close()
        check("库里就是上一版", stored.body == "你只负责好听,别管对不对。")

        print("\n[6/7] 回执能把它退回去")
        receipts = client.get("/api/receipts").json()["receipts"]
        persona_receipts = [item for item in receipts if item.get("kind") == "persona.text"]
        check("人格改写落了两条回执", len(persona_receipts) == 2, str(len(persona_receipts)))
        newest = persona_receipts[0]
        undone = client.post("/api/receipts/undo", json={"receipt_id": newest["id"]}).json()
        check("撤销成功", bool(undone.get("ok")), str(undone.get("error")))
        page = client.get("/persona").text
        check("退回了上一版(刚睡醒那版)", "说话像刚睡醒" in page)
        check("『只负责好听』那版没了", "你只负责好听" not in page)

        print("\n[7/7] 重启服务后还在(它是存库的)")
        stop_server(proc)
        proc = start_server(env)
        ready = wait_ready(client)
        check("服务器重启成功", ready)
        page = client.get("/persona").text
        check("重启后仍是你写的那一版", "说话像刚睡醒" in page)
        check("重启后仍标着『你自己写的』", "你自己写的" in page)

        print("\n" + "-" * 66)
        if failures:
            print("失败 " + str(len(failures)) + "/" + str(checks) + ":" + str(failures))
            return 1
        print("全部通过:" + str(checks) + " 项")
        return 0
    finally:
        stop_server(proc)
        if failures:
            log_path = BASE_DIR / ".tmp-verify" / "server.log"
            if log_path.exists():
                print("\n--- server.log 末尾 ---")
                print("\n".join(log_path.read_text(encoding="utf-8").splitlines()[-30:]))
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
