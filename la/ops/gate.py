"""自我更新之后自动跑一遍 T0,把结论留在磁盘上。

## 这个模块回答一个问题

**"我刚改完自己的这一版,是好的吗?"**

三个答案各有各的用处,而它们**不能互相替代**:

    磁盘上那份状态   人可以随时回来看(人格页上就是读它)
    一个布尔值       代码可以拿它挡下一次保存(拒绝发布)
    一份记录         事后追得了"哪一版、什么时候、红在哪"

## 为什么是"后台跑 + 拦下一次",而不是"保存前同步跑"

T0 是一千多条测试,实测 **两分钟上下**。把它挂在保存按钮上,等于让用户对着转圈;
而浏览器超时、进程被杀,那一分钟就白花了。所以:

    * 保存**立刻成功**(人格是数据,写库是瞬间的);
    * 保存之后**自动**起一个后台子进程跑 T0;
    * 结论落盘;**下一次**自我更新如果看见"上一版是红的",就拒绝 —— 除非你显式强推。

⚠️ 一条防呆规则:红的那份记录如果**代码指纹和现在不一样**,说明它评的不是当前这一版
   (典型:你改完代码重启了)。这种情况**不拦** —— 拿一份过期判决书挡人,只会教人
   去点"强推"。

## 为什么用子进程,不在进程内跑

进程内跑会把整套测试的导入、临时目录、SQLite 连接全灌进正在服务的那个进程里。
子进程既隔离,又能被 Ctrl-C / 超时杀掉。`sys.executable` 的 pythonw 问题见
verify.python_exe()。
"""

from __future__ import annotations

import dataclasses
import json
import threading
import time
from pathlib import Path
from typing import Any

from .. import config
from . import verify

def enable_auto(flag: bool = True) -> None:
    """打开/关闭"改完自动跑"。生产入口打开它;测试默认关着(一次就是整套测试)。"""
    global _AUTO
    _AUTO = bool(flag)


def auto_enabled() -> bool:
    return _AUTO


def status_path() -> Path:
    """状态文件在哪。

    ⚠️ **必须是函数,不能是模块常量**:`config.DATA_DIR` 会被改(测试把数据目录指到临时目录、
       评测脚本也这么干)。import 期绑死的路径会让测试读到开发机上的真实状态文件 ——
       于是"你的验收红着"就会变成"别人的测试挂了"。
    """
    return Path(config.DATA_DIR) / "eval" / "gate-latest.json"

#: 状态文件里的 schema 版本。将来加字段不改它、改含义才改它。
SCHEMA = 1

#: 自动跑要不要开。默认**关**,由生产入口显式打开(见 enable_auto)。
#:
#: ⚠️ 为什么是一个模块开关,而不是 create_app(with_gate=...) 的参数:
#:    `python -m la --web` 真正服务的是**模块级**那个 app(`la.web.app:app`),不是
#:    create_app() 的返回值 —— 挂成参数会"看着传了、其实没生效",而这里静默失效的后果
#:    是"自我更新之后再也没自动验过",没人会发现。开关放模块级,谁打开都算数。
_AUTO = False
_LOCK = threading.Lock()
_RUNNING = False
_STARTED_AT = 0.0


@dataclasses.dataclass
class GateStatus:
    """一轮 T0 的结论。**只存结论,不存输出** —— 输出在 verify 的记录里。"""

    ok: bool = False
    ran_at: str = ""
    duration_s: float = 0.0
    reason: str = ""
    tier: int = 0
    total: int = 0
    red: list[str] = dataclasses.field(default_factory=list)
    #: 红的那几项**为什么红**(每项一句,来自验收记录里的 detail)。
    #: ⚠️ 2026-10-25 真机踩到:云端从来没同步过 tests/,T0 的单元测试 0.14 秒就红一项
    #:    (ImportError: tests),而"红了"只把 key 写进页面 —— 人看到的是一句
    #:    "验收:红了 1 项",既不知道哪儿坏了,也不知道下一次保存为什么会被挡。
    red_detail: str = ""
    detail: str = ""
    signature: dict[str, Any] = dataclasses.field(default_factory=dict)
    error: str = ""
    #: 另一轮验收正在跑,这一轮没跑成(既不算通过,也**不算红**)
    busy: bool = False

    def as_dict(self) -> dict[str, Any]:
        payload = dataclasses.asdict(self)
        payload["schema"] = SCHEMA
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "GateStatus | None":
        if not isinstance(data, dict):
            return None
        fields = {f for f in cls.__dataclass_fields__}
        try:
            return cls(**{key: data[key] for key in data if key in fields})
        except TypeError:
            return None

    @property
    def code_sha(self) -> str:
        return str((self.signature or {}).get("code_sha") or "")

    def render(self) -> str:
        """一行,给页面用。"""
        if self.busy:
            return "另一轮验收还在跑,这一次没跑(等它结束)"
        if self.error:
            return "验收没跑成:" + self.error
        stamp = (self.ran_at or "")[-8:]
        head = ("全过" if self.ok else "红了 " + str(len(self.red)) + " 项")
        tail = " · ".join(
            [piece for piece in (
                (stamp + " 跑过") if stamp else "",
                self.detail,
                "%.0f 秒" % self.duration_s if self.duration_s else "",
                ("原因:" + self.reason) if self.reason else "",
                # 红着的时候把"坏在哪"也说出来(截短:这是一行横幅,不是日志)
                ("红在:" + self.red_detail[:90]) if self.red_detail else "",
            ) if piece]
        )
        return head + "(" + tail + ")"


