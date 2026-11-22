"""验收闸门(`python -m la --verify`)的护栏(D146)。

⚠️ 这个文件里最要紧的一条是 `test_every_script_is_registered`:
   34 个验收脚本散在 scripts/ 里,谁都可以新加一个,然后**没人会跑它**。
   这条测试把它变成"要么登记、要么明说它是工具" —— 否则那个新脚本会安静地烂掉。
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from la import config
from la.ops import verify

BASE_DIR = Path(__file__).resolve().parent.parent
SCRIPTS = BASE_DIR / "scripts"
#: 测试的临时产物一律建在**系统临时目录**,不污染仓库(2026-10-17 审计)。
TMP_ROOT = Path(tempfile.gettempdir()) / "la-verify-tests"


def _runner(passed: bool, seconds: float = 1.0, exit_code: int = 0,
            detail: str = "假装跑过"):
    """一个不真跑任何东西的 runner。"""
    def run(check: verify.Check, timeout_s: int) -> verify.CheckResult:
        del timeout_s
        return verify.CheckResult(check.key, check.title, check.tier, passed,
                                  skipped=(exit_code == verify.EXIT_SKIPPED),
                                  seconds=seconds, detail=detail, exit_code=exit_code)
    return run


class RegistryTestCase(unittest.TestCase):
    def test_every_script_is_registered(self) -> None:
        on_disk = {path.name for path in SCRIPTS.glob("verify_*.py")}
        registered = {Path(check.target).name for check in verify.CHECKS
                      if check.kind == "script"}
        tools = {Path(path).name for path, _what in verify.TOOLS}
        missing = on_disk - registered
        self.assertFalse(missing, "这些验收脚本没人跑(登记进 CHECKS 或 TOOLS):"
                                  + "、".join(sorted(missing)))
        all_scripts = {path.name for path in SCRIPTS.glob("*.py")}
        self.assertFalse((registered | tools) - all_scripts,
                         "登记了不存在的脚本:" + "、".join(sorted((registered | tools) - all_scripts)))

    def test_every_analysis_script_is_listed(self) -> None:
        on_disk = {path.name for path in SCRIPTS.glob("*.py")}
        known = ({Path(check.target).name for check in verify.CHECKS if check.kind == "script"}
                 | {Path(path).name for path, _ in verify.TOOLS})
        unknown = on_disk - known
        self.assertFalse(unknown, "scripts/ 里这些脚本既不是验收项、也没标成工具:"
                                  + "、".join(sorted(unknown)))

    def test_keys_are_unique_and_tiers_are_known(self) -> None:
        keys = [check.key for check in verify.CHECKS]
        self.assertEqual(len(keys), len(set(keys)), "key 会进记录与命令行,不许重名")
        for check in verify.CHECKS:
            self.assertIn(check.tier, (0, 1, 2), check.key)
            self.assertIn(check.kind, ("unit", "script", "builtin"), check.key)

    def test_t0_is_cheap_and_offline(self) -> None:
        """T0 是**每次自我更新都跑**的那一层:不许要密钥,不许要网络。"""
        t0 = [check for check in verify.CHECKS if check.tier == 0]
        self.assertTrue(t0)
        for check in t0:
            self.assertEqual("", check.needs, check.key + " 不许依赖外部服务")

    def test_t2_is_marked_as_paying(self) -> None:
        """真打模型的那些必须自己声明,否则人会在不知情的时候花钱。"""
        for check in verify.CHECKS:
            if check.tier == 2:
                self.assertTrue(check.needs, check.key + " 没写明需要什么")


class SelectionTestCase(unittest.TestCase):
    def test_tier_is_cumulative(self) -> None:
        zero = verify.select(0)
        one = verify.select(1)
        self.assertTrue(any(c.tier == 0 for c in zero))
        self.assertFalse(any(c.tier > 0 for c in zero), "T0 不该带上后面的")
        self.assertTrue(any(c.tier == 1 for c in one))
        self.assertFalse(any(c.tier == 2 for c in one))
        self.assertGreater(len(one), len(zero))

    def test_only_filters_by_key_or_title(self) -> None:
        picked = verify.select(1, "persona_page")
        self.assertEqual(["persona_page"], [c.key for c in picked])
        self.assertTrue(verify.select(1, "人格"), "标题里含这个词也要能选中")
        self.assertEqual([], verify.select(1, "根本没有这一项"))

    def test_level_parsing(self) -> None:
        self.assertEqual(0, verify._level_to_tier("0"))
        self.assertEqual(2, verify._level_to_tier("all"))
        self.assertEqual(2, verify._level_to_tier("2"))
        with self.assertRaises(ValueError):
            verify._level_to_tier("t0")


class CountParsingTestCase(unittest.TestCase):
    UNIT = verify.Check("unit", "单元测试", 0, "unit")

    def test_unit_success(self) -> None:
        out = "\n".join(["....",
                         "----------------------------------------------------------------------",
                         "Ran 1180 tests in 83.208s", "", "OK"])
        self.assertEqual((1180, 1180), verify._parse_counts(self.UNIT, out)[:2])

    def test_unit_failure_counts_both_failures_and_errors(self) -> None:
        out = "Ran 20 tests in 1.0s\n\nFAILED (failures=2, errors=1)"
        passed, total, detail = verify._parse_counts(self.UNIT, out)
        self.assertEqual((17, 20), (passed, total))
        self.assertEqual("17/20 条", detail)

    def test_script_fraction_and_total(self) -> None:
        script = verify.Check("x", "x", 1, "script", "scripts/verify_x.py")
        self.assertEqual((17, 17), verify._parse_counts(script, "结果: 17/17 项通过")[:2])
        self.assertEqual((97, 97), verify._parse_counts(script, "全部通过:97 项")[:2])
        self.assertEqual((0, 0), verify._parse_counts(script, "什么都没打印")[:2])


class RunTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TMP_ROOT / uuid.uuid4().hex[:8]
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_report_is_ok_and_recorded(self) -> None:
        report = verify.run(verify.select(0), runner=_runner(True))
        self.assertTrue(report.ok)
        path = verify.write_record(report, self.root)
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual("verify", payload["kind"])
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["items"]), len(report.items))
        self.assertIn("signature", payload, "记录里必须带版本签名,否则事后认不出这是哪一版")
        self.assertTrue(payload["items"][0]["seconds"] >= 0)

    def test_red_items_make_the_report_red(self) -> None:
        report = verify.run(verify.select(0), runner=_runner(False, detail="第 3 条断言"))
        self.assertFalse(report.ok)
        self.assertEqual(len(verify.select(0)), len(report.red))
        self.assertIn("第 3 条断言", report.render())

    def test_skipped_is_not_red(self) -> None:
        """没有密钥的机器上 T2 会"跳过" —— 跳过不算红,否则人会去把闸门关掉。"""
        skipped = verify.run(
            [verify.Check("smoke", "冒烟", 2, "script", "scripts/smoke_real_api.py")],
            runner=_runner(False, exit_code=verify.EXIT_SKIPPED, detail="没配密钥"))
        self.assertTrue(skipped.ok, "跳过的项不该判红")
        self.assertEqual(1, len(skipped.skipped))
        self.assertIn("跳过", skipped.render())

    def test_progress_is_called_once_per_item(self) -> None:
        seen: list[str] = []
        verify.run(verify.select(0), runner=_runner(True), progress=lambda r: seen.append(r.key))
        self.assertEqual([check.key for check in verify.select(0)], seen)

    def test_latest_record_reads_the_newest(self) -> None:
        report = verify.run(verify.select(0), runner=_runner(True))
        verify.write_record(report, self.root)
        newest = verify.latest_record(self.root)
        self.assertIsNotNone(newest)
        self.assertEqual(0, newest["tier"])
        self.assertIsNone(verify.latest_record(self.root / "nope"))


class CommandTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TMP_ROOT / uuid.uuid4().hex[:8]
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _run(self, **kwargs) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            # ⚠️ 每条例都用自己的锁:默认锁是全局的(%TEMP%/la-verify.lock),
            #    而"整个测试套件"本身可能正被 --verify 跑着(它持有那把锁)——
            #    不隔离的话这里会拿到"忙",这 3 条就变成假红(实测过一次)。
            code = verify.cmd_verify(
                directory=self.root,
                lock_path=self.root / "verify.lock",
                **kwargs)
        return code, buffer.getvalue()

    def test_exit_zero_when_green(self) -> None:
        code, out = self._run(level="0", runner=_runner(True))
        self.assertEqual(0, code)
        self.assertIn("可以发布", out)
        self.assertEqual(1, len(list(self.root.glob("verify-*.json"))))

    def test_exit_one_when_red(self) -> None:
        code, out = self._run(level="0", runner=_runner(False))
        self.assertEqual(1, code)
        self.assertIn("不要发布", out)

    def test_json_mode_prints_exactly_one_line(self) -> None:
        """gate.py 靠这一行读结果:多打一行就解析不到。"""
        code, out = self._run(level="0", as_json=True, runner=_runner(True))
        lines = [row for row in out.splitlines() if row.strip()]
        self.assertEqual(1, len(lines), "输出:" + out)
        payload = json.loads(lines[0])
        self.assertEqual("verify", payload["kind"])
        self.assertEqual(0, code)

    def test_dry_run_runs_nothing(self) -> None:
        code, out = self._run(level="0", dry_run=True)
        self.assertEqual(0, code)
        self.assertIn("会跑这", out)
        self.assertEqual([], list(self.root.glob("verify-*.json")), "空跑不该留记录")

    def test_bad_level_is_a_usage_error(self) -> None:
        code, out = self._run(level="t9")
        self.assertEqual(2, code)
        self.assertIn("层级", out)

    def test_list_checks_covers_all_tiers(self) -> None:
        code, out = self._run(list_checks=True)
        self.assertEqual(0, code)
        for tier in verify.TIER_TITLES:
            self.assertIn(verify.TIER_TITLES[tier], out)
        self.assertIn("tool_schema", out)
        self.assertIn("analyze_context_cost.py", out)


class TemplateCheckTestCase(unittest.TestCase):
    def test_templates_compile(self) -> None:
        """T0 的静态检查:Jinja 语法错要在这里暴露,而不是等你打开页面。"""
        check = verify.BY_KEY["templates"]
        result = verify._check_templates(check, 60)
        self.assertTrue(result.ok, result.detail)
        self.assertIn("模板", result.detail)

    def test_a_broken_template_would_be_red(self) -> None:
        original = config.BASE_DIR
        broken = TMP_ROOT / uuid.uuid4().hex[:8]
        target = broken / "la" / "web" / "templates"
        target.mkdir(parents=True)
        (target / "bad.html").write_text("{% if x %}", encoding="utf-8")
        try:
            config.BASE_DIR = broken
            result = verify._check_templates(verify.BY_KEY["templates"], 60)
        finally:
            config.BASE_DIR = original
            shutil.rmtree(broken, ignore_errors=True)
        self.assertFalse(result.ok)
        self.assertIn("TemplateSyntaxError", result.detail)


class SingleFlightTestCase(unittest.TestCase):
    """两轮验收不许同时跑 —— 实测同时跑会卡死(正常 85 秒,两个实例 30 分钟不动)。"""

    def setUp(self) -> None:
        self.root = TMP_ROOT / uuid.uuid4().hex[:8]
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = self.root / "verify.lock"

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_a_second_run_is_refused(self) -> None:
        holder = verify.verify_lock(self.lock)
        holder.__enter__()
        self.addCleanup(holder.__exit__)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = verify.cmd_verify(level="0", runner=_runner(True),
                                      directory=self.root, lock_path=self.lock)
        self.assertEqual(verify.EXIT_BUSY, code)
        self.assertIn("已经有一轮验收在跑", buffer.getvalue())
        self.assertEqual([], list(self.root.glob("verify-*.json")), "被挡下的一轮不该留记录")

    def test_a_stale_lock_is_reclaimed(self) -> None:
        self.lock.write_text("99999", encoding="utf-8")
        import os as _os
        old = time.time() - verify.LOCK_STALE_S - 60
        _os.utime(self.lock, (old, old))
        with contextlib.redirect_stdout(io.StringIO()):
            code = verify.cmd_verify(level="0", runner=_runner(True),
                                     directory=self.root, lock_path=self.lock)
        self.assertEqual(0, code)

    def test_the_lock_is_released_afterwards(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            verify.cmd_verify(level="0", runner=_runner(True),
                              directory=self.root, lock_path=self.lock)
        self.assertFalse(self.lock.exists(), "跑完必须把锁删掉,否则下一次永远拿到忙")


class CheckArgsTestCase(unittest.TestCase):
    def test_t2_persona_passes_a_label(self) -> None:
        """不带 --label 时 argparse 退 2,而 2 的含义是"跳过" —— 会静默变绿。"""
        check = verify.BY_KEY["persona"]
        self.assertIn("--label", check.args)
        self.assertIn("{stamp}", " ".join(check.args))

    def test_tail_detail_prefers_stderr_and_skips_blank_tails(self) -> None:
        detail = verify._tail_detail("Traceback...\nRuntimeError: 429 限流\n", "失败 1/4\n")
        self.assertIn("429", detail)
        self.assertEqual("", verify._tail_detail("", "  \n \n"))


if __name__ == "__main__":
    unittest.main()
