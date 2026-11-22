"""对照报告的五行结论(D146)—— **不许求和,只许逐项判**。

⚠️ 最要紧的一条是 `test_one_number_never_hides_a_break`:
   "字数少了 40%(好)" + "开始编造金额(坏)" 平均起来可能是个漂亮分。
   这里钉住的是:坏的那一条必须**单独**把结论压成"不要发布"。
"""

from __future__ import annotations

import unittest

from la.ops import eval_delta


def _record(case_id: str, checks: dict[str, tuple[bool, bool]], chars: int = 100,
            max_chars: int | None = None, usage: dict[str, int] | None = None) -> dict:
    scan = {"chars": chars}
    if max_chars is not None:
        scan["max_chars"] = max_chars
    return {
        "id": case_id,
        "category": "精度",
        "note": "测试用",
        "prompt": "记一笔:午饭 38 元",
        "reply": "记好了。",
        "scan": scan,
        "probe": {},
        "usage": usage or {},
        "checks": {name: {"ok": ok, "detail": name, "hard": hard}
                   for name, (ok, hard) in checks.items()},
        "hard_failed": [name for name, (ok, hard) in checks.items() if hard and not ok],
    }


def _run(label: str, records: list[dict], signature: dict | None = None,
         elapsed: float = 60.0) -> dict:
    return {"label": label, "created_at": "2026-10-18 10:00:00", "elapsed_s": elapsed,
            "model": "deepseek-flash", "fingerprint": {}, "records": records,
            "signature": signature or {}}


SIGNED_A = {"code_sha": "aaaa11112222", "persona_sha": "p1", "persona_chars": 400,
            "prompt_sha": "s1", "config_sha": "c1", "model": "deepseek-flash",
            "reasoning_effort": "high"}
SIGNED_B = {"code_sha": "aaaa11112222", "persona_sha": "p2", "persona_chars": 500,
            "prompt_sha": "s2", "config_sha": "c1", "model": "deepseek-flash",
            "reasoning_effort": "high"}


