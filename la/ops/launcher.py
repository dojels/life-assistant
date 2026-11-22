"""服务启动器:一条命令启停/重启家庭助手与**所有**微信渠道。

**为什么需要它**:改完代码要重启两个进程,还要确认它们真的起来了 ——
手动做这件事既烦又容易忘。这个启动器把"启/停/重启/看状态/看日志"收成一条命令。

**设计上刻意做的几个选择**:

1. **不引任何第三方依赖**(不用 psutil)。进程存活用 Windows 自带的 `tasklist` 判断,
   零依赖、零安装 —— 和 D3「计划任务零依赖」的口径一致。
2. **用 `pythonw.exe` 拉起服务**,不弹黑窗口。日志自己重定向到 `data/logs/`。
3. **不做 Windows 服务封装**(D3 已否决:要管理员权限、要装/卸脚本,对本项目无收益)。
   要开机自启就用 `task` 子命令注册计划任务 —— 那是 D3 选定的路。
4. **进程会死,所以有 `--watch`**:带看护模式时,子进程退出会被重新拉起。
   默认**不开**看护:自动复活是"自主性"那类东西,《自我生存.md》要求它必须在
   **主人明确配置**下才允许(`自我生存.md` §684:"不得自行注册自启项")。

用法:

    .venv/Scripts/python.exe -m la.ops.launcher start        # 启动全部(Web + 每个微信号)
    .venv/Scripts/python.exe -m la.ops.launcher restart      # 重启全部(改完代码就它)
    .venv/Scripts/python.exe -m la.ops.launcher status       # 看状态
    .venv/Scripts/python.exe -m la.ops.launcher logs wechat  # 默认微信账号的日志
    .venv/Scripts/python.exe -m la.ops.launcher logs wechat-mom  # 指定账号的日志
    .venv/Scripts/python.exe -m la.ops.launcher stop         # 停止全部

    start web / start wechat     # 只启一个
    task install                 # 注册开机自启(计划任务,登录时启动)
    task remove                  # 取消开机自启
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config

#: 计划任务名(注册开机自启用)
TASK_NAME = "LifeAssistant"


def harden_console() -> None:
    """把控制台切到 UTF-8(容错)。

    ⚠️ 为什么必须有这个函数(真实事故,2026-10-13):
    launcher.cmd 里做了 `chcp 65001` + `PYTHONIOENCODING=utf-8`,但**只走 .cmd 才有**。
    计划任务、脚本、或者直接 `python -m la.ops.launcher restart` 都没有这层保护 ——
    GBK 控制台上一打 `✓` 就 UnicodeEncodeError。而在 restart 里它崩在
    **"停完、还没启"的中间态**:命令失败了,服务却全停着 —— 这是最坏的失败模式。

    做法与 .cmd 等价:先把控制台代码页设成 UTF-8(Windows),再把两个流的编码
    设成 utf-8 且 errors="replace"(换不了编码时至少不让一个符号打断命令)。
    """
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:  # noqa: BLE001 - 没有控制台(重定向到文件)时忽略
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 不是 TextIOWrapper 之类
            try:
                stream.reconfigure(errors="replace")
            except Exception:  # noqa: BLE001
                pass


def _scripts_dir() -> Path:
    return Path(config.BASE_DIR) / ".venv" / "Scripts"


def _python(console: bool = False) -> Path:
    """选解释器:console=True 用 python.exe(要看输出),否则用 pythonw.exe(不弹窗)。"""
    name = "python.exe" if console else "pythonw.exe"
    candidate = _scripts_dir() / name
    return candidate if candidate.exists() else Path(sys.executable)


def _run_dir() -> Path:
    path = Path(config.DATA_DIR) / "run"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _log_dir() -> Path:
    path = Path(config.DATA_DIR) / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class Service:
    """一个可管理的常驻服务。"""

    key: str
    title: str
    module: str
    args: list[str] = field(default_factory=list)
    #: 起好后应该能连上的本机端口(用来做"真的起来了"的检查)
    port: int | None = None

    @property
    def pid_file(self) -> Path:
        return _run_dir() / (self.key + ".pid")

    @property
    def out_log(self) -> Path:
        return _log_dir() / (self.key + ".out.log")

    @property
    def err_log(self) -> Path:
        return _log_dir() / (self.key + ".err.log")

    def cmdline(self) -> list[str]:
        return [str(_python()), "-m", self.module, *self.args]


def _safe_account(name: str) -> str:
    """账号名只保留能安全进路径的字符(与 weixin_store.use_account 同一口径)。"""
    clean = "".join(ch for ch in str(name or "").strip()
                    if ch.isalnum() or ch in ("_", "-", "."))
    return clean.strip(".")


def wechat_services(root: Path | None = None) -> dict[str, Service]:
    """发现所有**已登录**的微信号:默认账号 + data/wechat/accounts/<name>/。

    ⚠️ 账号是**运行时发现**的,不能写死成两个:
    用户可能只登录了默认账号,也可能先后加了 mom/dad;启动器每次启动都重新扫一遍,
    新扫码的账号下一次双击就会一起拉起,不需要改代码。

    ⚠️ 只收**有 credentials.json** 的目录。没扫码成功的空目录也拉起来,只会在
    pythonw 里挂一个永远等二维码的进程 —— 用户看不到二维码,还以为服务坏了。

    root 只给测试用;正常走 config.DATA_DIR/wechat。
    """
    base = Path(root) if root is not None else Path(config.DATA_DIR) / "wechat"
    found: dict[str, Service] = {
        # 旧单号路径:data/wechat/credentials.json。保留它,老用户无需迁移。
        "wechat": Service(
            key="wechat",
            title="微信渠道 · 默认账号",
            module="la.bridge.adapters.weixin_ilink_main",
        ),
    }
    accounts = base / "accounts"
    if accounts.is_dir():
        for entry in sorted(accounts.iterdir(), key=lambda item: item.name.lower()):
            if not entry.is_dir():
                continue
            name = _safe_account(entry.name)
            if not name or name != entry.name:
                continue
            try:
                credentials = entry / "credentials.json"
                if not credentials.is_file() or credentials.stat().st_size <= 0:
                    continue
            except OSError:
                continue
            found["wechat-" + name] = Service(
                key="wechat-" + name,
                title="微信渠道 · " + name,
                module="la.bridge.adapters.weixin_ilink_main",
                args=["--account", name],
            )
    return found


SERVICES: dict[str, Service] = {
    "web": Service(
        key="web",
        title="家庭助手 Web 界面",
        module="la",
        args=["--web", "127.0.0.1:8765"],
        port=8765,
    ),
}
SERVICES.update(wechat_services())
#: 依赖顺序:web 先起(它不依赖别的),然后每个微信号各起一个进程。
#: 默认账号固定排在最前,其余按账号名排序 —— 启动器的输出顺序才是稳定的。
ORDER = ["web"] + [key for key in SERVICES if key.startswith("wechat")]


# ------------------------------------------------------------------ 进程存活
# ⚠️ 系统命令的输出是**本地代码页**(中文 Windows 上是 GBK),不是 UTF-8。
#    平时没事,因为 text=True 走 locale.getencoding();但只要有人用 -X utf8 或设了
#    PYTHONUTF8=1,Python 就会拿 UTF-8 去解这些字节,**读取线程直接抛
#    UnicodeDecodeError**,一屏红色堆栈而退出码还是 0 —— 最难查的那种"看着坏了其实没错"。
#    tasklist 的输出恰好全是 ASCII 所以躲过了,schtasks / taskkill 会回中文,必炸。
#    统一加 errors="replace":解错也只是乱码,绝不再抛。
_SUBPROCESS_TEXT = {"capture_output": True, "text": True, "errors": "replace"}


#: 我们自己的服务只可能是这两个镜像名(计划任务里直指 pythonw.exe;开发时是 python.exe)。
#: ⚠️ 判"进程还在不在"必须**连镜像名一起看** —— PID 会被系统回收,收到别的程序身上,
#:    那时只查 PID 会得到"服务还在跑"的**假活**,`start` 于是跳过重启、端口却一直不通。
#:    实测(2026-10-15):web.pid 里是 5188,而 5188 早已变成 spoolsv.exe,
#:    双击 launcher 只起了微信、Web 静默没起,状态行还显示"PID 5188"。
_EXPECTED_IMAGES = ("python", "pythonw")


def _image_of(pid: int) -> str:
    """这个 PID 的镜像名(去掉 .exe,小写);查不到返回空串。"""
    if os.name != "nt":
        return ""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "PID eq " + str(pid), "/NH", "/FO", "CSV"],
            timeout=15, creationflags=_no_window(), **_SUBPROCESS_TEXT,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    import csv
    import io

    for fields in csv.reader(io.StringIO(out.stdout or "")):
        if len(fields) >= 2 and fields[1].strip() == str(pid):
            return fields[0].strip().lower().removesuffix(".exe")
    return ""


def is_running(pid: int) -> bool:
    """这个 PID 上跑的是不是**我们的**服务。

    用系统自带的 tasklist,不引 psutil;并且**必须**校验镜像名 —— 详见 _EXPECTED_IMAGES
    上面那条实测记录:PID 被回收之后,"存在"不等于"是我们的进程"。
    """
    if pid <= 0:
        return False
    image = _image_of(pid)
    if not image:
        return False
    return image in _EXPECTED_IMAGES


def _no_window() -> int:
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return 0


def read_pid(service: Service) -> int:
    try:
        text = service.pid_file.read_text(encoding="utf-8")
        return int(text.splitlines()[0].strip())
    except (OSError, ValueError, IndexError):
        return 0


def read_pid_module(service: Service) -> str:
    """PID 文件第二行记录的服务模块名;旧格式没有这一行 → 空串。"""
    try:
        lines = service.pid_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    return lines[1].strip() if len(lines) >= 2 else ""


def pid_belongs_to(service: Service, pid: int) -> bool:
    """这个 PID 上是不是**这个服务**。

    旧 PID 文件没有模块名,只能退回镜像名判断;新文件会核对第二行。
    """
    if not is_running(pid):
        return False
    recorded = read_pid_module(service)
    return (not recorded) or recorded == service.module


def write_pid(service: Service, pid: int) -> None:
    # 与 service_runner 同一格式:第一行 PID,第二行模块名。
    service.pid_file.write_text(str(pid) + "\n" + service.module + "\n", encoding="utf-8")


def clear_pid(service: Service) -> None:
    try:
        service.pid_file.unlink()
    except OSError:
        pass


def alive(service: Service) -> tuple[bool, int]:
    pid = read_pid(service)
    if pid and pid_belongs_to(service, pid):
        return True, pid
    return False, pid


def port_open(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.6)
        return sock.connect_ex(("127.0.0.1", port)) == 0


# ------------------------------------------------------------------ 引导
#
# ⚠️ **为什么用计划任务启动,而不是 subprocess.Popen**
#
# Popen + DETACHED_PROCESS(甚至再加 CREATE_BREAKAWAY_FROM_JOB)在本机**实测都活不下来**:
# 调用它的那条命令一结束,整棵进程树被回收,服务跟着死。表现是
# "start 报告成功、status 两秒后说未运行",而日志里能看到服务其实正常启动过。
#
# `schtasks` 拉起的进程**不属于调用者的进程树**,因此能活下来。
# 代价是 PID 拿不到(中间隔着 cmd.exe),所以改成**服务自己写 PID**
# (见 la/ops/service_runner.py)。这也正好是 D3 选定的常驻方式。
BOOT_TASK_PREFIX = "LifeAssistant-Boot-"


def boot_task_name(service: Service) -> str:
    return BOOT_TASK_PREFIX + service.key


def _boot_parts(service: Service) -> tuple[str, str]:
    """引导任务要跑的命令与参数。**分开返回**,不要再靠 partition(" ") 切。

    ⚠️ 旧实现把整个命令行按第一个空格切成 exe/rest;Python 路径里只要有空格
    (例如用户名带空格),`<Command>` 就会被切成 `C:\\Program` 这种半截路径,任务永远起不来。
    """
    args = ["-m", "la.ops.service_runner", service.key, service.module, *service.args]
    return str(_python()), subprocess.list2cmdline(args)


def _task_xml(service: Service) -> str:
    """计划任务的 XML 定义。

    ⚠️ **必须用 XML 而不是 `schtasks /Create /TR`**。踩过的坑:
    计划任务默认的工作目录是 `C:\\Windows\\System32`,在那里 `-m la.ops.service_runner`
    根本 import 不到 `la` —— 表现是**任务"成功"执行但服务完全没起来**,
    而且退出码只有 1、什么日志都不留(因为 service_runner 自己在 import 阶段就挂了)。
    XML 里的 `<WorkingDirectory>` 才能把这个定死。
    """
    exe, rest = _boot_parts(service)
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo><Description>家庭助手服务引导(" + service.key
        + ")</Description></RegistrationInfo>\n"
        "  <Triggers/>\n"
        '  <Principals><Principal id="Author">'
        "<LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel>"
        "</Principal></Principals>\n"
        "  <Settings>"
        "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>"
        "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>"
        "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>"
        "<AllowHardTerminate>true</AllowHardTerminate>"
        "<StartWhenAvailable>false</StartWhenAvailable>"
        "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>"     # 不限时(0 = 无限)
        "<Enabled>true</Enabled>"
        "<Hidden>false</Hidden>"
        "</Settings>\n"
        "  <Actions Context=\"Author\">\n"
        "    <Exec><Command>" + _xml(exe) + "</Command>"
        "<Arguments>" + _xml(rest) + "</Arguments>"
        "<WorkingDirectory>" + _xml(str(config.BASE_DIR)) + "</WorkingDirectory></Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


def _xml(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _launch_via_task(service: Service, *, attempts: int = 3) -> bool:
    """注册引导任务并立即运行。

    ⚠️ 为什么要重试(2026-10-13 加固):`schtasks /Create` 在**刚开机那几十秒**里
    会偶发失败(计划任务服务/外壳还没完全就绪)。以前这里一次失败就放弃 ——
    用户看到的是"双击了启动器,什么都没发生",而且日志里一个字都没有,
    只能靠猜。现在:失败重试 3 次;仍然失败就把 schtasks 的原话打出来。
    """
    name = boot_task_name(service)
    xml_path = _run_dir() / (service.key + ".task.xml")
    try:
        # schtasks /XML 要求 UTF-16
        xml_path.write_text(_task_xml(service), encoding="utf-16")
    except OSError as exc:
        print("  ✗ 写任务定义失败:" + str(exc))
        return False

    what = "注册"
    detail = ""
    for attempt in range(1, attempts + 1):
        _schtasks("/Delete", "/TN", name, "/F")   # 有就先删,保证定义是最新的
        created = _schtasks("/Create", "/TN", name, "/XML", str(xml_path), "/F")
        if created.returncode == 0:
            ran = _schtasks("/Run", "/TN", name)
            if ran.returncode == 0:
                return True
            what = "运行"
            detail = (ran.stderr or ran.stdout or "").strip()
        else:
            what = "注册"
            detail = (created.stderr or created.stdout or "").strip()
        if attempt < attempts:
            time.sleep(1.5 * attempt)
    print("  ✗ " + what + "引导任务失败(试了 " + str(attempts) + " 次):" + detail[:300])
    return False


def _clear_boot_task(service: Service) -> None:
    _schtasks("/Delete", "/TN", boot_task_name(service), "/F")


# ------------------------------------------------------------------ 启停
def start(service: Service, *, wait: float = 20.0) -> bool:
    running, pid = alive(service)
    if running:
        print("  · " + service.title + " 已在运行 (PID " + str(pid) + ")")
        return True
    clear_pid(service)   # 残留的旧 PID 先清掉,免得误判

    if not _launch_via_task(service):
        return False
    print("  · " + service.title + " 已拉起(经计划任务脱离进程树),等它就绪 …")

    # 等它自己把 PID 写出来 —— 这比猜 PID 可靠
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        time.sleep(0.5)
        pid = read_pid(service)
        if pid and pid_belongs_to(service, pid):
            if service.port is None:
                print("  ✓ " + service.title + " 就绪 (PID " + str(pid) + ")")
                return True
            if port_open(service.port):
                print("  ✓ " + service.title + " 就绪 (PID " + str(pid)
                      + ",端口 " + str(service.port) + " 可连)")
                return True

    # 没等到:分情况说清楚,别只说"失败"
    pid = read_pid(service)
    if pid and pid_belongs_to(service, pid):
        print("  ⚠ " + service.title + " 进程在(PID " + str(pid) + "),但端口 "
              + str(service.port) + " 还没通 —— 可能在初始化,用 status 再看")
        return True
    print("  ✗ " + service.title + " 没能起来 —— 日志尾部:")
    _print_tail(service.err_log, 14)
    _print_tail(service.out_log, 8)
    return False


def stop(service: Service, *, force: bool = True, wait: float = 12.0) -> bool:
    """停止服务。

    ⚠️ 顺序很关键:先删引导任务,再杀进程。
    反过来的话,刚杀掉的可能被任务列表里的残留项再拉起来。
    """
    running, pid = alive(service)
    _clear_boot_task(service)
    if not running:
        print("  · " + service.title + " 没在运行")
        clear_pid(service)
        return True

    if not pid:
        print("  · " + service.title + " 有 PID 文件但进程已不在,清理状态")
        clear_pid(service)
        return True

    print("  · 停止 " + service.title + " (PID " + str(pid) + ") …")
    # 先温和地杀进程树(/T),不行再强杀(/F)。常驻服务不该拖着不退。
    for extra in ([], ["/F"]):
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", *extra],
                           timeout=20, creationflags=_no_window(), **_SUBPROCESS_TEXT)
        except (OSError, subprocess.SubprocessError) as exc:
            print("  ✗ taskkill 失败:" + str(exc))
        deadline = time.monotonic() + (wait if not extra else 4.0)
        while time.monotonic() < deadline:
            if not is_running(pid):
                break
            time.sleep(0.3)
        if not is_running(pid):
            break

    gone = not is_running(pid)
    if gone:
        clear_pid(service)
        print("  ✓ 已停止")
    else:
        print("  ✗ 还在运行(PID " + str(pid) + ")—— 可能需要管理员权限")
    return gone


# ------------------------------------------------------------------ 状态
def status(*, verbose: bool = True) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for key in ORDER:
        service = SERVICES[key]
        running, pid = alive(service)
        entry: dict[str, Any] = {"running": running, "pid": pid, "title": service.title}
        if service.port is not None:
            entry["port"] = service.port
            entry["port_open"] = port_open(service.port)
        report[key] = entry
        if verbose:
            mark = "●" if running else "○"
            line = "  " + mark + " " + service.title.ljust(24)
            line += ("PID " + str(pid)) if running else "未运行"
            if service.port is not None:
                line += "   端口 " + str(service.port) + (
                    " 可连" if entry.get("port_open") else " 未通"
                )
            print(line)
    return report


def tail(service: Service, lines: int = 30) -> None:
    for path in (service.out_log, service.err_log):
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if not text:
            continue
        print("--- " + path.name + "(最后 " + str(lines) + " 行)---")
        for line in text[-lines:]:
            print("  " + line)


def _print_tail(path: Path, lines: int) -> None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    for line in text[-lines:]:
        print("      " + line)


def follow(service: Service, *, interval: float = 1.0) -> int:
    """跟踪日志(类似 tail -f)。Ctrl+C 退出。"""
    print("跟踪 " + service.title + " 的日志(两份合看),Ctrl+C 退出")
    print("  输出:" + str(service.out_log))
    print("  错误:" + str(service.err_log))
    print("-" * 60)
    positions = {service.out_log: 0, service.err_log: 0}
    # 从头看会很吵,直接跳到文件末尾
    for path in positions:
        if path.exists():
            positions[path] = path.stat().st_size
    try:
        while True:
            for path in (service.out_log, service.err_log):
                if not path.exists():
                    continue
                size = path.stat().st_size
                if size < positions[path]:
                    positions[path] = 0      # 文件被截断了
                if size == positions[path]:
                    continue
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(positions[path])
                    chunk = handle.read()
                    positions[path] = handle.tell()
                for line in chunk.splitlines():
                    if line.strip():
                        print(line, flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("")
        print("已停止跟踪(服务还在跑)")
        return 0


# ------------------------------------------------------------------ 组合命令
def restart_all(*, only: str | None = None, force: bool = False) -> bool:
    """重启全部(或某一个)。

    ⚠️ **安全网**:停止阶段无论出什么错(异常、Ctrl+C、编码崩),
    已经停下来的服务都必须被重新拉起 —— 见 harden_console() 的注释里那次真实事故。
    最坏的失败模式不是"重启失败",而是"停完没启、服务全停着而用户不知道"。
    """
    keys = [only] if only else ORDER
    print("停止…")
    failure: BaseException | None = None
    try:
        for key in reversed(keys):
            stop(SERVICES[key], force=force)
    except BaseException as exc:  # noqa: BLE001 - 包括 KeyboardInterrupt
        failure = exc
    print("启动…")
    ok = True
    for key in keys:
        ok = start(SERVICES[key]) and ok
    print("")
    status()
    if failure is not None:
        print("  ⚠ 停止阶段出过错(" + type(failure).__name__ + ": " + str(failure)[:80]
              + "),但服务已经重新拉起 —— 请用 status 复核。")
        if isinstance(failure, KeyboardInterrupt):
            # ⭐ 服务已经拉回来了,但 Ctrl+C 的语义不能吞:照旧把中断抛回去。
            raise failure
        return False
    return ok


def stop_all(*, only: str | None = None) -> bool:
    keys = [only] if only else ORDER
    ok = True
    for key in reversed(keys):
        ok = stop(SERVICES[key]) and ok
    return ok


def watchdog(*, interval: float = 20.0) -> int:
    """看护模式:任何服务退出就重新拉起。

    ⚠️ **默认不启用**,必须由主人显式运行或注册成计划任务 ——
    《自我生存.md》§684 明确要求"不得自行注册自启项",
    自愈能力只能在主人明确配置下存在。
    """
    print("看护模式已开启(每 " + str(int(interval)) + " 秒检查一次)。Ctrl+C 退出。")
    print("说明:这是「进程死了就拉起来」,不是「自我生存」—— 它不做任何自主决策。")
    try:
        while True:
            for key in ORDER:
                service = SERVICES[key]
                running, _ = alive(service)
                if not running:
                    stamp = time.strftime("%H:%M:%S")
                    print(stamp + " 发现 " + service.title + " 不在,拉起")
                    start(service)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("")
        print("看护已停止(服务保持运行)")
        return 0


# ------------------------------------------------------------------ 开机自启(D3 选的路)
def _schtasks(*extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["schtasks", *extra], timeout=60, creationflags=_no_window(), **_SUBPROCESS_TEXT,
    )


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _autostart_command() -> str:
    """自启要跑的命令:静默启动两个服务。

    ⚠️ 必须先进项目目录(2026-10-18 审计):注册表 Run / `schtasks /TR` 的默认
    工作目录是 System32,而 `-m la.ops.launcher` 依赖当前目录能 import 到 `la`。
    旧命令没有 chdir,登录自启会 ModuleNotFoundError,且 pythonw 无日志、启动器
    还会打印“✓ 已注册”。计划任务 XML 那条路有 WorkingDirectory,所以只有
    “登录自启/开机自启”受影响。
    """
    return ("cmd /c cd /d " + chr(34) + str(config.BASE_DIR) + chr(34)
            + " && " + chr(34) + str(_python()) + chr(34) + " -m la.ops.launcher start")


def task_install(*, at_logon: bool = True) -> int:
    """注册开机自启。

    **优先用注册表 Run 键**:零权限、随时可撤、用户能自己在
    「任务管理器 → 启动」里看见并禁用 —— 对"我明确同意它自启"这件事,
    可见性比什么都重要。

    ⚠️ 实测:**`schtasks /Create /SC ONLOGON /RL LIMITED` 在非管理员下会被拒绝**
    ("拒绝访问")。所以计划任务那条路只在需要"开机就起(不登录)"时才用,
    而那本来就得管理员权限 —— 那就如实告诉用户,而不是假装成功。
    """
    if at_logon:
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, TASK_NAME, 0, winreg.REG_SZ, _autostart_command())
        except OSError as exc:
            print("注册失败:" + str(exc))
            print("提示:注册表 Run 键属于当前用户,正常不需要管理员。")
            return 1
        print("✓ 已注册开机自启(当前用户登录时启动)")
        print("  命令:" + _autostart_command())
        print("  查看:任务管理器 → 启动 里能看到「" + TASK_NAME + "」,可随时禁用")
        print("  取消:python -m la.ops.launcher task remove")
        return 0

    tr = _autostart_command()
    result = _schtasks("/Create", "/TN", TASK_NAME, "/TR", tr, "/SC", "ONSTART",
                       "/RL", "HIGHEST", "/F")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:300]
        print("注册失败:" + detail)
        if "拒绝访问" in detail or "Access is denied" in detail:
            print("说明:「开机启动(不登录)」必须管理员权限。两个选择:")
            print("  1. 用管理员开一次终端,再跑这条命令;")
            print("  2. 不加 --at-startup —— 登录时启动,零权限就够。")
        return 1
    print("✓ 已注册开机自启(计划任务 " + TASK_NAME + ",开机时触发)")
    print("  取消:python -m la.ops.launcher task remove")
    return 0


def task_remove() -> int:
    removed = False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, TASK_NAME)
        removed = True
    except FileNotFoundError:
        pass
    except OSError as exc:
        print("删注册表项失败:" + str(exc))
    result = _schtasks("/Delete", "/TN", TASK_NAME, "/F")
    if result.returncode == 0:
        removed = True
    if not removed:
        print("没有找到已注册的开机自启(可能本来就没注册)")
        return 0
    print("✓ 已取消开机自启")
    return 0


def task_query() -> int:
    """看当前注册了什么。"""
    found = False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, TASK_NAME)
        print("● 注册表 Run 键: " + str(value))
        found = True
    except FileNotFoundError:
        print("○ 注册表 Run 键: 未注册")
    except OSError as exc:
        print("○ 注册表 Run 键: 读取失败 " + str(exc))
    result = _schtasks("/Query", "/TN", TASK_NAME, "/FO", "LIST")
    if result.returncode == 0:
        print("● 计划任务: 已注册")
        found = True
    else:
        print("○ 计划任务: 未注册")
    if not found:
        print("")
        print("注册登录自启:python -m la.ops.launcher task install")
    return 0


# ------------------------------------------------------------------ CLI
def _pick(name: str | None) -> str | None:
    if name is None:
        return None
    if name not in SERVICES:
        print("未知服务:" + name + "(可选:" + " / ".join(SERVICES) + ")")
        return ""
    return name


def main(argv: list[str] | None = None) -> int:
    # ⚠️ 第一件事就是加固控制台:这个函数的输出里有 ✓ ⚠ ✗ 这类符号,
    #    而 GBK 控制台遇到它们会直接抛 UnicodeEncodeError(见 harden_console)。
    harden_console()
    parser = argparse.ArgumentParser(
        prog="la-launcher", description="家庭助手 · 服务启动器"
    )
    parser.add_argument(
        "action",
        choices=["start", "stop", "restart", "status", "logs", "watch", "task"],
        help="要做什么",
    )
    parser.add_argument("service", nargs="?", default=None,
                        help="只对某个服务操作:" + " / ".join(SERVICES))
    parser.add_argument("--force", action="store_true", help="停止时强制结束进程")
    parser.add_argument("--lines", type=int, default=30, help="logs 默认显示多少行")
    parser.add_argument("--follow", action="store_true", help="logs 持续跟踪")
    parser.add_argument("--interval", type=float, default=20.0, help="watch 的检查间隔(秒)")
    parser.add_argument("--at-startup", action="store_true",
                        help="task install 时用开机触发(默认登录触发)")
    args = parser.parse_args(argv)

    # task 的第二个位置参数是子命令
    if args.action == "task":
        sub = args.service or "query"
        if sub == "install":
            return task_install(at_logon=not args.at_startup)
        if sub == "remove":
            return task_remove()
        return task_query()

    picked = _pick(args.service)
    if picked == "":
        return 2

    if args.action == "status":
        print("家庭助手 · 服务状态")
        report = status()
        running = sum(1 for item in report.values() if item["running"])
        print("")
        print(str(running) + "/" + str(len(report)) + " 个服务在运行")
        return 0

    if args.action == "start":
        keys = [picked] if picked else ORDER
        print("启动…")
        ok = True
        for key in keys:
            ok = start(SERVICES[key]) and ok
        print("")
        status()
        return 0 if ok else 1

    if args.action == "stop":
        ok = stop_all(only=picked) if picked else stop_all()
        return 0 if ok else 1

    if args.action == "restart":
        return 0 if restart_all(only=picked, force=args.force) else 1

    if args.action == "logs":
        if picked == "" or picked is None:
            print("logs 要看哪个服务?例如:logs wechat")
            return 2
        service = SERVICES[picked]
        if args.follow:
            return follow(service)
        tail(service, lines=args.lines)
        return 0

    if args.action == "watch":
        return watchdog(interval=args.interval)

    return 2


if __name__ == "__main__":
    raise SystemExit(main())