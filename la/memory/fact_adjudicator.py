"""**事实**准入的语义一半:让模型判关系,而不是让代码算相似度。

(证据裁决在 `adjudicator.py`;本模块只管"永久事实库收不收这一条"。)

为什么不是"相似度去重":
  "用户喜欢吃香菜" 与 "用户不喜欢吃香菜" 的编辑距离极近 —— 纯字面阈值会把
  **矛盾**判成**重复**,然后合并掉一条。这两件事的关系正好相反,必须由懂语义的一方判。

五种关系(与 `guard.py` 的确定性检查合起来才是完整准入):
    new         全新的,库里没有
    duplicate   与 #N 是同一条(说法不同也算)
    refines     与 #N 是同一条,但候选更准确/更完整 —— 用它的说法更新 #N
    contradicts 与 #N 冲突(同一个人对同一件事改了口)—— 新条生效,#N 置为已取代
    review      模型判不出来 / 输出不合规 —— **降级为待审**,绝不猜

⚠️ 裁决失败一律走 review(候选入库但**不进注入**),而不是"默认当新的放进去"。
   宁可让一条事实晚一轮生效、等人放行,也不要让一句没核过的话每轮进上下文。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from . import guard

REL_NEW = "new"
REL_DUPLICATE = "duplicate"
REL_REFINES = "refines"
REL_CONTRADICTS = "contradicts"
REL_REVIEW = "review"
RELATIONSHIPS: tuple[str, ...] = (REL_NEW, REL_DUPLICATE, REL_REFINES, REL_CONTRADICTS)

#: 模型可能写成中文或别的说法 —— 都映射回来,别因为一个词就整条作废
_REL_ALIASES: dict[str, str] = {
    "new": REL_NEW, "add": REL_NEW, "新": REL_NEW, "新增": REL_NEW, "新的": REL_NEW,
    "duplicate": REL_DUPLICATE, "dup": REL_DUPLICATE, "重复": REL_DUPLICATE,
    "same": REL_DUPLICATE, "同一条": REL_DUPLICATE, "一致": REL_DUPLICATE,
    "refines": REL_REFINES, "refine": REL_REFINES, "update": REL_REFINES,
    "更准确": REL_REFINES, "细化": REL_REFINES, "更新": REL_REFINES, "修正": REL_REFINES,
    "contradicts": REL_CONTRADICTS, "contradict": REL_CONTRADICTS, "conflict": REL_CONTRADICTS,
    "矛盾": REL_CONTRADICTS, "冲突": REL_CONTRADICTS, "改口": REL_CONTRADICTS,
    "review": REL_REVIEW, "unknown": REL_REVIEW, "待审": REL_REVIEW, "不确定": REL_REVIEW,
}

#: 给离线假客户端认的暗号 —— 它看到这句就返回一个确定的判决(见 llm/fake.py)
PROMPT_MARKER = "【事实准入裁决】"

#: 裁决调用的输出上限。合并后的说法最长 `config.FACT_MAX_CONTENT_CHARS`(120 字),
#: 400 留足余量;关键是这一次**不思考**,不再和思维链抢同一个 completion 预算(D170)。
VERDICT_MAX_TOKENS = 400

SYSTEM_PROMPT = """你是"永久事实库"的准入裁判。库里每一条事实都会**每一轮**进入对话上下文,一条错的会持续污染之后所有对话 —— 所以你的默认选择是"不记",而不是"记下来总没坏处"。

输入分三部分:已有事实列表(带编号)、待审候选、当前容量。
你要判断候选与已有事实的关系,只输出一个 JSON 对象,不要任何解释文字:

{"relationship":"new"}                                  全新的一条
{"relationship":"duplicate","target_id":N}              与 #N 是同一条(说法不同/更啰嗦也算)
{"relationship":"refines","target_id":N,"text":"…"}     与 #N 是同一件事,但候选更准确;text 给合并后的最终说法
{"relationship":"contradicts","target_id":N}            与 #N 冲突:同一个人对同一件事改了口

