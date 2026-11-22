"""标准答案:**只能由模型独立解出、老师批改、或家长手填** —— 永远不是孩子的作答。

⭐ 这一条是用户 2026-10-12 明确提出的,而且**在数据里当场验证过**:

  旧提示词要求"answer_text 抄照片上出现的答案或批改",而照片上那行字多半是
  **孩子做错的作答**。实测两条:
    * 错题 #5(锯木头):抄进来的 answer_text 是"孩子第一次算的 65 分钟 + 老师订正
      54 分钟"两段混在一起;
    * 错题 #3(小正方体):抄进来的是"7;订:8" —— 孩子 7 被红笔圈错,老师订正 8。

三条纪律:

  1. **隔离**。解题这一趟的输入里**不出现** `student_answer` —— 大模型会被上下文里的
     数字锚定(你给它看 65,它就爱往 65 附近凑)。这不是洁癖,是实测出来的:
     不看孩子作答时,同一个模型对锯木头题给出 54(与老师订正一致)。
  2. **不硬答**。图形题只给文字时模型会拒答("题干不完整,缺少②号组合体的示意图")——
     这是**正确**行为,要保留:answer_kind="incomplete" + missing_info 逐条列出缺什么。
     带图之后模型反而更敢答了,但答案未必对(实测:孩子 7、老师 8、模型 4)。
  3. **三方对照,不自动采信**。模型 / 老师批改 / 孩子三方过一遍:
     老师与模型一致 → agreed;不一致 → **conflict**(该题不进"标准答案"区);
     只有模型一方 → unverified(照印,但标注"未确认",用户拍板的口径)。
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

#: 三方对照的四种取值(与 app_problems.answer_status 同一口径)
STATUS_AGREED = "agreed"
STATUS_CONFLICT = "conflict"
STATUS_INCOMPLETE = "incomplete"
STATUS_UNVERIFIED = "unverified"
STATUSES = (STATUS_AGREED, STATUS_CONFLICT, STATUS_INCOMPLETE, STATUS_UNVERIFIED)

#: 答案的来源(与 app_problems.answer_source 同一口径)
SOURCE_MODEL = "model"
SOURCE_TEACHER = "teacher"
SOURCE_MANUAL = "manual"
SOURCE_LEGACY = "legacy"

MAX_ANSWER_CHARS = 600
MAX_STEP_CHARS = 300
MAX_STEPS = 8
MAX_MISSING = 6

SOLVE_PROMPT = """你是老师,正在为错题本确定**标准答案**。

⭐ 三条纪律,请严格遵守:
1. 只依据下面给你的题干(以及随附的图形截图)作答。**不要**去猜学生写过什么 ——
   你看不到学生的作答,也不该参考它。
2. 题干或图形**缺了必要信息**时(例如题目说"如下图"却没有图、说"②号组合体"却没给、
   条件被截断),**不要硬答**:answer 留空字符串、answer_kind 填 "incomplete",
   并在 missing_info 里逐条列出缺什么。宁可说"缺",也不要给一个错的答案。
3. 没有唯一解的题(作文、口语、开放性应用题、需要作图才能完成的题):answer_kind 填
   "reference",answer 给参考答案与评分要点,不要假装唯一解。

只输出 JSON,不要解释、不要代码块标记。格式:
{"answer":"","steps":["",""],"answer_kind":"unique","missing_info":[""],"confidence":0.9}

