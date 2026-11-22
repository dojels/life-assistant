"""P2 验收:跨进程的会话轮次锁。

Web 与微信是两个进程;同一个成员会话不能同时跑两轮。这里用两个子进程
模拟,断言两段临界区不重叠,并验证等锁超时。

运行:
    .venv/Scripts/python.exe -m unittest tests.test_turn_lock -v
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from la.memory.turn_lock import _ensure_byte, lock_path, turn_lock

PROJECT_ROOT = Path(__file__).resolve().parent.parent

HOLD = """
import asyncio
import sys
import time
from pathlib import Path
from la.memory.turn_lock import turn_lock

root = Path(sys.argv[1])
tag = sys.argv[2]
hold = float(sys.argv[3])

async def main():
    async with turn_lock("race", timeout=30, directory=root):
        (root / (tag + ".start")).write_text(str(time.time()))
        await asyncio.sleep(hold)
        (root / (tag + ".end")).write_text(str(time.time()))

asyncio.run(main())
"""


def _spawn(root: Path, tag: str, hold: float) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", HOLD, str(root), tag, str(hold)],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _read_float(path: Path) -> float:
    return float(path.read_text(encoding="utf-8").strip())


class TurnLockProcessTest(unittest.TestCase):
    def test_existing_byte_is_not_written_again_in_append_mode(self) -> None:
        """a+b 的 tell() 还是 0,但文件已有字节 —— 不能再写,否则并发 flush 会炸。"""
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "lock.turn"
            path.write_bytes(b"\0")
            with path.open("a+b") as handle:
                _ensure_byte(handle)
            self.assertEqual(path.stat().st_size, 1, "初始化字节只能有一个")

    def test_two_processes_do_not_overlap(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            procs = [_spawn(root, "a", 0.25), _spawn(root, "b", 0.25)]
            for proc in procs:
                out, err = proc.communicate(timeout=30)
                self.assertEqual(proc.returncode, 0, err)

            a0 = _read_float(root / "a.start")
            a1 = _read_float(root / "a.end")
            b0 = _read_float(root / "b.start")
            b1 = _read_float(root / "b.end")

            # 无重叠:后开始的那个必须在前一个结束之后(留 20ms 时钟容差)
            self.assertGreaterEqual(max(a0, b0), min(a1, b1) - 0.02)
            # 串行总时长至少是两段之和
            self.assertGreaterEqual(max(a1, b1) - min(a0, b0), 0.4)

    def test_waiting_for_the_lock_times_out(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            holder = _spawn(root, "hold", 1.2)
            try:
                deadline = time.monotonic() + 5
                while not (root / "hold.start").exists():
                    if time.monotonic() > deadline:
                        self.fail("holder 没有拿到锁")
                    time.sleep(0.02)

                async def short_wait():
                    async with turn_lock("race", timeout=0.15, directory=root):
                        pass

                started = time.monotonic()
                with self.assertRaises(TimeoutError):
                    asyncio.run(short_wait())
                self.assertLess(time.monotonic() - started, 1.0)
            finally:
                holder.communicate(timeout=20)

    def test_lock_file_name_is_safe(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = lock_path("member:1", Path(tmp))
            self.assertEqual(path.name, "member%3A1.jsonl.turn")
            self.assertNotIn(":", path.name)


if __name__ == "__main__":
    unittest.main()
