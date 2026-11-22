"""页面可编辑的密钥:加密落库、按版本号热加载。

三层设计(与 ops/credentials.py 的分层是同一套优先级,只是多了一层库):

    真实环境变量  >  页面设置(app_secrets / sys_settings)  >  settings.yaml  >  .env

为什么页面要压过 .env:用户点过保存之后,他的点击就是最新意图;
.env 从此只当"出厂默认"(与 sys_settings 里那个联网开关同一条口径)。
**但环境变量仍然最高** —— 那是运维/CI 的一次性覆盖,故意留的紧急通道。

加密边界(别夸大):AES-256-GCM + 本机 key 文件,防的是误提交、备份泄露、
日志泄露、别人翻你屏幕;**不防**已经拿到你整块磁盘的人 —— 那种情况下 .env 也是明文。
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes

from .. import config
from ..base import live_config

#: 密钥文件名(放在数据目录里,已被 .gitignore 的 data/ 覆盖)
KEY_FILE_NAME = ".secret.key"

#: 加密的密钥字段。名字就是 app_secrets.name,同时也是 settings 里的覆盖键。
SECRET_NAMES: tuple[str, ...] = ("llm.api_key", "tts.api_key")

#: 明文可存的配置(存在 sys_settings,同样的键名)。
PUBLIC_NAMES: tuple[str, ...] = (
    "llm.provider",
    "llm.base_url",
    "llm.model",
    "llm.reasoner_model",
    "llm.reasoning_effort",
    "tts.provider",
    "tts.voice",
)

#: 每个密钥对应的环境变量(显示"来源"用,也用于优先级判断)
ENV_NAMES: dict[str, str] = {
    "llm.api_key": "DEEPSEEK_API_KEY",
    "tts.api_key": "DASHSCOPE_API_KEY",
}

#: 版本号键。任何一次保存都 +1,各进程据此重建 Settings。
REV_KEY = "config_rev"

#: 同一进程里最多每秒查一次库(工具执行里 get_settings 会被调很多次)。
SYNC_TTL_S = 1.0

_KEY_CACHE: bytes | None = None
_last_check = 0.0
_loaded = False


def key_path() -> Path:
    return Path(config.DATA_DIR) / KEY_FILE_NAME


def _create_key(path: Path) -> bytes:
    """生成并落盘 32 字节密钥。O_EXCL 防两个进程同时建出两把不同的钥匙。"""
    data = get_random_bytes(32)
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_bytes()[:32]
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass
    return data


def _key() -> bytes:
    global _KEY_CACHE
    if _KEY_CACHE is not None:
        return _KEY_CACHE
    path = key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        data = path.read_bytes().strip()
        if len(data) < 32:
            raise RuntimeError("密钥文件损坏(长度不足):" + str(path))
        _KEY_CACHE = data[:32]
    else:
        _KEY_CACHE = _create_key(path)
    return _KEY_CACHE


def encrypt(name: str, text: str) -> bytes:
    """nonce(12) || ciphertext+tag。AAD 绑字段名 —— 把 llm 的密文挪到 tts 解不开。"""
    nonce = get_random_bytes(12)
    cipher = AES.new(_key(), AES.MODE_GCM, nonce=nonce)
    cipher.update(name.encode("utf-8"))
    body, tag = cipher.encrypt_and_digest(str(text).encode("utf-8"))
    return nonce + body + tag


def decrypt(name: str, blob: bytes) -> str | None:
    """解不开就返回 None(换机器/丢 key/换了字段名都走这条),绝不抛给调用方。"""
    raw = bytes(blob or b"")
    if len(raw) < 12 + 16:
        return None
    nonce, body, tag = raw[:12], raw[12:-16], raw[-16:]
    try:
        cipher = AES.new(_key(), AES.MODE_GCM, nonce=nonce)
        cipher.update(name.encode("utf-8"))
        return cipher.decrypt_and_verify(body, tag).decode("utf-8")
    except Exception:  # noqa: BLE001 - 解密失败一律当"没配置"
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _table_ready(conn: sqlite3.Connection | None, table: str) -> bool:
    if conn is None:
        return False
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def get_secret(conn: sqlite3.Connection | None, name: str) -> str | None:
    if not _table_ready(conn, "app_secrets"):
        return None
    try:
        row = conn.execute(
            "SELECT ciphertext FROM app_secrets WHERE name = ?", (name,)
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return decrypt(name, row["ciphertext"])


def has_secret(conn: sqlite3.Connection | None, name: str) -> bool:
    if not _table_ready(conn, "app_secrets"):
        return False
    try:
        row = conn.execute("SELECT 1 FROM app_secrets WHERE name = ?", (name,)).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def set_secret(
    conn: sqlite3.Connection, name: str, value: str, *, member_id: int | None = None
) -> None:
    text = str(value or "").strip()
    if not text:
        raise ValueError("密钥不能是空字符串;要清除请用「清除」")
    if name not in SECRET_NAMES:
        raise ValueError("未知的密钥名:" + name)
    conn.execute(
        "INSERT INTO app_secrets(name, ciphertext, updated_at, updated_by)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(name) DO UPDATE SET ciphertext = excluded.ciphertext,"
        " updated_at = excluded.updated_at, updated_by = excluded.updated_by",
        (name, encrypt(name, text), _now(), member_id),
    )
    bump_rev(conn)


def clear_secret(conn: sqlite3.Connection, name: str) -> None:
    conn.execute("DELETE FROM app_secrets WHERE name = ?", (name,))
    bump_rev(conn)


def get_setting(conn: sqlite3.Connection | None, name: str) -> str | None:
    if not _table_ready(conn, "sys_settings"):
        return None
    try:
        row = conn.execute("SELECT value FROM sys_settings WHERE key = ?", (name,)).fetchone()
    except sqlite3.Error:
        return None
    return None if row is None else str(row["value"])


def set_setting(
    conn: sqlite3.Connection, name: str, value: str, *, member_id: int | None = None
) -> None:
    if name not in PUBLIC_NAMES:
        raise ValueError("未知的配置项:" + name)
    conn.execute(
        "INSERT INTO sys_settings(key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
        " updated_at = excluded.updated_at",
        (name, str(value).strip(), _now()),
    )
    bump_rev(conn)


def clear_setting(conn: sqlite3.Connection, name: str) -> None:
    conn.execute("DELETE FROM sys_settings WHERE key = ?", (name,))
    bump_rev(conn)


def current_rev(conn: sqlite3.Connection | None) -> int:
    raw = get_setting(conn, REV_KEY)
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return 0


def bump_rev(conn: sqlite3.Connection) -> int:
    """保存后 +1,并**立刻**推给本进程的 live_config(别的进程靠 sync 拉)。"""
    current = current_rev(conn)
    nxt = current + 1
    conn.execute(
        "INSERT INTO sys_settings(key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
        " updated_at = excluded.updated_at",
        (REV_KEY, str(nxt), _now()),
    )
    conn.commit()
    _reload_into_memory(conn, nxt)
    return nxt


def _reload_into_memory(conn: sqlite3.Connection, rev: int) -> None:
    global _loaded, _last_check, _KEY_CACHE
    values: dict[str, str] = {}
    for name in PUBLIC_NAMES:
        raw = get_setting(conn, name)
        if raw:
            values[name] = raw
    for name in SECRET_NAMES:
        text = get_secret(conn, name)
        if text:
            values[name] = text
    live_config.apply(values, rev)
    _loaded = True
    _last_check = time.monotonic()


def sync(conn: sqlite3.Connection | None, *, force: bool = False) -> int:
    """把库里的配置拉进本进程(最多每秒一次)。返回当前版本号。

    每个进程各拉各的:Web 在每次请求装配服务时拉,微信桥接在每轮消息前拉。
    """
    global _last_check, _loaded
    if conn is None:
        return live_config.rev()
    now = time.monotonic()
    if not force and _loaded and (now - _last_check) < SYNC_TTL_S:
        return live_config.rev()
    _last_check = now
    rev = current_rev(conn)
    if not force and _loaded and rev == live_config.rev():
        return rev
    _reload_into_memory(conn, rev)
    return rev


def reset_cache() -> None:
    """测试用:忘掉进程内的 key 与同步状态。"""
    global _KEY_CACHE, _last_check, _loaded
    _KEY_CACHE = None
    _last_check = 0.0
    _loaded = False
    live_config.reset()


def mask(value: str) -> str:
    """只留头尾。与 ops/credentials.py 的 mask 同一口径(不打印完整密钥)。"""
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return text[:4] + "…" + text[-4:]


def _real_env(name: str) -> str:
    """真实环境变量(排除 .env 注入的那些)—— 与 config.get_settings 同一口径。"""
    try:
        return (config._real_env(name) or "").strip()  # noqa: SLF001 - 同一层实现
    except Exception:  # noqa: BLE001
        return ""


@dataclass(frozen=True)
class Entry:
    key: str
    label: str
    secret: bool
    source: str          # env | page | settings.yaml | .env | missing
    preview: str = ""
    updated_at: str = ""

    @property
    def configured(self) -> bool:
        return self.source != "missing"


LABELS: dict[str, str] = {
    "llm.api_key": "对话密钥",
    "llm.base_url": "接口地址",
    "llm.model": "对话模型",
    "llm.reasoner_model": "推理模型",
    "llm.reasoning_effort": "思考强度",
    "llm.provider": "服务商",
    "tts.api_key": "语音密钥",
    "tts.provider": "语音服务商",
    "tts.voice": "音色",
}


def describe(conn: sqlite3.Connection | None) -> list[Entry]:
    """给设置页用:每一项的来源 / 掩码 / 修改时间。**永远不含明文**。"""
    yaml_values = config.read_settings_yaml()
    dotenv_values = config.load_dotenv()
    page_settings = {}
    page_secrets = {}
    if _table_ready(conn, "sys_settings"):
        try:
            for row in conn.execute(
                "SELECT key, value, updated_at FROM sys_settings WHERE key LIKE 'llm.%'"
                " OR key LIKE 'tts.%'"
            ):
                page_settings[str(row["key"])] = (str(row["value"]), str(row["updated_at"]))
        except sqlite3.Error:
            pass
    if _table_ready(conn, "app_secrets"):
        try:
            for row in conn.execute("SELECT name, updated_at FROM app_secrets"):
                page_secrets[str(row["name"])] = str(row["updated_at"])
        except sqlite3.Error:
            pass
    entries: list[Entry] = []
    for name in PUBLIC_NAMES + SECRET_NAMES:
        secret = name in SECRET_NAMES
        env_name = ENV_NAMES.get(name, "")
        source = "missing"
        preview = ""
        updated = ""
        if secret:
            if env_name and _real_env(env_name):
                source, preview = "env", mask(_real_env(env_name))
            elif name in page_secrets:
                source = "page"
                updated = page_secrets[name]
                decrypted = get_secret(conn, name)
                preview = mask(decrypted) if decrypted else "无法解密"
            elif yaml_values.get(env_name):
                source, preview = "settings.yaml", mask(yaml_values[env_name])
            elif dotenv_values.get(env_name):
                source, preview = ".env", mask(dotenv_values[env_name])
        else:
            if name in page_settings:
                source, updated = "page", page_settings[name][1]
                preview = page_settings[name][0][:60]
            elif yaml_values.get(name):
                source, preview = "settings.yaml", str(yaml_values[name])[:60]
            elif dotenv_values.get(name):
                source, preview = ".env", str(dotenv_values[name])[:60]
        entries.append(Entry(name, LABELS.get(name, name), secret, source, preview, updated))
    return entries

