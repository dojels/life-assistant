"""错题的**声音素材**:录入时生成一次,绑定在题上,之后每次复习都用同一段。

⭐ 为什么是"绑定"而不是"每次出卷现做"(用户 2026-10-13 拍板):

  1. **不稳定**:同一道听力题,今天念的和下周念的**不是同一段声音** —— 音色、语速、
     停顿都可能变。孩子反复听的是"每次都不一样的东西",而家长以为他听的是同一道题。
  2. **白花 token 与语音费**:错题是按记忆曲线**反复**学的。每复习一次,都要让模型重新
     判断"这题要不要出声、念什么",再合成一遍 —— 拿 token 和真金白银换同一段声音。
  3. **离线也能出卷**:绑定之后,出卷只读库与产物目录。模型欠费、断网、没配 key,
     卷面照样有声音(与"领域数据的读取路径不含 LLM"是同一条纪律)。

⚠️ 白名单:只认下面这几种 kind。认不出来的**一律不猜、不发声** —— 听写念错一个词,
   孩子会照着错的写,那比"没有音频"糟得多(与图形"宁可不画也不画错"同一条规矩)。

⚠️ 与 `tts_audio` 工具(la/domain/tools.py)的分工:
   那个是**临时用**的(模型当场要一段声音贴进网页);这里是**错题自己的素材**,
   按题存、按题取。两者共用同一个 TtsEngine 与同一份内容缓存,所以同一段文字
   在哪边先做出来,另一边都不会再花钱。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from .tts import DictationAudio, TtsEngine, parse_words

#: 听力:放一段话/句子给孩子听(整段朗读)
KIND_LISTENING = "listening"
#: 听写:报词语/单词(读一遍 → 停 N 秒 → 再读一遍)
KIND_DICTATION = "dictation"
#: 跟读:把**一句话/一个短语整体**读给孩子听(读一遍 → 留白等他跟着读 → 再读一遍)
#: ⚠️ 2026-11-09 用户报的 bug:"今天录的 take care of,我要录的是短语跟读,智能体只能录成
#:    听写,并且把三个词分开读" —— 短语在听写口径下会被拆成词表,一声一声报,短语就不成短语了。
KIND_FOLLOW = "follow"
#: 明确表示"这道题不需要出声"
KIND_NONE = "none"

KINDS = (KIND_LISTENING, KIND_DICTATION, KIND_FOLLOW)

#: 模型自由发挥的写法 → 白名单。⭐ 只收**语义词**,不做模糊匹配:
#: 认不出来就当"没有声音",而不是随便挑一个。
ALIASES = {
    "listening": KIND_LISTENING,
    "listen": KIND_LISTENING,
    "听力": KIND_LISTENING,
    "听录音": KIND_LISTENING,
    "听音频": KIND_LISTENING,
    "朗读": KIND_LISTENING,
    "passage": KIND_LISTENING,
    "reading": KIND_LISTENING,
    "read_aloud": KIND_LISTENING,
    "follow": KIND_FOLLOW,
    "repeat": KIND_FOLLOW,
    "repeat_after_me": KIND_FOLLOW,
    "跟读": KIND_FOLLOW,
    "跟读音频": KIND_FOLLOW,
    "短语跟读": KIND_FOLLOW,
    "dictation": KIND_DICTATION,
    "dictate": KIND_DICTATION,
    "spell": KIND_DICTATION,
    "听写": KIND_DICTATION,
    "单词": KIND_DICTATION,
    "词语": KIND_DICTATION,
    "默写": KIND_DICTATION,
    "word": KIND_DICTATION,
    "words": KIND_DICTATION,
    "none": KIND_NONE,
    "no": KIND_NONE,
    "无": KIND_NONE,
    "不需要": KIND_NONE,
    "": KIND_NONE,
}

#: 一道题最多念多少字。⚠️ 超了**不截断**(截一半的听力材料是错的),如实说不做。
MAX_CHARS = 400
#: 听写最多几个词(再多就不是"一道题"了,该用听写页)
MAX_WORDS = 30
#: 默认节奏(秒)。与 tts_audio 工具的默认值同一口径,不在这里另立一套。
DICTATION_REPEAT = 2
DICTATION_GAP_S = 4.0
READING_REPEAT = 1
READING_GAP_S = 3.0
#: 跟读:整句念一遍 → 停 3 秒(孩子跟) → 再念一遍。停得比听写短:跟读是"接着读",不是"想一想"。
FOLLOW_REPEAT = 2
FOLLOW_GAP_S = 3.0
#: 跟读**词表**时每条之间的留白(一个词不用等 3 秒)
FOLLOW_ITEM_GAP_S = 2.5

# -- "题干自己会说话":不用模型也能判"要不要出声、念什么" ---------------------
#: 题干里出现这些字 = 这道题**要用耳朵做**(听写/默写/听音)
DICTATION_HINTS = ("听写", "默写", "报听写", "听音", "听单词", "听词语", "没听出来", "没听清")
#: 出现这些 = 是**放一段材料**(整段朗读),不是报词
#: ⚠️ 2026-10-15 真机补上「朗读」:家长说「这5个单词录成跟读的错题」,
#:    题面写着「跟读单词(星期一到星期五):Monday …」—— 原来的线索里一个词都不命中,
#:    于是**一声不出**(题干里明明写着要念什么)。
LISTENING_HINTS = ("听录音", "听短文", "听对话", "听句子", "听问题", "听歌", "选出你听到", "听到的",
                   "朗读")
#: 出现这些 = 孩子要**跟着念**(先听标准音,再自己读)。
#: ⚠️ 2026-11-09 从"听力"里拆出来:跟读的**短语**在听写口径下会被拆成词表
#:    (「take care of」→ take、care、of 一声一声报),那已经不是短语了。
FOLLOW_HINTS = ("跟读", "照着读", "跟读音频")
#: 出现这些 = **认读类**:孩子先自己读,再听标准音对照(2026-10-24 用户要求)。
#: ⚠️ 与"跟读"的区别:跟读是**先听后跟**,认读是**先读后听** —— 节奏不一样:
#:    认读只念一遍(留白给他先读),跟读要念两遍(第二遍让他跟)。
READ_ALOUD_HINTS = ("认读", "读一读", "读单词", "读句子", "读课文", "拼读", "会读", "发音", "读音")
#: 一个"词"最多几个字 —— 再长就不像词表,像句子
MAX_WORD_CHARS = 16
#: 句子标点:出现它就说明这截是**句子**,不是词表(英文句号也算 —— 实测踩过)
_SENTENCE_MARKS = "。.!?！?;；…"


def whitelist_name(value: Any) -> str:
    """把一个 kind 收敛到白名单。认不出来返回空串(**不猜**)。"""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text in KINDS:
        return text
    return ALIASES.get(text, "")


def normalize_text(value: Any) -> str:
    """要念的原文:去多余空白、统一换行。空 → 空串。"""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(line.split()) for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


@dataclass
class AudioPlan:
    """一段**准备要说的话**。它只描述"念什么、怎么念",不含音频字节。"""

    kind: str = ""
    text: str = ""
    #: 用途(read_aloud = 先自己读、再听标准音;follow = 跟读,先听再跟)。只影响文案与节奏。
    purpose: str = ""
    #: 跟读**词表**时拆好的每一条(空 = 这是一整句话,整段念)。
    #: ⚠️ 短语不拆:短语拆开念就不再是短语了(2026-11-09 take care of)。
    items: list[str] = field(default_factory=list)
    repeat: int = 1
    gap_s: float = READING_GAP_S
    title: str = ""
    #: 非空 = 这一段**没做成**(太长 / 内容没拿到)。调用方要把它说出来,不许静默。
    error: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.text) and not self.error

    @property
    def is_dictation(self) -> bool:
        return self.kind == KIND_DICTATION

    @property
    def is_follow(self) -> bool:
        return self.kind == KIND_FOLLOW

    @property
    def words(self) -> list[str]:
        return parse_words(self.text) if self.is_dictation else []

    @property
    def label(self) -> str:
        if self.purpose == "read_aloud":
            return "认读音频"
        if self.is_follow:
            return "跟读音频"
        return "听写音频" if self.is_dictation else "听力音频"

    def describe(self) -> str:
        """一句话说清这段要念什么(回执/命令行用)。"""
        if self.error:
            return self.label + "没做成:" + self.error
        if self.is_dictation:
            words = self.words
            head = "、".join(words[:8]) + ("…" if len(words) > 8 else "")
            return self.label + "(" + str(len(words)) + " 个词:" + head + ")"
        if self.is_follow:
            if self.items:
                head = "、".join(self.items[:6]) + ("…" if len(self.items) > 6 else "")
                return self.label + "(" + str(len(self.items)) + " 条:" + head + ")"
            body = self.text.replace("\n", " ")
            return self.label + "(" + (body[:24] + "…" if len(body) > 24 else body) + ")"
        body = self.text.replace("\n", " ")
        if len(body) > 24:
            body = body[:24] + "…"
        return self.label + "(" + body + ")"


def plan_audio(
    kind: Any, text: Any, *, title: str = "", subject: str = "", purpose: str = ""
) -> AudioPlan | None:
    """把"模型说这题要出声 + 要念的原文"变成一份可执行的朗读计划。

    返回 None = **这题不需要音频**(或者是认不出来的 kind)—— 调用方什么都不做。
    返回的 plan 可能 `not ok`(带 error),那是"该有声音但没做成",必须说出来。
    """
    name = whitelist_name(kind)
    if not name or name == KIND_NONE:
        return None
    body = normalize_text(text)
    plan = AudioPlan(kind=name, text=body, title=title, purpose=str(purpose or ""))
    if not body:
        plan.error = "没拿到要念的原文(照片上找不到,我也不会自己编)"
        return plan
    if len(body) > MAX_CHARS:
        plan.error = "要念的内容有 " + str(len(body)) + " 个字,太长了(自动朗读只做 " + str(MAX_CHARS) + " 字以内)"
        return plan
    if plan.is_dictation:
        words = plan.words
        if not words:
            plan.error = "听写词表是空的"
            return plan
        if len(words) > MAX_WORDS:
            plan.error = "听写词表有 " + str(len(words)) + " 个词,太多了(最多 " + str(MAX_WORDS) + " 个)"
            return plan
        plan.repeat = DICTATION_REPEAT
        plan.gap_s = DICTATION_GAP_S
        plan.title = title or (subject + "听写" if subject else "听写")
        # 词表按**词**拼接:模型常把一句话塞进 text,这里统一成"、"-分隔的口径
        plan.text = "、".join(words)
    elif plan.is_follow:
        # ⭐ 跟读分两种,判据只有一个:**它写成了列表吗**?
        #    * 有分隔符(、,，;；/)= 一串词/短语 → 一条一条念,每条念一遍、留白；
        #    * 没有分隔符 = **一句话**(「take care of」「Let your partner guess her name.」)
        #      → 整句连读两遍,中间留白等他跟。**绝不**按空格拆词:短语拆开就不是短语了。
        plan.title = title or (subject + "跟读" if subject else "跟读")
        if not plan.purpose:
            plan.purpose = "follow"
        items = explicit_list_items(plan.text)
        if items:
            plan.items = items
            plan.text = "、".join(items)
            plan.repeat = 1
            plan.gap_s = FOLLOW_ITEM_GAP_S
        else:
            plan.repeat = FOLLOW_REPEAT
            plan.gap_s = FOLLOW_GAP_S
            # "What's he like? / What does he like?" —— 斜杠是**写法**,不是要念的字:
            # 照念出来是"slash"(2026-11-09 重做 #17 时发现)。短语跟读里把它变成一个停顿。
            plan.text = pause_for_slash(plan.text)
    else:
        plan.repeat = READING_REPEAT
        plan.gap_s = READING_GAP_S
        if plan.purpose == "read_aloud":
            # 先让他读,再放一遍标准音:只念一遍,中间留一点空
            plan.repeat = 1
            plan.gap_s = 2.0
    return plan


def looks_like_word(name: Any) -> bool:
    """像不像"一个词":不太长、不带句子标点、至少有一个能念的字符。"""
    text = str(name or "").strip()
    if not text or len(text) > MAX_WORD_CHARS:
        return False
    if not any(char.isalnum() for char in text):
        # ⚠️ 只剩标点的不是词:" ）" / "——" 念出来是一声噪音,还会把真正的词表挤掉。
        #    实测 #13「英语单词不会读:answer(读音 /ˈɑːnsə(r)/)」被音标规则洗完以后
        #    拆出过一个孤零零的 ")",照单全念就是"右括号"。
        return False
    return not any(mark in text for mark in _SENTENCE_MARKS)


#: 括号里的**备注**不是要写的内容:"一亩地(孩子听写时没听出来)" → "一亩地"
#: (实测:不剥掉的话,整串会被当成"一个很长的词"念出来)
_ANNOTATION = re.compile(r"[（(\[【][^）)\]】]*[）)\]】]")

#: 音标标注:"Monday /ˈmʌndeɪ/" → "Monday"。
#: ⭐ 音标**不是要念的英文** —— 交给语音引擎念 /ˈmʌndeɪ/ 只会读出乱码;
#: 而留着它,整个词条就不再像"一个词"(长度超限),题干里的词表就读不出来了。
#: 只在斜杠/方括号里**确实带国际音标字符**时才算音标:普通斜杠(1/2、and/or)不许误伤。
_PHONETIC = re.compile(r"\s*[/\[［]\s*[^/\]］]*[ˈˌːəɪʊɛɔæʌθðʃʒŋɜɑɒ][^/\]］]*[/\]］]")
#: "第几页/第几段/第几课"这类**定位词**:出现它就不该被当成"要写的一个词"。
#: ⚠️ 实测:题干"跟读课文:P18 第3段"被拆成词表后念的是"P18、第3段" —— 一串编号。
_PLACE_MARK = re.compile(
    r"第\s*[0-9一二三四五六七八九十百]+\s*(页|段|课|单元|部分)"
    r"|P\.?\s*[0-9]+|Page\s*[0-9]+|Unit\s*[0-9]+|Lesson\s*[0-9]+",
    re.IGNORECASE,
)
#: 整截只有定位、没有内容:P18 第3段 / 第三单元 / Lesson 5
#: ⚠️ "P18 第3段"是**一个**定位(空格里那个,拆开看每一半都不完整)—— 所以两半都要认。
_PLACE_REF = re.compile(
    r"^(P\.?[0-9]+|Page[0-9]+|Unit[0-9]+|Lesson[0-9]+)?"
    r"(第[0-9一二三四五六七八九十百]+(页|段|课|单元|题|部分))?$",
    re.IGNORECASE,
)


def clean_material(text: Any) -> str:
    """把"要念的原文"洗一遍:加空格的线索("跟 读")归一、剥掉音标标注。

    ⚠️ 只洗**格式**,不改字:音标剥掉是因为念不出、且会把词表撑成"长词条"。
    """
    body = normalize_text(text)
    if not body:
        return ""
    body = _PHONETIC.sub("", body)
    # 线索词里可能有空格("跟 读单词"),按字比对才认得出来
    return " ".join(body.split())


def looks_like_place_reference(text: Any) -> bool:
    """这截是不是只有"第几页/第几段"的定位,没有真正的内容?"""
    body = clean_material(text).replace(" ", "")
    if not body:
        return True
    match = _PLACE_REF.match(body)
    # ⚠️ 空模式("")也会 match —— 必须确认真匹配上了**一段定位**,而不是"什么都没匹配到"
    return bool(match and match.group(0))


def has_place_marker(text: Any) -> bool:
    """这截里有没有"第 3 段 / P18 / Unit 5"这种**定位词**?

    ⚠️ 有定位词的截**不许当词表念** —— "跟读课文:P18 第3段" 念出来就是"P18、第3段"。
    ⚠️ 比对前先把空格去掉:"P18 第3段"是**一个定位**,不是两个词。
    """
    return bool(_PLACE_MARK.search(clean_material(text).replace(" ", "")))


def strip_notes(text: Any) -> str:
    """去掉括号备注,只留正文。"""
    return " ".join(_ANNOTATION.sub(" ", str(text or "")).split())


#: 中文字符(判"这个词条是中文还是英文")
_CJK = re.compile(r"[\u3400-\u9fff]")
#: 科目名 → 在这个科目里"该念的词"长什么样。
#: ⭐ 只用于**在两种候选之间挑一个**(挑不出就退回原来的口径),不用于删掉材料。
_SUBJECT_LANGUAGE = {
    "英语": "latin", "english": "latin",
    "语文": "cjk", "chinese": "cjk",
}


def language_of(subject: Any) -> str:
    """这门课的词表该是什么文字:"latin" / "cjk" / ""(不判)。"""
    name = str(subject or "").strip().lower()
    if not name:
        return ""
    for key, want in _SUBJECT_LANGUAGE.items():
        if key in name:
            return want
    return ""


def entry_is_clean(text: Any, subject: Any) -> bool:
    """这一条是**要念的那个词**本身,不是附带的释义/说明。

    ⚠️ 实测的反例(来自库里 #9 的答案):"Monday 星期一" / "星期三(d 不发音)" ——
    照单全念,孩子听到的是"星期一""不发音"这些**说明文字**,而不是要跟读的英文。
    """
    want = language_of(subject)
    if not want:
        return True
    has_cjk = bool(_CJK.search(str(text or "")))
    return has_cjk if want == "cjk" else not has_cjk


def words_in_language(raw: Any, subject: Any) -> list[str]:
    """拆词表,**只要同语种的那几条**;挑不出来返回空(调用方会退回原来的口径)。"""
    words = words_from_list(raw)
    if not words:
        return []
    kept = [word for word in words if entry_is_clean(word, subject)]
    return kept if kept else []


def words_from_list(raw: Any) -> list[str]:
    """从一段文字里拆出**词表**。像句子(带句号/问号)就返回空 —— 那不是词表。"""
    items = parse_words(raw)
    if not items or len(items) > MAX_WORDS:
        return []
    words: list[str] = []
    for item in items:
        cleaned = strip_notes(item).strip()
        if not looks_like_word(cleaned):
            return []
        words.append(cleaned)
    return words


#: 跟读/朗读列表里**一条**最多多长:再长就是一段话,不该按条念。
MAX_ITEM_CHARS = 40
#: 写成列表的迹象(有它才拆条;没有就当成一句话)
_LIST_MARKS = "、，,;；\n/／"
_LIST_SPLIT = re.compile(r"[、，,;；\n/／]+")


def explicit_list_items(text: Any) -> list[str]:
    """**只在原文真的写成了列表时**拆条(跟读用)。

    ⚠️ 与 parse_words 的分工:那个连空格也拆(听写就是要一个词一个词报);
       这个**不拆空格** —— 「take care of」是一句话,按空格拆开就成了三个词,
       孩子听到的是 take、care、of 三声,短语就不成短语了(2026-11-09 用户报的原话)。
    """
    body = str(text or "")
    if not any(mark in body for mark in _LIST_MARKS):
        return []
    items: list[str] = []
    for part in _LIST_SPLIT.split(body):
        item = strip_notes(part).strip()
        if not item:
            continue
        if len(item) > MAX_ITEM_CHARS:
            return []
        if any(mark in item for mark in _SENTENCE_MARKS):
            return []
        if not any(char.isalnum() for char in item):
            return []
        if item not in items:
            items.append(item)
    return items if len(items) > 1 else []


#: 斜杠(中英文两种)。它在"要念的原文"里是**分隔的写法**,不是读音。
_SLASH = re.compile(r"\s*[/／]\s*")


def pause_for_slash(text: Any) -> str:
    """"A / B" → "A，B"(念出来不该是"slash")。"""
    body = _SLASH.sub("，", str(text or ""))
    return body.strip("，,;； ")


def tail_after_colon(text: Any) -> str:
    """取最后一个冒号**之后**的那截:"听写词语:一亩地" → "一亩地"。

    ⚠️ **没有冒号就返回空串**,绝不许把整句当内容 —— 实测:"默写古诗《静夜思》"
    会被当成"一个词"念出来,而那是**指令**,不是要写的东西。
    """
    body = str(text or "")
    for mark in (":", "："):
        if mark in body:
            return body.rsplit(mark, 1)[-1].strip()
    return ""


#: 这些字出现在候选材料里 = 它是**一句要求**,不是要念的内容。
#: ⚠️ 2026-11-09 #25 题干尾巴:「请连起来读整个短语「take care of」,不要拆成单个单词读。」
#:    照单全念,孩子听到的是一整句要求,而不是那个短语。
_INSTRUCTION_HINTS = (
    "跟读", "朗读", "听写", "默写", "认读", "拼读", "读一读", "请", "不要", "连读",
    "每个词", "整个短语", "录音", "发音", "读音", "读准", "先读", "再读", "读一遍",
)


def _bare_stem(stem: Any) -> str:
    """题干里去掉"跟读"这类线索词之后剩下的原文(只在题干没有冒号时才敢用)。"""
    body = clean_material(stem)
    if any(mark in body for mark in (":", "：")):
        return ""
    for hint in FOLLOW_HINTS:
        body = body.replace(hint, " ")
    return " ".join(body.split())


def follow_material(text: Any) -> str:
    """一截候选材料 → 要跟读的那句话;它其实是**指令**就返回空串。"""
    body = strip_notes(clean_material(text)).strip()
    if len(body) < 2:
        return ""
    if any(hint in body for hint in _INSTRUCTION_HINTS):
        return ""
    if looks_like_place_reference(body) or has_place_marker(body):
        return ""
    return body


def follow_plan(candidates: Sequence[Any], *, subject: str = "") -> AudioPlan | None:
    """从候选里挑出**要跟读的那一句**,做成计划;都不可用就返回 None。"""
    for candidate in candidates:
        body = follow_material(candidate)
        if not body:
            continue
        plan = plan_audio(
            KIND_FOLLOW, body, subject=subject,
            title=((subject or "") + "跟读") if subject else "",
        )
        if plan is None:
            continue
        plan.notes.append("跟读材料是从题干/答案里读出来的")
        return plan
    return None


def plan_from_problem(
    *, stem: Any, answer: Any = "", subject: str = "", extra_hints: str = ""
) -> AudioPlan | None:
    """**只看题干与答案**,判断这题要不要出声、念什么(纯函数,不调模型)。

    三种结局:
      * `None` —— 没有出声线索(普通笔答题),调用方**什么都别做**;
      * `plan.ok` —— 词表/材料从题干或答案里读出来了,可以合成;
      * `plan.error` —— 看出这题要出声,但**材料不在题干里**(例如只写着"听录音"),
        如实说不做,而不是编一段出来。
    """
    text = (str(stem or "") + " " + str(answer or "")).strip()
    if not text:
        return None
    # 知识点名也算线索:题干只写"Tuesday、Wednesday…"、知识点写着"单词认读"的题,
    # 光看题干是判不出要不要发音的(2026-10-24 用户截图里的 #13 就是这种)。
    hay = (text + " " + str(extra_hints or "")).replace(" ", "").replace("\n", "")
    wants_dictation = any(hint in hay for hint in DICTATION_HINTS)
    wants_follow = any(hint in hay for hint in FOLLOW_HINTS)
    wants_listening = any(hint in hay for hint in LISTENING_HINTS)
    wants_read = any(hint in hay for hint in READ_ALOUD_HINTS)
    if not (wants_dictation or wants_follow or wants_listening or wants_read):
        return None
    # ⚠️ 先洗一遍再拆词:音标("Monday /ˈmʌndeɪ/")不洗掉的话,整个词条就"不像一个词",
    #    题干里明明写着词表却读不出来(2026-10-15 跟读题实测)。
    answer_material = clean_material(answer)
    stem_tail = clean_material(tail_after_colon(stem))
    # 题干本身就是全部材料(没有冒号)的情况:**只在"线索来自知识点"时才敢用** ——
    # 否则"听录音,选出你听到的单词"这种指令会被当成词表念出来(旧测试守着的坑)。
    bare = ""
    if extra_hints:
        candidate = clean_material(stem)
        if (
            len(candidate) >= 4
            and not any(item in candidate for item in DICTATION_HINTS + LISTENING_HINTS)
            and not has_place_marker(candidate)
        ):
            bare = candidate
    # ⭐ 跟读**先于**听写判(2026-11-09 用户报的 bug:「今天录的 take care of,我要录的是
    #    **短语跟读**,智能体只能录成听写,并且把三个词分开读」):跟读的材料是**一句话**,
    #    按听写口径拆成 take、care、of 就毁了短语;要拆也只按原文真有的分隔符拆。
    if wants_follow and not wants_dictation:
        plan = follow_plan(
            (stem_tail, answer_material, bare, _bare_stem(stem)), subject=subject,
        )
        if plan is not None:
            return plan
        return AudioPlan(
            kind=KIND_FOLLOW,
            error="题干里没有可念的句子或词表(只写了要跟读哪一段)",
        )
    # ⚠️ 位置优先于"像不像词":定位词("第3段")本身很短、也不带句号,单看很像一个词。
    words: list[str] = []
    if not has_place_marker(stem) and not has_place_marker(answer):
        # ⭐ 题干之后那截**同语种**的词表优先:实测库里的 #9 答案抄成了
        #    "Monday 星期一 /ˈmʌndeɪ/;Tuesday 星期二 …" —— 照单全念会读出中文释义,
        #    而题面写着的那五个英文单词才是孩子要跟读的东西。
        words = (words_in_language(stem_tail, subject)
                 or words_in_language(answer_material, subject)
                 or words_from_list(answer_material)
                 or words_from_list(stem_tail)
                 # 题干本身就是一张词表(没有冒号):靠知识点名判出"要发音"之后,
                 # 还得有东西可念 —— 这是最后一层,给的就是题干本身。
                 or words_from_list(bare))
    if words:
        # 跟读/听写 → 念两遍的听写节奏;**认读** → 只念一遍(先让他自己读)
        read_aloud = wants_read and not (wants_dictation or wants_listening)
        plan = plan_audio(
            KIND_LISTENING if read_aloud else KIND_DICTATION,
            "、".join(words),
            subject=subject,
            purpose="read_aloud" if read_aloud else "",
            title=((subject or "") + "认读") if read_aloud else "",
        )
        if plan is not None:
            plan.notes.append("词表是从题干/答案里读出来的")
            return plan
    # 整段材料:优先题干里冒号后面的原文,其次答案本身(例如默写的古诗)
    material = stem_tail or bare or clean_material(answer)
    # ⚠️ 材料不能是**指令本身**("听录音,选出你听到的单词"不是要念的内容)
    instructions = DICTATION_HINTS + LISTENING_HINTS
    # ⚠️ 也不能是**只有定位、没有内容**的那种("P18 第3段"念出来是一串编号):
    #    这种情况走下面的 error 分支如实说"题干里没有可念的材料",而不是编一段出来。
    if (len(material) >= 4 and not any(item in material for item in instructions)
            and not looks_like_place_reference(material)):
        plan = plan_audio(
            KIND_LISTENING, material, subject=subject,
            purpose="read_aloud" if (wants_read and not wants_listening) else "",
        )
        if plan is not None:
            plan.notes.append("要念的材料是从题干/答案里读出来的")
            return plan
    if wants_dictation:
        return AudioPlan(kind=KIND_DICTATION, error="题干里没写要听写的词表,照片上也没有")
    if wants_read:
        return AudioPlan(
            kind=KIND_LISTENING, purpose="read_aloud",
            error="题干里没有可念的单词/句子(只写了要读哪一段)",
        )
    return AudioPlan(kind=KIND_LISTENING, error="题干里没有可念的听力材料(只有一句指令)")


def render_plan(engine: TtsEngine, plan: AudioPlan | None) -> DictationAudio:
    """按计划合成音频(走引擎自己的缓存与主备切换)。"""
    if plan is None:
        return DictationAudio(error="没有朗读计划")
    if not plan.ok:
        return DictationAudio(error=plan.error)
    if plan.is_dictation:
        return engine.build_dictation(
            plan.words, title=plan.title or "听写", repeat=plan.repeat, gap_s=plan.gap_s,
        )
    if plan.is_follow and plan.items:
        # 跟读词表:一条念一遍、留白等他跟 —— 不报序号,也不说"请准备好纸笔"
        return engine.build_follow(plan.items, gap_s=plan.gap_s)
    return engine.build_reading(plan.text, repeat=plan.repeat, gap_s=plan.gap_s)


def spec_payload(
    plan: AudioPlan | None,
    audio: DictationAudio | None = None,
    *,
    artifact_id: str = "",
    error: str = "",
) -> dict[str, Any]:
    """写进 app_problems.audio_spec 的那份 JSON。

    ⭐ 它要能让**未来的自己**不调模型就重建这段音频:text 与 kind 是事实源,
    其余都是"当初是怎么做的"的存档(音色/引擎/秒数/花了多少)。
    """
    payload: dict[str, Any] = {
        "kind": (plan.kind if plan else ""),
        "text": (plan.text if plan else ""),
        "label": (plan.label if plan else "音频"),
    }
    if plan is not None and plan.purpose:
        payload["purpose"] = plan.purpose
    if plan is not None:
        payload["repeat"] = int(plan.repeat)
        payload["gap_s"] = float(plan.gap_s)
        if plan.items:
            # 跟读词表:原文是列表,重做时按这几条念(不必再从文本里猜)
            payload["items"] = list(plan.items)
    if artifact_id:
        payload["artifact_id"] = artifact_id
    if audio is not None:
        payload.update({
            "media_type": audio.media_type,
            "seconds": round(float(audio.seconds), 2),
            "byte_size": len(audio.data),
            "provider": audio.provider,
            "voice": audio.voice,
            "cached_hits": int(audio.cached_hits),
            "segments": int(audio.segments),
            "spent_yuan": audio.spent_yuan,
            "notes": list(audio.notes),
        })
        if not audio.ok:
            payload["error"] = audio.error or "发声失败"
    if error:
        payload["error"] = error
    return payload


def spec_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def spec_of(raw: Any) -> dict[str, Any]:
    """把库里的 audio_spec 读成 dict。读不出来给空 dict(不抛)。"""
    if isinstance(raw, dict):
        return dict(raw)
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        loaded = json.loads(text)
    except Exception:  # noqa: BLE001
        return {}
    return loaded if isinstance(loaded, dict) else {}


def describe_spec(spec: dict[str, Any]) -> str:
    """把绑定好的音频说成人话(回执/卷面用)。"""
    if not spec:
        return ""
    label = str(spec.get("label") or "音频")
    error = str(spec.get("error") or "").strip()
    if error:
        return label + "没做成:" + error
    bits: list[str] = []
    seconds = spec.get("seconds")
    if isinstance(seconds, (int, float)) and seconds:
        bits.append("%.1f 秒" % float(seconds))
    voice = str(spec.get("voice") or "").strip()
    if voice:
        bits.append(voice)
    text = str(spec.get("text") or "").replace("\n", " ").strip()
    if text:
        bits.append(text if len(text) <= 20 else text[:20] + "…")
    return label + "(" + " · ".join(bits) + ")" if bits else label


def words_of(text: str) -> list[str]:
    return parse_words(text)


__all__ = [
    "ALIASES",
    "AudioPlan",
    "DICTATION_GAP_S",
    "DICTATION_HINTS",
    "DICTATION_REPEAT",
    "FOLLOW_GAP_S",
    "FOLLOW_HINTS",
    "FOLLOW_ITEM_GAP_S",
    "FOLLOW_REPEAT",
    "LISTENING_HINTS",
    "KIND_DICTATION",
    "KIND_FOLLOW",
    "KIND_LISTENING",
    "KIND_NONE",
    "KINDS",
    "MAX_CHARS",
    "MAX_ITEM_CHARS",
    "MAX_MERGED_FOLLOW",
    "MAX_MERGED_WORDS",
    "MAX_WORDS",
    "explicit_list_items",
    "follow_material",
    "follow_plan",
    "merge_audio_specs",
    "READING_GAP_S",
    "READING_REPEAT",
    "clean_material",
    "describe_spec",
    "entry_is_clean",
    "language_of",
    "looks_like_place_reference",
    "tail_after_colon",
    "looks_like_word",
    "normalize_text",
    "plan_audio",
    "plan_from_problem",
    "render_plan",
    "spec_json",
    "spec_of",
    "spec_payload",
    "strip_notes",
    "whitelist_name",
    "words_from_list",
    "words_in_language",
    "words_of",
]

# ---------------------------------------------------------------- 合并听写 / 跟读
#: 一天的合并听写最多多少个词(再多就不是"一段"了,该分两次听)
MAX_MERGED_WORDS = 60
#: 一天的合并跟读最多多少条。短语比单词长、每条还要留白让孩子跟着读,
#: 所以比词表少一半:再多就不是"一段"了,该分两次听。
MAX_MERGED_FOLLOW = 24
#: 词表分隔符(与 tts.parse_words 同一套口径)
_WORD_SPLIT = re.compile(r"[、,，;；\n]+")
_LATIN_LETTER = re.compile(r"[A-Za-z]")


def _follow_pieces(spec: dict[str, Any]) -> list[str]:
    """一段跟读 spec 里"要念的每一条"。

    ⚠️ 短语**不许**在这里被拆开:列表模式的 spec 带着 items(拆好了),
       短语模式的 text 里的逗号是**句内停顿**,不是分隔符 —— 只能整条算一条。
    """
    raw = spec.get("items")
    if isinstance(raw, list):
        pieces = [str(item).strip() for item in raw if str(item).strip()]
        if pieces:
            return pieces
    text = normalize_text(spec.get("text"))
    return [text] if text else []


def merge_audio_specs(
    entries: Sequence[dict[str, Any]],
    *,
    limit: int = MAX_MERGED_WORDS,
    follow_limit: int = MAX_MERGED_FOLLOW,
) -> list[dict[str, Any]]:
    """把**同一天多道听写/跟读题**并成按(怎么念, 语言)分组的一份(纯函数,不调模型)。

    用户 2026-10-24:*"一天里面有多道听写题,应该要能够合并成一段听写音频
    (语文和英文是要分开的)"* —— 一道题一段音频的话,家长要来回点十几次。
    2026-11-09 同一句话问到跟读:*"如果一天同时有多个跟读,能合并吗"* —— 同一个办法。

    ⚠️ 听写与跟读**不并在一起**:一个是"报词、孩子写",一个是"念句、孩子跟着读",
       节奏完全不同(听写每条报两遍,跟读整条念完留白)。
    语言按**内容本身**判:带拉丁字母 → 英语,否则语文。不按学科名判 ——
    学科写的是"英语",但词表可能是"星期一、星期二"这种中文释义。
    """
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    seen: dict[tuple[str, str], set[str]] = {}
    for item in entries:
        spec = spec_of(item.get("spec") if isinstance(item, dict) else None)
        kind = whitelist_name(spec.get("kind"))
        if kind == KIND_DICTATION:
            pieces = [part.strip() for part in _WORD_SPLIT.split(str(spec.get("text") or ""))]
            pieces = [piece for piece in pieces if piece]
            field, cap = "words", limit
        elif kind == KIND_FOLLOW:
            pieces = _follow_pieces(spec)
            # ⚠️ 键名不能叫 "items":dict 自带 .items 方法,Jinja 的 g.items 会先拿到方法
            #    (实测 2026-11-09:模板里 g.items|length 直接报 "no len()")。
            field, cap = "phrases", follow_limit
        else:
            continue
        if not pieces:
            continue
        language = "en" if _LATIN_LETTER.search(" ".join(pieces)) else "zh"
        key = (kind, language)
        group = groups.get(key)
        if group is None:
            group = {
                "kind": kind,
                "language": language,
                "label": ("英语" if language == "en" else "语文")
                         + ("跟读" if kind == KIND_FOLLOW else "听写"),
                field: [],
                "problem_ids": [],
            }
            groups[key] = group
            seen[key] = set()
        problem_id = int(item.get("problem_id") or 0)
        if problem_id and problem_id not in group["problem_ids"]:
            group["problem_ids"].append(problem_id)
        for piece in pieces:
            name = piece.strip()
            mark = name.lower()
            if mark in seen[key]:
                continue
            if len(group[field]) >= cap:
                group["truncated"] = True
                continue
            seen[key].add(mark)
            group[field].append(name)
    return [group for group in groups.values() if group.get("words") or group.get("phrases")]


def merge_dictation_specs(
    entries: Sequence[dict[str, Any]], *, limit: int = MAX_MERGED_WORDS
) -> list[dict[str, Any]]:
    """只要**听写**那几组(旧名字,保留给老调用方与既有测试)。"""
    return [group for group in merge_audio_specs(entries, limit=limit)
            if group["kind"] == KIND_DICTATION]
