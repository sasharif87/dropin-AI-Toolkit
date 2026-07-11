"""Tests for testgate.py — the semantic guard around fix application.

The status parsing here decides whether a fix is accepted or reverted, so a
misread "pass"/"fail"/"no_tests"/"unavailable" silently lets a regression
through or blocks a good fix. subprocess.run is stubbed so the parsing logic is
exercised without a real pytest/jest on PATH. Stdlib unittest only.
"""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import testgate


class _FakeCompleted:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _Gate(testgate.TestGate):
    """A gate over a temp dir with a real tests/ directory so available()==True."""


def _make_gate(tmp, framework="pytest", make_tests_dir=True):
    if make_tests_dir:
        os.makedirs(os.path.join(tmp, "tests"), exist_ok=True)
    return testgate.TestGate(tmp, {"test_framework": framework}, tests_dir="tests")


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_framework_unavailable(self):
        gate = testgate.TestGate(self.root, {}, tests_dir="tests")
        ok, reason = gate.available()
        self.assertFalse(ok)
        self.assertIn("framework", reason)

    def test_no_tests_dir_unavailable(self):
        gate = testgate.TestGate(self.root, {"test_framework": "pytest"},
                                 tests_dir=None)
        ok, reason = gate.available()
        self.assertFalse(ok)

    def test_available_when_framework_and_dir_present(self):
        gate = _make_gate(self.root)
        ok, reason = gate.available()
        self.assertTrue(ok)
        self.assertEqual(reason, "")


class CommandConstructionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_pytest_command_defaults_to_tests_dir(self):
        gate = _make_gate(self.root, "pytest")
        cmd = gate._command(None)
        self.assertIn("pytest", cmd)
        self.assertIn("tests", cmd)

    def test_pytest_command_uses_targets(self):
        gate = _make_gate(self.root, "pytest")
        cmd = gate._command(["tests/test_x.py"])
        self.assertIn("tests/test_x.py", cmd)
        self.assertNotIn("tests", [c for c in cmd if c == "tests"])

    def test_jest_command_uses_npx(self):
        gate = _make_gate(self.root, "jest")
        cmd = gate._command(None)
        self.assertEqual(cmd[0], "npx")
        self.assertIn("jest", cmd)


class RunStatusParsingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self._orig = testgate.subprocess.run

    def tearDown(self):
        testgate.subprocess.run = self._orig
        self._tmp.cleanup()

    def _stub(self, completed=None, raises=None):
        def fake_run(*a, **k):
            if raises is not None:
                raise raises
            return completed
        testgate.subprocess.run = fake_run

    def test_pass(self):
        gate = _make_gate(self.root)
        self._stub(_FakeCompleted(0, "5 passed in 1s"))
        status, failed, _ = gate.run()
        self.assertEqual(status, "pass")
        self.assertEqual(failed, set())

    def test_fail_parses_pytest_failed_lines(self):
        gate = _make_gate(self.root)
        out = "FAILED tests/test_a.py::test_one\nERROR tests/test_b.py::test_two\n"
        self._stub(_FakeCompleted(1, out))
        status, failed, _ = gate.run()
        self.assertEqual(status, "fail")
        self.assertEqual(
            failed, {"tests/test_a.py::test_one", "tests/test_b.py::test_two"})

    def test_no_tests_status(self):
        gate = _make_gate(self.root)
        self._stub(_FakeCompleted(5, "no tests ran"))
        status, failed, _ = gate.run()
        self.assertEqual(status, "no_tests")

    def test_nonzero_without_parseable_failures(self):
        gate = _make_gate(self.root)
        self._stub(_FakeCompleted(2, "collection error"))
        status, failed, _ = gate.run()
        self.assertEqual(status, "fail")
        self.assertEqual(failed, {"<exit code 2>"})

    def test_unavailable_when_binary_missing(self):
        gate = _make_gate(self.root)
        self._stub(raises=FileNotFoundError())
        status, failed, reason = gate.run()
        self.assertEqual(status, "unavailable")

    def test_timeout_reported_as_failure(self):
        gate = _make_gate(self.root)
        self._stub(raises=subprocess.TimeoutExpired(cmd="pytest", timeout=1))
        status, failed, _ = gate.run()
        self.assertEqual(status, "fail")
        self.assertIn("<suite timeout>", failed)

    def test_jest_fail_lines_parsed(self):
        gate = _make_gate(self.root, "jest")
        self._stub(_FakeCompleted(1, "FAIL src/a.test.js\nFAIL src/b.test.js\n"))
        status, failed, _ = gate.run()
        self.assertEqual(status, "fail")
        self.assertEqual(failed, {"src/a.test.js", "src/b.test.js"})

    def test_unavailable_short_circuits_before_run(self):
        gate = testgate.TestGate(self.root, {}, tests_dir=None)
        status, failed, _ = gate.run()
        self.assertEqual(status, "unavailable")


class BaselineAndNewFailureTests(unittest.TestCase):
    def test_new_failures_excludes_baseline_reds(self):
        gate = testgate.TestGate("/x", {"test_framework": "pytest"}, tests_dir="tests")
        gate.baseline_failures = {"tests/test_a.py::test_old"}
        new = gate.new_failures(
            {"tests/test_a.py::test_old", "tests/test_b.py::test_new"})
        self.assertEqual(new, {"tests/test_b.py::test_new"})

    def test_new_failures_with_no_baseline_treats_all_as_new(self):
        gate = testgate.TestGate("/x", {"test_framework": "pytest"}, tests_dir="tests")
        self.assertEqual(gate.new_failures({"t::a"}), {"t::a"})


class AffectedTestsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        os.makedirs(os.path.join(self.root, "tests"))

    def tearDown(self):
        self._tmp.cleanup()

    def _write_test(self, name, content=""):
        with open(os.path.join(self.root, "tests", name), "w", encoding="utf-8") as fh:
            fh.write(content)

    def test_matches_by_filename_stem(self):
        self._write_test("test_payment.py", "def test_x():\n    pass\n")
        gate = testgate.TestGate(self.root, {"test_framework": "pytest"},
                                 tests_dir="tests")
        hits = gate.affected_tests("services/payment.py")
        self.assertEqual(hits, ["tests/test_payment.py"])

    def test_matches_by_import_reference(self):
        self._write_test("test_misc.py", "from services import billing\n\ndef test_x():\n    pass\n")
        gate = testgate.TestGate(self.root, {"test_framework": "pytest"},
                                 tests_dir="tests")
        hits = gate.affected_tests("services/billing.py")
        self.assertEqual(hits, ["tests/test_misc.py"])

    def test_no_match_returns_none(self):
        self._write_test("test_other.py", "import unrelated\n")
        gate = testgate.TestGate(self.root, {"test_framework": "pytest"},
                                 tests_dir="tests")
        self.assertIsNone(gate.affected_tests("services/payment.py"))


class RegexTests(unittest.TestCase):
    def test_failed_line_regex(self):
        matches = testgate._FAILED_LINE.findall(
            "FAILED tests/test_a.py::test_one - AssertionError\n")
        self.assertEqual(matches, ["tests/test_a.py::test_one"])

    def test_error_line_regex(self):
        matches = testgate._FAILED_LINE.findall("ERROR tests/test_b.py\n")
        self.assertEqual(matches, ["tests/test_b.py"])

    def test_js_fail_line_regex(self):
        matches = testgate._FAIL_LINE_JS.findall("FAIL src/x.test.ts\n")
        self.assertEqual(matches, ["src/x.test.ts"])


if __name__ == "__main__":
    unittest.main()
