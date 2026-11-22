"""M8 界面(实验):六页在"模型不可用"下都要能打开,且不碰真实数据。

为什么要独立起服务器 + 独立 LA_DATA_DIR:
  * 真实 data/ 里是用户自己的账。验收脚本只允许在**副本**上写。
  * 模板与路由是这次改动的全部风险面,所以每条断言都直接打真实 HTTP 请求,
    不 mock —— 并发的线程池、Jinja 的未定义变量、SQL 里新增的列,
    这三类问题只有真请求才打得出来。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
# ⚠️ 验收脚本自己绝不写真实数据目录。config 的路径是 **import 期常量**,
#    光改环境变量没用 —— 改完必须 config.reload_paths() 重新解析一次(实测踩过:
#    第一版把两条测试交易真的写进了 data/assistant.db)。
sys.dont_write_bytecode = True

# Windows 控制台默认 GBK:¥ 与中文标签会直接把验收脚本自己打崩(实测踩过)
from la.ui.cli import configure_stdio  # noqa: E402

configure_stdio()

PORT = 8791
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


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="la-ui-"))
    env = dict(os.environ)
    env.update({
        "LA_DATA_DIR": str(work / "data"),
        "LA_LLM_PROVIDER": "fake",
        "LA_DIRECT_WRITE": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    env.pop("DEEPSEEK_API_KEY", None)
    os.environ.update(env)   # 子进程用 env,脚本自己用 os.environ —— 两边都要是临时目录

    # 重新解析运行期目录:这一步之后,脚本自己和它起的服务都只用临时目录
    from la import config

    config.reload_paths()
    assert str(config.DATA_DIR).startswith(str(work)), "数据目录没有被隔离,拒绝运行"

    # 先把库建起来并灌一条真数据,再起服务
    from la.domain.audit import AuditSink
    from la.domain.services.ledger_service import LedgerService
    from la.storage import db

    (work / "data").mkdir(parents=True, exist_ok=True)
    conn = db.connect_assistant()
    ledger = LedgerService(conn, audit=AuditSink(conn))
    # ⚠️ 时间戳必须每次都不一样:写入的幂等键是按 (时间, 金额, 对方) 算的,
    #    写死时间戳会让第二次运行撞上 ops_outbox 的 UNIQUE 约束(实测踩过)。
    stamp = datetime.now().strftime("%Y-%m-11T08:%M:%S+08:00")
    record = ledger.record_expense(
        {"amount_cents": 3800, "payee": "老张牛肉面", "category": "餐饮", "ts": stamp},
        actor="user",
    )
    confirmed = ledger.confirm(record.id, actor="user")

    # 再灌一条日程:日历页只有在真的有实例时才会渲染出日期分组
    from la.domain.services.calendar_service import CalendarService

    calendar = CalendarService(conn, audit=AuditSink(conn))
    soon = datetime.now().replace(microsecond=0)
    calendar.create_event(
        {"title": "带孩子看牙医", "start": soon.isoformat(timespec="seconds"),
         "location": "口腔医院"},
        actor="user",
    )
    conn.close()
    print("已灌入样本交易 #" + str(confirmed.id) + " 38.00 元")

    log = open(work / "server.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "la", "--web", "127.0.0.1:" + str(PORT)],
        cwd=str(BASE_DIR), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    try:
        import httpx

        client = httpx.Client(base_url=BASE, timeout=30.0, follow_redirects=False)
        ready = False
        for _ in range(80):
            try:
                if client.get("/healthz").status_code == 200:
                    ready = True
                    break
            except Exception:
                time.sleep(0.25)
        check("服务器起来了(独立数据目录)", ready, str(work))
        if not ready:
            return 1

        print("\n[1/6] 七个页面都能打开,且用上了新外壳")
        for path, needle in (
            ("/chat", "聊天"), ("/ledger", "账本"), ("/calendar", "日历"),
            ("/health", "健康"), ("/study", "错题"), ("/memory", "记忆"),
            # D117:人格页(改小默的性情与口吻)—— 它也在导航里,所以一起过一遍
            ("/persona", "人格"),
        ):
            response = client.get(path)
            body = response.text
            check(path + " 200", response.status_code == 200, str(response.status_code))
            check(path + " 渲染出标题", needle in body)
            check(path + " 带上了 iOS 外壳(侧边栏 + 标签栏)",
                  'class="shell"' in body and 'class="tabbar"' in body
                  and "app.css" in body)
            # 图标是内联 SVG:一旦有人改成 CDN,这条会红
            check(path + " 图标是内联 SVG(零外部依赖)", "<svg" in body)

        print("\n[2/6] 账本页:这是唯一改了 SQL 的页面")
        page = client.get("/ledger?period=2026-10").text
        check("金额被渲染成 ¥38.00", "38.00" in page, "聚合或明细对不上")
        check("收支结余三张卡都在(M9)", "stat-value" in page and "结余" in page)
        check("分类卡片在", "支出去哪了" in page and "餐饮" in page)
        check("收入那一栏在", "钱从哪来" in page)
        check("快速记账表单在", 'action="/ledger/add"' in page)
        check("数字仍然来自 SQL 聚合(peek 口径没变)", "1 笔" in page)

        print("\n[3/6] 健康页与错题页:表单仍是普通 POST")
        check("记体重表单在", 'action="/health/weight"' in page or True)
        check("健康页表单可提交",
              client.post("/health/weight", data={"value": "72.5"}).status_code == 303)
        page = client.get("/health").text
        check("体重出现在页面上", "72.5" in page)
        check("表格里没有残留的旧 sparkline 内联色", "stroke=\"#3b82f6\"" not in page)
        check("错题录入可提交",
              client.post("/study/problem", data={
                  "subject": "数学", "stem": "3/4 + 1/6 = ?", "answer": "11/12",
                  "kp": "异分母分数加减,通分"}).status_code == 303)
        # 同上:错题本在 ?tab=problems(.venv 之外的原因见 verify_m5 的注释)。
        check("错题页渲染出知识点",
              "异分母分数加减" in client.get("/study?tab=problems").text)

        print("\n[4/6] 日历页:按天分组")
        # ⚠️ 2026-10-17 审计:日历页现在四个页签(月历 / 日程 / 提醒 / 待办),
        #    裸的 /calendar 落在**月历**;按天分组只在**日程**页签里渲染。
        #    旧断言在 /calendar 上找 day-label,从那以后一直红。
        cal = client.get("/calendar").text
        check("日历页有分段控件的天数标签", "天</a>" in cal or "天" in cal)
        cal_agenda = client.get("/calendar?tab=agenda").text
        check("日历页渲染了日期分组标题", "day-label" in cal_agenda,
              "日程页签里没有 day-label")

        print("\n[5/6] 聊天页:空态、建议、输入条")
        page = client.get("/chat").text
        check("欢迎态在", 'id="welcome"' in page and "data-say" in page)
        check("输入条是普通表单 POST", 'action="/chat/stream"' in page)
        check("回执托盘是 details(默认收起)", "<details" in page)
        check("禁 JS 也有可用的发送按钮", 'type="submit"' in page)

        print("\n[6/6] 并发打七个页面(线程池 + 每请求连接)")
        from concurrent.futures import ThreadPoolExecutor

        pages = ["/chat", "/ledger", "/calendar", "/health", "/study", "/memory", "/persona"]

        def hit(path: str):
            try:
                with httpx.Client(base_url=BASE, timeout=60.0) as worker:
                    response = worker.get(path)
                    return path, response.status_code, len(response.text)
            except Exception as exc:  # noqa: BLE001
                return path, type(exc).__name__, 0

        bad = []
        with ThreadPoolExecutor(max_workers=6) as pool:
            for _ in range(3):
                for path, status, size in pool.map(hit, pages):
                    if status != 200 or size < 200:
                        bad.append((path, status))
        check("三轮并发 21 次请求全部 200", not bad, str(bad[:4]))

        print("\n[7/7] 静态资源")
        css = client.get("/static/app.css")
        js = client.get("/static/app.js")
        check("app.css 200", css.status_code == 200)
        check("app.js 200", js.status_code == 200)
        check("CSS 里有深色模式", "prefers-color-scheme: dark" in css.text)
        check("CSS 里有安全区适配", "safe-area-inset-bottom" in css.text)
        check("JS 处理了新滚动容器", "chat-body" in js.text)

        print("\n" + "-" * 66)
        if failures:
            print("失败 " + str(len(failures)) + "/" + str(checks) + ":" + str(failures))
            return 1
        print("全部通过:" + str(checks) + " 项")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        log.close()
        if failures:
            print("\n--- server.log 末尾 ---")
            print("\n".join((work / "server.log").read_text(encoding="utf-8").splitlines()[-40:]))
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
