"""版本签名:同一版必须签出同一个指纹(D146)。

⚠️ 这个文件里最要紧的一条是 `test_signature_is_stable`:签名一旦带时间、带随机数、
   带"当前目录","对照两份记录"就会退化成"对照两个时刻的快照" —— 那还不如不签。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
import uuid
from pathlib import Path

from la import config
from la.agent.persona import PersonaState
from la.agent.prompts import PERSONA_BODY, PERSONA_NAME
from la.ops import version

BASE_DIR = Path(__file__).resolve().parent.parent
#: 同上:临时产物不进仓库(2026-10-17 审计)。
TMP_ROOT = Path(tempfile.gettempdir()) / "la-version-tests"


class _Fixed:
    """假的 PersonaStore:state() 永远返回那一份。"""

    def __init__(self, state: PersonaState) -> None:
        self._state = state

    def state(self) -> PersonaState:
        return self._state


class VersionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TMP_ROOT / uuid.uuid4().hex[:8]
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _tree(self, name: str, files: dict[str, str]) -> Path:
        base = self.root / name
        for rel, text in files.items():
            path = base / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return base

    def test_signature_is_stable(self) -> None:
        first = version.signature()
        second = version.signature()
        self.assertEqual(first.as_dict(), second.as_dict(),
                         "同一版两次签名必须相同,否则对照没有意义")
        self.assertTrue(first.known)
        self.assertEqual(len(first.code_sha), version._SHA_CHARS)

    def test_code_fingerprint_changes_only_when_code_changes(self) -> None:
        base = self._tree("a", {"x/one.py": "A = 1\n", "two.py": "B = 2\n"})
        sha_a, files = version.code_fingerprint(base)
        self.assertEqual(files, 2)
        self.assertEqual(version.code_fingerprint(base)[0], sha_a, "内容没变就必须一样")

        (base / "two.py").write_text("B = 3\n", encoding="utf-8")
        self.assertNotEqual(version.code_fingerprint(base)[0], sha_a,
                            "改一个字节必须换指纹")

    def test_code_fingerprint_ignores_pycache(self) -> None:
        base = self._tree("b", {"one.py": "A = 1\n"})
        before = version.code_fingerprint(base)
        (base / "__pycache__").mkdir()
        (base / "__pycache__" / "one.cpython-314.pyc").write_bytes(b"junk")
        self.assertEqual(version.code_fingerprint(base), before,
                         "字节码缓存不该进指纹(否则每跑一次测试版本就变了)")

    def test_empty_tree_is_safe(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        sha, files = version.code_fingerprint(empty)
        self.assertEqual(files, 0)
        self.assertEqual(len(sha), version._SHA_CHARS, "算不出来也要给一个能存的串")

    def test_persona_change_shows_up_in_the_signature(self) -> None:
        plain = version.signature()
        custom = version.signature(
            store=_Fixed(PersonaState(body="说话慢一点,能一句说完就别写第二句。", custom=True)))
        self.assertNotEqual(plain.persona_sha, custom.persona_sha)
        self.assertNotEqual(plain.prompt_sha, custom.prompt_sha)
        self.assertFalse(plain.persona_custom)
        self.assertTrue(custom.persona_custom)
        self.assertIn("你写的", custom.render())

    def test_name_is_part_of_the_signature(self) -> None:
        plain = version.signature()
        renamed = version.signature(
            store=_Fixed(PersonaState(body=PERSONA_BODY, custom=False,
                                      name="阿豆", name_custom=True)))
        self.assertEqual(plain.persona_sha, renamed.persona_sha, "正文没变,正文指纹就不该变")
        self.assertEqual("阿豆", renamed.persona_name)
        self.assertNotEqual(plain.render(), renamed.render())

    def test_broken_store_falls_back_instead_of_raising(self) -> None:
        class Boom:
            def state(self) -> object:
                raise RuntimeError("库坏了")

        signed = version.signature(store=Boom())
        self.assertEqual(signed.persona_sha, version.signature().persona_sha)

    def test_config_fingerprint_covers_the_budgets(self) -> None:
        before = version.config_fingerprint()
        original = config.BUDGET_COMPACT_TRIGGER
        try:
            config.BUDGET_COMPACT_TRIGGER = original + 1
            self.assertNotEqual(version.config_fingerprint(), before,
                                "改了压缩触发线,版本指纹必须变")
        finally:
            config.BUDGET_COMPACT_TRIGGER = original
        self.assertEqual(version.config_fingerprint(), before)

    def test_diff_names_what_changed(self) -> None:
        a = version.Version(code_sha="111111111111", persona_sha="aaaa", persona_chars=400,
                            prompt_sha="p1", config_sha="c1", model="m1",
                            reasoning_effort="high")
        b = version.Version(code_sha="222222222222", persona_sha="bbbb", persona_chars=410,
                            prompt_sha="p2", config_sha="c2", model="m2",
                            reasoning_effort="high")
        changed = a.diff(b)
        self.assertTrue(any("代码" in row for row in changed))
        self.assertTrue(any("人格" in row for row in changed))
        self.assertTrue(any("预算" in row for row in changed))
        self.assertTrue(any("模型" in row for row in changed))
        self.assertEqual([], a.diff(a), "完全一样就不该报出变化")

    def test_unknown_signature_never_claims_a_diff(self) -> None:
        blank = version.Version()
        self.assertFalse(blank.known)
        self.assertEqual([], blank.diff(version.Version(code_sha="1", persona_sha="2")),
                         "旧记录没有签名 —— 不许凭空编出「差在哪」")

    def test_from_dict_tolerates_old_and_junk_records(self) -> None:
        self.assertFalse(version.Version.from_dict(None).known)
        self.assertFalse(version.Version.from_dict({"junk": 1}).known)
        signed = version.Version.from_dict({"code_sha": "abc", "model": "m"})
        self.assertEqual("abc", signed.code_sha)
        self.assertEqual("m", signed.model)
        self.assertEqual(PERSONA_NAME, signed.persona_name, "缺字段用默认值,不炸")


if __name__ == "__main__":
    unittest.main()
