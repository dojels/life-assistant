"""记忆页端到端验收:真起一个服务器,验证每一层记忆都看得见。

⭐ 核心 DoD:**不配密钥也能看** —— 记忆页读的全是本地文件与本地表。
   而且看到的数字必须和数据库里的真实行数对得上,不能只是"页面能打开"。

    .venv/Scripts/python.exe scripts/verify_memory.py
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
DATA_DIR = tempfile.mkdtemp(prefix="la-verify-mem-")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + ("   " + detail if detail else ""))


def probe_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "LA_DATA_DIR": DATA_DIR,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": str(BASE_DIR),
        "DEEPSEEK_API_KEY": "",
    })
    return env


def run_probe(name: str, source: str) -> str:
    path = Path(DATA_DIR) / ("_" + name + ".py")
    path.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [PYTHON, str(path)], cwd=str(BASE_DIR), env=probe_env(),
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    return (result.stdout or "") + (result.stderr or "")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


SEED = """
from la import config
from la.storage import db
from la.memory.store import MemoryStore

store = MemoryStore(db.connect_memory())
store.append('default', 'user/message', {'role': 'user', 'content': '我叫 Kevin'})
store.append('default', 'assistant/message', {'role': 'assistant', 'content': '记住了'})
store.append('default', 'tool/result', {'name': 'x'}, surface=False)
store.add_fact('用户的英文名是 Kevin', fact_type='identity')
store.add_fact('用户喝美式', fact_type='preference')
store.upsert_summary(
    session_id='default', start_seq=1, end_seq=2,
    start_time='2026-10-08T10:00:00+08:00',
    end_time='2026-10-08T10:05:00+08:00',
    text='用户自我介绍叫 Kevin,聊了咖啡口味。', anchors=['Kevin'],
)
conn = db.connect_memory()
print('EVENTS', conn.execute('SELECT count(*) FROM events').fetchone()[0])
print('FACTS', conn.execute('SELECT count(*) FROM facts WHERE active = 1').fetchone()[0])
print('SUMMARIES', conn.execute('SELECT count(*) FROM summary_pool').fetchone()[0])
"""

COUNTER = """
from la.storage import db
conn = db.connect_memory()
names = sorted(
    row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
        " AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
)
parts = []
for name in names:
    count = conn.execute("SELECT count(*) FROM " + name).fetchone()[0]
    parts.append(name + "=" + str(count))
