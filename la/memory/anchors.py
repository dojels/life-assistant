"""锚点抽取与匹配。

摘要模板要求"锚点行必须列出区间内出现的**所有**日期/金额/人名/项目名"(宁多勿漏)。
锚点同时是检索排序的第一优先级 —— 比 FTS 相关度更靠前(设计深化 §3.1)。
"""

from __future__ import annotations

import re

# 日期:2026-10-08 / 10月8日 / 10/8 / 上周三
_DATE_PATTERNS = (
    re.compile(r"\d{4}-\d{1,2}-\d{1,2}"),
    re.compile(r"\d{4}年\d{1,2}月\d{1,2}日"),
    re.compile(r"\d{1,2}月\d{1,2}日"),
    re.compile(r"(?:上|这|本|下)(?:周|星期|礼拜)[一二三四五六日天]"),
    re.compile(r"(?:今|明|昨|后)天"),
)

# 金额:3800元 / 3.8万元 / 6 万 / ￥3800
_AMOUNT_PATTERN = re.compile(
    r"(?:[¥￥]\s*\d+(?:\.\d+)?)|(?:\d+(?:\.\d+)?\s*(?:万元|元|块钱|块|万))"
)

# 引号里的专名与书名号:餐厅名、项目名、文件名的常见写法
_QUOTED_PATTERN = re.compile(r"[《“\"'「【]([^》”\"'」】]{1,30})[》”\"'」】]")

# 英文专名(≥2 字母且首字母大写,排除句首常见词的效果有限,宁多勿漏)
_LATIN_NAME = re.compile(r"\b[A-Z][A-Za-z0-9_]{1,}\b")

# 纯数字串(日期/金额之外的编号,如订单号)
_NUMBER_TOKEN = re.compile(r"\b\d{4,}\b")


def _clean(token: str) -> str:
    return token.strip().strip("《》“”\"'「」【】,。;:!?、 ")


def extract_anchors(text: str) -> list[str]:
    """从一段文本里抽取锚点,保持出现顺序并去重。"""
    if not text:
        return []
    found: list[str] = []
    for pattern in _DATE_PATTERNS:
        found.extend(pattern.findall(text))
    found.extend(_AMOUNT_PATTERN.findall(text))
    found.extend(match.group(1) for match in _QUOTED_PATTERN.finditer(text))
    found.extend(_LATIN_NAME.findall(text))
    found.extend(_NUMBER_TOKEN.findall(text))

    seen: set[str] = set()
    ordered: list[str] = []
    for token in found:
        cleaned = _clean(token)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        ordered.append(cleaned)
    return ordered


#: 设计深化 §3.1:anchor_hit = 命中的锚点**类别数** / 3,类别就是这三类。
KIND_DATE = "date"
KIND_NUMBER = "number"
KIND_NAME = "name"
ANCHOR_KINDS: tuple[str, ...] = (KIND_DATE, KIND_NUMBER, KIND_NAME)


def anchor_kind(token: str) -> str:
    """给一个锚点归类:日期 / 数字 / 人名实体(认不出的一律算人名实体)。"""
    text = (token or "").strip()
    if not text:
        return KIND_NAME
    for pattern in _DATE_PATTERNS:
        if pattern.fullmatch(text):
            return KIND_DATE
    for pattern in (_AMOUNT_PATTERN, _NUMBER_TOKEN):
        if pattern.fullmatch(text):
            return KIND_NUMBER
    digits = text.replace(".", "", 1).replace("-", "", 1).replace("+", "", 1)
    if digits.isdigit():
        return KIND_NUMBER
    return KIND_NAME


def anchor_kinds(tokens: list[str]) -> set[str]:
    """一组锚点覆盖了哪几类(用于 anchor_hit 的分子)。"""
    return {anchor_kind(token) for token in tokens if token}


def as_anchor_list(value: object) -> list[str]:
    """把库里存的 JSON 锚点列转成 list(容错)。"""
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str) and value.strip():
        import json

        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return []
        if isinstance(loaded, list):
            return [str(item) for item in loaded]
    return []


def anchor_hits(query_anchors: list[str], doc_anchors: list[str]) -> list[str]:
    """返回命中的锚点(双向包含也算命中:查询里写"老张牛肉面",文档里写"老张牛肉面店")。"""
    hits: list[str] = []
    for query_token in query_anchors:
        for doc_token in doc_anchors:
            if query_token == doc_token or query_token in doc_token or doc_token in query_token:
                hits.append(query_token)
                break
    return hits
