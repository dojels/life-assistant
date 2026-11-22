"""M6 端到端验收:安全与体验。

⭐ 核心 DoD:**误删能撤销、乱传路径会被拒、备份真的能恢复、只读开关真的拦得住**。
   每一条都在**独立进程**里跑(不是同一个 Python 里自说自话)。

    .venv/Scripts/python.exe scripts/verify_m6.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
DATA_DIR = tempfile.mkdtemp(prefix="la-verify6-")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + ("   " + detail if detail else ""))


def probe_env() -> dict[str, str]:
    """探针脚本住在临时目录里,所以要把项目根塞进 PYTHONPATH 才 import 得到 la。"""
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
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    return (result.stdout or "") + (result.stderr or "")


def cli(*args: str, env_extra: dict[str, str] | None = None, expect: int = 0) -> str:
    """跑一次真实的命令行进程,返回合并后的输出。"""
    env = dict(os.environ)
    env.update({
        "LA_DATA_DIR": DATA_DIR,
        "PYTHONIOENCODING": "utf-8",
        "DEEPSEEK_API_KEY": "",
    })
    env.pop("LA_OPENAI_KEY", None)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [PYTHON, "-m", "la", *args],
        cwd=str(BASE_DIR), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    if expect is not None and proc.returncode != expect:
        print("    (命令 " + " ".join(args) + " 退出码 " + str(proc.returncode) + ")")
        print("    " + output.strip().replace("\n", "\n    ")[:1200])
    return output


def main() -> int:
    print("验收数据目录: " + DATA_DIR)
    print("=" * 72)

    # ---- 0. 先把库建起来,并塞一笔可撤销的交易 ---------------------------
    print("\n[0] 准备数据")
    cli("--check")
    seed = (
        "import sqlite3, json\n"
        "from la.storage import db\n"
        "from la.domain.services.ledger_service import LedgerService\n"
        "from la.domain.schemas.ledger import ExpenseCreate\n"
        "conn = db.connect_assistant()\n"
        "ledger = LedgerService(conn)\n"
        "rec = ledger.record_expense(ExpenseCreate(amount_cents=8800, category='餐饮',"
        " account='支付宝', payee='海底捞', narration='聚餐'), actor='user')\n"
        "ledger.confirm(rec.id)\n"
        "print('SEEDED', rec.id)\n"
    )
    seeded = run_probe("seed", seed)
    seed_ok = "SEEDED" in seeded
    check("种子数据写入(一笔已确认支出)", seed_ok, seeded[-200:])
    if not seed_ok:
        return _summary()

    # ---- 1. 软删除 + 撤销 -------------------------------------------------
    print("\n[1] 误删能撤销(M6.3)")
    listing = cli("--ledger", "5")
    check("删除前账本能看到这笔", "海底捞" in listing, listing.strip()[-80:])

    out = cli("--rm-expense", "1", "--reason", "手滑")
    check("删除成功且提示可撤销", "已删除" in out and "--undo" in out, out.strip()[-90:])

    listing = cli("--ledger", "5")
    check("⭐ 删除后账本里看不见了", "海底捞" not in listing, listing.strip()[-80:])

    out = cli("--undo-log")
    check("撤销日志记下了这一步", "海底捞" in out, out.strip()[-90:])

    out = cli("--undo")
    check("撤销执行成功", "已撤销" in out, out.strip()[-90:])

    listing = cli("--ledger", "5")
    check("⭐ 撤销后又回来了", "海底捞" in listing, listing.strip()[-80:])

    out = cli("--undo")
    check("撤销不能重复使用", "不能撤销" in out, out.strip()[-90:])

    # ---- 2. 软删除不影响汇总/导出 ----------------------------------------
    print("\n[2] 删除的数据不参与统计(M6.3)")
    before = cli("--query-expense", "2026-10")
    cli("--rm-expense", "1")
    after = cli("--query-expense", "2026-10")
    check("删除后聚合变小(或归零)",
          before != after or "合计 0 分" in after,
          "删除前:" + before.strip().splitlines()[1][:40] if len(before.strip().splitlines()) > 1 else "")
    export = cli("--export-beancount")
    check("删除后导出里没有它", "海底捞" not in export)
    cli("--undo")

    # ---- 3. 路径白名单 ----------------------------------------------------
    print("\n[3] 路径白名单(M6.5)")
    probe = (
        "import asyncio\n"
        "from la.domain.tools import check_tool_path\n"
        "from la.ops.paths import PathNotAllowed\n"
        "blocked = []\n"
        "for bad in ['.env', 'C:/Windows/System32/drivers/etc/hosts', '../../secret.csv',"
        " 'id_rsa']:\n"
        "    try:\n"
        "        check_tool_path(bad)\n"
        "    except PathNotAllowed:\n"
        "        blocked.append(bad)\n"
        "print('BLOCKED', len(blocked))\n"
        "try:\n"
        "    check_tool_path('README.md')\n"
        "    print('ALLOWED_OK')\n"
        "except PathNotAllowed:\n"
        "    print('ALLOWED_FAIL')\n"
    )
    body = run_probe("pathprobe", probe)
    check("⭐ .env / 系统路径 / 穿越 / 私钥 全部被拒", "BLOCKED 4" in body, body.strip()[-160:])
    check("项目内的正常文件仍然放行", "ALLOWED_OK" in body, body.strip()[-80:])

    # ---- 4. 只读开关 ------------------------------------------------------
    print("\n[4] 一键停止写入(M6/护栏 G18)")
    probe = (
        "import asyncio\n"
        "from la.base.phases import PHASE_ANSWER, CircuitBreaker\n"
        "from la.tools.registry import CODE_READ_ONLY, ToolRegistry, ToolSpec\n"
        "calls = []\n"
        "async def reader(p):\n"
        "    calls.append('read')\n"
        "    return {'value': 1}\n"
        "async def writer(p):\n"
        "    calls.append('write')\n"
        "    return {'value': 2}\n"
        "reg = ToolRegistry(read_only=True)\n"
        "reg.register(ToolSpec(name='query_summary_pool', description='r', phase=PHASE_ANSWER,\n"
        "                      risk='low', timeout_ms=1000, handler=reader, writes=False))\n"
        "reg.register(ToolSpec(name='expense_record', description='w', phase=PHASE_ANSWER,\n"
        "                      risk='low', timeout_ms=1000, handler=writer))\n"
        "b = CircuitBreaker(phase=PHASE_ANSWER)\n"
        "r = asyncio.run(reg.execute('query_summary_pool', {}, breaker=b))\n"
        "w = asyncio.run(reg.execute('expense_record', {}, breaker=b))\n"
        "print('READ_OK', r.ok)\n"
        "print('WRITE_BLOCKED', (not w.ok) and w.error_code == CODE_READ_ONLY)\n"
        "print('HANDLER_NOT_CALLED', 'write' not in calls)\n"
    )
    body = run_probe("ro", probe)
    check("只读模式下读工具照常可用", "READ_OK True" in body, body.strip()[-200:])
    check("⭐ 只读模式下写工具被拒(READ_ONLY)", "WRITE_BLOCKED True" in body, "")
    check("⭐ 写操作的 handler 根本没被调用", "HANDLER_NOT_CALLED True" in body, "")

    # ---- 5. 备份 + 恢复演练 -----------------------------------------------
    print("\n[5] 备份与恢复演练(M6.6)")
    out = cli("--backup")
    check("备份跑通", "备份完成" in out, out.strip().splitlines()[0][:80] if out.strip() else "")

    out = cli("--backups")
    check("备份可列出且带体积", out.count("- ") >= 1 and "MB" in out, out.strip()[-100:])

    out = cli("--restore-drill")
    check("⭐ 恢复演练:真的恢复了一份", "恢复演练" in out, "")
    check("⭐ 还原库完整性检查通过", "assistant.db 完整性" in out and "✅" in out,
          out.strip()[-160:])
    check("⭐ 当前代码能读还原库(app_txns_live 视图在)",
          "当前代码能读还原库" in out and "app_txns_live" in out, "")
    check("⭐ 行数与线上逐表对上", "行数与线上一致" in out, "")
    check("演练结论是「可用」", "能救命" in out, out.strip().splitlines()[-1][:80])

    # ---- 6. 护栏 ----------------------------------------------------------
    print("\n[6] 护栏检查(M6.1)")
    out = cli("--guardrails", expect=None)
    check("护栏全部跑完", "条检查" in out, out.strip().splitlines()[-1][:70] if out.strip() else "")
    check("审计只追加得到验证(G01)", "G01" in out and "⚠️" not in out.split("G01")[1][:60], "")
    check("不适用项写清了理由", "⏭️" in out, "")
    failed = [line for line in out.splitlines() if line.strip().startswith("❌")]
    check("没有任何一条护栏失败", not failed, ";".join(failed)[:200])

    # ---- 7. 周报 / 月报 ---------------------------------------------------
    print("\n[7] 周报月报(M6.8)")
    out = cli("--report", "7")
    check("周报能出", "小结" in out, out.strip().splitlines()[0][:70] if out.strip() else "")
    out30 = cli("--report", "30")
    check("月报能出(天数可配)", "小结" in out30 and out30 != out, "")

    # ---- 8. 隐私自查 ------------------------------------------------------
    print("\n[8] 隐私自查(M6.7)")
    out = cli("--privacy")
    check("列出数据文件与体积", "数据目录" in out, "")
    check("⭐ 分清「会出网」和「永不出网」的表",
          "会随请求发给模型 API 的表" in out and "永不出网的本地表" in out, "")
    check("账本被明确标为本地", "app_txns" in out.split("永不出网的本地表")[-1][:200], "")
    check("密钥只以掩码出现", "sk-" not in out or "…" in out, "")
    check("给出信用来源", "密钥来源" in out, "")

    # 密钥泄漏探针:把真 key 塞进 .env,再跑一次 --privacy
    env_file = BASE_DIR / ".env"
    original = env_file.read_text(encoding="utf-8") if env_file.exists() else None
    try:
        if original is not None:
            env_file.write_text(
                original + "\nLA_PRIVACY_PROBE=sk-probe0123456789abcdef\n", encoding="utf-8"
            )
        out = cli("--privacy")
        check("⭐ 完整密钥绝不出现在输出里", "sk-probe0123456789abcdef" not in out, "")
    finally:
        if original is not None:
            env_file.write_text(original, encoding="utf-8")

    # ---- 9. 幂等语义 ------------------------------------------------------
    print("\n[9] 幂等语义(M6.2)")
    probe = (
        "import os\n"
        "from la.storage import db\n"
        "from la.ops.idempotency import IdempotencyStore\n"
        "conn = db.connect_assistant()\n"
        "store = IdempotencyStore(conn)\n"
        "first = store.begin('v6-token', {'v': 70.5})\n"
        "store.complete('v6-token', {'ok': True})\n"
        "second = store.begin('v6-token', {'v': 70.5})\n"
        "clash = store.begin('v6-token', {'v': 99.9})\n"
        "print('KINDS', first.kind, second.kind, second.http_status, clash.kind, clash.http_status)\n"
        "store2 = IdempotencyStore(conn)\n"
        "store2.begin('v6-fail', {'v': 1})\n"
        "store2.fail('v6-fail', 'boom')\n"
        "retry = store2.begin('v6-fail', {'v': 1})\n"
        "print('RETRY', retry.kind)\n"
    )
    body = run_probe("idem", probe)
    check("首次是 new,重复是 replay(200)", "KINDS new replay 200" in body, body.strip()[-160:])
    check("同 key 不同载荷是 conflict(422)", "conflict 422" in body, "")
    check("⭐ 失败过的 key 可以重试,不会永久卡死", "RETRY new" in body, "")

    # ---- 10. 历史导入 -----------------------------------------------------
    print("\n[10] 历史导入(M6.9)")
    history = Path(DATA_DIR) / "old_chat.json"
    history.write_text(
        json.dumps([
            {"role": "user", "content": "我家孩子今年上初二,数学比较弱"},
            {"role": "assistant", "content": "记下了。要不要先从薄弱知识点开始?"},
            {"role": "user", "content": "我习惯早上七点起床"},
        ], ensure_ascii=False),
        encoding="utf-8",
    )
    out = cli("--import-history", str(history))
    check("历史记录导入成功", "导入完成" in out, out.strip()[-100:])

    out2 = cli("--import-history", str(history))
    check("⭐ 同一个文件第二次导入不会翻倍", "已经导过了" in out2, out2.strip()[-100:])

    probe = (
        "from la.storage import db\n"
        "conn = db.connect_memory()\n"
        "n = conn.execute(\"SELECT count(*) FROM events WHERE session_id = 'imported'\""
        ").fetchone()[0]\n"
        "print('EVENTS', n)\n"
    )
    body = run_probe("histcheck", probe)
    check("⭐ 事件数正好是 3 条(没有重复)", "EVENTS 3" in body, body.strip()[-120:])

    bad = Path(DATA_DIR) / "not_a_chat.txt"
    bad.write_text("这是一段没有角色标记的普通文本。\n随便写点什么。\n", encoding="utf-8")
    out3 = cli("--import-history", str(bad), expect=None)
    check("⭐ 解析不出对话时明确报错,而不是静默写空", "导入失败" in out3, out3.strip()[-100:])

    return _summary()


def _summary() -> int:
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n" + "=" * 72)
    print("结果: " + str(passed) + "/" + str(len(RESULTS)) + " 项通过")
    for name, ok, _ in RESULTS:
        if not ok:
            print("  未通过: " + name)
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
