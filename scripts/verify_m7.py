"""M7 端到端验收:统一写入回执 / 一键撤销 / 执行反馈。

⭐ 核心 DoD(四条):
   1. 领域写入**直接执行**,不再停在待确认状态(LA_READ_ONLY 仍然是总闸);
   2. 写入后**给回执**(SSE receipt 帧 + /api/receipts 列表),回执里说明写了什么;
   3. 回执能**一键撤销**,撤完数据真的回到写入前;
   4. 执行反馈是**流式**的:思考增量 / 步骤账本 / 每步用时 / done 汇总。

验收方式(两条腿都要):
   A. **真起一个 uvicorn 进程**,用真实 HTTP 打它 —— 回执接口、页面、SSE 事件词表;
   B. **真实模型脚本**(假客户端)跑一轮完整对话,验证"写 -> 回执 -> 撤销"闭环。

    .venv/Scripts/python.exe scripts/verify_m7.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
DATA_DIR = tempfile.mkdtemp(prefix="la-verify7-")

# Windows 控制台默认 GBK:⭐ 这类字符会直接把脚本打崩(不是显示乱码,是 UnicodeEncodeError)
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + ("   " + detail if detail else ""))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def parse_sse(body: str) -> list[tuple[str, object]]:
    frames: list[tuple[str, object]] = []
    name = ""
    data: list[str] = []
    for line in body.split("\n"):
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif line == "":
            if name:
                raw = "\n".join(data)
                try:
                    frames.append((name, json.loads(raw)))
                except json.JSONDecodeError:
                    frames.append((name, raw))
            name, data = "", []
    return frames


def check_js_syntax() -> tuple[bool, str]:
    """前端是零构建的原生 JS —— 没有打包器替你发现语法错误,只能自己查。

    有 node 就用 node --check(真解析一遍),没有就退回括号配平检查(弱但聊胜于无)。
    """
    path = BASE_DIR / "la" / "web" / "static" / "app.js"
    source = path.read_text(encoding="utf-8")
    import shutil

    node = shutil.which("node")
    if node:
        proc = subprocess.run(
            [node, "--check", str(path)], capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        return proc.returncode == 0, (proc.stderr or "node --check ok")[:300].strip()
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[str] = []
    for char in source:
        if char in "([{":
            stack.append(char)
        elif char in pairs:
            if not stack or stack.pop() != pairs[char]:
                return False, "括号不配平(没装 node,只能粗查)"
    return (not stack), "括号配平(没装 node,未做真解析)"


def cli(*args: str, env_extra: dict[str, str] | None = None) -> str:
    env = dict(os.environ)
    env.update({"LA_DATA_DIR": DATA_DIR, "PYTHONIOENCODING": "utf-8"})
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [PYTHON, "-m", "la", *args], cwd=str(BASE_DIR), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    return (proc.stdout or "") + (proc.stderr or "")


def main() -> int:  # noqa: C901 - 验收脚本本来就是一条直线
    import httpx

    print("验收数据目录: " + DATA_DIR)
    print("=" * 72)

    # ---- A. 真起服务器 --------------------------------------------------
    port = free_port()
    base = "http://127.0.0.1:" + str(port)
    env = dict(os.environ)
    env.update({
        "LA_DATA_DIR": DATA_DIR,
        "PYTHONIOENCODING": "utf-8",
        "DEEPSEEK_API_KEY": "",
        "LA_LLM_PROVIDER": "fake",   # 离线假客户端:验收不需要密钥
    })
    print("服务地址: " + base + "(LA_LLM_PROVIDER=fake)")
    server = subprocess.Popen(
        [PYTHON, "-m", "la", "--web", "127.0.0.1:" + str(port)],
        cwd=str(BASE_DIR), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace",
    )
    try:
        client = httpx.Client(base_url=base, timeout=60.0, follow_redirects=False)
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

        print("\n[1/6] 聊天页把「写操作直接执行」说清楚")
        page = client.get("/chat").text
        check("聊天页 200", "家庭助手" in page)
        check("页面上写明了直写与撤销", "撤销" in page and "直接执行" in page)
        check("前端资源里带步骤账本", "step-trace" in client.get("/static/app.js").text)
        check("样式表里带记账本样式", ".step-trace" in client.get("/static/app.css").text)
        js_syntax = check_js_syntax()
        check("前端 JS 语法通过(node --check,没有 node 就跳过)", js_syntax[0], js_syntax[1])

        print("\n[2/6] 流式对话:事件词表与执行反馈")
        with client.stream("POST", "/chat/stream", data={"text": "你好"}) as response:
            body = "".join(response.iter_text())
        frames = parse_sse(body)
        kinds = [name for name, _ in frames]
        check("SSE 返回 200", response.status_code == 200, str(response.status_code))
        check("有状态帧", "status" in kinds, ",".join(kinds[:6]))
        check("有正文帧", "html" in kinds)
        check("有收尾帧", "done" in kinds)
        done = next(data for name, data in frames if name == "done")
        check("done 带步骤账本", isinstance(done, dict) and "steps" in done,
              str(list(done.keys()) if isinstance(done, dict) else type(done)))
        check("步骤行带起始时间(前端本地走秒)", all("since" in s for s in done.get("steps", [])))
        check("收尾不留转圈的行",
              all(s["status"] != "running" for s in done.get("steps", [])),
              str([(s["title"], s["status"]) for s in done.get("steps", [])]))
        check("done 带『已执行 N 步 · 用时 Xs』",
              "用时" in str(done.get("label", "")), str(done.get("label")))

        print("\n[3/6] 聊天页底部的回执托盘")
        page = client.get("/chat").text
        check("回执托盘已渲染或为空", "receipt-tray" in page or "最近的写入回执" in page)

        print("\n[4/6] 回执接口:没有时是空列表,不是 500")
        payload = client.get("/api/receipts?limit=5").json()
        check("GET /api/receipts 返回结构", "receipts" in payload and "open" in payload)
        bad = client.post("/api/receipts/undo", json={"receipt_id": 424242}).json()
        check("撤销不存在的回执给明确错误", bad.get("ok") is False and "没有这条写入回执" in bad.get("error", ""),
              str(bad))

        print("\n[5/6] 领域写入 -> 回执 -> 撤销(真实 HTTP + 真实数据库)")
        seed = (
            "import json\n"
            "from la.storage import db\n"
            "from la.domain.receipts import ReceiptLog\n"
            "from la.domain.services.ledger_service import LedgerService\n"
            "conn = db.connect_assistant()\n"
            "receipts = ReceiptLog(conn)\n"
            "receipts.session_id = 'web'\n"
            "ledger = LedgerService(conn, receipts=receipts)\n"
            "rec = ledger.record_expense("
            "{'amount_cents': 10000, 'payee': '修车', 'category': '交通',"
            " 'ts': '2026-10-10T09:00:00+08:00'}, actor='agent', session_id='web',"
            " status='confirmed')\n"
            "print(json.dumps({'txn': rec.id, 'receipt': receipts.last_id(session_id='web')}))\n"
        )
        probe = Path(DATA_DIR) / "_seed_receipt.py"
        probe.write_text(seed, encoding="utf-8")
        env_probe = dict(env)
        env_probe["PYTHONPATH"] = str(BASE_DIR)
        out = subprocess.run(
            [PYTHON, str(probe)], cwd=str(BASE_DIR), env=env_probe,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        text = (out.stdout or "") + (out.stderr or "")
        info = {}
        for line in (out.stdout or "").splitlines():
            if line.strip().startswith("{"):
                info = json.loads(line.strip())
        check("探针写下一笔并落回执", bool(info.get("receipt")), text.strip()[-200:])

        listed = client.get("/api/receipts?limit=5").json()["receipts"]
        # ⚠️ 2026-10-17 审计:两本回执账合并后,id 带上了来源前缀("a:1" / "m:3")——
        #    两张表的 id 各自从 1 开始,不区分的话「撤销 #3」会撤到另一条上。
        #    旧断言拿裸整数去比,于是这条从合并那天起一直红。
        def _same(raw, item_id: str) -> bool:
            return bool(raw) and item_id in (str(raw), "a:" + str(raw), "m:" + str(raw))

        check("回执出现在接口里",
              any(_same(info.get("receipt"), item["id"]) for item in listed),
              json.dumps(listed[:1], ensure_ascii=False))
        first = next((item for item in listed if _same(info.get("receipt"), item["id"])), {})
        check("回执说明了写了什么", "100" in first.get("label", ""), first.get("label", ""))
        check("回执标记为可撤销", first.get("undoable") is True)

        undone = client.post("/api/receipts/undo", json={"receipt_id": info["receipt"]}).json()
        check("撤销返回成功", undone.get("ok") is True, json.dumps(undone, ensure_ascii=False))
        check("撤销说明了做了什么", bool(undone.get("note")), str(undone.get("note")))

        env_check = dict(env)
        env_check["PYTHONPATH"] = str(BASE_DIR)
        verify_src = (
            "from la.storage import db\n"
            "conn = db.connect_assistant()\n"
            "live = conn.execute('SELECT COUNT(*) FROM app_txns_live').fetchone()[0]\n"
            "all_rows = conn.execute('SELECT COUNT(*) FROM app_txns').fetchone()[0]\n"
            "print(str(live) + '/' + str(all_rows))\n"
        )
        probe2 = Path(DATA_DIR) / "_check_live.py"
        probe2.write_text(verify_src, encoding="utf-8")
        out2 = subprocess.run(
            [PYTHON, str(probe2)], cwd=str(BASE_DIR), env=env_check,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        counts = ((out2.stdout or "") + (out2.stderr or "")).strip().splitlines()
        check("⭐ 撤销后账本读路径看不到它(行还在,是软删)",
              counts and counts[-1].strip() == "0/1", counts[-1] if counts else "")

        again = client.post("/api/receipts/undo", json={"receipt_id": info["receipt"]}).json()
        check("同一条回执不能撤两次", again.get("ok") is False and "已经撤销" in again.get("error", ""),
              str(again))

        print("\n[6/6] 只读总闸优先于直写开关")
        env_ro = dict(os.environ)
        env_ro.update({
            "LA_DATA_DIR": DATA_DIR, "PYTHONIOENCODING": "utf-8",
            "DEEPSEEK_API_KEY": "", "LA_LLM_PROVIDER": "fake", "LA_READ_ONLY": "1",
        })
        port2 = free_port()
        base2 = "http://127.0.0.1:" + str(port2)
        server2 = subprocess.Popen(
            [PYTHON, "-m", "la", "--web", "127.0.0.1:" + str(port2)],
            cwd=str(BASE_DIR), env=env_ro,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace",
        )
        try:
            client2 = httpx.Client(base_url=base2, timeout=60.0)
            up = False
            for _ in range(80):
                try:
                    if client2.get("/healthz").status_code == 200:
                        up = True
                        break
                except Exception:
                    time.sleep(0.25)
            check("只读模式的服务器起来了", up, base2)
            if up:
                with client2.stream("POST", "/chat/stream", data={"text": "你好"}) as response:
                    body2 = "".join(response.iter_text())
                check("只读模式下聊天仍可用(不白屏)", "event: done" in body2)
                page2 = client2.get("/chat").text
                check("聊页明示只读模式", "只读模式" in page2)
        finally:
            server2.terminate()
            try:
                server2.wait(timeout=10)
            except Exception:
                server2.kill()
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except Exception:
            server.kill()

    print("\n" + "=" * 72)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("结果: " + str(passed) + "/" + str(len(RESULTS)) + " 通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print("  未通过: " + name + ("  " + detail if detail else ""))
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
