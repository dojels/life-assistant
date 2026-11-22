"""版本签名:一层很薄的「这是哪一版」的指纹。

## 为什么需要它

一份评测记录如果不知道自己测的是**哪一版**,它就只能证明"当时跑过"。
「这版比上一版好」这句话要求两件事:①两次记录各自知道自己是什么;
②两份签名一比,能说清**差在哪**。没有 ②,你把"改了人格"和"换了模型"两种
变化混在一次对照里,数据再漂亮也读不出因果。

所以这里只做一件事:把"她是谁"压成几个十六进制串。压进去的是**会改变行为**的东西:

    code_sha      la/ 下所有 .py 的内容指纹   —— 逻辑与提示词常量的载体
    prompt_sha    稳定前缀(身份 + 人格全文)   —— 每次请求开头那一块
    persona_sha   人格主体                    —— 单独一份,用来分辨"改的是人格还是代码"
    config_sha    预算与窗口那组常数           —— 决定上下文怎么切、什么时候压缩
    model/provider/reasoning_effort           —— 换模型是**比改提示词更大**的变量

⚠️ 三条口径:

1. **只算 .py**。模板与静态文件改了不影响"她是谁",但会让页面变样 ——
   那部分交给 `la/ops/verify.py` 的 T0 静态检查(模板能编译),不混进版本签名,
   否则每次调 CSS 都会让历史记录"不可比",签名就没人看了。
2. **常数额外列名字**(`_CONFIG_KEYS`),不全量扫 config。加一个新常量不该让所有
   历史签名一夜作废;而改这几个数一定改变行为,必须看得见。
3. **签名不带时间**。同一版跑两次必须字节相同(tests/test_ops_version.py 钉着)——
   否则"对照"会退化成"两次不同时刻的快照",而不是"两个版本"。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .. import config
from ..agent.prompts import IDENTITY, PERSONA_BODY, PERSONA_NAME, build_persona_text

#: 参与 config_sha 的常量。列名字而不是全扫:见上面口径 ②。
_CONFIG_KEYS: tuple[str, ...] = (
    "CONTEXT_WINDOW_TOKENS",
    "SINGLE_TURN_INPUT_SHARE",
    "TOKEN_ESTIMATE_RATIO",
    "BUDGET_REQUEST",
    "BUDGET_HARD_MAX",
    "BUDGET_TURN_WORKING",
    "BUDGET_TOOLS",
    "BUDGET_SYSTEM",
    "BUDGET_RUNTIME",
    "BUDGET_HOT_SUMMARY",
    "BUDGET_CONVERSATION",
    "BUDGET_COMPACT_TRIGGER",
    "BUDGET_COMPACT_RETAIN_RATIO",
    "BUDGET_IN_TURN_HEADROOM",
    "BUDGET_CHECKPOINT",
    "BUDGET_EVIDENCE",
    "MAX_HISTORY_MESSAGES",
)

#: 代码指纹的根:la/ 包本身(BASE_DIR/la)。
CODE_ROOT = Path(config.BASE_DIR) / "la"

_SHA_CHARS = 12


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:_SHA_CHARS]


def _digest_bytes(parts: list[bytes]) -> str:
    h = hashlib.sha256()
    for chunk in parts:
        h.update(chunk)
        h.update(b"\0")
    return h.hexdigest()[:_SHA_CHARS]


#: 缓存:(文件数, 最新 mtime) -> (sha, 文件数)。
#: ⚠️ 只缓存**默认根**(la/)。传了别的目录就不缓存 —— 测试要能反复造临时树。
_CODE_CACHE: dict[str, tuple[tuple[int, int], tuple[str, int]]] = {}


def code_fingerprint(root: Path | str | None = None) -> tuple[str, int]:
    """la/ 下所有 .py 的内容指纹。返回 (sha, 文件数)。

    缓存键用「文件数 + 最新 mtime」而不是内容:一次 stat 就够,改任何一个字节
    mtime 必然变。真变了才重算,~70 个文件约 5 毫秒。
    """
    base = Path(root) if root is not None else CODE_ROOT
    try:
        files = sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)
    except OSError:
        return "000000000000", 0
    if not files:
        return "000000000000", 0
    try:
        newest = max(p.stat().st_mtime_ns for p in files)
    except OSError:
        newest = 0
    stamp = (len(files), newest)

    use_cache = root is None
    if use_cache:
        cached = _CODE_CACHE.get("code")
        if cached is not None and cached[0] == stamp:
            return cached[1]

    parts: list[bytes] = []
    for path in files:
        try:
            rel = path.relative_to(base).as_posix().encode("utf-8")
            parts.append(rel + b"\n" + path.read_bytes())
        except OSError:
            continue
    out = (_digest_bytes(parts), len(files))
    if use_cache:
        _CODE_CACHE["code"] = (stamp, out)
    return out


def config_fingerprint() -> str:
    """预算与窗口那组常数的指纹(只看行为相关的那些)。"""
    rows = [name + "=" + str(getattr(config, name, "?")) for name in _CONFIG_KEYS]
    return _sha("\n".join(rows))


@dataclass(frozen=True)
class Version:
    """一份可以存进记录、也可以并排给人看的版本签名。"""

    code_sha: str = "000000000000"
    code_files: int = 0
    prompt_sha: str = ""
    persona_sha: str = ""
    persona_chars: int = 0
    persona_name: str = PERSONA_NAME
    persona_custom: bool = False
    config_sha: str = ""
    model: str = ""
    provider: str = ""
    reasoning_effort: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Version":
        """从记录里读回来。缺字段的旧记录 → 全空签名(不报错,只标记"旧记录")。"""
        if not isinstance(data, dict):
            return cls()
        known = {f: data[f] for f in cls.__dataclass_fields__ if f in data}
        return cls(**known)

    @property
    def short(self) -> str:
        return self.code_sha + "/" + self.persona_sha

    @property
    def known(self) -> bool:
        """有没有真签过名。旧记录 = False(对照时要说明"这两份没法确认差在哪")。"""
        return bool(self.code_sha and self.code_sha != "000000000000")

    def render(self) -> str:
        return (self.code_sha + " · 人格 " + self.persona_sha + "("
                + str(self.persona_chars) + " 字,"
                + ("你写的" if self.persona_custom else "出厂") + ",叫" + self.persona_name
                + ") · " + (self.model or "?")
                + " · 预算 " + self.config_sha)

    def diff(self, other: "Version") -> list[str]:
        """两份签名差在哪 —— 给对照报告的第一行用。

        空列表 = 签名相同(那这份对照测的不是"版本",而是随机性)。
        """
        out: list[str] = []
        if not self.known or not other.known:
            return []
        if self.code_sha != other.code_sha:
            out.append("代码 " + self.code_sha + "→" + other.code_sha)
        if self.persona_sha != other.persona_sha:
            out.append("人格 " + str(self.persona_chars) + "→" + str(other.persona_chars) + " 字")
        if self.prompt_sha != other.prompt_sha and self.persona_sha == other.persona_sha:
            out.append("身份块(人格没变)")
        if self.config_sha != other.config_sha:
            out.append("预算 " + self.config_sha + "→" + other.config_sha)
        if self.model != other.model:
            out.append("模型 " + (self.model or "?") + "→" + (other.model or "?"))
        if self.reasoning_effort != other.reasoning_effort:
            out.append("思考强度 " + self.reasoning_effort + "→" + other.reasoning_effort)
        return out


def signature(store: Any = None, settings: Any = None, root: Path | str | None = None) -> Version:
    """照一张当前版本的签名。

    `store` 给 PersonaStore 时读**库里的**人格(她此刻真实的样子);不给就读代码里的
    出厂默认。这个区别必须写在签名里(persona_custom),否则"评测测的是哪一版人格"
    会含糊成一句没人能核对的话。
    """
    if store is not None:
        try:
            state = store.state()
            body = state.body
            name = state.name
            custom = bool(state.custom)
        except Exception:  # noqa: BLE001 - 签名失败不该让评测/闸门挂掉
            body, name, custom = PERSONA_BODY, PERSONA_NAME, False
    else:
        body, name, custom = PERSONA_BODY, PERSONA_NAME, False

    if settings is None:
        try:
            settings = config.get_settings()
        except Exception:  # noqa: BLE001
            settings = None

    code_sha, code_files = code_fingerprint(root)
    persona_text = build_persona_text(body)
    return Version(
        code_sha=code_sha,
        code_files=code_files,
        prompt_sha=_sha(IDENTITY + "\n\n" + persona_text),
        persona_sha=_sha(body),
        persona_chars=len(body),
        persona_name=name,
        persona_custom=custom,
        config_sha=config_fingerprint(),
        model=str(getattr(settings, "model", "") or ""),
        provider=str(getattr(settings, "provider", "") or ""),
        reasoning_effort=str(getattr(settings, "reasoning_effort", "") or ""),
    )


def render_json(version: Version) -> str:
    return json.dumps(version.as_dict(), ensure_ascii=False, sort_keys=True)
