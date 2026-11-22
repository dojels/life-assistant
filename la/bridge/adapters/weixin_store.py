"""扫码凭据的本地持久化。

放在 `data/wechat/credentials.json`。**这个文件等同于你的微信身份**,
所以:目录与文件都用最严的权限(Windows 上尽力而为),并且**永不进日志**。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ... import config
from .weixin_protocol import WINDOW_S, BotCredentials

#: 相对数据目录的位置
SUBDIR = "wechat"
FILENAME = "credentials.json"
#: 一个进程只服务一个微信号。要多号(爸爸/妈妈各自扫码),用 `use_account()` 切到
#: `data/wechat/accounts/<name>/`;默认空 = 兼容旧路径 `data/wechat/credentials.json`。
_ACCOUNT = ""
#: 会话游标:长轮询的断点,丢了只会重复拉一次,不会丢消息
CURSOR_FILENAME = "cursor.txt"
#: 会话窗口(context_token + 剩余额度)。丢了 = 主动推送失效,直到用户再说一句话
SESSION_FILENAME = "session.json"
#: 待发队列:发失败(断网 / 额度用完)的回复先存这儿,等能发了再补上。
#: ⭐ 不存的话,那些回复是**真的没了** —— 用户看到的是"连续无响应"(2026-10-13 实测)
OUTBOX_FILENAME = "outbox.json"


def use_account(name: str | None) -> str:
    """切换本进程的微信账号目录。空 = 旧版单账号路径。

    ⚠️ 账号名会进文件路径:只保留字母/数字/下划线/点/横线,防目录穿越。
    一个进程只能有一个账号;多账号请起多个进程(各扫各的码)。
    """
    global _ACCOUNT
    clean = "".join(ch for ch in str(name or "").strip()
                    if ch.isalnum() or ch in ("_", "-", "."))
    clean = clean.strip(".")
    _ACCOUNT = clean
    return _ACCOUNT


def current_account() -> str:
    return _ACCOUNT


def _clean_account(name: str | None) -> str:
    clean = "".join(ch for ch in str(name or "").strip()
                    if ch.isalnum() or ch in ("_", "-", "."))
    return clean.strip(".")


def _dir(account: str | None = None) -> Path:
    """账号目录。`account=None` = 用本进程当前账号;空串 = 显式指默认账号。

    ⚠️ 为什么要有"显式"这一层:Web 是多线程的,用 use_account() 切全局会串号
       (两个请求同时给不同账号扫码)。页面那边一律传 account=,不碰这个全局。
    """
    name = _ACCOUNT if account is None else _clean_account(account)
    path = Path(config.DATA_DIR) / SUBDIR
    if name:
        path = path / "accounts" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


#: 扫码成功后由**网页进程**留下的"请重连"标志;桥接在下一轮循环边界消费它。
RELOGIN_FILENAME = ".relogin"


def credentials_path(account: str | None = None) -> Path:
    return _dir(account) / FILENAME


def relogin_path(account: str | None = None) -> Path:
    return _dir(account) / RELOGIN_FILENAME


def request_relogin(account: str | None = None) -> None:
    """告诉常驻进程"凭据换了,下一轮重连"。写文件而不是发信号:跨进程、跨平台、不需要权限。"""
    try:
        relogin_path(account).write_text(str(int(__import__("time").time())), encoding="utf-8")
    except OSError:
        pass


def consume_relogin(account: str | None = None) -> bool:
    """取走标志(一次性)。没有 = False。"""
    path = relogin_path(account)
    if not path.exists():
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def known_accounts() -> list[str]:
    """默认账号("")+ data/wechat/accounts/ 下的每个目录。给设置页列账号用。"""
    names: list[str] = [""]
    base = Path(config.DATA_DIR) / SUBDIR / "accounts"
    if base.is_dir():
        for child in sorted(base.iterdir()):
            if child.is_dir() and _clean_account(child.name) == child.name:
                names.append(child.name)
    return names


def _lock_down(path: Path) -> None:
    """尽力收紧权限。Windows 上 chmod 语义有限,失败就算了,不抛。"""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load(account: str | None = None) -> BotCredentials | None:
    path = credentials_path(account)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    creds = BotCredentials.from_dict(raw)
    return creds if creds.token else None


def save(creds: BotCredentials, account: str | None = None) -> Path:
    path = credentials_path(account)
    payload = json.dumps(creds.as_dict(), ensure_ascii=False, indent=2)
    # 先写临时文件再替换:扫码成功那一刻崩掉,不至于留下半个文件
    temp = path.with_suffix(".tmp")
    temp.write_text(payload, encoding="utf-8")
    _lock_down(temp)
    temp.replace(path)
    return path


def clear(account: str | None = None) -> None:
    folder = _dir(account)
    for path in (credentials_path(account), folder / CURSOR_FILENAME,
                 session_path(account), folder / OUTBOX_FILENAME):
        try:
            path.unlink()
        except OSError:
            pass


def session_path(account: str | None = None) -> Path:
    return _dir(account) / SESSION_FILENAME


def load_session(account: str | None = None) -> dict | None:
    """读回上次进程留下的会话窗口。没有 / 坏了 / 没有 token 都当没有。"""
    path = session_path(account)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or not raw.get("context_token"):
        return None
    return raw


def save_session(payload: dict, account: str | None = None) -> None:
    """把会话窗口落盘 —— **主动推送靠它活过重启**。

    ⚠️ 为什么必须存:context_token 是长轮询从消息里换来的,原先只活在内存里。
    服务一重启(计划任务每天都会重起),窗口就凭空消失 —— 表现是"到点提醒一条
    都推不出来",而用户明明上午刚说过话。这不是可选项,是这个功能能不能用的前提。

    ⚠️ 敏感级别与 credentials.json 相同:它等同于"能替你发消息的通行证",
    所以放同一个目录、同样 chmod 600、同样**永不进日志**。
    """
    path = session_path(account)
    try:
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        _lock_down(temp)
        temp.replace(path)
    except OSError:
        pass


def windows(*, now_s: float | None = None) -> dict[str, dict]:
    """盘上每个账号留下的会话窗口:user_id → {account, age_s, open}。

    ⚠️ 为什么网页进程也要读它:context_token 只活在桥接进程的内存里,但
       "这个人还能不能收到主动消息"是**设置页必须提前说清楚**的事 ——
       否则用户只能等到晚上 19:30 发现"没收到",再去翻日志。session.json
       是跨进程唯一可信的那份记录。
    """
    moment = time.time() if now_s is None else float(now_s)
    found: dict[str, dict] = {}
    for account in known_accounts():
        raw = load_session(account)
        if not raw:
            continue
        user_id = str(raw.get("user_id") or "")
        if not user_id:
            continue
        last_seen_ms = int(raw.get("last_seen_ms") or 0)
        age = None if not last_seen_ms else max(0.0, moment - last_seen_ms / 1000.0)
        found[user_id] = {
            "account": account,
            "age_s": age,
            "open": True if age is None else age < WINDOW_S,
        }
    return found


def outbox_path(account: str | None = None) -> Path:
    return _dir(account) / OUTBOX_FILENAME


def load_outbox(account: str | None = None) -> list[dict]:
    """读回"当时没发出去"的回复。

    ⭐ 为什么要落盘(2026-10-13 实测):21:32–21:53 本机到微信服务端的网络断了
    (ReadTimeout → ConnectError → getaddrinfo failed),那几条回复**发不出去就没了** ——
    用户那边看到的是"连续无响应"。进程还重启过一次,只放内存里同样会丢。

    ⚠️ 里面是**用户的原话回复**,敏感级别与 session.json 相同:同目录、同样 chmod 600。
    """
    path = outbox_path(account)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict) and str(item.get("text") or "").strip()]


def save_outbox(items: list[dict], account: str | None = None) -> None:
    path = outbox_path(account)
    try:
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        _lock_down(temp)
        temp.replace(path)
    except OSError:
        pass


def load_cursor(account: str | None = None) -> str:
    try:
        return (credentials_path(account).parent / CURSOR_FILENAME).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def save_cursor(cursor: str, account: str | None = None) -> None:
    try:
        (credentials_path(account).parent / CURSOR_FILENAME).write_text(cursor, encoding="utf-8")
    except OSError:
        pass