# --- 读写磁盘 ----------------------------------------------------------------


def latest(path: Path | None = None) -> GateStatus | None:
    target = Path(path or status_path())
    try:
        return GateStatus.from_dict(json.loads(target.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def save(status: GateStatus, path: Path | None = None) -> Path:
    target = Path(path or status_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(status.as_dict(), ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(target)   # 原子替换:页面不会读到半截 JSON
    return target


# --- 跑 ----------------------------------------------------------------------


def run(reason: str = "", *, timeout_s: int = 1800, python: str | None = None,
        path: Path | None = None) -> GateStatus:
    """**同步**跑一轮 T0(--verify 0 --json)。失败也返回状态,不抛异常。"""
    started = time.time()
    args = [python or verify.python_exe(), "-m", "la", "--verify", "0", "--json"]
    code, out, err, _seconds = verify.run_subprocess(args, timeout_s)

    payload: dict[str, Any] | None = None
    for line in reversed((out or "").splitlines()):
        text = line.strip()
        if text.startswith("{"):
            try:
                payload = json.loads(text)
                break
            except ValueError:
                continue

    status = GateStatus(reason=reason, duration_s=round(time.time() - started, 1))
    if payload is None:
        status.ok = False
        status.error = ("没拿到结果(退出码 " + str(code) + ")"
                        + (":" + (err or out).strip().splitlines()[-1][:80]
                           if (err or out or "").strip() else ""))
    elif payload.get("busy"):
        # 两轮验收不许并发(见 ops/verify.py 的单飞锁)。这一轮**不算跑过**,
        # 所以既不能写"全过",也不能判红。
        status.busy = True
        status.ok = False
        status.error = ""
        status.detail = str(payload.get("detail") or "另一轮验收在跑")
    else:
        status.ok = bool(payload.get("ok"))
        status.ran_at = str(payload.get("generated_at") or "")
        status.tier = int(payload.get("tier") or 0)
        items = payload.get("items") or []
        status.total = len(items)
        status.red = list(payload.get("red") or [])
        status.signature = payload.get("signature") or {}
        passed = sum(1 for item in items if item.get("ok") or item.get("skipped"))
        status.detail = str(passed) + "/" + str(status.total) + " 项通过"
        # 红的那几项的**原因**也存下来:挡住下一次保存时,人要知道到底哪儿坏了
        # (否则只会看到"红了 1 项",然后去点「强推」)。
        notes: list[str] = []
        for item in items:
            if item.get("ok") or item.get("skipped"):
                continue
            key = str(item.get("key") or "?")
            why = " ".join(str(item.get("detail") or "").split())
            notes.append(key + (": " + why[:140] if why else ""))
        status.red_detail = " / ".join(notes)[:400]
    save(status, path)
    return status


def start(reason: str = "", *, path: Path | None = None,
          timeout_s: int = 1800, python: str | None = None) -> bool:
    """后台跑一轮。已经在跑就**不重复起**(返回 False)。

    返回值的意思是"这一次有没有真的起一个";不是"验证通过没"(那要等)。
    """
    global _RUNNING, _STARTED_AT
    with _LOCK:
        if _RUNNING:
            return False
        _RUNNING = True
        _STARTED_AT = time.time()

    def worker() -> None:
        global _RUNNING
        try:
            run(reason, timeout_s=timeout_s, python=python, path=path)
        except Exception:  # noqa: BLE001 - 后台线程炸了不能静默,也不能带走进程
            try:
                save(GateStatus(ok=False, reason=reason, ran_at=time.strftime("%H:%M:%S"),
                                error="后台跑验收时异常"), path)
            except Exception:  # noqa: BLE001
                pass
        finally:
            with _LOCK:
                _RUNNING = False

    threading.Thread(target=worker, name="la-gate", daemon=True).start()
    return True


def running() -> float:
    """还在跑就返回已经跑了多少秒,否则 0。

    ⚠️ 跑着的返回值至少 0.1:刚起步那一瞬间 round(…, 1) 会给出 0.0,而 0.0 的意思是
       "没在跑" —— 一个既表示"跑了 0 秒"又表示"没跑"的返回值,调用方必然写错判断。
    """
    with _LOCK:
        if not _RUNNING:
            return 0.0
        return max(0.1, round(time.time() - _STARTED_AT, 1))


# --- 拿它挡人 ----------------------------------------------------------------


def should_block(status: GateStatus | None,
                 current_code_sha: str = "") -> tuple[bool, str]:
    """这一版能不能继续自我更新?返回 (挡不挡, 给人看的那句话)。

    三条**不挡**的情形,每条都有理由:

    1. 没有记录 —— 从没验过不等于验不过。第一次保存不该被一张不存在的判决书拦住;
    2. 全过 —— 没什么可挡的;
    3. 记录评的是**别的代码** —— 过期判决书;见模块开头那条防呆规则。
    """
    if status is None or status.error or status.busy:
        return False, ""
    if status.ok:
        return False, ""
    if current_code_sha and status.code_sha and status.code_sha != current_code_sha:
        return False, "上一轮验收判的是另一版代码(" + status.code_sha + "),不作数"
    detail = "、".join(status.red[:3]) + ("…" if len(status.red) > 3 else "")
    return True, ("上一版没通过验收:" + str(len(status.red)) + " 项红"
                  + ("(" + detail + ")" if detail else "")
                  + ("——" + status.red_detail[:160] if status.red_detail else "")
                  + "。修好再改,或者勾「我知道,仍然保存」。")
