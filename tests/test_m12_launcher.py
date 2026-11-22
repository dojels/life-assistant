"""启动器:控制台编码加固 + 重启安全网(2026-10-13 的真实事故)。

那次事故的经过:直接 `python -m la.ops.launcher restart`(没走 launcher.cmd,
所以没有 `chcp 65001`)→ 停止阶段打印 `✓` 时抛 UnicodeEncodeError →
**微信渠道已经停了,启动阶段却没跑** → 服务全停着,命令还失败了。

两条都要守住:
  1. `harden_console()` 让任何入口都打得出 `✓`(GBK 控制台也不例外);
  2. `restart_all()` 的 try/finally 安全网:**停止阶段炸了也要把服务拉回来**。

运行:
    .venv/Scripts/python.exe -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path

from la.ops import launcher


class TestConsoleHardening(unittest.TestCase):
    def test_gbk_stream_cannot_print_checkmark(self) -> None:
        """先把"为什么需要加固"钉住:GBK 严格模式下 `✓` 是打不出来的。"""
        stream = io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict")
        with self.assertRaises(UnicodeEncodeError):
            stream.write("  " + chr(0x2713) + " 已停止")
        stream.detach()

    def test_harden_console_switches_stream_to_utf8(self) -> None:
        original = sys.stdout
        stream = io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict")
        sys.stdout = stream
        try:
            launcher.harden_console()
            self.assertEqual(str(sys.stdout.encoding).lower().replace("-", ""), "utf8")
            sys.stdout.write("  " + chr(0x2713) + " 已停止")
        finally:
            sys.stdout = original
            stream.detach()

    def test_status_runs_on_a_gbk_console(self) -> None:
        """真的跑一遍 status:加固之前在 GBK 控制台上这一步就会崩。"""
        original_out, original_err = sys.stdout, sys.stderr
        buffer = io.BytesIO()
        out = io.TextIOWrapper(buffer, encoding="gbk", errors="strict")
        err = io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict")
        sys.stdout, sys.stderr = out, err
        try:
            code = launcher.main(["status"])
            out.flush()
            text = buffer.getvalue().decode("utf-8", "replace")
        finally:
            sys.stdout, sys.stderr = original_out, original_err
            out.detach()   # 不 detach 的话,GC 会把底层 BytesIO 一起关掉
            err.detach()
        self.assertEqual(code, 0)
        self.assertIn("服务状态", text)


class TestRestartSafetyNet(unittest.TestCase):
    def setUp(self) -> None:
        self._original = (launcher.start, launcher.stop, launcher.status)
        # ⚠️ restart_all 会打印 ⚠/✓;测试进程的 stdout 是 GBK 控制台,
        #    不接管的话**测试自己**就会撞上那个 UnicodeEncodeError。
        self._stdout = sys.stdout
        self.output = io.StringIO()
        sys.stdout = self.output

    def tearDown(self) -> None:
        launcher.start, launcher.stop, launcher.status = self._original
        sys.stdout = self._stdout

    def test_services_are_restarted_even_if_stop_explodes(self) -> None:
        started: list[str] = []

        def fake_start(service, **kwargs):  # noqa: ANN001, ANN003
            started.append(service.key)
            return True

        def exploding_stop(service, **kwargs):  # noqa: ANN001, ANN003
            if service.key == "web":     # 停止顺序是 reversed(ORDER):先 wechat,后 web
                raise UnicodeEncodeError("gbk", chr(0x2713), 0, 1, "boom")
            return True

        launcher.start = fake_start
        launcher.stop = exploding_stop
        launcher.status = lambda **kwargs: {}  # noqa: ARG005

        ok = launcher.restart_all()

        self.assertFalse(ok, "停止阶段出过错,返回值必须是 False")
        self.assertEqual(sorted(started), sorted(launcher.SERVICES),
                         "已经停下来的服务必须被重新拉起")
        self.assertIn("停止阶段出过错", self.output.getvalue())
        self.assertIn("已经重新拉起", self.output.getvalue())

    def test_keyboard_interrupt_during_stop_still_restarts(self) -> None:
        started: list[str] = []

        def fake_start(service, **kwargs):  # noqa: ANN001, ANN003
            started.append(service.key)
            return True

        def interrupted_stop(service, **kwargs):  # noqa: ANN001, ANN003
            raise KeyboardInterrupt

        launcher.start = fake_start
        launcher.stop = interrupted_stop
        launcher.status = lambda **kwargs: {}  # noqa: ARG005

        # ⚠️ KeyboardInterrupt 是 BaseException:安全网先接住它把服务拉回来,
        #    然后**照旧抛回去**(Ctrl+C 的语义不能吞)。
        with self.assertRaises(KeyboardInterrupt):
            launcher.restart_all()
        self.assertEqual(sorted(started), sorted(launcher.SERVICES))

    def test_success_path_returns_true(self) -> None:
        started: list[str] = []
        launcher.start = lambda service, **kwargs: (started.append(service.key), True)[1]
        launcher.stop = lambda service, **kwargs: True
        launcher.status = lambda **kwargs: {}
        self.assertTrue(launcher.restart_all())
        self.assertEqual(sorted(started), sorted(launcher.SERVICES))


class TestWechatAccountDiscovery(unittest.TestCase):
    """一个启动器要能一次管多个微信号,否则爸妈就得各开一个终端。"""

    def test_discovers_logged_in_accounts_and_skips_unfinished_ones(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "wechat"
            accounts = base / "accounts"
            (accounts / "mom").mkdir(parents=True)
            (accounts / "mom" / "credentials.json").write_text("{}", encoding="utf-8")
            (accounts / "dad").mkdir()                    # 还没扫码
            (accounts / "empty").mkdir()
            (accounts / "empty" / "credentials.json").write_text("", encoding="utf-8")
            (accounts / "bad name").mkdir()
            (accounts / "bad name" / "credentials.json").write_text("{}", encoding="utf-8")

            found = launcher.wechat_services(base)

            self.assertIn("wechat", found, "默认账号必须保留")
            self.assertIn("wechat-mom", found)
            self.assertEqual(found["wechat-mom"].args, ["--account", "mom"])
            self.assertNotIn("wechat-dad", found, "没扫码的目录不能拉起空等二维码的进程")
            self.assertNotIn("wechat-empty", found, "空凭据文件不算已登录")
            self.assertNotIn("wechat-bad name", found, "账号名必须先过路径安全")

    def test_new_account_appears_without_code_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "wechat"
            (base / "accounts" / "dad").mkdir(parents=True)
            (base / "accounts" / "dad" / "credentials.json").write_text("{}", encoding="utf-8")
            found = launcher.wechat_services(base)
            self.assertIn("wechat-dad", found)
            self.assertEqual(found["wechat-dad"].title, "微信渠道 · dad")


class TestPidIdentity(unittest.TestCase):
    """2026-10-15 的真实事故:PID 被系统回收,服务再也起不来。

    现象:双击 launcher 后微信起来了、Web 没有,`status` 还显示"PID 5188"。
    成因:`is_running()` 只查"这个 PID 有没有进程",而 5188 早已变成 spoolsv.exe ——
    查到别的程序活着,就认为我们**已在运行**,`start` 于是跳过重启,端口一直不通。
    修法:连**镜像名**一起看(只可能是 python / pythonw)。
    """

    @unittest.skipUnless(
        sys.platform == "win32",
        "launcher 是 Windows 启动器(计划任务/tasklist);POSIX 上进程存活由 systemd 管",
    )
    def test_our_own_pid_is_recognised(self) -> None:
        import os

        pid = os.getpid()
        self.assertTrue(launcher.is_running(pid), "测试进程自己就是 python,必须认得出来")
        self.assertIn(launcher._image_of(pid), ("python", "pythonw"))

    def test_a_dead_pid_is_not_running(self) -> None:
        self.assertFalse(launcher.is_running(0))
        self.assertFalse(launcher.is_running(-1))
        self.assertFalse(launcher.is_running(999_999_999))

    @unittest.skipUnless(sys.platform == "win32", "镜像名检查只在 Windows 上有意义")
    def test_a_recycled_pid_belonging_to_another_program_is_not_us(self) -> None:
        """找一个**不是 python** 的进程,把它当成 PID 文件内容 —— 必须判 False。"""
        import subprocess

        from unittest import mock

        # ⚠️ 不真的去跑 tasklist 找"别人的进程":一是慢,二是测试环境里管道输出可能拿不到。
        #    这里直接喂一段**真实格式**的 tasklist CSV(引号包裹、GBK 中文可替换),
        #    测的是"解析 + 判定"这条逻辑本身 —— 正是 2026-10-15 那次事故出问题的地方。
        fake = (
            chr(34) + "spoolsv.exe" + chr(34) + "," + chr(34) + "5188" + chr(34)
            + "," + chr(34) + "Services" + chr(34) + "," + chr(34) + "0" + chr(34)
            + "," + chr(34) + "15,008 K" + chr(34)
        )
        # ⚠️ 必须用字符串目标:`mock.patch(模块对象, "属性")` 不接受对象形式。
        with mock.patch("la.ops.launcher.subprocess.run") as run:
            run.return_value.stdout = fake
            self.assertEqual(launcher._image_of(5188), "spoolsv")
            self.assertFalse(
                launcher.is_running(5188),
                "别人的进程被当成了我们的服务 —— 那正是 2026-10-15 那次事故",
            )
        with mock.patch("la.ops.launcher.subprocess.run") as run:
            run.return_value.stdout = (
                chr(34) + "pythonw.exe" + chr(34) + "," + chr(34) + "10828" + chr(34)
                + "," + chr(34) + "Console" + chr(34) + "," + chr(34) + "0" + chr(34)
                + "," + chr(34) + "99,000 K" + chr(34)
            )
            self.assertEqual(launcher._image_of(10828), "pythonw")
            self.assertTrue(launcher.is_running(10828))

    def test_pid_file_with_a_recycled_pid_is_not_alive(self) -> None:
        """整条链路:alive() 也要判 False,否则 start() 会跳过重启。"""
        service = launcher.SERVICES["web"]
        original = launcher.read_pid
        try:
            launcher.read_pid = lambda svc: 4       # PID 4 = Windows 的 System
            running, pid = launcher.alive(service)
        finally:
            launcher.read_pid = original
        self.assertFalse(running, "PID 4 不是我们的进程,alive() 必须说 False")


if __name__ == "__main__":
    unittest.main()

