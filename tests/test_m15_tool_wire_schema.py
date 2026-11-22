# -*- coding: utf-8 -*-
"""wire schema 体检:发给模型的那份参数 schema,必须"最小但不丢语义"。

为什么值得单独立一个文件:
  * 我们**有两份** schema —— ToolSpec.parameters_json_schema() 是单真源(给 REST 与
    内部校验用),真正发到 wire 上的是 la/llm/tools.py 里 minimize_parameters() 的产物。
    两者一旦悄悄跑偏(少个参数、丢条说明),模型就开始猜,BAD_PARAMS 就来了;
  * 这份 wire 文本还背着**前缀缓存**的纪律:wire 顺序是 system -> tools -> 历史,
    所以它必须在会话内**逐字节稳定** —— 抖动一次,后面的历史全部失去复用。

运行:
    .venv/Scripts/python.exe -m unittest tests.test_m15_tool_wire_schema -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from la.domain.audit import AuditSink
from la.domain.services.ledger_service import LedgerService
from la.domain.tools import build_ledger_tools
from la.llm.tools import build_tool_catalog, catalog_fingerprint, minimize_parameters, to_openai_tool
from la.storage import db
from la.storage.migrations import MIGRATIONS_ASSISTANT


def _iter_schemas(node):
    """深度遍历一个 schema,产出所有 dict 节点(用来全局搜某类键)。"""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _iter_schemas(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_schemas(item)


class ToolWireSchemaCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.assistant = db.connect(self.tmp / "assistant.db")
        db.migrate(self.assistant, MIGRATIONS_ASSISTANT)
        self.service = LedgerService(self.assistant, audit=AuditSink(self.assistant))
        self.specs = build_ledger_tools(self.service)
        self.catalog = build_tool_catalog(self.specs)

    def tearDown(self) -> None:
        try:
            self.assistant.close()
        except Exception:
            pass
        self._tmp.cleanup()

    # -- 体积 ---------------------------------------------------------------
    def test_wire_schema_is_smaller_than_the_raw_single_source(self) -> None:
        """最小化必须真的省下字节 —— 否则这份复杂度白背。"""
        raw = sum(len(json.dumps(s.parameters_json_schema(), ensure_ascii=False)) for s in self.specs)
        wire = sum(len(json.dumps(t["function"]["parameters"], ensure_ascii=False)) for t in self.catalog)
        self.assertLess(wire, raw, "wire 参数 schema 没有比原始小,minimize_parameters 失效了")
        self.assertGreater((raw - wire) / raw, 0.05, "省得还不到 5%,八成是递归里漏了某一层")

    # -- 不丢语义 -----------------------------------------------------------
    def test_wire_keeps_every_parameter_name_required_and_field_description(self) -> None:
        """最小化只许动"写法",不许动**参数名 / 必填 / 字段说明**。"""
        by_name = {t["function"]["name"]: t for t in self.catalog}
        for spec in self.specs:
            raw = spec.parameters_json_schema()
            wire = by_name[spec.name]["function"]["parameters"]
            self.assertEqual(set(raw.get("properties") or {}), set(wire.get("properties") or {}),
                             spec.name + ": 参数名变了")
            self.assertEqual(list(raw.get("required") or []), list(wire.get("required") or []),
                             spec.name + ": required 变了")
            for prop, source in (raw.get("properties") or {}).items():
                target = wire["properties"][prop]
                if not isinstance(target, dict):
                    continue      # 塌成简写的单类型字段,没有说明可丢
                if "description" in source:
                    self.assertIn("description", target,
                                  spec.name + "." + prop + ": 字段说明被压掉了")

    def test_wire_drops_html_only_keys_but_keeps_nested_parameter_names(self) -> None:
        """title/examples 这类**只给人看**的键一律不进 wire;参数名当然要留。"""
        for item in self.catalog:
            schema = item["function"]["parameters"]
            for node in _iter_schemas(schema):
                for key in node:
                    self.assertNotIn(key, ("title", "examples", "example"),
                                     item["function"]["name"] + ": wire 上出现了 " + key)
            self.assertIn("properties", schema, item["function"]["name"] + ": 参数结构被压没了")

    def test_optional_parameters_collapse_to_a_union_type(self) -> None:
        """anyOf:[X, null] 必须压成 type:[X, "null"] —— 这是省字节的主力。"""
        record = to_openai_tool(next(s for s in self.specs if s.name == "expense_record"))
        payee = record["function"]["parameters"]["properties"]["payee"]
        self.assertNotIn("anyOf", payee, "可选参数没塌成联合类型")
        self.assertEqual(payee.get("type"), ["string", "null"])
        self.assertIn("description", payee, "塌了之后字段说明不能丢")

    # -- 缓存纪律 -----------------------------------------------------------
    def test_wire_bytes_are_stable_across_repeated_builds(self) -> None:
        """会话内**逐字节稳定** —— 这是前缀缓存不被击穿的前提。"""
        first = json.dumps(build_tool_catalog(self.specs), ensure_ascii=False, sort_keys=True)
        second = json.dumps(build_tool_catalog(self.specs), ensure_ascii=False, sort_keys=True)
        self.assertEqual(first, second, "同一份 spec 两次装配出了不同的字节")
        self.assertEqual(catalog_fingerprint(self.catalog),
                         catalog_fingerprint(build_tool_catalog(self.specs)))

    def test_minimizer_does_not_mutate_the_input(self) -> None:
        """原始 schema 是单真源,REST 侧还要用 —— 最小化只许读,不许改。"""
        for spec in self.specs:
            before = json.dumps(spec.parameters_json_schema(), ensure_ascii=False, sort_keys=True)
            to_openai_tool(spec)
            after = json.dumps(spec.parameters_json_schema(), ensure_ascii=False, sort_keys=True)
            self.assertEqual(before, after, spec.name + ": 最小化改到了原始 schema")

    # -- 健壮性 -------------------------------------------------------------
    def test_minimizer_keeps_unknown_constraints_verbatim(self) -> None:
        """压不动的东西宁可原样留着(枚举、$ref、长度约束一个都不许丢)。"""
        node = {
            "type": "object",
            "title": "T",
            "properties": {
                "kind": {"anyOf": [{"enum": ["a", "b"], "type": "string"}, {"type": "null"}]},
                "ref": {"$ref": "#/$defs/X"},
                "bounded": {"minLength": 1, "maxLength": 5, "type": "string"},
            },
            "required": ["kind"],
        }
        out = minimize_parameters(node)
        self.assertEqual(out["properties"]["kind"]["type"], ["string", "null"])
        self.assertIn("enum", out["properties"]["kind"])
        self.assertEqual(out["properties"]["ref"], {"$ref": "#/$defs/X"})
        self.assertEqual(out["properties"]["bounded"]["maxLength"], 5)
        self.assertEqual(out["required"], ["kind"])
        self.assertNotIn("title", out)

    # -- 结构合法性(2026-10-15 真机 400 的回归测试)-------------------------
    #: JSON Schema 里"值必须是 schema 对象"的关键字。
    #: ⚠️ 这条清单来自一次真机事故:最小化器曾把 {"type": "string"} 塌成裸 "string",
    #:    连 `items` 也一起塌了 → 服务端 400:
    #:    Invalid schema for function ...: "string" is not of types "boolean", "object"
    #:    (微信端整轮失败)。离线 fake 测不到这种错,只能靠**结构检查**。
    SCHEMA_KEYWORDS = ("items", "additionalProperties", "additionalItems", "contains",
                       "not", "propertyNames", "if", "then", "else", "unevaluatedItems",
                       "unevaluatedProperties")
    # ⚠️ `required` 不在此列:它是**属性名数组**,不是 schema 数组。
    LIST_KEYWORDS = ("anyOf", "oneOf", "allOf", "prefixItems")

    def _schema_positions(self, schema, path: str = "", inside=False):
        """产出所有"值应当是一个 schema 对象"的位置,以及它们实际拿到的东西。"""
        if not isinstance(schema, dict):
            if inside:
                yield path, schema
            return
        for key, value in schema.items():
            here = path + "/" + key
            if key in self.SCHEMA_KEYWORDS:
                # items / additionalProperties 的值必须是 schema 对象(或布尔)。
                if isinstance(value, dict):
                    yield from self._schema_positions(value, here + "/", True)
                else:
                    yield here, value
            elif key in self.LIST_KEYWORDS:
                if isinstance(value, list):
                    for index, item in enumerate(value):
                        if isinstance(item, dict):
                            yield from self._schema_positions(item, here + "/" + str(index), True)
                        else:
                            yield here + "/" + str(index), item
                else:
                    yield here, value
            elif key == "properties":
                for prop, sub in (value or {}).items():
                    if isinstance(sub, dict):
                        yield from self._schema_positions(sub, here + "/" + prop, True)
                    else:
                        yield here + "/" + prop, sub
            elif key in ("type", "enum", "const", "default", "examples", "description",
                         "title", "format", "pattern", "minimum", "maximum",
                         "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength",
                         "minItems", "maxItems", "multipleOf", "minProperties",
                         "maxProperties", "$ref", "$defs"):
                continue
            else:
                yield from self._schema_positions(value, here + "/", False)

    def test_every_wire_schema_is_structurally_valid(self) -> None:
        """在 schema 位置上,值必须是对象 —— 不许是裸字符串/裸数字。

        这是**离线**能把 2026-10-15 那次真机 400 拦下来的唯一办法。
        """
        bad: list[str] = []
        for item in self.catalog:
            name = item["function"]["name"]
            for path, value in self._schema_positions(item["function"]["parameters"]):
                if not isinstance(value, (dict, bool)):
                    bad.append(name + path + " = " + repr(value))
        self.assertEqual(bad, [], "这些位置的值不是 schema 对象(服务端会拒):" + "; ".join(bad[:8]))

    def test_items_is_always_a_schema_object(self) -> None:
        """点名钉住最常被踩的那个:`items`。"""
        for item in self.catalog:
            for node in _iter_schemas(item["function"]["parameters"]):
                if "items" in node:
                    self.assertIsInstance(
                        node["items"], dict,
                        item["function"]["name"] + " 的 items 必须是 schema 对象(不能是裸类型)",
                    )

if __name__ == "__main__":
    unittest.main()
