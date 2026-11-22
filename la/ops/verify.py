"""一条命令跑完所有验收:`python -m la --verify`。

## 为什么要有这个文件

在此之前,验收散在 34 个 `scripts/verify_*.py` 里:每个脚本自己起临时数据目录、起服务器、
打印"97/97 通过"。它们都对,但**没有任何人会把 34 个全跑一遍** —— 于是"改完自己验一遍"
这件事在实践中不存在,存在的是"改完挑一个看着像的跑一遍"。

这个模块把"验收"变成**一条命令 + 一份记录 + 一个退出码**。三件东西缺一不可:

    一条命令   人才会跑(而且能被别的程序调 —— 见 la/ops/gate.py)
    一份记录   事后才答得出"这一版到底验过没有"
    一个退出码 才挡得住"红了还发布"

## 三层,按**要花多少钱**分,不按"重要不重要"分

    T0  离线、免费、**每次自我更新都跑**    单元测试 + 模板编译
    T1  离线、免费、慢(每个几秒到几十秒)  真起服务器的端到端脚本
    T2  真打模型、**花钱**                  评测集与真机验收

⚠️ 分层的第一原则是"闸门必须便宜到会被真的跑"。一个要 5 块钱、20 分钟的闸门,
    人就会跳过它 —— 跳过等于没有。所以默认 `--verify` 只跑 T0。

## 两条硬规则

1. **默认串行**。有四个脚本把端口写死在源码里(8791 / 8793 / 8794),其中两个还用
   同一个端口 —— 并发跑必然撞车。`--jobs` 留着,但默认 1。
2. **不重写脚本**。这里只负责"挑哪些、按什么顺序、结果怎么记",判定逻辑仍归各自的
   脚本(它们比这个文件更懂自己在测什么)。
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Sequence

from .. import config
from . import version as version_mod

#: 记录落在哪(data/ 已 gitignore)。
EVAL_DIR = Path(config.DATA_DIR) / "eval"

TIER_TITLES = {
    0: "T0 · 离线 / 免费 / 每次自我更新都跑",
    1: "T1 · 端到端(离线真起服务器)",
    2: "T2 · 真机(打真实 API,花钱)",
}

#: 退出码 2 的含义:脚本自己说"这次跳过了"(典型:没配密钥、没有浏览器)。
#: ⚠️ 跳过**不算红** —— 否则没密钥的机器上闸门永远是红的,人就会去关掉闸门。
EXIT_SKIPPED = 2


@dataclasses.dataclass(frozen=True)
class Check:
    """一项验收。key 要短(命令行里要打),title 要能直接看懂。"""

    key: str
    title: str
    tier: int
    #: "unit" = 内置的单元测试命令;"script" = scripts/ 下那个脚本;"builtin" = 进程内检查
    kind: str
    target: str = ""
    #: 额外命令行参数(例如人格评测必须给 --label)。
    #: `{stamp}` 会在真跑的时候替换成时间戳:评测记录不该互相覆盖。
    args: tuple[str, ...] = ()
    #: 给人看的"需要什么";空 = 什么都不需要
    needs: str = ""
    #: 这一项的**超时**(秒),不是预计耗时
    timeout_s: int = 600
    why: str = ""


def _script(key: str, title: str, why: str = "", timeout_s: int = 600,
            path: str = "") -> Check:
    """登记一个 scripts/ 下的验收脚本。path 留空就按 key 拼文件名。"""
    return Check(key=key, title=title, tier=1, kind="script",
                 target=path or ("scripts/verify_" + key + ".py"),
                 why=why, timeout_s=timeout_s)


#: T0 —— 自我更新的**最低门槛**。必须快、必须免费、必须每次都跑。
T0: tuple[Check, ...] = (
    Check("unit", "全量单元测试", 0, "unit", timeout_s=900,
          why="一千多条;这是唯一能发现结构性退化的东西"),
    Check("templates", "页面模板能编译", 0, "builtin", timeout_s=60,
          why="改模板是最常见的手滑;Jinja 语法错要到打开页面才炸"),
)

#: T1 —— 端到端。真起服务器 / 真写库,但**不打模型**(LA_LLM_PROVIDER=fake)。
T1: tuple[Check, ...] = (
    _script("m1", "M1 记忆:写入 → 摘要 → 跨会话命中"),
    _script("m2", "M2 检索与反馈"),
    _script("m3", "M3 目标 / 简报 / 提醒 / 技能"),
    _script("m4_0", "M4.0 服务层地基 + 发件箱 + 审计"),
    _script("m4_1", "M4.1 账单导入 / 去重 / 导出"),
    _script("m4_2", "M4.2 日历 RRULE / 例外 / 提醒不重发"),
    _script("m4_3a", "M4.3a 拍照录入到错题"),
    _script("m4_3c", "M4.3c 排程 / 复习日志 / 孩子端"),
    _script("m4_4", "M4.4 健康测量 / 趋势 / 能量收支"),
    _script("m5", "M5 五个页面与写操作"),
    _script("m6", "M6 安全与体验(备份 / 护栏)"),
    _script("m7", "M7 统一回执 / 一键撤销"),
    _script("m9", "M9 账本收支一体 + 反馈回路"),
    _script("m10", "M10 拍照录入 / 学生档案 / 推送"),
    _script("m11", "M11 素材层 / 产物层 / 微信媒体"),
    _script("m12", "M12 基础能力层(读 / 看 / 找 / 写 / 联网)"),
    _script("bridge", "桥接层:渠道无关的那一半"),
    _script("ui", "六个页面在模型不可用下都能打开"),
    _script("memory", "记忆页:每一层都看得见"),
    _script("trace", "轨迹页:看到的就是发出去的那一份", path="scripts/verify_trace_page.py"),
    _script("persona_page", "人格页:改得动、下一轮生效、退得回去"),
    _script("report_page", "使用报表:数字与库里一致"),
    _script("self_view", "自我观察:它看到的账与库对得上"),
    _script("wechat_voice", "微信语音:转写文字进来,不留假承诺"),
)

#: T2 —— 真打模型。**花钱**,所以只在候选版本上跑,并且要人看着。
T2: tuple[Check, ...] = (
    Check("persona", "人格装配评测(23 条,真打模型)", 2, "script",
          "scripts/verify_persona.py", needs="真实 API", timeout_s=1800,
          # ⚠️ 这个脚本**必须**带 --label,否则 argparse 报错退 2 —— 而 2 的含义是
          #    "脚本自己说跳过",于是这一项会**静默变绿**(2026-10-17 审计发现的坑)。
          args=("--label", "t2-{stamp}"),
          why="D63 的那套手段;记录落在 data/eval/persona-t2-<时间>.json"),
    Check("smoke", "真实 API 冒烟", 2, "script", "scripts/smoke_real_api.py",
          needs="真实 API", timeout_s=300),
    Check("reasoning", "思维链回传规则", 2, "script", "scripts/verify_reasoning_replay.py",
          needs="真实 API", timeout_s=300),
    Check("reasoning_budget", "思考预算:空串真的不思考、裁决不被截断", 2, "script",
          "scripts/verify_reasoning_budget.py", needs="真实 API", timeout_s=300),
    Check("tool_schema", "整份工具清单被 API 接受", 2, "script",
          "scripts/verify_tool_schema_live.py", needs="真实 API", timeout_s=300),
    Check("prefix_cache", "前缀缓存:易变段放在哪", 2, "script",
          "scripts/verify_prefix_cache.py", needs="真实 API", timeout_s=300),
    Check("page_shot", "它能不能自己看一眼网页", 2, "script",
          "scripts/verify_page_shot.py", needs="真实 API(视觉)", timeout_s=600),
    Check("audio", "TTS 音频走到聊天窗口", 2, "script",
          "scripts/verify_audio_delivery.py", needs="真实 TTS", timeout_s=600),
)

CHECKS: tuple[Check, ...] = T0 + T1 + T2

#: **不进闸门**的脚本:分析工具与一次性清理。列出来是为了不再"消失在目录里",
#: 但把它们塞进闸门只会让闸门变慢、变脆。
TOOLS: tuple[tuple[str, str], ...] = (
    ("scripts/analyze_context_cost.py", "上下文成本:缓存命中 / 每一轮由什么组成"),
    ("scripts/analyze_conversation_budget.py", "对话窗口预算:对话段到底买住了多少"),
    ("scripts/fix_legacy_facts.py", "一次性:清理准入闸上线前混进来的事实"),
    ("scripts/move_self_identity_to_persona.py",
     "一次性:把「关于助手自己」的事实迁进人格页并停用(默认 dry-run)"),
    ("scripts/migrate_archive_paths.py", "一次性:微信 ADS 档案迁成普通 JSONL"),
    ("scripts/migrate_member_sessions.py", "一次性:旧 web/微信会话合并成 member:<id>"),
    ("scripts/reset_pin.py", "本机兜底:忘记 PIN/恢复码时重置成员 PIN"),
    ("scripts/diagnose_bindings.py", "微信双人验收:看联系人绑到谁、消息落进哪个会话"),
    ("scripts/preview_ledger.py", "看账本页长什么样:导出可直接截图的单文件 HTML"),
    ("scripts/preview_pages.py", "看健康页 / 素材页长什么样:同上"),
    ("scripts/preview_health.py", "看健康页四个页签长什么样并出截图(不需要真模型)"),
    ("scripts/regenerate_summaries.py",
     "一次性:用当前摘要器重生成指定旧摘要(默认 dry-run,带备份)"),
)

BY_KEY = {check.key: check for check in CHECKS}


# --- 结果 --------------------------------------------------------------------


@dataclasses.dataclass
class CheckResult:
    key: str
    title: str
    tier: int
    ok: bool
    skipped: bool = False
    seconds: float = 0.0
    detail: str = ""
    passed: int = 0
    total: int = 0
    exit_code: int | None = None

    @property
    def mark(self) -> str:
        if self.skipped:
            return "--"
        return "OK" if self.ok else "XX"


@dataclasses.dataclass
class Report:
    tier: int
    items: list[CheckResult] = dataclasses.field(default_factory=list)
    generated_at: str = ""
    elapsed_s: float = 0.0
    signature: dict[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def red(self) -> list[CheckResult]:
        return [item for item in self.items if not item.ok and not item.skipped]

    @property
    def skipped(self) -> list[CheckResult]:
        return [item for item in self.items if item.skipped]

    @property
    def ok(self) -> bool:
        return not self.red

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "verify",
            "tier": self.tier,
            "ok": self.ok,
            "generated_at": self.generated_at,
            "elapsed_s": round(self.elapsed_s, 1),
            "signature": self.signature,
            "red": [item.key for item in self.red],
            "items": [dataclasses.asdict(item) for item in self.items],
        }

    def render(self) -> str:
        if self.items:
            head = ("验收 · " + TIER_TITLES.get(self.tier, "T" + str(self.tier))
                    + " · " + str(len(self.items)) + " 项")
        else:
            head = "验收(没有选中任何项)"
        lines = [head]
        width = max([_width(item.title) for item in self.items] + [10])
        for tier in sorted({item.tier for item in self.items}):
            lines.append("")
            lines.append(TIER_TITLES.get(tier, "T" + str(tier)))
            for item in self.items:
                if item.tier != tier:
                    continue
                lines.append("  [" + item.mark + "] " + item.key.ljust(12) + "  "
                             + _pad(item.title, width) + "  "
                             + ("%5.1fs" % item.seconds).ljust(7) + "  " + item.detail)
        lines.append("")
        lines.append("-" * 66)
        if self.red:
            lines.append("红的 " + str(len(self.red)) + " 项:" + ", ".join(i.key for i in self.red))
        else:
            lines.append("全部通过" + ("" if not self.skipped else
                                     "(" + str(len(self.skipped)) + " 项跳过)"))
        return "\n".join(lines)


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    """按**显示宽度**补空格 —— 中文标题用 ljust 会差一截,表格就歪了。"""
    return text + " " * max(0, width - _width(text))


# --- 选择 --------------------------------------------------------------------


def select(tier: int = 0, only: str = "") -> list[Check]:
    """挑出这一轮要跑的项。tier 是**累积**的:--verify 1 表示 T0 + T1。"""
    picked = [check for check in CHECKS if check.tier <= tier]
    needle = (only or "").strip().lower()
    if needle:
        picked = [check for check in picked
                  if needle in check.key.lower() or needle in check.title.lower()]
    return picked


# --- 单飞锁 ------------------------------------------------------------------
#
# ⚠️ 2026-10-17 审计实测:两轮验收同时跑会**卡死**(正常 85 秒的单元测试,两个实例一起跑
#    30 分钟不动)。而这正是 D146 之后会出现的场景:后台闸门在跑 T0,人又手动跑一次。
#    单机单进程的项目不需要分布式锁,一个**独占文件**就够 —— 拿不到就明说"已经在跑",
#    而不是让两个实例互相踩。
LOCK_PATH = Path(tempfile.gettempdir()) / "la-verify.lock"
#: 锁超过这么久就当它是上一轮崩掉留下的(单元测试本身 85 秒;T1 全套也就几分钟)。
LOCK_STALE_S = 45 * 60
EXIT_BUSY = 3


class VerifyBusy(RuntimeError):
    """已经有一轮验收在跑。"""


class verify_lock:  # noqa: N801 - 与 contextlib 的用法保持一致
    """独占文件锁。拿不到抛 VerifyBusy;拿得到就在退出时删掉。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or LOCK_PATH)
        self.held = False

    def _stale(self) -> bool:
        try:
            return (time.time() - self.path.stat().st_mtime) > LOCK_STALE_S
        except OSError:
            return True

    def __enter__(self) -> "verify_lock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in (1, 2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if attempt == 1 and self._stale():
                    # 上一轮崩了留下的:删掉重来(只在"确认很旧"时这么做)
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                raise VerifyBusy(
                    "已经有一轮验收在跑(" + str(self.path) + ")。"
                    "等它结束,或删掉这个文件再试。") from None
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(str(os.getpid()))
            self.held = True
            return self
        raise VerifyBusy("拿不到验收锁:" + str(self.path))

    def __exit__(self, *_exc: object) -> None:
        if self.held:
            try:
                self.path.unlink()
            except OSError:
                pass
            self.held = False


