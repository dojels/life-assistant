"""两份评测记录之间差在哪 —— 压成五行,外加一句结论。

## 为什么不能只给一个"综合分"

一个数会把**不同方向的变化**平均掉:少说了 200 字(好)和开始编造金额(坏)加在一起,
可能正好是个漂亮分。所以这里不求和,只做两件事:

1. **逐项判**:哪些检查从「过」变「不过」(退化),哪些从「不过」变「过」(修好)。
   这个不需要模型、不受判分心情影响 —— 记录里本来就有每条的结论。
2. **分层给结论**:有几条**否决项**一旦破了,无论别的怎么变都是"不要发布";
   其余退化只报出来,由人决定是不是可接受。

## 五行是什么

    正确性 / 字数 / 成本 / 否决项 / 结论

前四条是**事实**,最后一条是**判断**。把判断单独摆一行,是为了让"这版能不能发"
成为一个可以用一句话回答的问题,而不是一堆数字要人自己加。

⚠️ 本模块**只读记录、不跑模型**:它可以在任何时刻对着旧记录重算(与
   `scripts/verify_persona.py --rescore` 同一条思路:判分口径会变,记录要能重判)。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

#: 一旦从「过」变「不过」就**不要发布**的检查。
#:
#: 判据只有一条:**它坏了,后面的数字就不用看了** —— 编数字、说谎、外发密钥、
#: 该写的没写进去。这些不是"稍微差一点",是坏了。
VETO_CHECKS = frozenset({
    "ledger_3800",            # 说记了账,账本里却没有
    "no_fabricated_money",    # 凭空报金额
    "no_fabricated_steps",    # 凭空报步数
    "no_record_admission",    # 没数据却说有
    "secret_guard",           # 吐出密钥形状的串
    "task_created",           # 说建了待办/提醒,一条都没写
})


def _hard_checks(item: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {name: check for name, check in (item.get("checks") or {}).items()
            if check.get("hard")}


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def _pct(before: float | None, after: float | None) -> str:
    if not before or after is None:
        return ""
    delta = (after - before) / before * 100.0
    return ("+" if delta > 0 else "") + ("%.1f" % delta) + "%"


def _usage(item: dict[str, Any]) -> dict[str, int] | None:
    raw = item.get("usage")
    if not isinstance(raw, dict):
        return None
    try:
        return {key: int(raw.get(key) or 0) for key in ("prompt", "cached", "completion", "calls")}
    except (TypeError, ValueError):
        return None


@dataclass
class EvalDelta:
    """两份记录之间的差。所有字段都是**从记录里算出来的事实**,不含判断。"""

    before_label: str = ""
    after_label: str = ""
    #: 两边都跑过的用例(取 after 的顺序)
    cases: int = 0
    #: after 这一份:硬检查通过数 / 总数
    hard_ok: int = 0
    hard_total: int = 0
    #: 从「过」变「不过」:[(用例, 检查, 说明)]
    broke: list[tuple[str, str, str]] = field(default_factory=list)
    #: 从「不过」变「过」
    fixed: list[tuple[str, str, str]] = field(default_factory=list)
    #: 两边都不过
    still_red: list[str] = field(default_factory=list)
    #: after 里仍然红的用例(含新用例)
    red_cases: list[str] = field(default_factory=list)
    #: 字数(中位)
    chars_before: float | None = None
    chars_after: float | None = None
    #: 超过 max_chars 的用例数(after)
    over_limit: int = 0
    #: 每轮 prompt token(中位)与缓存命中率
    prompt_before: float | None = None
    prompt_after: float | None = None
    cache_rate: float | None = None
    usage_cases: int = 0
    #: 两份签名的差别(空 = 签名相同,或旧记录没签名)
    signature_changed: list[str] = field(default_factory=list)
    signatures_known: bool = False
    elapsed_before: float = 0.0
    elapsed_after: float = 0.0

    # --- 判断 ---------------------------------------------------------------

    @property
    def veto(self) -> list[tuple[str, str, str]]:
        return [row for row in self.broke if row[1] in VETO_CHECKS]

    @property
    def verdict(self) -> str:
        """一句人话。**只有这里做判断**,其余字段都是事实。"""
        if not self.cases:
            return "没法比(两边没有共同的用例)"
        if self.veto:
            return "不要发布(否决项被打破:" + "、".join(row[1] for row in self.veto) + ")"
        if self.broke:
            return "先别发布 —— 有 " + str(len(self.broke)) + " 处退化,看完再决定"
        if self.fixed:
            return "可以发布(还修好了 " + str(len(self.fixed)) + " 处)"
        return "可以发布(这一版没测出变化)"

    @property
    def publishable(self) -> bool:
        return not self.veto and not self.broke and bool(self.cases)

    # --- 输出 ---------------------------------------------------------------

    def render(self) -> list[str]:
        """五行。给终端、给 Markdown 报告都直接可用。"""
        ok_line = ("正确性  " + str(self.hard_ok) + "/" + str(self.hard_total) + " 条硬检查通过"
                   + " · 红 " + str(len(self.red_cases)) + " 条用例"
                   + " · 修好 " + str(len(self.fixed)) + " / 弄坏 " + str(len(self.broke))
                   + " / 仍然红 " + str(len(self.still_red)))
        if self.chars_after is None:
            chars_line = "字数    这次记录没存字数"
        else:
            diff = _pct(self.chars_before, self.chars_after)
            over = (" · 超上限 " + str(self.over_limit) + " 条") if self.over_limit else ""
            chars_line = ("字数    中位 " + str(int(self.chars_after)) + " 字"
                          + ("(比上一版 " + diff + ")" if diff else "") + over)
        if not self.usage_cases:
            # ⚠️ "没存"要分清是**哪一边**没存:旧记录(2026-10-17 之前)没有 token 账,
            #    而新记录是有的 —— 写成"这次没存"会让用户以为这一版退化了。
            cost_line = ("成本    两份记录里有一份没存 token 账(2026-10-17 之前的记录没有这项),"
                         "成本没法对照")
        else:
            diff = _pct(self.prompt_before, self.prompt_after)
            cost_line = ("成本    每轮 prompt 中位 " + _k(self.prompt_after) + " token"
                         + ("(比上一版 " + diff + ")" if diff else "")
                         + ("" if self.cache_rate is None
                            else " · 缓存命中 " + str(round(self.cache_rate * 100)) + "%"))
        veto_line = ("否决项  " + (("**破了 " + str(len(self.veto)) + " 条**:"
                                   + "、".join(row[0] + "/" + row[1] for row in self.veto))
                      if self.veto else "全过"))
        return [ok_line, chars_line, cost_line, veto_line, "结论    " + self.verdict]

    def render_changes(self) -> str:
        if not self.signatures_known:
            return "版本    这两份记录有一份没有版本签名(旧记录),无法确认它们差在哪"
        if not self.signature_changed:
            return "版本    签名**相同** —— 这份对照测的是随机性,不是版本差别"
        return "版本    变的是:" + " · ".join(self.signature_changed)


def _k(value: float | None) -> str:
    """17250 → "17.3k"。给人看的近似数,不追求精确。"""
    if value is None:
        return "?"
    if value >= 1000:
        return "%.1fk" % (value / 1000.0)
    return str(int(round(value)))


def diff(before: dict[str, Any], after: dict[str, Any]) -> EvalDelta:
    """算两份记录的差。两边都只用到"记录里写下来的东西"。"""
    before_records = {row.get("id"): row for row in (before.get("records") or [])}
    after_records = {row.get("id"): row for row in (after.get("records") or [])}
    common = [key for key in after_records if key in before_records]

    out = EvalDelta(
        before_label=str(before.get("label") or "?"),
        after_label=str(after.get("label") or "?"),
        cases=len(common),
        elapsed_before=float(before.get("elapsed_s") or 0.0),
        elapsed_after=float(after.get("elapsed_s") or 0.0),
    )

    chars_before: list[float] = []
    chars_after: list[float] = []
    prompt_before: list[float] = []
    prompt_after: list[float] = []
    cached = 0
    prompt_sum = 0

    for case_id in common:
        old = before_records[case_id]
        new = after_records[case_id]
        old_checks = _hard_checks(old)
        new_checks = _hard_checks(new)
        for name, check in new_checks.items():
            out.hard_total += 1
            if check.get("ok"):
                out.hard_ok += 1
        # 逐项对照。只在**两边都有这一项**时判"变化":一边没有它,说明口径改了。
        for name, check in new_checks.items():
            prior = old_checks.get(name)
            if prior is None:
                continue
            was, now = bool(prior.get("ok")), bool(check.get("ok"))
            if was and not now:
                out.broke.append((case_id, name, str(check.get("detail") or "")))
            elif not was and now:
                out.fixed.append((case_id, name, str(check.get("detail") or "")))

        old_red = [n for n, c in old_checks.items() if not c.get("ok")]
        new_red = [n for n, c in new_checks.items() if not c.get("ok")]
        if new_red:
            out.red_cases.append(case_id)
        if new_red and old_red:
            out.still_red.append(case_id)

        old_chars = (old.get("scan") or {}).get("chars")
        new_chars = (new.get("scan") or {}).get("chars")
        if isinstance(old_chars, (int, float)):
            chars_before.append(float(old_chars))
        if isinstance(new_chars, (int, float)):
            chars_after.append(float(new_chars))
        if (new.get("scan") or {}).get("max_chars") and isinstance(new_chars, (int, float)):
            if new_chars > float(new["scan"]["max_chars"]):
                out.over_limit += 1

        old_usage = _usage(old)
        new_usage = _usage(new)
        if old_usage is not None and new_usage is not None:
            prompt_before.append(float(old_usage["prompt"]))
            prompt_after.append(float(new_usage["prompt"]))
            if new_usage["calls"] or new_usage["prompt"]:
                out.usage_cases += 1
                cached += new_usage["cached"]
                prompt_sum += new_usage["prompt"]

    out.chars_before = _median(chars_before)
    out.chars_after = _median(chars_after)
    out.prompt_before = _median(prompt_before)
    out.prompt_after = _median(prompt_after)
    out.cache_rate = (cached / prompt_sum) if prompt_sum else None

    old_version = before.get("signature") or {}
    new_version = after.get("signature") or {}
    try:
        from .version import Version

        signed_before = Version.from_dict(old_version)
        signed_after = Version.from_dict(new_version)
        out.signatures_known = signed_before.known and signed_after.known
        out.signature_changed = signed_before.diff(signed_after)
    except Exception:  # noqa: BLE001 - 签名只影响一行说明,不该让对照挂掉
        out.signatures_known = False

    return out
