"""把**自己写的网页**渲染成一张截图(无头 Chrome)—— page_view 的引擎。

做这件事的动机(用户 2026-10-17):
    *"现在智能体是不是没有自己截图的能力,比如自己生成的 html,用户可以让它自己看了找问题。"*
它写出来的页面,以前只有**源码**可看 —— 版面对不对、有没有挤在一起、白屏没有,
它一概不知道,只能问用户"你看看行不行"。这个模块补的就是那一环。

实现照 D:\\云朵辅助培训智能体\\app\\shots.py(那边真机跑过),**四条边界一条不少**:

  1. **只截自己生成的东西。** 入口只有产物层的 .html(页面解析在 base/html_page.py 的
     page_view 里),不接受任意路径 —— 用户资料(素材/文档)要看正文用 file 就够,
     给它们开"看画面"的口子只会扩大能力面。
  2. **绝不用 file:// 打开。** 页面里的 <img src="file:///C:/..."> 能把本机任意图片
     「画」进画面,再经视觉模型读走 —— 那是一条真实的泄露路径。所以这里起一个**临时
     回环 HTTP 服务**(127.0.0.1 + 随机端口 + 一次性路径 token),响应头带与网页预览
     **完全相同**的 CSP(sandbox / default-src 'none' / connect-src 'none'),
     只服务这一个文件,截完就关。
  3. **进程硬隔离 + 硬超时。** 独立临时 --user-data-dir、禁网
     (--host-resolver-rules=MAP * 0.0.0.0, EXCLUDE 127.0.0.1)、
     超时按 PID 杀整棵进程树;全局串行(Chrome 一个实例几百 MB)。
     ⚠️ EXCLUDE 127.0.0.1 不能再漏:只写 MAP * 会把回环也映射掉,连我们自己的页面
     都拉不到,截出来是 Chrome 的错误页(云朵实测踩过)。
  4. **失败一律抛 ShotError(带人话)**,绝不静默给一张空图 —— 空图会被当成"页面没问题"。

⚠️ CSP 的定义在 base/html_page.py(唯一一份):**截图必须和用户点『预览』看到的
是同一个沙箱**,否则"我看过了没问题"就是一句假话。
"""

from __future__ import annotations

import contextlib
import http.server
import os
import secrets
import shutil
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .. import config

#: 默认视图尺寸。1280×900 与云朵一致:桌面版式能一次看全,又不至于大到图标糊成一片。
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 900
MIN_SIDE = 320
DEFAULT_TIMEOUT = 25.0
#: 页面里的 JS/字体渲染等多久再截(Chrome 的 virtual-time-budget)。
DEFAULT_VIRTUAL_TIME_MS = 5000
#: 截图保留多久(每次截图顺手清一次)。它是诊断用的临时物,不是产物。
DEFAULT_TTL_HOURS = 24

#: 找浏览器的顺序:配置优先,其次 Chrome,最后 Edge(Windows 上几乎总有一个)。
CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

_shot_lock = threading.Lock()


