"""常驻服务的包装器:自己写 PID、自己重定向日志,然后跑目标模块。

**为什么需要这一层**:服务必须**脱离调用者的进程树**才能活下来
(否则调用它的终端/脚本一结束,整棵树被回收,服务跟着死)。

Windows 上唯一可靠的脱离方式是**计划任务**(`schtasks`)。但计划任务有个麻烦:
它启动的是 `cmd.exe`,中间还套一层,父进程拿不到真正服务的 PID。

所以反过来做:**让服务自己写 PID**。这就是标准守护进程的做法,
而且它对"谁把我拉起来的"完全无所谓 —— 手起、计划任务起、看护进程起,都一样。

用法(由 launcher 调用,不用手敲):

    pythonw -m la.ops.service_runner <key> <module> [args...]
"""

from __future__ import annotations

import os
import runpy
import sys
import time
from pathlib import Path


def _dirs() -> tuple[Path, Path]:
    # 这里刻意**不 import config**:包装器要尽早接管 stdout,
    # 而 import 项目模块可能触发各种副作用。数据目录从环境变量或默认位置推。
    base = Path(os.environ.get("LA_BASE_DIR") or Path(__file__).resolve().parents[2])
    data = Path(os.environ.get("LA_DATA_DIR") or (base / "data"))
    return data / "run", data / "logs"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        print("用法: service_runner <key> <module> [args...]", file=sys.stderr)
        return 2
    key, module = args[0], args[1]
    extra = args[2:]

    run_dir, log_dir = _dirs()
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    # 先把 PID 落盘 —— 必须在任何可能失败的事情之前
    pid_file = run_dir / (key + ".pid")
    try:
        # ⚠️ 第二行写模块名(2026-10-18 审计):PID 会被系统回收给别的 Python,
        #    只比镜像名会把“别人的 python”当成自己的服务。第二行让 launcher 能核对
        #    “这个 PID 上跑的到底是不是这个服务”。
        pid_file.write_text(str(os.getpid()) + "\n" + module + "\n", encoding="utf-8")
    except OSError:
        pass

    out_path = log_dir / (key + ".out.log")
    err_path = log_dir / (key + ".err.log")
    try:
        out = open(out_path, "a", encoding="utf-8", buffering=1)
        err = open(err_path, "a", encoding="utf-8", buffering=1)
    except OSError:
        out, err = sys.stdout, sys.stderr

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    banner = ("\n===== " + stamp + " 启动: " + module + " " + " ".join(extra)
              + " (pid " + str(os.getpid()) + ") =====\n")
    for handle in (out, err):
        try:
            handle.write(banner)
        except OSError:
            pass

    sys.stdout = out
    sys.stderr = err
    sys.stdin = open(os.devnull, "r", encoding="utf-8")

    try:
        sys.argv = [module, *extra]
        runpy.run_module(module, run_name="__main__", alter_sys=True)
        return 0
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 0
        return code
    except BaseException as exc:  # noqa: BLE001 - 包装器要把死因记下来
        import traceback

        traceback.print_exc()
        print("\n[service_runner] " + module + " 异常退出:"
              + type(exc).__name__ + " " + str(exc), file=sys.stderr)
        return 1
    finally:
        # 正常/异常退出都把 PID 文件清掉,免得 launcher 以为还活着
        try:
            pid_file.unlink()
        except OSError:
            pass
        for handle in (out, err):
            try:
                handle.close()
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