print("|".join(parts))
"""


def main() -> int:
    import httpx

    print("验收数据目录: " + DATA_DIR)
    print("=" * 72)

    print(chr(10) + "[1] 灌入三层的种子数据")
    seeded = run_probe("seed", SEED)
    check("事件写入 3 条(含 1 条不进上下文的)", "EVENTS 3" in seeded, seeded.strip()[-200:])
    check("事实写入 2 条", "FACTS 2" in seeded, "")
    check("摘要写入 1 条", "SUMMARIES 1" in seeded, "")
    if "EVENTS 3" not in seeded:
        return _summary()

    port = free_port()
    base = "http://127.0.0.1:" + str(port)
    server = subprocess.Popen(
        [PYTHON, "-m", "la", "--web", "127.0.0.1:" + str(port)],
        cwd=str(BASE_DIR), env=probe_env(),
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
        check("服务器起来了(故意不配密钥)", ready, base)
        if not ready:
            return _summary()

        print(chr(10) + "[2] 导航里有「记忆」")
        page = client.get("/memory")
        check("记忆页返回 200", page.status_code == 200, str(page.status_code))
        body = page.text
        check("导航栏出现「记忆」", 'href="/memory"' in body)
        # M7 重做了导航:改成 sidebar + tabbar,标记是 class="nav-item on" + aria-current,
        # 而且属主顺序是 class 在前 href 在后 —— 所以这里用正则找那个 <a> 块,
        # 而不是硬拼一个字符串(那样每次改模板都会假红)。
        memory_links = re.findall(r'<a[^>]*href="/memory"[^>]*>', body)
        check("导航栏里能找到「记忆」链接", len(memory_links) >= 1, str(len(memory_links)) + " 处")
        check(
            "⭐ 当前页高亮(带 on 或 aria-current)",
            any(("aria-current" in link) or ("on" in link) for link in memory_links),
            (memory_links[0][:90] if memory_links else "(没找到链接)"),
        )
        check("其他页面也带上了这一项", 'href="/memory"' in client.get("/ledger").text, "")

        print(chr(10) + "[3] 八层全在,顺序按数据流向")
        # 注:这里**故意不列 L1** —— L1 的标记字符串是 L1b 的前缀,
        # 用 in 判断会互相干扰;L1 的渲染由下面 [5] 的内容检查覆盖。
        markers = ["L0", "L1b", "L2", "L3", "L3c", "L4", "L5"]
        found = [code for code in markers
                 if '<span class="tag">' + code + "</span>" in body]
        # 2026-10-13(D108):新增 L3c 事实候选层 —— 候选是"还没进事实层"的东西,
        # 所以它排在 L3 与 L4 之间,层次数从 7 变 8。
        check("层次标记都出现(L1 由 [5] 的内容检查覆盖)", len(found) == len(markers),
              ",".join(found))
        positions = [body.index('<span class="tag">' + code + "</span>") for code in markers]
        check("⭐ 顺序是「事实源 → 模型看到的」,没有颠倒",
              positions == sorted(positions), str(positions))

        print(chr(10) + "[4] 可重建标记:归档 / 摘要池 / 事实都不可重建")
        check("归档层标了「不可重建」", "不可重建" in body)
        check("归档层的说明点到「删了就真没了」", "删了就真没了" in body, "")
        # 2026-10-12 审核:L2 摘要池与 L3 事实**不可重建**(归档里没有摘要正文 /
        # 事实的类别与敏感标记),所以"不可重建"至少 3 处(两张表的说明 + 标签),
        # 而"可重建"只剩 L1 / L1b / L4 / L5 这四层。
        check("可重建只剩四层、且不可重建至少三处",
              body.count("可重建") >= 4 and body.count("不可重建") >= 3,
              "可重建 " + str(body.count("可重建")) + " 次 / 不可重建 "
              + str(body.count("不可重建")) + " 次")

        print(chr(10) + "[5] ⭐ 每一层的内容真的看得见")
        check("L0 归档:列出了 jsonl 文件", "default.jsonl" in body, "")
        check("L1 事件镜像:会话行出现", "default" in body, "")
        check("L1b 类型分布:三种事件类型都在",
              all(kind in body for kind in
                  ("user/message", "assistant/message", "tool/result")), "")
        check("L2 摘要池:摘要正文可见", "聊了咖啡口味" in body, "")
        check("L3 事实:两条事实都可见",
              "用户的英文名是 Kevin" in body and "用户喝美式" in body, "")
        # 2026-10-13(D108):L3c 候选层要真的渲染出来,并且说明它"不进上下文";
        # 同时 L3 要显示"这一轮到底注入了哪几条"(上限之外被挤掉时必须看得见)。
        check("L3c 事实候选层渲染出来了",
              "fact_candidates" in body and "入库前的闸门" in body, "")
        check("L3 显示「本轮注入」与「每轮成本」",
              "本轮注入" in body and "每轮成本" in body, "")
        check("L3 有人工动作:停用 / 改口",
              "/deactivate" in body and "/correct" in body, "")
        check("L4 热摘要块:渲染出 memory 原文", "&lt;memory&gt;" in body, "")
        check("L4 里能看到事实内容(说明它真的会进上下文)", "Kevin" in body, "")
        # 2026-10-12 审核:工具 schema 从"混在 system 里"改成**单列一行**(见 D95),
        # 所以这里是"五个文本段 + 工具 schema"六行,少任何一行都算失败。
        check("L5 投影预算:五个文本段 + 工具 schema 都列出来了",
              all(seg in body for seg in ("system", "runtime", "memory",
                                          "evidence", "conversation", "工具 schema")), "")

        print(chr(10) + "[6] ⭐ 看一眼不该改变任何东西")
        before = run_probe("count_before", COUNTER)
        client.get("/memory")
        client.get("/memory")
        after = run_probe("count_after", COUNTER)
        check("两次访问后行数完全没变", before.strip() == after.strip() and len(before) > 40,
              before.strip()[:120])

        print(chr(10) + "[7] 空记忆不会崩,而且会说清怎么办")
        empty_dir = tempfile.mkdtemp(prefix="la-verify-mem-empty-")
        env = dict(probe_env())
        env["LA_DATA_DIR"] = empty_dir
        port2 = free_port()
        empty = subprocess.Popen(
            [PYTHON, "-m", "la", "--web", "127.0.0.1:" + str(port2)],
            cwd=str(BASE_DIR), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        try:
            client2 = httpx.Client(base_url="http://127.0.0.1:" + str(port2), timeout=30.0)
            ok = False
            for _ in range(80):
                try:
                    if client2.get("/healthz").status_code == 200:
                        ok = True
                        break
                except Exception:
                    time.sleep(0.25)
            blank = client2.get("/memory") if ok else None
            check("全新数据目录下记忆页仍是 200",
                  blank is not None and blank.status_code == 200, "")
            if blank is not None:
                check("明确提示没有长期记忆 + 怎么填",
                      "还没有任何" in blank.text
                      and "记住:我不吃香菜" in blank.text, "")
                # ⚠️ 2026-10-17 审计:L5 那一层的名字在 D134/D141 清理里从"投影预算"改成了
                #    "上下文预算与压缩",于是这条一直红着。改成认**层次代号** ——
                #    这才是这条检查真正要守的东西(骨架列全),而且不会因为改名再坏一次。
                check("空页也把层次骨架列全了",
                      all(code in blank.text
                          for code in ("L0", "L1", "L1b", "L2", "L3", "L3c", "L4", "L5")), "")
            client2.close()
        finally:
            empty.terminate()
            try:
                empty.wait(timeout=10)
            except subprocess.TimeoutExpired:
                empty.kill()

        client.close()
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    return _summary()


def _summary() -> int:
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(chr(10) + "=" * 72)
    print("结果: " + str(passed) + "/" + str(len(RESULTS)) + " 项通过")
    for name, ok, _ in RESULTS:
        if not ok:
            print("  未通过: " + name)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())