"""显式记忆请求的**代码层**识别。

为什么要规则而不是只靠模型:
    "记住 X" 这类**显式**指令不该依赖模型判断 —— 与"删除必须走 confirm"同理,
    属于确定性路由(能力域 §10.7①)。

M1 还没有工具调用(工具层在 M4 落地),这里先把"人明确要求记住"的部分做实:
    * 显式标记(记住/记一下/帮我记/别忘)→ memory_note
    * 身份类陈述("我的英文名是 Kevin""叫我 Kevin")
    * 偏好类陈述("我不吃香菜""我喜欢先列要点")
模型侧的 memory_note 工具将来与它并存:规则兜底显式请求,模型处理隐含请求。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .anchors import extract_anchors

NOTE_MARKERS: tuple[str, ...] = ("记住", "记一下", "帮我记", "帮我记住", "记下来", "别忘", "备忘")

_IDENTITY_LABELS = "英文名|中文名|名字|姓名|昵称|称呼|生日|电话|手机号|邮箱|地址|职业|公司|学校|账号"
_IDENTITY_PATTERN = re.compile(
    r"我(?:的)?(" + _IDENTITY_LABELS + r")(?:是|叫|为)\s*([^\s,。,.;;!?、]{1,30})"
)
_NICKNAME_PATTERN = re.compile(
    # ⚠️ 尾巴那个组必须分两档:单字"吧"可省,双字"就行/就好/即可"不能省 ——
    #    写成 (?:就行|就好|吧|即可|就可以)? 时,正则会给双字词留出"不匹配"的余地,
    #    于是把"就行"当成了名字的一部分(实测抓到过"就行"这个"昵称")。
    r"叫我\s*([A-Za-z][A-Za-z0-9_]{1,19}|[\u4e00-\u9fff]{2,6}?)(?:就行|就好|即可|就可以|吧)?"
)
_PREFERENCE_PATTERN = re.compile(
    r"我(不|很|挺|比较|最)?(喜欢|爱吃|讨厌|不喜欢|习惯|偏好)\s*([^\s,。,.;;!?、]{1,20})"
)
_AVERSION_PATTERN = re.compile(r"我(?:不吃|不喝|不要|受不了|忌口)\s*([^\s,。,.;;!?、]{1,20})")

# "叫我就行" 这类句子会被昵称规则误抓成值 "就行" —— 用拒绝表挡掉
_NICKNAME_REJECT = {"就行", "就好", "即可", "可以", "我吧", "我啊"}


@dataclass
class DetectedNote:
    content: str
    fact_type: str  # identity | preference | decision | other
    keywords: list[str] = field(default_factory=list)
    explicit: bool = False


def _clean_value(value: str) -> str:
    return value.strip().strip("《》“”\"'「」【】,。;:!?、 ")


def _join(verb: str, value: str) -> str:
    """把模板与取值拼起来。**ASCII 取值前面补一个空格**,中文不补。

    这样"用户的英文名是 Kevin"读起来正常,而"用户不接受香菜"不会被准入闸
    当成"汉字中间夹空格"的模板拼装痕迹(那条规则是拦坏数据的,不该被我们自己踩中)。
    """
    if value and value[0].isascii() and not value[0].isspace():
        return verb + " " + value
    return verb + value


#: 疑问句特征词:带这些词的句子在描述"用户在问什么",不是一条事实。
#: 实测:"我喜欢吃什么东西?" 曾经被写成"用户喜欢 吃什么东西"这条永久事实。
_QUESTION_WORDS: tuple[str, ...] = ("什么", "怎么", "为什么", "哪", "多少", "吗", "呢", "是不是")


def detect_notes(text: str) -> list[DetectedNote]:
    """从一句用户输入里识别出应当写入长期记忆的事实(宁少勿滥)。"""
    if not text:
        return []
    # 问句直接不识别 —— 规则不需要理解语义,只要不把一个疑问当成陈述。
    if "?" in text or "？" in text or any(word in text for word in _QUESTION_WORDS):
        return []
    notes: list[DetectedNote] = []
    seen: set[str] = set()

    # ⚠️ "帮我记住:X" 里的 X 只作为 memory_note 的载荷,不再拿去做派生解析 ——
    # 否则同一句话会被写两次(实测:规则与模型工具各写一条,内容还不完全一样)。
    marker_at: int | None = None
    for marker in NOTE_MARKERS:
        index = text.find(marker)
        if index >= 0 and (marker_at is None or index < marker_at):
            marker_at = index
    head = text if marker_at is None else text[:marker_at]

    def add(content: str, fact_type: str, keywords: Iterable[str], explicit: bool) -> None:
        if content in seen:
            return
        seen.add(content)
        notes.append(
            DetectedNote(
                content=content,
                fact_type=fact_type,
                keywords=[k for k in keywords if k],
                explicit=explicit,
            )
        )

    for match in _IDENTITY_PATTERN.finditer(head):
        label = _clean_value(match.group(1))
        value = _clean_value(match.group(2))
        if value:
            add(_join("用户的" + label + "是", value), "identity", [value, label], False)

    for match in _NICKNAME_PATTERN.finditer(head):
        value = _clean_value(match.group(1))
        if value in _NICKNAME_REJECT or len(value) < 2:
            continue
        if value and not any(value in note.content for note in notes):
            add(_join("用户希望被称呼为", value), "identity", [value], False)

    for match in _PREFERENCE_PATTERN.finditer(head):
        degree = match.group(1) or ""
        verb = match.group(2)
        # ⚠️ 2026-10-13 修一个**把话说反**的 bug:
        #    这条正则的组 1 是可选的程度词 (`我(不|很|挺|比较|最)?喜欢`),所以
        #    "我不吃香菜" 里它照样能从"不"字后面匹配到"喜欢"吗 —— 不,它匹配的是
        #    **另一个词**;真正的问题是 `我(?:不吃|不喝…)` 那条 aversion 规则与本条
        #    **各自独立 add()**,于是同一句会同时产出"用户喜欢 香菜"(错)与
        #    "用户不接受 香菜"(对)。实测这条错事实在库里活了整整两天、每轮进上下文。
        #    修法:① 否定词紧跟动词时不生成"喜欢";② 任何偏好句与 aversion 撞车时,
        #    以 aversion 为准(宁可不记,也不能记反)。
        if degree == "不" and verb in ("喜欢", "爱吃", "习惯", "偏好"):
            continue
        # "我最喜欢**的**咖啡是冰美式" -> 把量词性的"的/是"剥掉,否则会写出"用户最喜欢 的咖啡是…"
        target = _clean_value(match.group(3)).lstrip("的").lstrip("是").lstrip("的")
        if target:
            if verb in ("讨厌", "不喜欢"):
                add(_join("用户不喜欢", target), "preference", [target], False)
            else:
                # ⚠️ 不再往动词后面塞空格:拼出来的"用户比较喜欢 吃辣"会被准入闸
                #    当成"模板拼装痕迹"而降级为待审(template_artifact)——
                #    那条规则是为了拦住坏数据,不该被我们自己的模板踩中。
                add(_join("用户" + degree + "喜欢", target), "preference", [target], False)

    for match in _AVERSION_PATTERN.finditer(head):
        target = _clean_value(match.group(1))
        if target:
            add(_join("用户不接受", target), "preference", [target], False)

    if marker_at is not None:
        payload = text
        for marker in NOTE_MARKERS:
            index = payload.find(marker)
            if index >= 0:
                payload = payload[index + len(marker):]
                break
        payload = _clean_value(payload.lstrip(":,:："))
        if payload:
            add(payload, "other", extract_anchors(payload), True)

    return notes
