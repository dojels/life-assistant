"""R3 验收:诊断脚本能在空库上跑起来并输出 JSON。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class DiagnoseBindingsTest(unittest.TestCase):
    def test_json_output_on_empty_data(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            env = dict(os.environ)
            env["LA_DATA_DIR"] = tmp
            env["PYTHONIOENCODING"] = "utf-8"
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "diagnose_bindings.py"), "--json"],
                cwd=str(ROOT), capture_output=True, text=True,
                encoding="utf-8", errors="replace", env=env, timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertIn("members", payload)
            self.assertIn("bindings", payload)
            self.assertIn("bridge_sessions", payload)


if __name__ == "__main__":
    unittest.main()
