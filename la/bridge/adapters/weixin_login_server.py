"""微信扫码登录页(只用标准库,零依赖)。

为什么单独一个服务而不是塞进 la/web/:
**登录是"账户与凭据"的事,不是"家庭助手功能"的事。**
它不该因为家庭助手的应用起不来、数据库被锁、模型挂了而打不开。
而且这个页面只在首次扫码时用一次。

用法:

    .venv/Scripts/python.exe -m la.bridge.adapters.weixin_login_server

然后浏览器打开 http://127.0.0.1:8788 —— 页面上就是二维码,手机扫它。

⚠️ **实现上刻意避开的一个坑**:第一版用 `http.server.ThreadingHTTPServer` +
`asyncio.run_coroutine_threadsafe`,结果 HTTP 线程和事件循环互相等,直接死锁
(而且症状是"请求挂 30 秒然后 500",很难一眼看出)。
现在改成**单事件循环 + asyncio.start_server**:HTTP 处理本身就是协程,
和 iLink 调用在同一个循环里,不存在跨线程交接,也就没有死锁可言。
手写这一小段 HTTP 解析(约 40 行)换来的是"不会有并发模型 bug"。
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import sys
import time
import webbrowser
from typing import Any

from . import weixin_store
from .weixin_login import QRSession, new_qr_session, poll_once
from .weixin_protocol import ILinkClient
from .weixin_qr import render_svg

DEFAULT_PORT = 8788
MAX_REQUEST = 64 * 1024


class LoginState:
    """登录会话状态。**单事件循环内使用,不需要锁。**"""

    def __init__(self, client: ILinkClient) -> None:
        self.client = client
        self.session: QRSession | None = None

    def say(self, text: str) -> None:
        print(time.strftime("%H:%M:%S ") + text, flush=True)

    async def ensure_session(self) -> QRSession:
        if self.session is not None and not self.session.expired and self.session.status != "expired":
            return self.session
        self.session = await new_qr_session(self.client)
        self.say("已生成二维码")
        return self.session

    async def refresh(self) -> QRSession:
        self.session = None
        return await self.ensure_session()

    async def poll(self) -> dict[str, Any]:
        session = await self.ensure_session()
        if session.status in ("confirmed", "expired"):
            return session.as_dict()
        was = session.status
        session = await poll_once(self.client, session)
        self.session = session
        if session.status == "confirmed" and session.credentials is not None:
            self.say("登录成功:bot " + session.credentials.bot_id
                     + " / 凭据已存 " + str(weixin_store.credentials_path()))
        elif session.status == "scaned" and was != "scaned":
            self.say("已扫码,请在手机上点「确认」")
        elif session.status == "expired" and was != "expired":
            self.say("二维码已过期")
        return session.as_dict()


# ---------------------------------------------------------------- HTTP
def _response(status: int, body: bytes, content_type: str) -> bytes:
    reason = {200: "OK", 404: "Not Found", 500: "Internal Server Error"}.get(status, "OK")
    head = (
        "HTTP/1.1 " + str(status) + " " + reason + "\r\n"
        "Content-Type: " + content_type + "\r\n"
        "Content-Length: " + str(len(body)) + "\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    return head + body


async def _read_request(reader: asyncio.StreamReader) -> tuple[str, str]:
    """返回 (method, path)。请求体一律不读 —— 这个页面没有表单。"""
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=10)
    except asyncio.TimeoutError:
        return "", ""
    parts = line.decode("latin-1").strip().split()
    if len(parts) < 2:
        return "", ""
    method, path = parts[0], parts[1]
    # 把请求头读完,免得客户端看到半截响应
    try:
        while True:
            header = await asyncio.wait_for(reader.readline(), timeout=10)
            if header in (b"\r\n", b"\n", b""):
                break
    except asyncio.TimeoutError:
        pass
    return method, path


async def handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    state: LoginState,
    port: int,
) -> None:
    try:
        method, path = await _read_request(reader)
        route = path.split("?")[0]
        if not route:
            writer.write(_response(400, b"bad request", "text/plain; charset=utf-8"))
        elif method == "GET" and route in ("/", "/index.html"):
            session = await state.ensure_session()
            if session.status == "confirmed":
                writer.write(_response(200, DONE_PAGE.encode("utf-8"), "text/html; charset=utf-8"))
            else:
                page = PAGE_TEMPLATE.format(
                    svg=render_svg(session.url),
                    url=html.escape(session.url),
                    stored=html.escape(str(weixin_store.credentials_path())),
                )
                writer.write(_response(200, page.encode("utf-8"), "text/html; charset=utf-8"))
        elif method == "GET" and route == "/status":
            payload = json.dumps(await state.poll(), ensure_ascii=False).encode("utf-8")
            writer.write(_response(200, payload, "application/json; charset=utf-8"))
        elif method == "GET" and route == "/qr.svg":
            session = await state.ensure_session()
            writer.write(_response(200, render_svg(session.url).encode("utf-8"), "image/svg+xml"))
        elif method == "POST" and route == "/refresh":
            await state.refresh()
            writer.write(_response(200, b'{"ok":true}', "application/json"))
        else:
            writer.write(_response(404, b"not found", "text/plain; charset=utf-8"))
    except Exception as exc:  # noqa: BLE001 - 一个请求失败不该让服务倒
        state.say("请求出错 " + type(exc).__name__ + ": " + str(exc)[:160])
        try:
            writer.write(_response(500, ("出错:" + str(exc)).encode("utf-8"),
                                   "text/plain; charset=utf-8"))
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


DONE_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>已登录 · 家庭助手</title>
<style>
  :root { color-scheme: light dark; }
  body { margin:0; min-height:100dvh; display:flex; align-items:center; justify-content:center;
         font:16px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
         background:#f6f6f8; color:#1c1c1e; padding:24px; }
  .card { background:#fff; padding:40px 32px; border-radius:18px; text-align:center;
          box-shadow:0 8px 30px rgba(0,0,0,.08); max-width:440px; }
  .ok { font-size:52px; line-height:1; }
  h1 { font-size:20px; margin:16px 0 8px; }
  p { margin:6px 0; color:#6b6b70; font-size:14px; }
  code { background:#f1f1f4; padding:2px 6px; border-radius:5px; font-size:13px; }
</style></head>
<body><div class="card">
  <div class="ok">✅</div>
  <h1>微信已连接</h1>
  <p>凭据已保存,这个页面可以关掉了。</p>
  <p>接着启动常驻服务:</p>
  <p><code>python -m la.bridge.adapters.weixin_ilink_main</code></p>
</div></body></html>"""

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>微信扫码登录 · 家庭助手</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; min-height:100dvh; display:flex; align-items:center; justify-content:center;
         font:16px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
         background:#f6f6f8; color:#1c1c1e; padding:24px; }}
  .card {{ background:#fff; padding:32px; border-radius:18px; text-align:center;
          box-shadow:0 8px 30px rgba(0,0,0,.08); max-width:470px; width:100%; }}
  h1 {{ font-size:20px; margin:0 0 6px; }}
  .sub {{ color:#6b6b70; font-size:14px; margin:0 0 22px; }}
  .qr {{ display:inline-block; padding:10px; background:#fff; border-radius:12px;
        border:1px solid #e5e5ea; line-height:0; }}
  .qr svg {{ width:300px; height:300px; max-width:72vw; max-height:72vw; }}
  .status {{ margin-top:18px; font-size:15px; min-height:24px; font-weight:600; }}
  .status.waiting {{ color:#6b6b70; font-weight:400; }}
  .status.scaned {{ color:#c77700; }}
  .status.expired, .status.error {{ color:#d7382f; }}
  button {{ margin-top:12px; font:inherit; font-size:14px; padding:8px 18px; border-radius:9px;
           border:1px solid #d1d1d6; background:#fff; cursor:pointer; }}
  button:hover {{ background:#f2f2f7; }}
  .hint {{ margin-top:20px; padding-top:16px; border-top:1px solid #ececf0;
          color:#8a8a8e; font-size:12.5px; text-align:left; line-height:1.75; }}
  .hint b {{ color:#48484a; }}
  code {{ background:#f1f1f4; padding:1px 5px; border-radius:4px; word-break:break-all; }}
  a {{ color:#6e5abf; word-break:break-all; }}
</style></head>
<body><div class="card">
  <h1>用手机微信扫码</h1>
  <p class="sub">我 → 设置 → 插件 → ClawBot</p>
  <div class="qr">{svg}</div>
  <div class="status waiting" id="status">等待扫码…</div>
  <div><button id="reload" hidden>换一张二维码</button></div>
  <div class="hint">
    <b>扫不到?</b> 可以直接在<b>手机微信</b>里打开这个链接:<br>
    <a href="{url}">{url}</a><br><br>
    <b>登录成功后</b>凭据写入 <code>{stored}</code>,之后启动免扫码。<br>
    <b>这个页面可以关掉</b>,它只负责这一次登录。
  </div>
</div>
<script>
const statusEl = document.getElementById('status');
const reloadBtn = document.getElementById('reload');
let done = false;

async function tick() {{
  if (done) return;
  try {{
    const s = await (await fetch('/status', {{cache:'no-store'}})).json();
    if (s.status === 'confirmed') {{
      done = true;
      statusEl.className = 'status';
      statusEl.textContent = '✅ 已登录,正在刷新…';
      reloadBtn.hidden = true;
      setTimeout(() => location.reload(), 700);
      return;
    }}
    if (s.status === 'scaned') {{
      statusEl.className = 'status scaned';
      statusEl.textContent = '已扫码,请在手机上点「确认」';
    }} else if (s.status === 'expired') {{
      statusEl.className = 'status expired';
      statusEl.textContent = '二维码已过期';
      reloadBtn.hidden = false;
    }} else {{
      statusEl.className = 'status waiting';
      statusEl.textContent = '等待扫码… 已等待 ' + s.age + ' 秒';
    }}
  }} catch (e) {{
    statusEl.className = 'status error';
    statusEl.textContent = '连不上本地服务,确认终端还在运行';
  }}
  setTimeout(tick, 2000);
}}

reloadBtn.addEventListener('click', async () => {{
  reloadBtn.disabled = true;
  try {{ await fetch('/refresh', {{method:'POST'}}); }} catch (e) {{}}
  location.reload();
}});

tick();
</script>
</body></html>"""


async def _serve(args: argparse.Namespace) -> int:
    client = ILinkClient()
    state = LoginState(client)

    async def on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await handle_client(reader, writer, state, args.port)

    try:
        server = await asyncio.start_server(on_client, args.host, args.port)
    except OSError as exc:
        print("端口 " + str(args.port) + " 起不来:" + str(exc), flush=True)
        print("换个端口试试:--port 8899", flush=True)
        await client.aclose()
        return 1

    url = "http://127.0.0.1:" + str(args.port)
    print("=" * 62, flush=True)
    print("微信扫码登录页:" + url, flush=True)
    print("用浏览器打开它 —— 页面上就是二维码,拿手机扫。", flush=True)
    print("登录成功会自动提示;按 Ctrl+C 结束。", flush=True)
    print("=" * 62, flush=True)
    if args.open:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass

    try:
        async with server:
            deadline = asyncio.get_running_loop().time() + args.timeout
            while True:
                session = await state.ensure_session()
                if session.status == "confirmed":
                    print("✅ 登录成功,凭据已保存。", flush=True)
                    if args.hold:
                        print("服务继续开着(--hold),Ctrl+C 结束。", flush=True)
                        await server.serve_forever()
                    return 0
                if asyncio.get_running_loop().time() > deadline:
                    print("等待超时(" + str(int(args.timeout)) + " 秒),退出。", flush=True)
                    return 1
                await asyncio.sleep(2)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("", flush=True)
        print("已停止。", flush=True)
        return 0
    finally:
        await client.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="la-weixin-login", description="微信扫码登录页(零依赖本地小服务)"
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址(默认只本机)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="端口(默认 " + str(DEFAULT_PORT) + ")")
    parser.add_argument("--no-open", action="store_true", help="不要自动打开浏览器")
    parser.add_argument("--hold", action="store_true", help="登录成功后继续挂着")
    parser.add_argument("--timeout", type=float, default=900.0, help="等待上限(秒,默认 900)")
    parser.add_argument("--account", default="",
                        help="多微信号:账号名(凭据放 data/wechat/accounts/<name>/);默认旧单号路径")
    args = parser.parse_args(argv)
    args.open = not args.no_open
    weixin_store.use_account(args.account)
    try:
        return asyncio.run(_serve(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