其中 answer_kind 只能取:unique(唯一解)| reference(参考答案)| incomplete(信息不足)。
steps 是给孩子看的**关键步骤**,最多 6 步,每步一句话。"""


@dataclass
class Solution:
    """一次独立解题的结果。**失败/拒答也是一种结果**,用 ok / answer_kind 判断。"""

    answer: str = ""
    steps: list[str] = field(default_factory=list)
    answer_kind: str = ""
    missing_info: list[str] = field(default_factory=list)
    confidence: float = 0.0
    raw: str = ""
    model: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.answer) and not self.error

    @property
    def incomplete(self) -> bool:
        return self.answer_kind == "incomplete" or bool(self.missing_info)

    def as_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "steps": list(self.steps),
            "answer_kind": self.answer_kind,
            "missing_info": list(self.missing_info),
            "confidence": self.confidence,
            "model": self.model,
            "error": self.error,
        }


def _text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(item) for item in value)
    return " ".join(str(value).split())[:limit]


def _items(value: Any, *, limit: int, item_chars: int) -> list[str]:
    if isinstance(value, str):
        raw: list[Any] = [line for line in value.replace(";", "\n").splitlines()]
    elif isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        raw = []
    kept: list[str] = []
    for item in raw:
        text = _text(item, item_chars).strip(" 　-·*0123456789.、")
        if text and text not in kept:
            kept.append(text)
    return kept[:limit]


def parse_solution(text: str) -> Solution:
    """把模型的 JSON 回复收敛成一个 Solution。解析不了就 error,不抛。"""
    from ...llm.vision import extract_json

    loaded = extract_json(text or "")
    if not isinstance(loaded, dict):
        return Solution(raw=str(text or "")[:2000], error="没解析出 JSON(可能被截断或是思考模式的尾巴)")
    kind = _text(loaded.get("answer_kind") or loaded.get("kind"), 20).lower()
    if kind not in ("unique", "reference", "incomplete"):
        kind = "reference" if _text(loaded.get("answer"), MAX_ANSWER_CHARS) else "incomplete"
    try:
        score = float(loaded.get("confidence"))
    except (TypeError, ValueError):
        score = 0.0
    return Solution(
        answer=_text(loaded.get("answer"), MAX_ANSWER_CHARS),
        steps=_items(loaded.get("steps"), limit=MAX_STEPS, item_chars=MAX_STEP_CHARS),
        answer_kind=kind,
        missing_info=_items(loaded.get("missing_info"), limit=MAX_MISSING, item_chars=120),
        confidence=min(1.0, max(0.0, score)),
        raw=str(text or "")[:2000],
    )


async def solve_problem(
    client: Any,
    *,
    stem: str,
    image: bytes | None = None,
    media_type: str = "",
    figure_note: str = "",
    model: str | None = None,
) -> Solution:
    """独立解一道题。⚠️ 入参里**没有** student_answer —— 见模块开头的纪律一。"""
    body = (stem or "").strip()
    if not body and not image:
        return Solution(error="既没有题干也没有图形,解不了")
    prompt = SOLVE_PROMPT + "\n\n题干:\n" + (body or "(题干没抄到,只能看图)")
    if figure_note:
        prompt += "\n\n图形说明:" + figure_note
    messages: list[dict[str, Any]] = []
    if image:
        from ...llm.vision import build_messages

        messages.extend(build_messages(image, media_type or "image/png", prompt=prompt))
    else:
        messages.append({"role": "user", "content": prompt})
    try:
        result = await client.chat(messages, model=model)
    except Exception as exc:  # noqa: BLE001 - 解不出来不该让整条链路断掉
        return Solution(error=type(exc).__name__ + ": " + str(exc)[:160])
    outcome = parse_solution(str(getattr(result, "text", "") or ""))
    outcome.model = str(getattr(result, "model", "") or "")
    if not outcome.answer and not outcome.error and not outcome.missing_info:
        outcome.error = "模型没有给出答案"
    return outcome


def solve_problem_sync(client: Any, **kwargs: Any) -> Solution:
    """同步入口(Web 端表单在线程池里跑,那里没有事件循环)。"""
    return asyncio.run(solve_problem(client, **kwargs))


# -- 三方对照(纯函数,可单独测)----------------------------------------------
_PUNCT = re.compile(r'''[\s,，。;；:：、!！?？"'“”‘’()（）\[\]【】=+\-*/×÷<>《》]''')
_NOISE = ("答案", "答", "解", "订正", "订", "批改", "所以", "因此", "是", "为", "约", "大约",
          "分钟", "分", "元", "种", "个", "段", "厘米", "米", "千克", "克", "平方", "立方")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def fingerprint(text: str) -> str:
    """把一句话压成可比较的骨架:去标点、去单位、去口头语。"""
    body = _PUNCT.sub("", str(text or ""))
    for word in _NOISE:
        body = body.replace(word, "")
    return body.strip()


def numbers(text: str) -> list[str]:
    """抽出所有数字(用于"54 分钟" vs "54" 这类同一答案的判定)。"""
    return _NUMBER.findall(str(text or ""))


def same_answer(left: str, right: str) -> bool:
    """两个答案是不是同一个。⚠️ 宁可判"不一样"(conflict 让人核对),也不许把错的判成一致的。

    ⚠️ 结论词是**一票否决**:实测踩过 —— "50 分钟能锯完" 与 "50 分钟不能锯完" 数字完全相同,
    只按数字比会把**完全相反的两个答案**判成一致。那种错比"多标一个待确认"严重得多。
    """
    a, b = fingerprint(left), fingerprint(right)
    if not a or not b:
        return False
    if conclusion_conflict(str(left), str(right)):
        return False
    if a == b or a in b or b in a:
        return True
    na, nb = numbers(left), numbers(right)
    return bool(na) and na == nb


#: 结论词对:"不能/能"这类。⚠️ 只有"一边只说了否定、另一边只说了肯定"才算冲突 ——
#: 两边都写了"50 不能、60 能"时,谁也没跟谁矛盾。
_CONCLUSION_PAIRS = (
    ("不能", "能"), ("不是", "是"), ("不对", "对"), ("不可以", "可以"),
    ("不可以", "可"), ("无法", "可以"), ("错误", "正确"),
)


def conclusion_conflict(left: str, right: str) -> bool:
    for negative, positive in _CONCLUSION_PAIRS:
        a_neg = negative in left and positive not in left.replace(negative, "")
        b_neg = negative in right and positive not in right.replace(negative, "")
        a_pos = positive in left and negative not in left
        b_pos = positive in right and negative not in right
        if (a_neg and b_pos) or (b_neg and a_pos):
            return True
    return False


def compatible(left: str, right: str) -> bool:
    """措辞不同但**不矛盾**:一边的数字是另一边的子集,且结论方向一致。

    ⭐ 为什么需要它(实测):锯木头那题老师写的是
    "8.2×4+5.3×4=54(分) 54>50 所以50分不能,60分能",模型写的是
    "50分钟不能锯完;60分钟能锯完。" —— 数字对得上、结论一致,只是**话不一样**。
    判成 conflict 会让一道**答对了**的题被拦下不让当标准答案,那比不判还糟。
    """
    a, b = fingerprint(left), fingerprint(right)
    if not a or not b:
        return False
    na, nb = numbers(left), numbers(right)
    if not na or not nb:
        return False
    if not (set(na) <= set(nb) or set(nb) <= set(na)):
        return False
    return not conclusion_conflict(str(left), str(right))


#: 老师批改的"记号"本身不是答案。实测踩过:一道题的 teacher_answer 抄成了"对号",
#: 于是三方对照里出现"老师批改「对号」"这种没法比的东西。
_MARK_POSITIVE = ("对号", "对勾", "✓", "√", "勾", "正确", "全对")
_MARK_NEGATIVE = ("错号", "错", "×", "✗", "✘", "叉", "不对")
#: ⚠️ "订正"这类**只有动作没有内容**的批注也算记号(实测:照片上老师写了"订正",
#: 被当成老师给的答案抄了进来)。`is_mark_only` 是"删掉记号之后什么都不剩"才算,
#: 所以 "订正:8" 仍然是一个**真答案**。
_MARK_NEUTRAL = ("订正", "改正", "重做", "阅", "批改", "已批改", "已阅", "good", "bad")
_MARK_ONLY = _MARK_POSITIVE + _MARK_NEGATIVE + _MARK_NEUTRAL


def is_mark_only(text: str) -> bool:
    """这句话是不是只有一个批改记号(没有任何答案内容)。"""
    body = _PUNCT.sub("", str(text or "")).strip()
    if not body:
        return False
    if body in _MARK_ONLY:
        return True
    # "对号(√)" 这种也认:去掉记号之后什么都不剩
    for mark in _MARK_ONLY:
        body = body.replace(mark, "")
    return not body


def mark_is_positive(text: str) -> bool:
    body = str(text or "")
    return any(mark in body for mark in _MARK_POSITIVE)


# -- 纯算式的本地验算 ---------------------------------------------------------
# ⭐ 为什么要有这一段:
#    大模型做算术不可靠是常识,而错题本里**大量**是纯算式题(实测那道是
#    "26×7÷(15-13)=?" —— 注意是**除号**,不是减号)。能不能算对,不该靠模型的运气。
#    所以纯算式一律本地算一遍:对得上就用本地值(并注明"与模型一致"),对不上就
#    **以本地为准**并把模型给的那个也写进备注 —— 人一眼能看出这里发生过分歧。
_ARITHMETIC_KEEP = re.compile(r"[0-9+\-*/×÷().．。 ]")
_ARITHMETIC_OK = re.compile(r"^[0-9+\-*/×÷(). ]+$")


def local_arithmetic(stem: str) -> tuple[str, float] | None:
    """题干如果是**纯算式**,本地算出结果。返回 (算式, 值);不是纯算式就返回 None。

    ⚠️ 只用 ast 走白名单(数字与四则运算),**绝不 eval 字符串** —— 题干来自照片,
    属于外部输入。除零、括号不配对等情况一律返回 None(交给模型,不硬算)。
    """
    import ast
    from fractions import Fraction

    text = str(stem or "").strip()
    if not text:
        return None
    body = text.replace("×", "*").replace("÷", "/").replace("．", ".").replace("。", ".")
    body = re.sub(r"[=＝]\s*[?？(]?\s*[\)]?\s*$", "", body)      # 去掉结尾的 "=?" / "=()"
    body = body.replace("=", "")
    body = "".join(_ARITHMETIC_KEEP.findall(body)).strip()
    if not _ARITHMETIC_OK.match(body) or not re.search(r"[+\-*/]", body):
        return None
    if not re.search(r"\d", body):
        return None
    try:
        tree = ast.parse(body, mode="eval")
    except SyntaxError:
        return None

    def walk(node: ast.AST) -> Fraction:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return Fraction(str(node.value))
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult,
                                                               ast.Div, ast.Pow)):
            left, right = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            return left ** int(right)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -walk(node.operand)
        raise ValueError("不支持的算式")

    try:
        value = walk(tree)
    except Exception:  # noqa: BLE001 - 算不出来就交给模型
        return None
    # ⚠️ **只认整数结果**。用 Fraction 精确算,算不尽就交回去 ——
    #    实测踩过:"3/4 + 1/6 = ?"(异分母分数加减)会被算成 0.9167,而正确答案是 11/12。
    #    分数与小数是小学的重点题型,本地验算**没有资格**替它给答案。
    if value.denominator != 1:
        return None
    return body.replace("*", "×").replace("/", "÷"), float(value)


def format_number(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return ("%.4f" % value).rstrip("0").rstrip(".")


def contains_number(text: str, value: float) -> bool:
    return format_number(value) in numbers(text)


def compare_three_way(
    *,
    model_answer: str,
    teacher_answer: str = "",
    student_answer: str = "",
    answer_kind: str = "",
    missing_info: Sequence[str] = (),
) -> tuple[str, str]:
    """(answer_status, 给人看的一句话)。**只摆事实,不下结论。**"""
    missing = [str(item) for item in (missing_info or []) if str(item).strip()]
    if answer_kind == "incomplete" or (not str(model_answer or "").strip()):
        note = "题干或图形不全,模型给不出可靠答案"
        if missing:
            note += ":缺 " + "、".join(missing[:3])
        elif str(model_answer or "").strip() == "":
            note = "模型没有给出答案(可能题干抄得不全,建议补拍)"
        return STATUS_INCOMPLETE, note + "。"
    teacher = str(teacher_answer or "").strip()
    student = str(student_answer or "").strip()
    mark = ""
    if teacher and is_mark_only(teacher):
        # 老师只打了个记号(实测:"对号"被当成老师给的答案抄了进来)。
        # ⭐ 打对号 = **孩子写的那个是对的** —— 于是孩子那一票可以当参考用,
        #    这是老数据里唯一能从照片上救回来的"标准答案"。
        mark = teacher
        teacher = student if (mark_is_positive(teacher) and student) else ""
    if teacher:
        if same_answer(teacher, model_answer):
            return STATUS_AGREED, "模型独立解出「" + _text(model_answer, 60) + "」,与老师批改一致。"
        if compatible(teacher, model_answer):
            return STATUS_AGREED, ("模型独立解出「" + _text(model_answer, 50) + "」,与老师批改的"
                                   "「" + _text(teacher, 50) + "」**数字与结论都对得上**(措辞不同)。")
        note = ("三方不一致:老师批改「" + _text(teacher, 40) + "」,模型解出「"
                + _text(model_answer, 40) + "」")
        if student:
            note += ",孩子写的是「" + _text(student, 40) + "」"
        return STATUS_CONFLICT, note + " —— 先核对,再当标准答案用。"
    note = "模型独立解出「" + _text(model_answer, 60) + "」(照片上没有老师批改可比对,未经确认)。"
    if mark:
        note += " 照片上老师的批改是「" + _text(mark, 12) + "」。"
    if student and not same_answer(student, model_answer):
        note += " 孩子原来的作答是「" + _text(student, 40) + "」。"
    return STATUS_UNVERIFIED, note


__all__ = [
    "SOLVE_PROMPT",
    "SOURCE_LEGACY",
    "SOURCE_MANUAL",
    "SOURCE_MODEL",
    "SOURCE_TEACHER",
    "STATUSES",
    "STATUS_AGREED",
    "STATUS_CONFLICT",
    "STATUS_INCOMPLETE",
    "STATUS_UNVERIFIED",
    "Solution",
    "compare_three_way",
    "fingerprint",
    "numbers",
    "parse_solution",
    "same_answer",
    "solve_problem",
    "solve_problem_sync",
]