# --- 执行 --------------------------------------------------------------------

Runner = Callable[[Check, int], CheckResult]


def python_exe() -> str:
    """子进程该用哪个解释器。

    ⚠️ 服务是用 `pythonw.exe` 起的(无控制台),那时 `sys.executable` 也是
       `pythonw.exe`:子进程没有 stdout,unittest 写日志那一步会炸。
       同目录下换成 `python.exe` —— 它一定在(同一个 venv)。
    """
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        sibling = exe.with_name("python.exe")
        if sibling.exists():
            return str(sibling)
    return str(exe)


def run_subprocess(args: Sequence[str], timeout_s: int) -> tuple[int, str, str, float]:
    env = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    import os

    full_env = {**os.environ, **env}
    started = time.time()
    try:
        proc = subprocess.run(
            list(args), cwd=str(config.BASE_DIR), env=full_env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout_s,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or "", time.time() - started
    except subprocess.TimeoutExpired:
        return 124, "", "超时:%d 秒还没跑完" % timeout_s, time.time() - started
    except OSError as exc:
        return 127, "", "起不来:" + str(exc), time.time() - started
    except Exception as exc:  # noqa: BLE001 - 一项炸了不该带走整轮
        return 1, "", type(exc).__name__ + ": " + str(exc), time.time() - started


_UNIT_RAN = re.compile(r"Ran (\d+) tests?")
_UNIT_FAILED = re.compile(r"FAILED \(([^)]*)\)")
#: 第一条红的用例名 —— 只写"1542/1557 条"没法让人知道去哪修(2026-10-25 云端)。
_UNIT_FIRST_BAD = re.compile(r"^(?:FAIL|ERROR):\s+([^\n(]+)", re.M)
_FRACTION = re.compile(r"(\d+)\s*/\s*(\d+)\s*项")
_ALL_PASS = re.compile(r"(?:全部通过|结果)[:：]?\s*(\d+)")


def _parse_counts(check: Check, out: str) -> tuple[int, int, str]:
    """从输出里抠出 (通过数, 总数, 一句明细)。抠不出来就返回 (0, 0, 末行)。"""
    if check.kind == "unit":
        ran = _UNIT_RAN.search(out)
        total = int(ran.group(1)) if ran else 0
        bad = 0
        failed = _UNIT_FAILED.search(out)
        if failed:
            for piece in failed.group(1).split(","):
                key, _, value = piece.partition("=")
                if key.strip() in ("failures", "errors"):
                    bad += int(value.strip() or 0)
        passed = max(0, total - bad)
        if total:
            detail = str(passed) + "/" + str(total) + " 条"
            first = _UNIT_FIRST_BAD.search(out)
            if first:
                detail += " · 首个:" + first.group(1).strip()[:60]
            return passed, total, detail
        return 0, 0, ""

    frac = _FRACTION.search(out)
    if frac:
        return int(frac.group(1)), int(frac.group(2)), frac.group(1) + "/" + frac.group(2) + " 项"
    total = _ALL_PASS.search(out)
    if total:
        return int(total.group(1)), int(total.group(1)), total.group(1) + " 项"
    return 0, 0, ""


def _tail_detail(err: str, out: str, limit: int = 110) -> str:
    """从 stderr(优先)与 stdout 里拼一句"红在哪"。

    ⚠️ 只取最后一行是不够的:异常的**消息**往往在倒数第二三行,而最后一行可能是空行、
       或者一句"失败 1/4" —— 那种 detail 等于没写。
    """
    for text in (err, out):
        rows = [row.strip() for row in (text or "").splitlines() if row.strip()]
        if not rows:
            continue
        tail = rows[-1]
        if len(rows) >= 2 and len(tail) < 12:
            tail = rows[-2] + " / " + tail
        return tail[:limit]
    return ""


def _last_line(text: str, limit: int = 90) -> str:
    for line in reversed([row.strip() for row in (text or "").splitlines()]):
        if line:
            return line[:limit]
    return ""


def _check_templates(check: Check, _timeout: int) -> CheckResult:
    """把每个模板都编译一遍(解析 + 编译,不渲染)。

    ⚠️ 用 Jinja 自己的转义/块规则解析:语法错在这里暴露,而不是等你打开页面。
    """
    started = time.time()
    try:
        from jinja2 import Environment, FileSystemLoader

        root = Path(config.BASE_DIR) / "la" / "web" / "templates"
        env = Environment(loader=FileSystemLoader(str(root)))
        names = sorted(p.name for p in root.glob("*.html"))
        if not names:
            return CheckResult(check.key, check.title, check.tier, True, seconds=0.0,
                               detail="没有模板(空目录)")
        for name in names:
            env.get_template(name)
        return CheckResult(check.key, check.title, check.tier, True,
                           seconds=time.time() - started,
                           detail=str(len(names)) + " 个模板")
    except Exception as exc:  # noqa: BLE001 - 编译失败就是红
        return CheckResult(check.key, check.title, check.tier, False,
                           seconds=time.time() - started,
                           detail=type(exc).__name__ + ": " + str(exc)[:80])


def _builtin(check: Check, timeout_s: int) -> CheckResult:
    if check.key == "templates":
        return _check_templates(check, timeout_s)
    return CheckResult(check.key, check.title, check.tier, False,
                       detail="未知的内置检查:" + check.key)


def run_check(check: Check, timeout_s: int) -> CheckResult:
    """跑一项。子进程的退出码是唯一判据 —— 打印的话谁都会打。"""
    if check.kind == "builtin":
        return _builtin(check, timeout_s)
    if check.kind == "unit":
        args = [python_exe(), "-m", "unittest", "discover", "-s", "tests", "-t", "."]
    else:
        stamp = time.strftime("%Y%m%d-%H%M")
        args = [python_exe(), check.target,
                *[piece.replace("{stamp}", stamp) for piece in check.args]]
    code, out, err, seconds = run_subprocess(args, timeout_s)
    # ⚠️ unittest 把 "Ran N tests" 与 "FAILED (...)" 写到 **stderr**:只解析 stdout 会
    #    拿到一条没有数字的 "OK",人就看不出到底跑了多少条。
    passed, total, detail = _parse_counts(check, out + "\n" + err)
    ok = code == 0
    skipped = code == EXIT_SKIPPED
    if not detail:
        # ⚠️ 瞬时失败(429 / 超时)时脚本常常最后一行是空的 —— 那就往前多找两行,
        #    否则 --verify 的表格里只剩一个空 detail,人根本不知道红在哪。
        detail = _tail_detail(err, out)
    if code == 124:
        detail = "超时(%d 秒)" % timeout_s
    elif skipped and not detail:
        detail = "脚本自己说跳过(通常是没配密钥)"
    return CheckResult(check.key, check.title, check.tier, ok, skipped=skipped,
                       seconds=seconds, detail=detail, passed=passed, total=total,
                       exit_code=code)


def run(checks: Sequence[Check], *, runner: Runner = run_check,
        progress: Callable[[CheckResult], None] | None = None,
        timeout_override: int = 0) -> Report:
    report = Report(tier=max((c.tier for c in checks), default=0))
    started = time.time()
    for check in checks:
        timeout_s = int(timeout_override or check.timeout_s)
        result = runner(check, timeout_s)
        report.items.append(result)
        if progress is not None:
            progress(result)
    report.elapsed_s = time.time() - started
    report.generated_at = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        report.signature = version_mod.signature().as_dict()
    except Exception:  # noqa: BLE001 - 签名拿不到不该让"验收结果"没了
        report.signature = {}
    return report


def write_record(report: Report, directory: Path | None = None) -> Path:
    """落一份记录。文件名带时间戳:历史要能排序,最新那份才排得出来。"""
    target = Path(directory or EVAL_DIR)
    target.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = target / ("verify-" + stamp + ".json")
    path.write_text(json.dumps(report.as_dict(), ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def latest_record(directory: Path | None = None) -> dict[str, Any] | None:
    target = Path(directory or EVAL_DIR)
    if not target.exists():
        return None
    files = sorted(target.glob("verify-*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def render_list() -> str:
    lines = ["可跑的验收项(--verify <层级> 是**累积**的:T1 = T0 + T1)", ""]
    for tier in (0, 1, 2):
        lines.append(TIER_TITLES[tier])
        for check in CHECKS:
            if check.tier != tier:
                continue
            needs = ("  ⚠️ " + check.needs) if check.needs else ""
            lines.append("  " + check.key.ljust(12) + check.title + needs)
            if check.why:
                lines.append("  " + " " * 12 + "· " + check.why)
        lines.append("")
    lines.append("不进闸门(分析 / 一次性工具)")
    for path, what in TOOLS:
        lines.append("  " + path.ljust(40) + what)
    return "\n".join(lines)
# --- 命令行入口 --------------------------------------------------------------


def _level_to_tier(level: str) -> int:
    text = str(level or "0").strip().lower()
    if text in ("all", "全部", "3"):
        return 2
    if text in ("0", "1", "2"):
        return int(text)
    raise ValueError("层级只能是 0 / 1 / 2 / all,拿到的是:" + text)


def cmd_verify(
    *,
    level: str = "0",
    only: str = "",
    as_json: bool = False,
    dry_run: bool = False,
    quiet: bool = False,
    list_checks: bool = False,
    timeout: int = 0,
    runner: Runner = run_check,
    directory: Path | None = None,
    lock_path: Path | None = None,
) -> int:
    """`python -m la --verify` 的真身。

    返回:0 全过 / 1 有红 / 2 一项都没跑(没有匹配项)。
    """
    if list_checks:
        print(render_list())
        return 0

    try:
        tier = _level_to_tier(level)
    except ValueError as exc:
        print(str(exc))
        return 2

    checks = select(tier, only)
    if not checks:
        print("没有匹配的验收项(层级 " + str(tier) + (", 过滤 " + only if only else "") + ")。")
        print("用 --list-checks 看有哪些。")
        return 2

    if dry_run:
        print("会跑这 " + str(len(checks)) + " 项:")
        for check in checks:
            print("  T" + str(check.tier) + "  " + check.key.ljust(12) + check.title)
        return 0

    if tier >= 2 and not as_json and not quiet:
        print("⚠️ T2 会真打模型、真的花钱。要省着来:先跑 --verify 1。")

    def progress(result: CheckResult) -> None:
        if quiet or as_json:
            return
        print("  [" + result.mark + "] " + result.key.ljust(12)
              + ("%6.1fs" % result.seconds).ljust(8) + result.detail, flush=True)

    # ⚠️ 两轮验收不许同时跑(实测会卡死)。拿不到锁就明说,不要硬上。
    try:
        lock = verify_lock(lock_path).__enter__()
    except VerifyBusy as exc:
        print(str(exc))
        if as_json:
            print(json.dumps({"kind": "verify", "ok": False, "busy": True,
                              "detail": str(exc)}, ensure_ascii=False))
        return EXIT_BUSY
    try:
        report = run(checks, runner=runner, progress=progress, timeout_override=timeout)
        path = write_record(report, directory)
    finally:
        lock.__exit__()

    if as_json:
        # ⚠️ --json 只许输出**这一行**:la/ops/gate.py 靠它读结果。
        print(json.dumps(report.as_dict(), ensure_ascii=False))
        return 0 if report.ok else 1

    if not quiet:
        print("")
        print(report.render())
    print("记录:" + str(path))
    print("结论:" + ("可以发布" if report.ok
                    else "不要发布(红 " + str(len(report.red)) + " 项)"))
    return 0 if report.ok else 1
