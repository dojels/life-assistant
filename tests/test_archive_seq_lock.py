"""P1 验收:两个进程同时往同一个会话追加时,seq 不能重复/覆盖。

模拟的正是 Web 与微信两个进程共用 member:<id> 的场景。

运行:
    .venv/Scripts/python.exe -m unittest tests.test_archive_seq_lock -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from la.memory.archive import EventLog, archive_filename

PROJECT_ROOT = Path(__file__).resolve().parent.parent

CHILD = """
import sys
from pathlib import Path
from la.memory.archive import EventLog

root = Path(sys.argv[1])
count = int(sys.argv[2])
tag = sys.argv[3]
log = EventLog("race:1", root)
for i in range(count):
    log.append("user/message", {"i": i, "who": tag})
"""


class CrossProcessSeqTest(unittest.TestCase):
    def test_two_processes_get_unique_seq(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            procs = [
                subprocess.Popen(
                    [sys.executable, "-c", CHILD, str(root), "40", tag],
                    cwd=str(PROJECT_ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                for tag in ("web", "wechat")
            ]
            for proc in procs:
                out, err = proc.communicate(timeout=60)
                self.assertEqual(proc.returncode, 0, "child failed:\n" + err)
                self.assertEqual(out.strip(), "", out)

            path = root / archive_filename("race:1")
            lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            events = [json.loads(line) for line in lines]
            seqs = [int(event["seq"]) for event in events]
            self.assertEqual(len(seqs), 80)
            self.assertEqual(seqs, list(range(1, 81)))
            self.assertEqual(len(set(seqs)), 80)

            log = EventLog("race:1", root)
            self.assertEqual(log.last_seq, 80)
            self.assertEqual(len(log.read()), 80)

    def test_last_seq_sees_another_instance_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = EventLog("shared:1", root)
            self.assertEqual(first.append("session/start", {}).seq, 1)

            second = EventLog("shared:1", root)
            self.assertEqual(second.append("session/start", {}).seq, 2)

            # 第一个实例不能因为缓存停在 1
            self.assertEqual(first.last_seq, 2)
            self.assertEqual(second.last_seq, 2)


if __name__ == "__main__":
    unittest.main()
