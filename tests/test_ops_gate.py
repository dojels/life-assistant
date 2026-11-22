"""自我更新之后的自动验收:结论落盘 + 红着就挡下一次(D146)。

⚠️ 两条最要紧的:
   1. `test_red_blocks_the_next_update` —— 红了必须真的挡住,否则"拒绝发布"只是一句话;
   2. `test_a_stale_verdict_does_not_block` —— 判决书评的是**别的代码**时不许挡人,
      否则人会学会无脑点"强推",闸门等于没有。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from la import config
from la.ops import gate, verify

BASE_DIR = Path(__file__).resolve().parent.parent
#: 同上:临时产物不进仓库(2026-10-17 审计)。
TMP_ROOT = Path(tempfile.gettempdir()) / "la-gate-tests"


def _payload(ok: bool, red: list[str] | None = None, code_sha: str = "aaaa11112222") -> str:
    items = [{"key": "unit", "title": "全量单元测试", "tier": 0, "ok": ok,
              "skipped": False, "seconds": 1.0, "detail": "1180/1180 条"}]
    for key in (red or []):
        items.append({"key": key, "title": key, "tier": 0, "ok": False, "skipped": False,
                      "seconds": 1.0, "detail": "第 3 条断言"})
    return json.dumps({"kind": "verify", "tier": 0, "ok": ok,
                       "generated_at": "2026-10-18 10:00:00", "elapsed_s": 85.0,
                       "red": list(red or []), "items": items,
                       "signature": {"code_sha": code_sha}}, ensure_ascii=False)


class GateStatusTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TMP_ROOT / uuid.uuid4().hex[:8]
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "gate-latest.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_save_then_latest(self) -> None:
        status = gate.GateStatus(ok=True, ran_at="2026-10-18 10:00:00", total=2, detail="2/2 项通过")
        gate.save(status, self.path)
        again = gate.latest(self.path)
        self.assertIsNotNone(again)
        self.assertTrue(again.ok)
        self.assertEqual("2/2 项通过", again.detail)
        self.assertEqual(gate.SCHEMA, json.loads(self.path.read_text(encoding="utf-8"))["schema"])

    def test_missing_or_broken_file_is_not_an_error(self) -> None:
        self.assertIsNone(gate.latest(self.path), "没有文件 = 从没验过,不是坏消息")
        self.path.write_text("{ 不是 JSON", encoding="utf-8")
        self.assertIsNone(gate.latest(self.path), "坏文件也不该让页面打不开")

    def test_render_reads_like_a_fact_not_a_banner(self) -> None:
        good = gate.GateStatus(ok=True, ran_at="2026-10-18 10:00:00", total=2,
                               detail="2/2 项通过", duration_s=85.2)
        self.assertIn("全过", good.render())
        self.assertIn("2/2 项通过", good.render())
        red = gate.GateStatus(ok=False, red=["unit", "templates"], detail="0/2 项通过")
        self.assertIn("红了 2 项", red.render())
        broken = gate.GateStatus(error="没拿到结果(退出码 1)")
        self.assertIn("没跑成", broken.render())

    def test_status_file_path_follows_the_data_dir(self) -> None:
        """⚠️ 路径必须是**现算**的:import 期绑死会让测试读到开发机上的真状态。"""
        original = config.DATA_DIR
        try:
            config.DATA_DIR = self.root
            self.assertEqual(self.root / "eval" / "gate-latest.json", gate.status_path())
        finally:
            config.DATA_DIR = original


class BlockingTestCase(unittest.TestCase):
    RED = gate.GateStatus(ok=False, red=["unit", "templates"],
                          signature={"code_sha": "aaaa11112222"})

    def test_red_blocks_the_next_update(self) -> None:
        blocked, note = gate.should_block(self.RED, "aaaa11112222")
        self.assertTrue(blocked)
        self.assertIn("没通过验收", note)
        self.assertIn("我已经知道", note.replace("我知道,仍然保存", "我已经知道")
                      + "我知道,仍然保存")  # 提示里必须给出强推那条路

    def test_green_does_not_block(self) -> None:
        self.assertEqual((False, ""),
                         gate.should_block(gate.GateStatus(ok=True), "aaaa11112222"))

    def test_no_record_does_not_block(self) -> None:
        """从没验过 ≠ 验不过。第一次保存不该被一张不存在的判决书拦住。"""
        self.assertEqual((False, ""), gate.should_block(None, "aaaa11112222"))

    def test_a_busy_run_does_not_block(self) -> None:
        """另一轮验收正在跑:这一次既不算通过也不算红,所以不许挡人。"""
        busy = gate.GateStatus(ok=False, busy=True, detail="另一轮验收在跑")
        self.assertEqual((False, ""), gate.should_block(busy, "aaaa11112222"))
        self.assertIn("另一轮验收还在跑", busy.render())

    def test_a_failed_run_does_not_block(self) -> None:
        """验收**没跑成**(超时、崩了)不是"红了" —— 拿它挡人会挡住正确的事。"""
        self.assertEqual((False, ""),
                         gate.should_block(gate.GateStatus(error="超时"), "aaaa11112222"))

    def test_a_stale_verdict_does_not_block(self) -> None:
        blocked, note = gate.should_block(self.RED, "ffff99990000")
        self.assertFalse(blocked, "判决书评的是另一版代码,不许拿它挡人")
        self.assertIn("另一版代码", note)


class RunTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TMP_ROOT / uuid.uuid4().hex[:8]
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "gate-latest.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _patch_subprocess(self, code: int, out: str, err: str = "") -> None:
        def fake(args, timeout_s):
            del args, timeout_s
            return code, out, err, 1.0

        original = verify.run_subprocess
        verify.run_subprocess = fake  # type: ignore[assignment]
        self.addCleanup(setattr, verify, "run_subprocess", original)

    def test_green_run_is_recorded(self) -> None:
        self._patch_subprocess(0, "一些进度行\n" + _payload(True) + "\n")
        status = gate.run("人格保存", path=self.path)
        self.assertTrue(status.ok)
        self.assertEqual(1, status.total)
        self.assertEqual("1/1 项通过", status.detail)
        self.assertTrue(gate.latest(self.path).ok, "跑完必须落盘 —— 页面读的是那个文件")

    def test_red_run_is_recorded_with_the_red_keys(self) -> None:
        self._patch_subprocess(1, _payload(False, ["unit", "templates"]))
        status = gate.run("人格保存", path=self.path)
        self.assertFalse(status.ok)
        self.assertEqual(["unit", "templates"], status.red)
        self.assertIn("红了 2 项", status.render())

    def test_red_reason_is_kept_and_shown(self) -> None:
        """红了要说得清**哪儿坏了** —— 只写 key,人只会去点「强推」(2026-10-25 云端真机)。"""
        self._patch_subprocess(1, _payload(False, ["unit", "templates"]))
        status = gate.run("人格保存", path=self.path)
        self.assertIn("unit", status.red_detail)
        self.assertIn("第 3 条断言", status.red_detail)
        self.assertIn("红在:", status.render())
        blocked, note = gate.should_block(status, "aaaa11112222")
        self.assertTrue(blocked)
        self.assertIn("第 3 条断言", note, "挡住保存时必须说明原因:" + note)
        # 落盘再读回来,原因还在(页面读的是文件)
        self.assertIn("第 3 条断言", gate.latest(self.path).red_detail)

    def test_garbage_output_degrades_to_an_error(self) -> None:
        self._patch_subprocess(1, "", "Traceback: 崩了")
        status = gate.run("人格保存", path=self.path)
        self.assertFalse(status.ok)
        self.assertTrue(status.error)
        self.assertEqual((False, ""), gate.should_block(status), "跑挂了不该挡人")

    def test_start_is_single_flight(self) -> None:
        """后台只允许一轮在跑:否则连点三次保存就是三个 1180 条测试的进程。"""
        release = threading.Event()
        calls: list[str] = []

        def slow(reason: str = "", **_kwargs):
            calls.append(reason)
            release.wait(5)
            return gate.GateStatus(ok=True)

        original = gate.run
        gate.run = slow  # type: ignore[assignment]
        self.addCleanup(setattr, gate, "run", original)

        self.assertTrue(gate.start("第一次", path=self.path))
        self.assertFalse(gate.start("第二次", path=self.path), "已经在跑就不许再起一个")
        self.assertGreater(gate.running(), 0)
        release.set()
        for _ in range(200):
            if not gate.running():
                break
            threading.Event().wait(0.01)
        self.assertEqual(0, gate.running())
        self.assertEqual(["第一次"], calls)


class AutoSwitchTestCase(unittest.TestCase):
    def test_auto_is_off_by_default(self) -> None:
        """默认关:测试套件里保存一次人格不能顺手起一个 1180 条测试的进程。"""
        self.assertFalse(gate.auto_enabled())

    def test_auto_can_be_switched(self) -> None:
        gate.enable_auto(True)
        self.addCleanup(gate.enable_auto, False)
        self.assertTrue(gate.auto_enabled())


if __name__ == "__main__":
    unittest.main()