判定规则:
1. 宁合并勿新增。同一件事只是换了说法、多了语气词、顺序不同 —— 都算 duplicate。
2. **"喜欢 X" 与 "不喜欢 X" 是 contradicts,不是 duplicate。** 不要因为字面像就判重复。
3. 候选如果只是把已有事实说得更完整(补了条件/范围),判 refines 并给出合并后的说法。
4. 候选里含疑问词("什么/怎么/哪")或描述的是"未来的一个动作"时,判 {"relationship":"review"}。
5. 拿不准就判 {"relationship":"review"} —— 会有人来看,猜错代价更大。"""


@dataclass
class FactVerdict:
    """一次裁决的结果。"""

    relationship: str
    target_id: int | None = None
    text: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.relationship in RELATIONSHIPS

    @property
    def admits_fact(self) -> bool:
        """这次裁决要不要写/改事实行。"""
        return self.relationship in (REL_NEW, REL_REFINES, REL_CONTRADICTS)


def parse_verdict(raw: str) -> FactVerdict:
    """从模型输出里抠出判决。抠不出来就 review —— 不猜。"""
    text = str(raw or "").strip()
    if not text:
        return FactVerdict(REL_REVIEW, reason="模型没有返回内容")
    payload: dict[str, Any] | None = None
    try:
        loaded = json.loads(text)
        if isinstance(loaded, dict):
            payload = loaded
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            try:
                loaded = json.loads(match.group(0))
                if isinstance(loaded, dict):
                    payload = loaded
            except json.JSONDecodeError:
                payload = None
    if payload is None:
        return FactVerdict(REL_REVIEW, reason="输出不是 JSON:" + text[:80])

    raw_rel = str(payload.get("relationship") or payload.get("relation") or "").strip().lower()
    relationship = _REL_ALIASES.get(raw_rel)
    if relationship is None:
        return FactVerdict(REL_REVIEW, reason="没见过的关系类型:" + raw_rel[:40])

    target: int | None = None
    try:
        value = payload.get("target_id")
        target = int(value) if value is not None else None
    except (TypeError, ValueError):
        target = None

    merged = str(payload.get("text") or "").strip()
    if relationship in (REL_DUPLICATE, REL_REFINES, REL_CONTRADICTS) and target is None:
        # 说"和某条重复"却没说哪条 —— 等于没说,交给人看
        return FactVerdict(REL_REVIEW, reason="判了 " + relationship + " 但没给 target_id")
    return FactVerdict(relationship, target_id=target, text=merged)


def render_existing(facts: Sequence[Any], *, limit: int = 30) -> str:
    """把已有事实渲染给裁判看。只给**生效中**的 —— 已取代的不该影响新判断。"""
    lines: list[str] = []
    for fact in list(facts)[:limit]:
        lines.append(
            "#" + str(getattr(fact, "id", "?"))
            + " [" + str(getattr(fact, "type", "other")) + "] "
            + " ".join(str(getattr(fact, "content", "")).split())
        )
    return "\n".join(lines) if lines else "(库里还没有任何生效事实)"


def build_prompt(content: str, facts: Sequence[Any], *, capacity_note: str = "") -> str:
    return (
        PROMPT_MARKER + "\n"
        "已有事实:\n" + render_existing(facts) + "\n\n"
        "待审候选:\n" + " ".join(str(content).split()) + "\n\n"
        "当前容量:" + (capacity_note or "未提供") + "\n\n"
        "只输出那一个 JSON 对象。"
    )


def _police_polarity(verdict: FactVerdict, content: str, facts: Sequence[Any]) -> FactVerdict:
    """模型把"喜欢/不喜欢"判成 duplicate/refines 时,按矛盾处理。

    ⚠️ 这不是不信任模型,是因为这一条判错的代价是**永久丢掉一个方向的偏好**:
       duplicate 会把新说法丢掉;refines 会用新说法**原地改写**旧事实 ——
       两条路都会让库里只剩一个方向,另一个方向再也查不到。
    """
    if verdict.relationship not in (REL_DUPLICATE, REL_REFINES) or verdict.target_id is None:
        return verdict
    target = next(
        (f for f in facts if int(getattr(f, "id", 0) or 0) == int(verdict.target_id)), None
    )
    if target is None:
        return verdict
    mine = guard.polarity(content)
    theirs = guard.polarity(str(getattr(target, "content", "")))
    if mine != "none" and theirs != "none" and mine != theirs:
        return FactVerdict(
            REL_CONTRADICTS, target_id=verdict.target_id,
            reason="极性相反,按改口处理(模型原判 duplicate)",
        )
    return verdict


async def arbitrate(
    client: Any,
    content: str,
    facts: Iterable[Any],
    *,
    capacity_note: str = "",
) -> FactVerdict:
    """调一次模型判关系。任何异常都降级为 review。"""
    known = list(facts)
    if client is None:
        return FactVerdict(REL_REVIEW, reason="这个会话没有模型客户端")
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_prompt(content, known, capacity_note=capacity_note)},
    ]
    try:
        # ⚠️ 这一次**真的不要思考**(reasoning_effort=""),只要那段 JSON。
        #
        #   2026-10-25 真机:这里原来只有 max_tokens=200,而客户端默认开着 high 思考 ——
        #   思考与正文共用这个额度,于是判决被截在半个字符串上:
        #       {"relationship":"refines","target_id":8,"text":"助手的名字是
        #   parse_verdict 判"输出不是 JSON" -> 每一条候选都落到"待人工放行",
        #   微信端用户确认了也写不进事实(用户实际踩到的那条)。
        #   判关系不需要推理链;关掉思考后,200 token 也够放一条完整的判决。
        #
        # ⚠️ 补记(D170):光把 "" 传给客户端还不够 —— 客户端当时只是"不发字段",
        #   而服务端默认仍开 high;现在空串在 wire 上是 reasoning_effort="none"。
        #   若哪天这里又被改回"不发字段",400 也只是把截断线往后挪,不是修复。
        result = await client.chat(
            messages, temperature=0.0, max_tokens=VERDICT_MAX_TOKENS,
            reasoning_effort="",
        )
    except Exception as exc:  # noqa: BLE001 - 裁决失败绝不能把一轮对话带崩
        return FactVerdict(REL_REVIEW, reason="裁决调用失败:" + type(exc).__name__)

    text = str(getattr(result, "text", "") or "")
    verdict = parse_verdict(text)
    # 截断/中断与"模型判不准"是两回事:前者是我们这边的问题(额度/服务端),
    # 后者是模型没把握。不分开写,下次又会有人去查 prompt(2026-10-25 就查错了方向)。
    finish = str(getattr(result, "finish_reason", "") or "")
    if not verdict.ok and finish and finish != "stop":
        if finish == "length":
            reason = (
                "裁决被生成长度上限截断(finish_reason=length,max_tokens="
                + str(VERDICT_MAX_TOKENS) + "):" + text[:80]
            )
        else:
            reason = "裁决未正常完成(finish_reason=" + finish + "):" + text[:80]
        return FactVerdict(REL_REVIEW, reason=reason)
    if not verdict.ok:
        return verdict
    return _police_polarity(verdict, content, known)


__all__ = [
    "FactVerdict",
    "PROMPT_MARKER",
    "RELATIONSHIPS",
    "REL_CONTRADICTS",
    "REL_DUPLICATE",
    "REL_NEW",
    "REL_REFINES",
    "REL_REVIEW",
    "VERDICT_MAX_TOKENS",
    "arbitrate",
    "build_prompt",
    "parse_verdict",
    "render_existing",
]