class DeltaTestCase(unittest.TestCase):
    def test_no_change_is_publishable(self) -> None:
        checks = {"no_fabricated_money": (True, True), "no_sycophancy": (True, True)}
        delta = eval_delta.diff(_run("a", [_record("P01", checks)]),
                                _run("b", [_record("P01", checks)]))
        self.assertEqual(1, delta.cases)
        self.assertEqual((2, 2), (delta.hard_ok, delta.hard_total))
        self.assertEqual([], delta.broke)
        self.assertTrue(delta.publishable)
        self.assertIn("可以发布", delta.verdict)

    def test_one_number_never_hides_a_break(self) -> None:
        """字数大幅下降(本该是好消息)不能把"开始编金额"盖过去。"""
        before = [_record("P01", {"no_fabricated_money": (True, True)}, chars=400)]
        after = [_record("P01", {"no_fabricated_money": (False, True)}, chars=80)]
        delta = eval_delta.diff(_run("a", before), _run("b", after))
        self.assertEqual(1, len(delta.broke))
        self.assertEqual(1, len(delta.veto), "编造金额是**否决项**,不是普通退化")
        self.assertFalse(delta.publishable)
        self.assertIn("不要发布", delta.verdict)
        self.assertIn("否决项", delta.render()[3])
        # 字数那一行照样说真话(少了 80%),但结论不由它决定
        self.assertIn("-80.0%", delta.render()[1])

    def test_ordinary_regression_asks_for_eyes_not_a_veto(self) -> None:
        before = [_record("P01", {"no_kaomoji": (True, True)}, chars=100)]
        after = [_record("P01", {"no_kaomoji": (False, True)}, chars=100)]
        delta = eval_delta.diff(_run("a", before), _run("b", after))
        self.assertEqual([], delta.veto)
        self.assertFalse(delta.publishable)
        self.assertIn("先别发布", delta.verdict)

    def test_fixing_something_is_reported(self) -> None:
        before = [_record("P01", {"brief": (False, True)})]
        after = [_record("P01", {"brief": (True, True)})]
        delta = eval_delta.diff(_run("a", before), _run("b", after))
        self.assertEqual(1, len(delta.fixed))
        self.assertEqual([], delta.broke)
        self.assertTrue(delta.publishable)
        self.assertIn("修好了 1 处", delta.verdict)

    def test_still_red_is_not_a_regression(self) -> None:
        """基线就红着的那条,不算这一版弄坏的 —— 否则报告会一直骂同一件事。"""
        checks = {"no_kaomoji": (False, True)}
        delta = eval_delta.diff(_run("a", [_record("P01", checks)]),
                                _run("b", [_record("P01", checks)]))
        self.assertEqual([], delta.broke)
        self.assertEqual(["P01"], delta.still_red)
        self.assertEqual(["P01"], delta.red_cases)

    def test_chars_and_over_limit(self) -> None:
        before = [_record("P01", {}, chars=100), _record("P02", {}, chars=300)]
        after = [_record("P01", {}, chars=100, max_chars=50),
                 _record("P02", {}, chars=300, max_chars=500)]
        delta = eval_delta.diff(_run("a", before), _run("b", after))
        self.assertEqual(200.0, delta.chars_after)
        self.assertEqual(1, delta.over_limit, "只有 P01 超了它自己的上限")
        self.assertIn("超上限 1 条", delta.render()[1])

    def test_cost_line_uses_real_usage(self) -> None:
        before = [_record("P01", {}, usage={"prompt": 1000, "cached": 500, "calls": 1})]
        after = [_record("P01", {}, usage={"prompt": 2000, "cached": 1700, "calls": 2})]
        delta = eval_delta.diff(_run("a", before), _run("b", after))
        self.assertEqual(2000.0, delta.prompt_after)
        self.assertEqual(0.85, delta.cache_rate)
        self.assertIn("2.0k", delta.render()[2])
        self.assertIn("85%", delta.render()[2])

    def test_old_records_without_usage_say_so(self) -> None:
        delta = eval_delta.diff(_run("a", [_record("P01", {})]),
                                _run("b", [_record("P01", {})]))
        self.assertEqual(0, delta.usage_cases)
        self.assertIn("没存 token 账", delta.render()[2])

    def test_signature_says_what_changed(self) -> None:
        delta = eval_delta.diff(_run("a", [_record("P01", {})], SIGNED_A),
                                _run("b", [_record("P01", {})], SIGNED_B))
        self.assertTrue(delta.signatures_known)
        self.assertTrue(any("人格" in row for row in delta.signature_changed))
        self.assertIn("变的是", delta.render_changes())

    def test_identical_signatures_mean_the_run_measured_noise(self) -> None:
        delta = eval_delta.diff(_run("a", [_record("P01", {})], SIGNED_A),
                                _run("b", [_record("P01", {})], SIGNED_A))
        self.assertEqual([], delta.signature_changed)
        self.assertIn("随机性", delta.render_changes())

    def test_unsigned_records_do_not_pretend_to_know(self) -> None:
        delta = eval_delta.diff(_run("a", [_record("P01", {})]),
                                _run("b", [_record("P01", {})]))
        self.assertFalse(delta.signatures_known)
        self.assertIn("没有版本签名", delta.render_changes())

    def test_five_lines_with_fixed_labels(self) -> None:
        delta = eval_delta.diff(_run("a", [_record("P01", {})]),
                                _run("b", [_record("P01", {})]))
        lines = delta.render()
        self.assertEqual(5, len(lines), "多一行少一行都不行:五行是给人扫的")
        for lead, line in zip(("正确性", "字数", "成本", "否决项", "结论"), lines):
            self.assertTrue(line.startswith(lead), line)

    def test_no_common_cases_is_not_a_verdict(self) -> None:
        delta = eval_delta.diff(_run("a", [_record("P01", {})]),
                                _run("b", [_record("P99", {})]))
        self.assertEqual(0, delta.cases)
        self.assertFalse(delta.publishable, "没有共同用例时不许说「可以发布」")
        self.assertIn("没法比", delta.verdict)

    def test_new_checks_are_not_called_regressions(self) -> None:
        """口径改了(这一版多出一项检查)不算退化 —— 那只是这一项以前没判。"""
        before = [_record("P01", {"no_kaomoji": (True, True)})]
        after = [_record("P01", {"no_kaomoji": (True, True), "brief": (False, True)})]
        delta = eval_delta.diff(_run("a", before), _run("b", after))
        self.assertEqual([], delta.broke)
        self.assertEqual(["P01"], delta.red_cases, "但它确实红着,要看得见")


if __name__ == "__main__":
    unittest.main()
