"""分类智能:把「这笔算什么」做成**可解释的候选**,而不是让模型凭感觉编一个分类名。

分工(与「先规则、规则未命中再靠模型」同源):
  1. **规则引擎**(app_rules,用户每次纠正学出来的)—— 最强证据,命中就定了;
  2. **本模块的确定性候选**(别名表 + 关键词表 + 字面相似度)—— 给模型一份排好序的候选;
  3. **模型**在候选之间做选择,或者提出新建一个分类(新建必须走用户确认框);
  4. 模型也拿不准 → 用 ask_user 让用户拍板。

为什么不让模型直接"想一个分类名":分类是会长期累积的统计维度,同一个意思写成
「餐饮」「吃饭」「伙食」三份,月度统计立刻分裂。所以**名字必须来自这份清单**,
扩清单要么走用户确认的新建,要么走用户纠正学出来的规则。

⚠️ 这里只做**确定性**打分(子串 + 别名 + difflib 相似度),不引模型、不引第三方库。
"""

from __future__ import annotations

import difflib
import sqlite3
from dataclasses import dataclass, field
from typing import Any

# 账单自带的分类名(支付宝/微信的写法)与我们字典的对照。
# 只做**确定性**映射:精确 -> 别名表 -> 子串。对不上就留空,绝不瞎猜。
CATEGORY_ALIASES: dict[str, str] = {
    "餐饮美食": "餐饮",
    "交通出行": "交通",
    "服饰装扮": "购物",
    "日用百货": "购物",
    "数码电器": "购物",
    "美容美发": "其他",
    "医疗健康": "医疗",
    "文化休闲": "娱乐",
    "运动户外": "娱乐",
    "酒店旅游": "娱乐",
    "教育培训": "教育",
    "住房物业": "居住",
    "生活缴费": "居住",
    "转账红包": "其他",
    "亲友代付": "人情",
    "商户消费": "其他",
    "扫二维码付款": "其他",
    "工资收入": "工资",
    "薪资": "工资",
    "退款": "退款",
    "报销": "报销",
}

# 关键词 -> 分类。命中即给一个**中等置信度**的候选(比别名弱,比字面相似度强)。
CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "餐饮": (
        "饭", "餐", "外卖", "早点", "早餐", "午餐", "晚餐", "夜宵", "咖啡", "奶茶", "饮料",
        "火锅", "烧烤", "面", "米线", "粉", "面包", "蛋糕", "零食", "水果", "食堂",
        "肯德基", "麦当劳", "星巴克", "瑞幸", "美团", "饿了么", "饭店", "小吃",
    ),
    "交通": (
        "地铁", "公交", "打车", "出租", "滴滴", "高铁", "火车", "机票", "航班", "加油", "油费",
        "停车", "过路", "高速", "单车", "车票", "ETC", "违章", "洗车", "代驾", "网约车",
    ),
    "购物": (
        "淘宝", "京东", "拼多多", "商场", "衣服", "鞋", "裤", "外套", "化妆品", "护肤",
        "日用", "纸巾", "洗衣液", "超市", "电器", "耳机", "手机", "数码", "快递",
    ),
    "居住": (
        "房租", "水费", "电费", "燃气", "物业", "取暖", "宽带", "话费", "垃圾费", "住房", "装修",
    ),
    "医疗": ("医院", "挂号", "药店", "药", "体检", "门诊", "牙", "诊所", "疫苗", "看病"),
    "教育": ("学费", "培训", "课程", "教材", "书本", "文具", "网课", "考试", "报名", "辅导"),
    "娱乐": (
        "电影", "游戏", "演出", "门票", "旅游", "酒店", "健身", "运动", "KTV", "密室",
        "剧本", "会员", "订阅", "视频", "音乐",
    ),
    "人情": ("红包", "礼金", "份子", "随礼", "请客", "礼物", "孝敬", "转账给"),
    "其他": (),
    "工资": ("工资", "薪水", "薪资", "月薪", "发薪"),
    "奖金": ("奖金", "绩效", "年终", "提成", "奖励"),
    "报销": ("报销", "公费", "差旅"),
    "退款": ("退款", "退货", "退还", "返现"),
    "红包收入": ("红包", "压岁钱", "礼金"),
    "理财": ("利息", "分红", "基金", "理财", "收益"),
    "兼职": ("兼职", "稿费", "外包", "副业"),
}

# 打分阈值:低于它的候选不给模型看 —— 给一堆噪音等于没给。
CANDIDATE_FLOOR = 0.34
# 别名/精确命中直接给到最高档(确定性证据)
SCORE_EXACT = 0.98
SCORE_ALIAS = 0.86
SCORE_KEYWORD_BASE = 0.55
SCORE_KEYWORD_STEP = 0.06
SCORE_KEYWORD_MAX = 0.82


