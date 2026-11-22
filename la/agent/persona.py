"""人格:**可编辑的那一半**,与不可编辑的那一半(D117)。

用户 2026-10-13 的原话:*"她的人格,在网页界面看不到"* —— 在此之前人设只存在于
la/agent/prompts.py 的一个常量里:要看它得读代码,要改它得改代码 + 重启进程。
这一层把**上半段**(性情与口吻)变成数据:存在 sys_settings(与聊天页那个「联网」开关
同一张表、同一条优先级),每轮由 MemorySession 现读 —— 改完**下一轮就生效**,不用重启。

三层优先级(与 settings_store 同一条思路):
    sys_settings['persona_text']  >  代码里的 PERSONA_BODY(出厂默认)

⭐ D144(2026-10-17):**名字也成了数据** —— sys_settings['persona_name'],出厂值仍是
   PERSONA_NAME。页面标题、轨迹页的角色标签都读它;换名字时,正文里出现的旧名字
   **一并替换**(否则正文叫旧名字、标题叫新名字,她自己都分不清)。

⚠️ 三件**刻意不做**的事:
  1. **不养版本表**。每次保存都落一条可撤销回执(kind = persona.text,undo_plan 里带
     上一版全文)—— 要退回哪一版就点哪条回执,不必再养一张历史表。
  2. **不写 memory 事件**。人格是配置,不是"发生过的事":写进事件日志会被摘要池收进去,
     之后检索"上次聊到什么"就会翻出一堆关于提示词的记录(而它每轮都进上下文)。
  3. **不让用户改尾巴**。优先级阶梯由 prompts.build_persona_text 无条件追加 ——
     用户能改的是"她像谁",不是"她能不能为了讨好人而不说实话"(见 D47 / D117)。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from .. import config
from ..ops import settings_store
from .prompts import IDENTITY, PERSONA_BODY, PERSONA_NAME, PERSONA_TAIL, build_persona_text

#: sys_settings 里的键名(与 web_enabled 同住一张表)
PERSONA_KEY = "persona_text"
#: ⭐ 名字单独一个键(D144)。为什么不从正文里解析:正文是给人读的叙事,
#:    名字要出现在**页面标题 / 轨迹页角色标签**里 —— 靠正则从散文里猜,
#:    猜错了没人发现;存成字段,读的人(和页面)才有一个确定的答案。
PERSONA_NAME_KEY = "persona_name"
#: 名字能有多长。12 个字够放两个字到四个字的称呼;再长会把页面标题挤歪。
MAX_NAME_CHARS = 12

#: 能写多长。⭐ 这个数是**产品取向**,不是技术天花板 —— 两件事要分开说:
#:   * 技术上:2000 字按 estimate_tokens 约 504 token,IDENTITY 实测 332、技能目录 95,
#:     三者合计约 931 token,离 BUDGET_SYSTEM(2500)还有一大截 ——
#:     真要逼近截尾,得写到 8,000 字以上。也就是说 **2001 字不会被截尾**;
#:     这个上限保护的是"她的性子别被篇幅稀释",不是"提示词装不下";
#:   * 但真吃光 BUDGET_SYSTEM 时,clip_to_budget 是**截尾**的 —— 先被砍掉的是
#:     技能目录(助手会突然不知道自己会什么,而且**不报错**)。所以上限必须存在,
#:     只是别拿"会被截尾"去解释一个还差八千字的输入(2026-10-17 改口径)。
#: tests/test_persona_edit.py 用"贴着上限的一份也不许被裁剪"钉住技术那一半。
MAX_CHARS = 2000

#: 短于这个长度不算"写好了":一个句号也叫人格,但那样她就没有性情了
MIN_CHARS = 4


def is_self_identity(text: object, name: str = PERSONA_NAME) -> bool:
    r"""这句话是不是在说**助手自己**是谁(名字 / 英文名 / 称呼)。

    ⭐ 2026-10-25:用户点名"智能体的英文名应该是人格部分的内容,要更新到人格界面"。
       在此之前,模型把"助手的英文名是 Mo"当成**用户的事实**写进永久事实层 ——
       事实层每轮无条件进上下文,而"我自己叫什么"本来就在人格里,两处各说各的。

    ⚠️ 这是**确定性**判据,只认主语明确的句子("助手 / 她的名字 / 你" + 名字类词):
       * "用户叫 X" 不是(主语是用户);
       * "你明天提醒我" 不是(没有名字类词,也不匹配"你以后叫"这种改名的说法);
       * "助手很聪明" 不是(没有名字类词)。
       拿不准的时候宁可判否 —— 判错了它只是继续走记忆那条老路,不会写错地方。
    """
    body = " ".join(str(text or "").split())
    if not body:
        return False
    refs = [re.escape(item) for item in dict.fromkeys(("助手", str(name or ""))) if item]
    ref = "|".join(refs)
    if re.search(
        r"(?:" + ref + r")的?(?:英文名|中文名|中文名字|名字|小名|昵称|称呼)", body
    ):
        return True
    # "你以后叫小夏" / "以后你叫小夏" / "从现在起你就叫小夏"
    # ⚠️ 叫字后面不许跟代词:"以后叫他来""你叫我干嘛" 都不是在说她叫什么。
    if re.search(r"(?:助手|你)(?:以后|今后|就|现在|从今天起)叫(?![他她我您的])", body):
        return True
    if re.search(
        r"(?:以后|今后|从今天起|从现在起)(?:你|助手)?(?:就)?叫(?![他她我您的])", body
    ):
        return True
    if re.search(r"你的(?:英文名|中文名|名字|小名|昵称|称呼)(?:是|叫)", body):
        return True
    return False


def as_persona_line(text: object, name: str = PERSONA_NAME) -> str:
    """把一句"关于助手的事实"收成能放进**人格页**的一句话。

    "助手的英文名是 Mo" -> "英文名是 Mo"(人格里本来就是"你",不必再写"助手的")。
    """
    body = normalize(text)
    pattern = r"^(?:助手|" + re.escape(str(name or PERSONA_NAME)) + r")(?:\([^)]*\))?的"
    return re.sub(pattern, "", body).strip() or body


class PersonaError(ValueError):
    """这份人格存不下去(空 / 太短 / 太长)。**消息是给用户看的**,会原样上页面。"""


def normalize(text: object) -> str:
    """收敛成能比较、能存的一种写法。

    ⚠️ 必须做:`<textarea>` 提交上来的是 CRLF(Windows 浏览器),而库里与提示词里
       该是 LF。不收敛的话"内容没变"永远判不出来(每次 diff 都多出满屏 \\r),
       而且看起来一样的两版会在回执里存成两份字节不同的文本。
    """
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in raw.split("\n")).strip()


def check_name(text: object) -> str:
    """收敛名字。空白折成一个空格;空或太长都不行(消息给用户看)。"""
    name = " ".join(str(text or "").split())
    if not name:
        raise PersonaError("名字不能是空的 —— 至少留一个称呼。")
    if len(name) > MAX_NAME_CHARS:
        raise PersonaError(
            "名字太长了:%d 个字符,最多 %d 个。" % (len(name), MAX_NAME_CHARS)
        )
    return name


def check_name_or_empty(value: object) -> str:
    """读库时用:能用就用,不能用(空 / 超长 / 脏数据)就当没存过。

    页面不该因为库里一条坏掉的名字而打不开 —— 与"空主体回落到出厂默认"同一条口径。
    """
    try:
        return check_name(value)
    except PersonaError:
        return ""


def check(text: object) -> tuple[str, list[str]]:
    """把用户输入收成能存的一版,外加"我替你改了什么"的说明。不合法就抛 PersonaError。"""
    body = normalize(text)
    notes: list[str] = []
    # ⭐ 用户完全可能把页面上那个只读预览整段复制回来(那里面有固定尾巴)。
    #    与其存成"尾巴 + 尾巴",不如替他摘掉并**明说**改了什么 —— 静默修改是不行的。
    if PERSONA_TAIL in body:
        body = normalize(body.replace(PERSONA_TAIL, ""))
        notes.append("去掉了粘进来的固定段落(保存时会自动追加,不用你写)")
    # ⭐ 同理:页面上 ① 身份与边界就显示在上面,顺手全选复制会把那一段也带进来。
    #    它比尾巴更危险 —— 尾巴只会重复一遍,身份那五条会让人格主体自己改写边界。
    if IDENTITY in body:
        body = normalize(body.replace(IDENTITY, ""))
        notes.append("去掉了粘进来的「身份与边界」那一段(它由代码注入,不在人格里)")
    if len(body) < MIN_CHARS:
        raise PersonaError(
            "太短了(至少 %d 个字)—— 那样只剩代码固定的那一段,她就没有性情了。" % MIN_CHARS
        )
    if len(body) > MAX_CHARS:
        # ⚠️ 口径 2026-10-17 改过:旧文案说"超了会被截尾",但 2001 字离截尾
        #    (约 8,000 字)还差得远 —— 用户照着这句话去理解,会以为提示词快满了。
        raise PersonaError(
            "太长了:%d 字,上限 %d 字。写这么长她那点性子会被稀释;"
            "而且这一段与身份、技能目录共用系统提示的 %d token 预算。"
            % (len(body), MAX_CHARS, config.BUDGET_SYSTEM)
        )
    return body, notes


@dataclass(frozen=True)
class PersonaState:
    """人格此刻的样子(页面与提示词都读它)。"""

    #: 当前生效的**主体**(用户写的,或出厂默认)
    body: str = PERSONA_BODY
    #: 库里存着自己写的一版 → True;否则就是出厂默认
    custom: bool = False
    #: 上次保存时间(库里没记录 = 空串)
    updated_at: str = ""
    #: ⭐ 她叫什么。默认是代码里那个出厂名字;改过就存在 sys_settings 里(D144),
    #:    页面标题与轨迹页的角色标签都读它 —— 不再是一个只写在正文里、界面不认的字符串。
    name: str = PERSONA_NAME
    #: 名字是库里存的那一个(而不是出厂的)→ True
    name_custom: bool = False

    @property
    def chars(self) -> int:
        return len(self.body)

    @property
    def source_label(self) -> str:
        return "你自己写的" if self.custom else "出厂默认"

    @property
    def full_text(self) -> str:
        """页面要展示"模型实际读到的那一段" = 主体 + 固定尾巴。"""
        return build_persona_text(self.body)


@dataclass(frozen=True)
class PersonaAppend:
    """一次「写进人格页」的结果 —— 工具层据此如实回话。

    ⚠️ status 必须区分清楚,因为模型会照着它说话:
       appended    真的写进去了(下一轮生效);
       unchanged   人格里本来就有这一句(没重复写);
       declined    用户说"先不写";
       unconfirmed 没人回答 / 没有提问通道 —— **不等于同意**;
       failed      校验没过(太长之类),一个字都没写。
    """

    status: str
    added: str = ""
    name: str = ""
    renamed: bool = False
    before: str = ""
    notes: tuple[str, ...] = ()
    reason: str = ""
    receipt_id: int = 0


class PersonaStore:
    """人格的读写口。

    ⚠️ `conn` 就是项目里那个每请求一套的 assistant 连接(Web 层用 services.assistant,
       会话层用 MemorySession.assistant_conn)。给 None 时**读**得到出厂默认、**写**会
       如实报错(没有库就没地方存)—— 不假装保存成功。
    """

    def __init__(self, conn: sqlite3.Connection | None) -> None:
        self.conn = conn

    def state(self) -> PersonaState:
        entry = settings_store.get_entry(self.conn, PERSONA_KEY)
        updated_at = ""
        body = ""
        if entry is not None:
            value, updated_at = entry
            body = normalize(value)
        # 名字与主体分开读:名字改过、正文没改,也各自成立
        stored_name = ""
        name_updated_at = ""
        name_entry = settings_store.get_entry(self.conn, PERSONA_NAME_KEY)
        if name_entry is not None:
            stored_name = check_name_or_empty(name_entry[0])
            name_updated_at = name_entry[1]
        # ⚠️ 主体是空串(手改库/半途残留)时按**出厂默认**算,不按"空人格"算 ——
        #    空人格会让助手只剩那几行优先级阶梯,连自己叫什么都不知道。
        return PersonaState(
            body=body or PERSONA_BODY,
            custom=bool(body),
            updated_at=updated_at or name_updated_at,
            name=stored_name or PERSONA_NAME,
            name_custom=bool(stored_name),
        )

    def body(self) -> str:
        return self.state().body

    def custom(self) -> bool:
        return self.state().custom

    def text(self) -> str:
        """这一轮真正进提示词的人设块(主体 + 固定的尾巴)。"""
        return self.state().full_text

    def save(self, raw: object, raw_name: object | None = None) -> tuple[str, list[str]]:
        """存下一版。返回 (上一版主体, 说明)。

        ⭐ 上一版要**原样**交回去:调用方把它写进回执的 undo_plan,撤销才有得退。
           没自定义过时给空串(撤销 = 回到出厂默认,而不是"存一份空的")。

        ⭐ 名字(D144):raw_name 传了就一起存;换了名字时,正文里出现的旧名字
           **一并换成新的**(同一份文本里两种叫法会让她自己都分不清),
          换了几处写在返回的说明里 —— 改了什么必须说出来。
        """
        # ⚠️ 先把两条校验都做完,再动库:校验不过时**一个字都不写**
        body, notes = check(raw)
        before = self.state()
        new_name: str | None = None
        if raw_name is not None and str(raw_name).strip():
            candidate = check_name(raw_name)
            if candidate != before.name:
                hits = body.count(before.name)
                if hits:
                    body = body.replace(before.name, candidate)
                    notes.append(
                        "正文里 %d 处「%s」也改成了「%s」" % (hits, before.name, candidate)
                    )
                else:
                    notes.append(
                        "正文里没出现「%s」—— 页面标题会改,但她自己不会知道这个新名字"
                        % before.name
                    )
                new_name = candidate
        # ⚠️ 两次写各自提交(sys_settings 的既有口径),所以顺序上先写**正文**再写名字:
        #    万一中间出错,留下的是"正文新、名字旧",再存一次就对了;
        #    反过来会留下"名字新、正文旧"—— 看起来像改名生效了,其实她还不认识这个称呼。
        settings_store.set_text(self.conn, PERSONA_KEY, body)
        if new_name is not None:
            self._write_name(new_name)
        return (before.body if before.custom else ""), notes

    def append(self, raw: object, *, name: object | None = None) -> tuple[str, list[str]]:
        """在人格主体**末尾追加**一句(用户在对话里确认过的),返回 (上一版, 说明)。

        ⭐ 为什么是"追加"而不是"整段重写":对话里用户说的是"你的英文名是 Mo",
           模型要落的只是这一句;让它把整份人格重写一遍,等于让它重写她的性情
           (而它并没有这个人格的完整上下文)。追加是**最小写入**。

        ⚠️ 与 save() 一样过 check():长度上限、固定尾巴、身份段都会被摘掉/拦住;
           校验不过时一个字都不写(抛 PersonaError)。
        """
        line = normalize(raw)
        if not line:
            raise PersonaError("要写进人格的那句话是空的。")
        before = self.state()
        body = before.body
        notes: list[str] = []
        if line in body:
            notes.append("人格里已经有这句了,没有重复追加")
            merged = body
        else:
            merged = (body.rstrip() + "\n" + line).strip() if body.strip() else line
        previous, save_notes = self.save(merged, name)
        return previous, notes + save_notes

    def _write_name(self, name: str) -> None:
        """名字等于出厂名就删键(回到出厂),否则存下来。"""
        if name and name != PERSONA_NAME:
            settings_store.set_text(self.conn, PERSONA_NAME_KEY, name)
        else:
            settings_store.clear(self.conn, PERSONA_NAME_KEY)

    def restore(self, body: object, name: object | None = None) -> None:
        """把主体(以及可选的名字)写回某一版 —— 撤销入口用。

        ⚠️ name 为 None 表示**不动名字**(老回执里没有这个字段,撤销时别把它清掉);
           传空串表示"撤销回出厂名字"。
        """
        text = normalize(body)
        if text:
            settings_store.set_text(self.conn, PERSONA_KEY, text)
        else:
            settings_store.clear(self.conn, PERSONA_KEY)
        if name is not None:
            self._write_name(" ".join(str(name or "").split()))

    def reset(self) -> str:
        """恢复出厂默认(主体与名字一起退回)。

        返回被替换掉的那一版主体(空串 = 主体本来就是默认,没动过)。
        ⚠️ 名字也一起清:只退正文不退名字,会出现"正文写着旧名字、标题叫新名字"的半截状态。
        """
        before = self.state()
        previous = before.body if before.custom else ""
        self.restore("", name="")
        return previous
