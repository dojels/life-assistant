"""把注册表里的工具转成模型能看懂的 OpenAI 工具格式。

⚠️ 工具清单必须**稳定且低频变更**:它会被注入提示并计入 token,
增删任一工具都会击穿整个 KV 前缀缓存(学习路线 §15 第 17 条)。
所以这里按名字排序输出,坚决不做"按场景动态增删"。

⚠️ 参数 schema 只从 ToolSpec 取,**不在本文件里手写第二份**。

⚠️ **两份 schema 是同一份的两个投影**(别把它们混成一个):
   1. ToolSpec.parameters_json_schema() —— **原始** Pydantic JSON Schema,
      是 REST 请求体与工具参数的**单真源**(tests/test_m4_0.py 与
      scripts/verify_m4_0.py 按逐字节相等钉着它,title / anyOf / 默认值都在)。
      改它 = 改合同。
   2. 本文件发到 wire 上的那份 —— 由 minimize_parameters() 从 ①**派生**,
      只做"等价的最小化"(删显示用字段、压等价写法),**不改任何校验语义**。
      为什么:53 个工具原样 39,929 字符,其中 71% 是 parameters 段,而这 71%
      里 57% 是 type/anyOf/null/title/default 这类**结构** —— 模型真正读的是
      工具级 description 与字段级 description,title 与 anyOf-null 是写给渲染器的。
      实测(2026-10-15,本机 53 工具):39,929 → 35,082 字符(省 12%);

⚠️ 前缀缓存的口径(实测 2026-10-15):wire 顺序是 **system → tools → 历史**,
   所以这份 wire 文本**必须在会话内逐字节稳定**(D122 之后这是硬纪律)——
   它一变,后面的全部历史都跟着失去前缀复用。

⚠️ 配套:docs/工具集瘦身对照表.md(第一批"wire 最小化"已落地;第二批"按域合并"待审)。
"""

from __future__ import annotations

import copy
from typing import Any, Iterable, Sequence

from ..tools.registry import ToolSpec

#: 只服务于渲染/内省的键,模型读了没用 —— 一律不进 wire。
#: 带分隔符的名字走包含匹配(如 x-title);其余走精确匹配。
_DISPLAY_KEYS = frozenset({"title", "examples", "example"})
_DISPLAY_KEY_FRAGMENTS = ("-title", "_title")

#: 递归里碰到这些键时,**整个子树照原样抄过去**、绝不进去改写。
#: items 例外:它是 JSON Schema 关键字(数组元素 schema),不是用户数据。
_OPAQUE_KEYS = frozenset({"properties", "const", "enum", "default"})


def _copy_type(type_node: Any) -> Any:
    """type 归一:去重、null 收尾;列表只剩一个值时塌成字符串。"""
    if isinstance(type_node, str):
        return type_node
    if isinstance(type_node, list):
        seen: list[Any] = []
        for item in type_node:
            if not isinstance(item, str) or item in seen:
                continue
            seen.append(item)
        # 统一写法:非 null 的在前、null 收尾(JSON Schema 不关心顺序,
        # 但**同一份 spec 必须恒定输出同一种顺序** —— 前缀缓存按字节比)。
        if "null" in seen:
            ordered = [t for t in seen if t != "null"] + ["null"]
            return ordered[0] if len(ordered) == 1 else ordered
        if len(seen) == 1:
            return seen[0]
        return seen
    return type_node


def _collapse_union(branches: Any) -> dict[str, Any] | None:
    """anyOf:[X, {"type":"null"}] -> X 再加上 "type":[X.type,"null"](等价写法)。

    ⚠️ 只在 X 是"普通对象 schema"(有 type、不是 $ref、没有 oneOf 之类)时才压。
    压不动就返回 None,调用方原样保留 —— **宁可多发字节,也别把语义改错**。
    """
    if not isinstance(branches, list) or len(branches) < 2:
        return None
    nulls = 0
    others: list[dict[str, Any]] = []
    for branch in branches:
        if not isinstance(branch, dict):
            return None
        if branch.get("type") == "null":
            nulls += 1
            continue
        if any(k in branch for k in ("$ref", "oneOf", "anyOf", "allOf", "not")):
            return None
        if not isinstance(branch.get("type"), str):
            return None
        others.append(branch)
    if nulls != 1 or len(others) != 1:
        return None
    base = dict(others[0])
    base["type"] = [base["type"], "null"]   # 非 null 在前、null 收尾(与 _copy_type 同一口径)
    return base


def minimize_parameters(node: Any) -> Any:
    """把 JSON Schema 压成等价的最小写法(口径见模块头)。"""
    if isinstance(node, list):
        return [minimize_parameters(item) for item in node]
    if not isinstance(node, dict):
        return node

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _DISPLAY_KEYS or any(frag in key for frag in _DISPLAY_KEY_FRAGMENTS):
            continue
        if key == "properties" and isinstance(value, dict):
            # ⚠️ 键是**参数名**(不能动),值是子 schema(**必须继续压**):
            #    早期写成"整个 properties 照抄",于是所有字段级 title/anyOf 一个都没压掉
            #    (实测踩过:只省 5% 而不是 12%)。
            out[key] = {
                prop: minimize_parameters(sub) if isinstance(sub, dict) else sub
                for prop, sub in value.items()
            }
            continue
        if key == "items" and isinstance(value, dict):
            out[key] = minimize_parameters(value)
            continue
        if key in _OPAQUE_KEYS:
            out[key] = value                      # 浅拷:外层深拷过,这里不共享子树
            continue
        if key in ("anyOf", "oneOf"):
            collapsed = _collapse_union(value)
            if collapsed is not None:
                for inner_key, inner_value in collapsed.items():
                    # ⚠️ type 必须走 _copy_type:直接递归会把 ["string","null"] 原样留下
                    # (实测踩过:整个 anyOf 塌不掉,全仓少省 7%)
                    out[inner_key] = _copy_type(inner_value) if inner_key == "type" else minimize_parameters(inner_value)
                continue
            out[key] = minimize_parameters(value)
            continue
        if key == "type":
            out[key] = _copy_type(value)
            continue
        out[key] = minimize_parameters(value)

    # ⚠️ **不做"叶子塌缩"**:曾经把 {"type": "string"} 写成裸 "string"(省 3 个 token),
    #    但这条规则会跑到 JSON Schema 的**关键字位置**上 —— 数组的 items 必须是 schema 对象,
    #    写成 "items": "string" 会被服务端直接拒:
    #    Invalid schema for function ...: "string" is not of types "boolean", "object"
    #    (2026-10-15 真机 400,微信端整轮失败)。省下的那几个 token 不值得这个风险,**整条删掉**。
    #    代价如实记:整份 catalog 多约 300 字符(≈0.8%),换"结构永远合法"。
    return out


def to_openai_tool(spec: ToolSpec) -> dict[str, Any]:
    schema = minimize_parameters(copy.deepcopy(spec.parameters_json_schema()))
    # parameters 顶层的 description 与工具自身的 description 说的是同一件事(各写一遍),
    # 只保留工具级那份 —— 少一份重复说明,语义不丢。
    schema.pop("description", None)
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": schema,
        },
    }


def build_tool_catalog(specs: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    """按名字排序 —— 顺序稳定是 KV 缓存命中的前提。"""
    return [to_openai_tool(spec) for spec in sorted(specs, key=lambda item: item.name)]


def catalog_fingerprint(catalog: Iterable[dict[str, Any]]) -> str:
    """工具清单的指纹。清单变了指纹就变 —— 用来解释"为什么这轮缓存没命中"。"""
    import hashlib
    import json

    payload = json.dumps(list(catalog), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