@dataclass
class Candidate:
    """一个候选分类 + **为什么是它**。理由要能说给用户听。"""

    name: str
    score: float
    why: str
    kind: str = "expense"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "score": round(float(self.score), 2),
            "why": self.why,
            "kind": self.kind,
        }


@dataclass
class CategoryAdvice:
    """「这笔该算什么」的完整答复:规则命中了什么 + 候选排序 + 已有清单。"""

    direction: str = "expense"
    rule_category: str | None = None
    candidates: list[Candidate] = field(default_factory=list)
    known: list[str] = field(default_factory=list)

    @property
    def needs_decision(self) -> bool:
        """规则没命中 -> 需要有人做决定(模型挑,或者用户拍板)。"""
        return not self.rule_category

    def as_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "rule_category": self.rule_category,
            "candidates": [item.as_dict() for item in self.candidates],
            "known_categories": list(self.known),
            "needs_decision": self.needs_decision,
        }


def _norm(text: str | None) -> str:
    return (text or "").strip().lower().replace(" ", "")


class CategoryAdvisor:
    """按已有分类清单算候选。**只读** —— 它不会新建任何东西。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # -- 清单 -------------------------------------------------------------
    def known(self, kind: str | None = None) -> list[str]:
        sql = "SELECT name, kind FROM app_categories WHERE archived = 0"
        params: list[Any] = []
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY id"
        return [str(row["name"]) for row in self.conn.execute(sql, params).fetchall()]

    def kind_of(self, name: str) -> str | None:
        row = self.conn.execute(
            "SELECT kind FROM app_categories WHERE name = ? AND archived = 0 LIMIT 1", (name,)
        ).fetchone()
        return str(row["kind"]) if row is not None else None

    def exists(self, name: str, kind: str | None = None) -> bool:
        if not (name or "").strip():
            return False
        if kind:
            row = self.conn.execute(
                "SELECT 1 FROM app_categories WHERE name = ? AND kind = ? AND archived = 0",
                (name.strip(), kind),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT 1 FROM app_categories WHERE name = ? AND archived = 0", (name.strip(),)
            ).fetchone()
        return row is not None

    # -- 候选 -------------------------------------------------------------
    def suggest(
        self,
        *,
        payee: str | None = None,
        narration: str | None = None,
        direction: str = "expense",
        limit: int = 4,
    ) -> list[Candidate]:
        """给这笔账排一份候选。text = 对方 + 备注(账单里最有信息量的两个字段)。"""
        text = _norm((payee or "") + " " + (narration or ""))
        known = self.known(direction)
        if not known:
            return []
        scored: dict[str, Candidate] = {}

        def offer(name: str, score: float, why: str) -> None:
            if score <= 0 or name not in known:
                return
            current = scored.get(name)
            if current is None or score > current.score:
                scored[name] = Candidate(name=name, score=score, why=why, kind=direction)

        if text:
            # 1) 分类名直接出现在文本里(「餐饮」/「交通」这类)
            for name in known:
                if _norm(name) and _norm(name) in text:
                    offer(name, SCORE_EXACT, "文字里直接出现了「" + name + "」")
            # 2) 账单分类名 -> 我们的分类
            for alias, target in CATEGORY_ALIASES.items():
                if _norm(alias) in text:
                    offer(target, SCORE_ALIAS, "账单里写作「" + alias + "」")
            # 3) 关键词
            for name in known:
                for word in CATEGORY_KEYWORDS.get(name, ()):  # 命中哪个词就说哪个词
                    if _norm(word) and _norm(word) in text:
                        score = min(
                            SCORE_KEYWORD_BASE + SCORE_KEYWORD_STEP * len(word),
                            SCORE_KEYWORD_MAX,
                        )
                        offer(name, score, "出现关键词「" + word + "」")
                        break
            # 4) 字面相似度(兜底:打错的字、不完整的名)
            for name in known:
                ratio = difflib.SequenceMatcher(None, _norm(name), text).ratio()
                if ratio >= 0.5:
                    offer(name, round(ratio * 0.5, 3), "字面接近")

        ranked = sorted(scored.values(), key=lambda item: (-item.score, item.name))
        picked = [item for item in ranked if item.score >= CANDIDATE_FLOOR][: max(1, int(limit))]
        if not picked:
            # 一个都没命中:给最通用的那个兜底(「其他」/「其他收入」),让模型有事可做
            fallback = "其他" if direction == "expense" else "其他收入"
            if fallback in known:
                picked = [Candidate(name=fallback, score=CANDIDATE_FLOOR, why="没命中关键词,兜底", kind=direction)]
        return picked


__all__ = [
    "CATEGORY_ALIASES",
    "CATEGORY_KEYWORDS",
    "Candidate",
    "CategoryAdvice",
    "CategoryAdvisor",
]