class ShotError(RuntimeError):
    """截图失败。**带一句能照做的人话**,不要只给一个错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class PageShot:
    """一次截图的结果。真值是 PNG 字节;path 只是"它落在哪"(给用户/排错看)。"""

    data: bytes
    width: int
    height: int
    elapsed: float
    path: Path
    url: str

    @property
    def kb(self) -> float:
        return round(len(self.data) / 1024.0, 1)


# ==========================================================================
# 临时回环服务(只服务这一次的这一页)
# ==========================================================================
@contextlib.contextmanager
def page_server(html: bytes, *, csp: str) -> Any:
    """起一个只服务这一页的临时 HTTP 服务,给出 URL;**用完立刻关**。

    ⚠️ 为什么不是 file://(照云朵 shots.py 的实测):file:// 没有响应头 CSP,
       页面可以 <img src="file:///C:/..."> 把本机任意图片画进画面,再被视觉模型读走。
    ⚠️ 为什么 token 不是"非零成本":它防的是本机上别的进程顺手来看一眼;
       生命周期只有一次截图(通常 2~5 秒),并且只认这一个 path。
    """
    token = secrets.token_urlsafe(18)
    payload = bytes(html)

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 - 基类规定的名字
            parts = urlsplit(self.path)
            query = parse_qs(parts.query)
            if parts.path != "/page" or query.get("t", [""])[0] != token:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            # ⭐ 与普通预览**同一串 CSP**:模型看到的就是用户看到的那个沙箱
            self.send_header("Content-Security-Policy", csp)
            self.send_header("Cache-Control", "no-store, max-age=0, must-revalidate")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args: Any) -> None:  # noqa: D401 - 闭嘴
            return

    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    except OSError as exc:  # 端口/权限问题都要说清楚
        raise ShotError("NO_SERVER", "起不了本机渲染服务(" + str(exc)[:80] + ")") from exc
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="shot-page-server", daemon=True)
    thread.start()
    try:
        port = int(server.server_address[1])
        yield "http://127.0.0.1:" + str(port) + "/page?t=" + token
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3.0)


# ==========================================================================
# Chrome
# ==========================================================================
def find_chrome(configured: str = "") -> str:
    """定位浏览器。找不到就抛一句能照做的错(配 SHOT_CHROME / 装 Chrome)。"""
    wanted = str(configured or getattr(config, "SHOT_CHROME", "") or "").strip()
    if wanted and Path(wanted).is_file():
        return wanted
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    for name in ("chrome", "google-chrome", "chromium", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    raise ShotError(
        "NO_BROWSER",
        "本机没找到 Chrome/Edge,渲染不了网页截图(装一个,或在 config.SHOT_CHROME 里写路径)",
    )


def build_args(
    chrome: str, url: str, out_png: str, user_data: str,
    *, width: int, height: int, virtual_time_ms: int,
) -> list[str]:
    """Chrome 参数。⚠️ 顺序固定,**URL 永远在最后**(Chrome 只认末尾那个当目标)。"""
    return [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-default-apps",
        "--disable-component-update",
        "--hide-scrollbars",
        "--mute-audio",
        "--force-device-scale-factor=1",
        # 禁网:所有主机名映射到 0.0.0.0,但**回环必须 EXCLUDE** —— 否则连自己都拉不到。
        "--host-resolver-rules=MAP * 0.0.0.0, EXCLUDE 127.0.0.1",
        "--no-proxy-server",
        "--disable-features=NetworkPrediction,PreconnectToSearch",
        "--user-data-dir=" + user_data,
        "--window-size=" + str(int(width)) + "," + str(int(height)),
        "--virtual-time-budget=" + str(int(virtual_time_ms)),
        "--screenshot=" + out_png,
        url,
    ]


def kill_tree(proc: Any) -> None:
    """杀整棵进程树(Chrome 会派生一堆子进程,父进程死了子进程照样占内存)。"""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
            )
        else:
            proc.kill()
    except Exception:  # noqa: BLE001 - 杀不掉也只能往下走
        pass
    try:
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        pass


def run_chrome(args: list[str], timeout: float) -> None:
    """跑一次 Chrome。devnull 收口(免得管道卡死),超时杀树。"""
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, creationflags=creationflags,
        )
    except OSError as exc:
        raise ShotError("NO_BROWSER", "启动浏览器失败(" + str(exc)[:80] + ")") from exc
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        raise ShotError(
            "TIMEOUT",
            "页面渲染超过 " + str(int(timeout)) + " 秒(可能有死循环,或一直在等外部资源)",
        ) from None
    finally:
        if proc.poll() is None:
            kill_tree(proc)


# ==========================================================================
# 主入口
# ==========================================================================
def shot_dir() -> Path:
    """截图目录。⭐ 跟着 config.DATA_DIR 现算 —— 不另立常量,测试换数据目录时自然跟着走。"""
    return Path(config.DATA_DIR) / "shots"


def cleanup(ttl_hours: float | None = None) -> int:
    """清掉过期截图(每次截图顺手做一次)。返回删了几个。"""
    hours = float(ttl_hours if ttl_hours is not None else DEFAULT_TTL_HOURS)
    root = shot_dir()
    if not root.is_dir():
        return 0
    deadline = time.time() - max(1.0, hours) * 3600.0
    removed = 0
    for path in root.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < deadline:
                path.unlink()
                removed += 1
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        except OSError:
            continue
    return removed


def png_size(data: bytes) -> tuple[int, int]:
    """PNG 宽高(只读 IHDR,不解码整图)。认不出返回 (0, 0)。"""
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        try:
            width, height = struct.unpack(">II", data[16:24])
            return int(width), int(height)
        except struct.error:
            return 0, 0
    return 0, 0


#: 「从第 N 像素开始看」的注入样式。⚠️ 只注入**喂给浏览器的那份副本**,产物文件一个字都不动。
#:
#: 为什么需要它:页面一长,截一张 1280×4000 的图,视觉模型会把整张缩小、字全糊 ——
#: **分屏看**才看得清。而「往下滚」在无头截图里是走不通的(实测 2026-10-17:
#: window.scrollTo 之后截出来**整张全白**,Chrome 的 --screenshot 拍的是布局视口顶部)。
#: 所以改成把**页面整体上移** —— 它改的是绘制而不是滚动位置,实测三屏分别拍到了
#: 红/蓝/绿三段,完全正确。
#: ⚠️ 已知差异:position:fixed 的元素在真实滚动时会跟着停在顶上,这里会一起上移。
#:    截图只是"看一眼",这条差异不影响判断版面,但**不许**拿它当"用户滚动时看到的样子"。
_OFFSET_STYLE_TMPL = b"<style>html{transform:translateY(-%dpx)}</style>"


def with_offset(html: bytes, offset_y: int) -> bytes:
    """把「页面上移 N 像素」的样式插进页面尾部(只用于本次渲染的副本)。"""
    value = int(offset_y or 0)
    if value <= 0:
        return html
    style = _OFFSET_STYLE_TMPL % value
    marker = html.lower().rfind(b"</body>")
    if marker >= 0:
        return html[:marker] + style + html[marker:]
    return html + style


def render_html(
    html: bytes | str,
    *,
    width: int = 0,
    height: int = 0,
    timeout: float = 0.0,
    chrome: str = "",
    runner: Callable[[list[str], float], None] | None = None,
    csp: str = "",
    offset_y: int = 0,
) -> PageShot:
    """把一段 HTML 渲染成 PNG。失败抛 ShotError。

    * runner 是**测试注入点**:默认 run_chrome;测试用它写一张真 PNG,不动真浏览器;
    * csp 默认取网页预览那一串(base/html_page.py 的 HTML_PREVIEW_CSP),
      显式传入只为让测试能断言"服务真的带上了它";
    * offset_y > 0 时把页面上移 N 像素再拍 = "从第 N 像素开始的那一屏"(分屏看长页面)。
    """
    payload = html.encode("utf-8") if isinstance(html, str) else bytes(html or b"")
    if not payload:
        raise ShotError("EMPTY", "这一页是空的,没东西可截")
    if not csp:
        from .html_page import HTML_PREVIEW_CSP  # 延迟 import:避免 base 内部成环

        csp = HTML_PREVIEW_CSP
    side = int(getattr(config, "SHOT_MAX_SIDE", 2400) or 2400)
    width = _clamp(width or getattr(config, "SHOT_WIDTH", DEFAULT_WIDTH), MIN_SIDE, side)
    height = _clamp(height or getattr(config, "SHOT_HEIGHT", DEFAULT_HEIGHT), MIN_SIDE, side)
    timeout = float(timeout or getattr(config, "SHOT_TIMEOUT_S", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT)
    virtual_time = int(getattr(config, "SHOT_VIRTUAL_TIME_MS", DEFAULT_VIRTUAL_TIME_MS)
                       or DEFAULT_VIRTUAL_TIME_MS)

    if not _shot_lock.acquire(timeout=timeout + 10):
        raise ShotError("BUSY", "正在等上一次截图渲染完(浏览器一次只能开一个)")
    started = time.time()
    workdir = ""
    try:
        cleanup()
        # 浏览器实体(有的话)晚一点找:没有浏览器时也别先建目录
        binary = find_chrome(chrome)
        root = shot_dir()
        root.mkdir(parents=True, exist_ok=True)
        workdir = str(root / ("work-" + secrets.token_hex(8)))
        os.makedirs(workdir, exist_ok=True)
        out_png = os.path.join(workdir, "shot.png")
        served = with_offset(payload, int(offset_y or 0))
        with page_server(served, csp=csp) as url:
            args = build_args(
                binary, url, out_png, os.path.join(workdir, "profile"),
                width=width, height=height, virtual_time_ms=virtual_time,
            )
            (runner or run_chrome)(args, timeout)
            if not os.path.isfile(out_png):
                raise ShotError(
                    "NO_OUTPUT",
                    "浏览器没有输出截图(页面可能加载失败,或者本机浏览器版本太旧)",
                )
            data = Path(out_png).read_bytes()
        if not data:
            raise ShotError("NO_OUTPUT", "浏览器输出的是空图")
        if not data.startswith(b"\x89PNG"):
            raise ShotError("BAD_OUTPUT", "浏览器输出的不是 PNG(截图失败)")
        keep = root / (time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4) + ".png")
        keep.write_bytes(data)
        real_width, real_height = png_size(data)
        return PageShot(
            data=data,
            width=real_width or width,
            height=real_height or height,
            elapsed=round(time.time() - started, 2),
            path=keep,
            url=url,
        )
    finally:
        _shot_lock.release()
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)


def _clamp(value: Any, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = low
    return max(low, min(high, number))


__all__ = [
    "DEFAULT_HEIGHT",
    "DEFAULT_TIMEOUT",
    "DEFAULT_WIDTH",
    "PageShot",
    "ShotError",
    "build_args",
    "cleanup",
    "find_chrome",
    "page_server",
    "png_size",
    "render_html",
    "run_chrome",
    "shot_dir",
    "with_offset",
]
