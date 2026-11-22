"""全局工具 schema 体检:模型看到的参数必须和代码真正吃的一致。

为什么值得单独立一个文件(而不是塞进某个域的测试):
  * 这条纪律是**横切**的 —— 任何新工具都该满足它,不属于某一个域;
  * 它防的是一类**只在真实会话里才暴露**的 bug:工具声明了 `required=("query",)`,
    但 `params` 是空的,于是模型看到的是一个没有任何属性的对象 ——
    它只能从描述里猜参数名,猜错就是 BAD_PARAMS / HANDLER_ERROR。
    单元测试传的是正确参数,所以永远发现不了(实测:
    query_timestamp_range / query_archive_search / query_summary_pool / spawn_sub_agents 四个都这样)。
  * 还有一类同款:凡在 `params` 或 `required` 里**出现过**的参数名,
    处理函数都必须真的读它 —— 否则是"声明了却不生效",比不声明更坏。

运行:
    .venv/Scripts/python.exe -m unittest tests.test_m13_tool_schema -v
"""

from __future__ import annotations

import ast
import inspect
import os
import tempfile
import textwrap
import unittest
from pathlib import Path

from la import config
from la.agent.session import MemorySession
from la.llm.fake import FakeClient
from la.storage import db

#: handler 之间互传的**内部**键(不是给模型的参数):`html_page` 的 _create/_patch
#: 会把解析结果塞回 params 再传给 _store,那属于实现细节。
_INTERNAL_PARAMS = frozenset({"page", "old", "new", "artifact_id"})


class ToolSchemaCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self._patched = {
            key: getattr(config, key)
            for key in ("DATA_DIR", "ARCHIVE_DIR", "MEDIA_DIR", "ARTIFACTS_DIR",
                        "ASSISTANT_DB", "MEMORY_DB")
        }
        self._env = {key: os.environ.get(key) for key in ("LA_LLM_PROVIDER", "DEEPSEEK_API_KEY")}
        os.environ["LA_LLM_PROVIDER"] = "fake"
        os.environ["DEEPSEEK_API_KEY"] = ""
        config.DATA_DIR = self.tmp
        config.ARCHIVE_DIR = self.tmp / "archive"
        config.MEDIA_DIR = self.tmp / "media"
        config.ARTIFACTS_DIR = self.tmp / "artifacts"
        config.ASSISTANT_DB = self.tmp / "assistant.db"
        config.MEMORY_DB = self.tmp / "memory.db"
        config._settings = None

        self.session = MemorySession(
            FakeClient(), config.get_settings(), session_id="schema-probe",
            assistant_conn=db.connect_assistant(),
        )
        self.specs = self.session.registry.specs()

    def tearDown(self) -> None:
        try:
            self.session.assistant_conn.close()
        except Exception:
            pass
        for key, value in self._patched.items():
            setattr(config, key, value)
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config._settings = None
        self._tmp.cleanup()

    @staticmethod
    def _read_names(func) -> set[str]:
        """这个模块里**可能**被读到的 params 键(静态扫描,不执行它)。

        ⚠️ 三个坑,都是实测踩过的:
          1. `inspect.getsource` 只给**函数自己**那一段 —— 而 handler 常常只做分发,
             真正读参数的是同文件的辅助函数(`image_view` -> `_load_target`),要跟着调用走;
          2. 闭包 handler 的兄弟函数不在源码里,得退一步拿外层 `build_*` 整段
             (实测:`html_page` 的 `_patch`/`_source`/`_create`);
          3. 跟到"看不见源码"的函数只会读到它自己的局部名,那是误报 ——
             所以只跟"手上真有源码"的名字。

        ⭐ 返回的是**模块级**的读取集合(所有函数的并集),因为调用方只拿它做
        "这个声明出来的参数,在整个模块里有没有人读过"的判断 —— 并集是超集,
        宁可不报也不冤枉(见 schema_ghosts 的说明)。
        """
        tree = ToolSchemaCase._module_ast(func)
        if tree is None:
            return set()
        return TestToolSchemaCase._scan_params_keys(tree)

    @staticmethod
    def _module_ast(func) -> ast.Module | None:
        try:
            module = __import__(func.__module__, fromlist=["_"])
            return ast.parse(textwrap.dedent(inspect.getsource(module)))
        except Exception:
            return None

    @staticmethod
    def _scan_params_keys(tree: ast.Module) -> set[str]:
        """把一棵树里所有 `params["x"]` / `params.get("x")` 的键收集起来。

        ⚠️ 不往下钻嵌套函数体对**这个用途**是正确的:并集越大越保守 ——
        宁可漏报一个假参数,也不要冤枉一个真参数(实测:`html_page` 的
        `_store` 读的是自己补进去的 `page/old/new`,不是给模型的参数)。
        """
        # 认别名:有些 handler 先 `payload = params or {}` 再读 payload —— 不认它就误报
        # (实测:health_energy 的 days、quiz_make_sheet 的 limit 都是这么读的)。
        aliases = {"params"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                source = node.value
                if isinstance(source, ast.BoolOp) and isinstance(source.values[0], ast.Name):
                    source = source.values[0]
                if isinstance(source, ast.Name) and source.id in aliases:
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            aliases.add(target.id)
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
                if node.value.id in aliases:
                    sl = node.slice
                    if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                        names.add(sl.value)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                callee = node.func
                target = callee.value
                # 认 `(params or {}).get("x")`:`params or {}` 是个 BoolOp,先剥壳
                if isinstance(target, ast.BoolOp) and isinstance(target.values[0], ast.Name):
                    target = target.values[0]
                if callee.attr == "get" and isinstance(target, ast.Name):
                    if target.id in aliases and node.args:
                        first = node.args[0]
                        if isinstance(first, ast.Constant) and isinstance(first.value, str):
                            names.add(first.value)
        return names

    def schema_ghosts(self, spec) -> list[str]:
        """声明进 schema、但**整个模块都没有人读**的参数名。

        ⚠️ 这个判据刻意做得保守:它只在"全模块都没人读"时才说话。
        原因见 _scan_params_keys 的说明 —— 这条检查的目的是"别声明假参数",
        而不是"必须把所有真参数都查出来";冤枉一个真参数比漏报更坏。
        """
        tree = self._module_ast(spec.handler)
        if tree is None:
            return []
        read = self._scan_params_keys(tree)
        if not read and spec.params:
            return []          # 扫不出任何东西(动态取值)就不下结论
        declared = set(spec.parameters_json_schema().get("properties", {}))
        return sorted(declared - read)


    def test_every_required_parameter_is_visible_in_the_schema(self) -> None:
        """⛔ 声明了必填、schema 里却没这个字段 —— 模型只能靠猜(本次修的正是这个)。"""
        gaps: list[str] = []
        for spec in self.specs:
            properties = spec.parameters_json_schema().get("properties", {})
            for key in spec.required:
                if key not in properties:
                    gaps.append(spec.name + "." + key)
        self.assertEqual(gaps, [], "这些必填参数没写进 schema,模型看不到:" + ", ".join(gaps))

    def test_tool_names_are_unique_and_sorted_stable(self) -> None:
        names = [spec.name for spec in self.specs]
        self.assertEqual(len(names), len(set(names)), "工具名不能重复")
        catalog = self.session.tool_catalog()
        listed = [item["function"]["name"] for item in catalog]
        self.assertEqual(listed, sorted(listed), "工具清单必须按名字排序(KV 前缀缓存的前提)")
        self.assertEqual(set(listed), set(names))

    def test_archive_and_spawn_tools_now_advertise_their_arguments(self) -> None:
        """这四条是本次修的对象,单独钉一遍 —— 免得以后又被"简化"掉。"""
        expected = {
            # 2026-10-12 一致性审核后补齐:query_summary_pool 加上设计稿里的
            # tags / since / limit;spawn_sub_agents 加上每代理 token 上限。
            "query_summary_pool": {"query", "tags", "since", "limit"},
            "query_archive_search": {"query", "limit"},
            "query_timestamp_range": {"start", "end", "keywords", "max_chars"},
            "spawn_sub_agents": {"tasks", "max_agents", "max_tokens_per_agent"},
        }
        by_name = {spec.name: spec for spec in self.specs}
        for name, keys in expected.items():
            spec = by_name[name]
            properties = set(spec.parameters_json_schema().get("properties", {}))
            self.assertEqual(properties, keys, name + " 的参数集合变了")
            self.assertTrue(set(spec.required) <= properties, name + " 的必填没进 schema")

        spawn = by_name["spawn_sub_agents"].parameters_json_schema()
        item = spawn["properties"]["tasks"]["items"]
        self.assertEqual(item.get("required"), ["query", "start", "end"],
                         "子代理任务的必填字段必须写进 items")

    def test_no_schema_declares_a_parameter_nobody_reads(self) -> None:
        """反向抽查:声明进 schema 的参数,模块里必须真的有人读它(否则模型永远不会传)。"""
        ghosts: list[str] = []
        for spec in self.specs:
            if not spec.params and spec.model is not None:
                continue          # 领域工具走 Pydantic 模型,单真源在那边,不在这条检查里
            for key in self.schema_ghosts(spec):
                ghosts.append(spec.name + "." + key)
        self.assertEqual(
            ghosts, [],
            "这些参数写进了 schema,但处理函数一个都没读 —— 那是假参数:" + ", ".join(ghosts),
        )


if __name__ == "__main__":
    unittest.main()