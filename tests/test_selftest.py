"""C7 self-test runner, tried on small throw-away test folders (never on this suite itself)."""

from __future__ import annotations

import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from core import selftest

PASSING = '''
import os
import unittest

class Demo(unittest.TestCase):
    def test_a(self):
        pass

    def test_no_slow_fixtures(self):
        self.assertNotIn("RUN_SLOW", os.environ)      # C7 keeps F10 off

    @unittest.skip("slow")
    def test_c(self):
        pass
'''
FAILING = '''
import unittest

class Broken(unittest.TestCase):
    def test_d(self):
        self.assertEqual("master", "masters")
'''


def repo_with(**modules: str) -> tempfile.TemporaryDirectory:
    tmp = tempfile.TemporaryDirectory(prefix="aieditor-selftest-")
    tests = Path(tmp.name) / "tests"
    tests.mkdir()
    for name, source in modules.items():
        (tests / f"{name}.py").write_text(textwrap.dedent(source))
    return tmp


class SelfTestTest(unittest.TestCase):

    def test_all_passing(self):
        with repo_with(test_ok=PASSING) as tmp, mock.patch.dict(os.environ, {"RUN_SLOW": "1"}):
            self.assertEqual(selftest.count(Path(tmp)), 3)
            lines = []
            self.assertTrue(selftest.run(lines.append, repo=Path(tmp)))
        self.assertIn("running 3 unit tests", lines[0])
        self.assertEqual(lines[-1][:48], "Self-test passed: 3 tests, OK (skipped=1) in 0:0")

    def test_failure_is_shown_in_full(self):
        with repo_with(test_ok=PASSING, test_broken=FAILING) as tmp:
            lines = []
            self.assertFalse(selftest.run(lines.append, repo=Path(tmp)))
        text = "\n".join(lines)
        self.assertIn("Self-test FAILED: 4 tests, FAILED (failures=1, skipped=1)", text)
        self.assertIn("Send everything below:", text)
        self.assertIn("FAIL: test_d (test_broken.Broken", text)     # 3.10 omits ".test_d"
        self.assertIn("AssertionError: 'master' != 'masters'", text)

    def test_progress_never_claims_100_before_the_end(self):
        with repo_with(test_ok=PASSING) as tmp:
            lines = []
            selftest.run(lines.append, repo=Path(tmp))
        progress = [l for l in lines if l.endswith("%")]
        self.assertTrue(progress)
        self.assertNotIn("  100%", progress)

    def test_import_error_in_a_test_module_fails(self):
        with repo_with(test_bad="import no_such_module\n") as tmp:
            lines = []
            self.assertFalse(selftest.run(lines.append, repo=Path(tmp)))
        self.assertIn("no_such_module", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
