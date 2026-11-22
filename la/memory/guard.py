"""事实准入的**确定性**一半:不调模型就能判死的那些。

为什么要有这一层(而不是"全交给 LLM 判"):
  * LLM 是概率性的 —— 同一句话今天拦、明天放,这种不确定性放在一个"永久生效、
    每轮进上下文"的层上是不能接受的(见 D64 的失效模式);
  * 长度、空值、残句、疑问句这些是**结构问题**,判死它们不需要语义理解。

分工(与 `adjudicator.py` 的一半合起来才是完整准入):
    规则/模型只出**候选** → 本模块**确定性**拦死结构垃圾 → LLM 判关系(新/重复/矛盾/改口)
    → 代码落库并留痕。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .. import config
from .anchors import extract_anchors

#: 疑问句的特征词。候选里出现这些,说明它是**用户在问什么**,不是一条事实 ——
#: 实测:"用户喜欢 吃什么东西""用户喜欢 什么" 都是这样进库的。
INTERROGATIVE_WORDS: tuple[str, ...] = (
    "什么", "怎么", "为什么", "哪", "哪里", "哪个", "多少", "多久", "吗", "呢",
    "是不是", "有没有", "何时", "几个人",
)

#: "关于未来的动作"特征词 + 时间词同时命中 -> 这不是永久事实,是待办/提醒。
#: 实测:"，明天晚上7点钟发信息提醒我" 曾经作为永久事实进库。
FUTURE_ACTIONS: tuple[str, ...] = (
    "提醒我", "提醒一下", "叫我", "别忘", "记得要", "要去", "需要去", "要交", "要发",
    "截止", "之前完成", "到时候",
)
TIME_WORDS: tuple[str, ...] = (
    "明天", "后天", "今天", "今晚", "上午", "下午", "晚上", "早上", "中午",
    "下周", "下个月", "下礼拜", "周一", "周二", "周三", "周四", "周五", "周六", "周日",
    "点", "点钟", "分钟后", "小时后", "天后",
)

#: 句子里出现这些词,说明它描述的是**未来的动作**而不是状态/偏好/身份。
#: 注意:这一条只用来"降级为待审",不用来"拒收" —— 万一判断错了,人还能在页面上放行。
SUBJECT_PREFIXES: tuple[str, ...] = ("用户", "助手", "他的", "她的", "我的")

_PUNCT_RE = re.compile("[" + re.escape(config.FACT_PUNCT_CHARS) + r"\s]+")
_LEADING_PUNCT_RE = re.compile("^[" + re.escape(config.FACT_PUNCT_CHARS) + r"\s]+")
#: 结尾挂在半个字上的特征:半句被截断时,最后一个字往往就是这些虚词。
#: 实测:"用户喜欢 打游戏，以前喜欢画画，喜欢看网络小说，然" —— 抽取正则到 20 字就停,
#:       停在了"然"上(原句是"…然后喜欢滑板")。
_DANGLING_TAILS: tuple[str, ...] = (
    "然", "但", "而", "因", "所", "和", "与", "及", "或", "也", "还", "就", "都", "很",
    "的", "了", "着", "过", "把", "被", "在", "从", "对", "向", "给", "让", "使", "是",
)

#: 极性方向。同一个事实的两种说法若**方向相反**,那是"矛盾"而不是"重复" ——
#: 必须交给 LLM 判关系,绝不能用字面相似度合并(见 D98)。
POLARITY_POSITIVE: tuple[str, ...] = ("喜欢", "爱吃", "偏好", "习惯", "希望", "想要")
POLARITY_NEGATIVE: tuple[str, ...] = (
    "不喜欢", "不吃", "不喝", "不要", "讨厌", "受不了", "忌口", "不接受", "厌恶", "拒绝",
)


@dataclass(frozen=True)
class GuardVerdict:
    """确定性判决。

    ok=True      -> 放行给 LLM 裁决
    needs_review -> 不拒收,但不许自动入库,留给人在记忆页上放行/丢弃
    """

    ok: bool
    reason: str = ""
    needs_review: bool = False
    code: str = "ok"

    @property
    def rejected(self) -> bool:
        return not self.ok and not self.needs_review


def normalize(text: str) -> str:
    """去重指纹的归一化:剥掉**所有**中英标点与空白。

    ⚠️ 这里曾经有一个汉字级别的漏洞:去重用的那个字符类里没有全角逗号
    (U+FF0C)、全角分号(U+FF1B)、全角问号(U+FF1F) —— 而中文里最常用的恰恰是它们。
    后果:"用户喜欢 打游戏，以前喜欢画画，喜欢看网络小说，然" 与
    "用户喜欢打游戏、看网络小说，以前喜欢画画，也喜欢滑板" 的指纹**不相等**,
    于是同一条事实写了两行、两行都每轮进上下文。现在改走 config.FACT_PUNCT_CHARS。
    """
    cleaned = _PUNCT_RE.sub("", str(text or ""))
    return cleaned.removeprefix("用户").casefold()


def polarity(text: str) -> str:
    """这句话的极性:positive / negative / none(不含偏好词)。"""
    body = str(text or "")
    if any(word in body for word in POLARITY_NEGATIVE):
        return "negative"
    if any(word in body for word in POLARITY_POSITIVE):
        return "positive"
    return "none"


def looks_like_question(text: str) -> bool:
    body = str(text or "")
    if "?" in body or "？" in body:
        return True
    if any(word in body for word in INTERROGATIVE_WORDS):
        # "我喜欢吃什么" 是问句;"我对花生过敏" 不是。只按疑问词判,不做句法分析。
        return "喜欢" in body or "想" in body or body.startswith("用户喜欢")
    return False


def looks_like_future_action(text: str) -> bool:
    """是不是"关于未来的一个动作" —— 那该进待办/提醒,不该进永久事实层。"""
    body = str(text or "")
    return any(word in body for word in FUTURE_ACTIONS) and any(
        word in body for word in TIME_WORDS
    )


def check_candidate(content: str) -> GuardVerdict:
    """一条候选的确定性检查。判死的理由必须**说得出是哪一条**。"""
    body = str(content or "").strip()
    if not body:
        return GuardVerdict(False, "内容是空的", code="empty")
    if _LEADING_PUNCT_RE.match(body):
        # "，明天晚上7点钟发信息提醒我" —— 开头就是标点,说明抽取时把主语吃掉了
        return GuardVerdict(False, "开头是标点,像是被截断的半句话", code="fragment")
    if len(body) > config.FACT_MAX_CONTENT_CHARS:
        return GuardVerdict(
            False,
            "超过 " + str(config.FACT_MAX_CONTENT_CHARS) + " 字(实测 "
            + str(len(body)) + " 字)。请拆成几条短事实,不要写成一整段。",
            code="too_long",
        )
    if looks_like_question(body):
        return GuardVerdict(False, "这是一个问句,不是事实", code="question")
    if body[-1] in _DANGLING_TAILS and not body.endswith(("的是", "为了")):
        # "…喜欢看网络小说，然" —— 结尾挂在虚词上,几乎一定是被截断的半句
        return GuardVerdict(
            False, "结尾停在「" + body[-1] + "」上,像是被截断的半句话", code="truncated",
        )
    if re.search(r"[\u4e00-\u9fff]\s+[\u4e00-\u9fff]", body):
        # "用户比较喜欢 吃香菜" —— 汉字之间出现空格,是**模板拼装**留下的痕迹,
        # 真实用户输入不会这么写。它不是错得离谱,但值得人看一眼。
        return GuardVerdict(
            False, "汉字中间有空格,像是模板拼出来的句子", needs_review=True,
            code="template_artifact",
        )
    if looks_like_future_action(body):
        return GuardVerdict(
            False,
            "这是关于未来的一个动作,应该记成待办或提醒,不是永久事实",
            needs_review=True,
            code="future_action",
        )
    # 兜底:一条事实至少要有个"谁/什么"的骨架,不能只有两三个字
    if len(normalize(body)) < 4:
        return GuardVerdict(False, "太短,不成一句话", code="too_short")
    return GuardVerdict(True)


def anchors_of(text: str) -> list[str]:
    """候选的锚点(日期/金额/实体)—— 供 LLM 裁决时对照已有事实。"""
    return extract_anchors(str(text or ""))


__all__ = [
    "GuardVerdict",
    "check_candidate",
    "looks_like_future_action",
    "looks_like_question",
    "normalize",
    "polarity",
    "anchors_of",
]
