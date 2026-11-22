"""M5 端到端验收:真起一个服务器,验证五个页面与写操作。

⭐ 核心 DoD:**关掉模型后,页面仍能正常查看与手工编辑数据**。
   所以这个脚本**故意不配密钥**,而且用真实的 HTTP 请求打真实进程。

    .venv/Scripts/python.exe scripts/verify_m5.py
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
DATA_DIR = tempfile.mkdtemp(prefix="la-verify5-")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + ("   " + detail if detail else ""))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> int:
    import httpx

    port = free_port()
    base = "http://127.0.0.1:" + str(port)
    env = dict(os.environ)
    env.update({
        "LA_DATA_DIR": DATA_DIR,
        "PYTHONIOENCODING": "utf-8",
        # ⚠️ 故意不给密钥:整个验收都在"模型不可用"的前提下跑
        "DEEPSEEK_API_KEY": "",
        "LA_LLM_PROVIDER": "deepseek",
    })
    print("验收数据目录: " + DATA_DIR)
    print("服务地址: " + base + "(故意不配密钥)")

    server = subprocess.Popen(
        [PYTHON, "-m", "la", "--web", "127.0.0.1:" + str(port)],
        cwd=str(BASE_DIR), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace",
    )
    try:
        client = httpx.Client(base_url=base, timeout=30.0, follow_redirects=False)
        ready = False
        for _ in range(80):
            try:
                if client.get("/healthz").status_code == 200:
                    ready = True
                    break
            except Exception:
                time.sleep(0.25)
        check("服务器起来了", ready, base)
        if not ready:
            return 1

        print("\n[1/6] 五个页面在**没有模型**时都能打开")
        for path, needle in (
            ("/ledger", "账本"), ("/calendar", "日历"), ("/health", "健康"),
            ("/study", "错题"), ("/chat", "聊天"),
        ):
            response = client.get(path)
            check(path + " 返回 200 且渲染出内容",
                  response.status_code == 200 and needle in response.text,
                  str(response.status_code))
        payload = client.get("/healthz").json()
        check("服务自检确认没有密钥", payload["model_configured"] is False,
              str(payload))

        print("\n[2/6] 健康页:手工记体重(纯表单 POST,不经过模型)")
        response = client.post("/health/weight", data={"value": "72.5"})
        check("表单提交后重定向(303)", response.status_code == 303)
        page = client.get("/health").text
        check("⭐ 页面上能看到刚记的体重", "72.5" in page)
        response = client.post("/health/workout", data={"activity": "跑步", "minutes": "30"})
        check("记运动也走表单", response.status_code == 303)
        check("运动出现在页面上", "跑步" in client.get("/health").text)

        print("\n[3/6] 错题页:手工录入 + 孩子端复习")
        response = client.post("/study/problem", data={
            "subject": "数学", "stem": "3/4 + 1/6 = ?", "answer": "11/12",
            "kp": "异分母分数加减,通分",
        })
        check("录入错题重定向", response.status_code == 303)
        # ⚠️ 2026-10-17 审计:/study 现在带页签(复习 / 错题本 / 薄弱点 / 录入),默认落在
        #    **复习**那一页 —— 题目列表要显式带 ?tab=problems,否则这条永远看不到题干。
        page = client.get("/study?tab=problems").text
        check("错题出现在列表里", "异分母分数加减" in page)
        # 薄弱知识点是**另一个页签**(与错题本分家,2026-10 的页签重构)。
        check("薄弱知识点区渲染出来了",
              "薄弱知识点" in client.get("/study?tab=weak").text)

        print("\n[4/6] 账本页:手工写入 -> 确认 -> 月度聚合")
        sys.path.insert(0, str(BASE_DIR))
        os.environ.update(env)
        from la.domain.audit import AuditSink
        from la.domain.services.ledger_service import LedgerService
        from la.storage import db

        conn = db.connect_assistant()
        ledger = LedgerService(conn, audit=AuditSink(conn))
        record = ledger.record_expense(
            {"amount_cents": 3800, "payee": "老张牛肉面", "category": "餐饮",
             "ts": "2026-10-08T12:30:00+08:00"},
            actor="user",
        )
        conn.close()
        page = client.get("/ledger?period=2026-10").text
        check("草稿出现在待确认区", "老张牛肉面" in page and "待确认" in page)

        response = client.post("/ledger/confirm/" + str(record.id))
        check("确认入账(表单 POST)", response.status_code == 303)
        page = client.get("/ledger?period=2026-10").text
        check("⭐ 确认后月度聚合出现金额", "38.00" in page, "¥38.00")

        print("\n[5/6] 聊天页:模型不可用时优雅失败(fail-closed)")
        page = client.get("/chat").text
        # M7(D65)之后页头改口径:不再是"写操作默认不会自动执行",
        # 而是"写操作直接执行,但每次写入都有回执与撤销"。
        check("聊天页本身能打开", "写操作直接执行" in page and "撤销" in page)
        with httpx.Client(base_url=base, timeout=60.0) as streaming:
            with streaming.stream("POST", "/chat/stream", data={"text": "你好"}) as response:
                body = "".join(response.iter_text())
        check("⭐ 返回 SSE 错误事件而不是 500 白屏", "event: error" in body,
              body.strip()[:70])

        print("\n[6/6] 并发访问:六个页面同时打,不许出现随机 500")
        # ⚠️ 这一节是为一个**概率性**的真 bug 立的:
        # FastAPI 把「同步的生成器依赖」和「同步端点」都丢进线程池,
        # 两者**不保证**落在同一个工作线程上 —— 于是「线程 A 建 SQLite 连接、
        # 线程 B 用它」是常态,而 sqlite3 默认 check_same_thread=True,
        # 会随机抛 ProgrammingError。
        # 顺序请求时线程池常常复用同一个空闲线程,所以本地怎么点都是好的,
        # 只有并发才打得出来。见 D66。
        from concurrent.futures import ThreadPoolExecutor

        pages = ["/chat", "/ledger", "/calendar", "/health", "/study", "/memory"]

        def hit(path: str):
            try:
                with httpx.Client(base_url=base, timeout=60.0) as worker:
                    return path, worker.get(path).status_code
            except Exception as exc:  # noqa: BLE001
                return path, type(exc).__name__

        failures: list[tuple[str, object]] = []
        rounds = 3
        with ThreadPoolExecutor(max_workers=12) as pool:
            for _round in range(rounds):
                for path, status in pool.map(hit, pages * 4):
                    if status != 200:
                        failures.append((path, status))
        check(
            "⭐ " + str(rounds * len(pages) * 4) + " 个并发请求全部 200(不再随机 500)",
            not failures, str(failures[:4]) if failures else "",
        )
        client.close()
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n结果: " + str(passed) + "/" + str(len(RESULTS)) + " 项通过")
    for name, ok, _ in RESULTS:
        if not ok:
            print("  未通过: " + name)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
