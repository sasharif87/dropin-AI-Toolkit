"""Tests for invariants.py — the pluggable design-invariant harness.

Stdlib unittest only: the toolkit tests itself on bare Python with no pip
install, matching the air-gapped sovereignty constraint. Also collectable by
pytest if it happens to be present.

Each case builds a synthetic project tree in a temp dir — a ``.invariants.py``
check module plus the source files its checks inspect — so the harness runs
against real files and a real import, not mocks. This mirrors the IME reference
(``_ime_ref/tests/test_invariants.py``), but generalized: there the 17 checks
are fixed; here the harness is fixed and the checks are the thing under test.
"""

import os
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from invariants import (
    run_invariants,
    load_check_module,
    function_node,
    Repo,
    Invariant,
    ENFORCED,
    GAP,
)


def _write(root, rel, content=""):
    path = os.path.join(root, rel.replace("/", os.sep))
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)


# A self-contained check module used by most end-to-end cases. It inspects
# app/service.py, which each test writes to be compliant or not.
_CHECK_MODULE = '''\
from invariants import Invariant, ENFORCED, GAP


def check_fails_closed(repo):
    body = repo.except_body_source("app/service.py", "egress")
    if not body or "return False" not in body:
        return ["egress: except-block no longer fails closed"]
    return []


def check_keeps_system(repo):
    if not repo.has_required_str_param("app/service.py", "chat", "system"):
        return ["chat(): 'system' is no longer a required parameter"]
    return []


INVARIANTS = [
    Invariant(1, "Egress fails closed", ENFORCED, check_fails_closed),
    Invariant(2, "System framing never suppressed", ENFORCED, check_keeps_system),
    Invariant(3, "Notification orchestration fail-closed", GAP,
              gap_note="domain is schema-only — no orchestrator reads it yet"),
]
'''

_COMPLIANT_SERVICE = textwrap.dedent('''\
    def egress(payload):
        try:
            return scan(payload)
        except Exception:
            return False  # fail closed


    def chat(system, prompt):
        return system + prompt
''')


class InvariantHarnessTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _module(self, content=_CHECK_MODULE):
        _write(self.root, ".invariants.py", content)

    def _service(self, content=_COMPLIANT_SERVICE):
        _write(self.root, "app/service.py", content)

    # ── happy path ─────────────────────────────────────────────────────
    def test_compliant_repo_passes(self):
        self._module()
        self._service()
        result = run_invariants(self.root)
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["passed"]), 2)
        self.assertEqual(result["failures"], [])
        self.assertEqual(len(result["gaps"]), 1)
        self.assertEqual(result["errors"], [])

    # ── enforced-check failures ────────────────────────────────────────
    def test_fail_closed_violation_flagged(self):
        self._module()
        # egress now swallows the error instead of returning False.
        self._service(_COMPLIANT_SERVICE.replace("return False  # fail closed",
                                                  "return True  # oops, opens"))
        result = run_invariants(self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["failures"]), 1)
        self.assertEqual(result["failures"][0]["id"], 1)
        self.assertTrue(any("fails closed" in p for p in result["failures"][0]["problems"]))

    def test_required_param_removed_flagged(self):
        self._module()
        self._service(_COMPLIANT_SERVICE.replace("def chat(system, prompt):",
                                                 "def chat(prompt, system=None):"))
        result = run_invariants(self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any(f["id"] == 2 for f in result["failures"]))

    def test_mixed_pass_fail_gap_counts(self):
        self._module()
        self._service(_COMPLIANT_SERVICE.replace("return False  # fail closed",
                                                  "return True"))
        result = run_invariants(self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["passed"]), 1)   # invariant 2 still holds
        self.assertEqual(len(result["failures"]), 1)  # invariant 1 broke
        self.assertEqual(len(result["gaps"]), 1)      # invariant 3 is a gap

    # ── gaps never fail the gate ───────────────────────────────────────
    def test_gap_only_registry_passes(self):
        self._module(
            "from invariants import Invariant, GAP\n"
            "INVARIANTS = [Invariant(1, 'unbuilt', GAP, gap_note='later')]\n")
        result = run_invariants(self.root)
        self.assertTrue(result["ok"])
        self.assertEqual(result["gaps"][0]["note"], "later")
        self.assertEqual(result["passed"], [])

    # ── opt-out ────────────────────────────────────────────────────────
    def test_no_config_opts_out(self):
        self._service()  # source present, but no .invariants.py
        result = run_invariants(self.root)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["config"])
        self.assertIn("nothing to check", result["note"])

    # ── fail-closed load / registry handling ───────────────────────────
    def test_syntax_error_in_module_fails(self):
        self._module("def broken(:\n")  # not valid python
        result = run_invariants(self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("could not import" in e for e in result["errors"]))

    def test_missing_invariants_list_fails(self):
        self._module("X = 1\n")  # imports fine, but no INVARIANTS
        result = run_invariants(self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("no INVARIANTS" in e for e in result["errors"]))

    def test_invariants_not_a_list_fails(self):
        self._module("INVARIANTS = 'nope'\n")
        result = run_invariants(self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("expected a list" in e for e in result["errors"]))

    def test_enforced_without_check_fails(self):
        self._module(
            "from invariants import Invariant, ENFORCED\n"
            "INVARIANTS = [Invariant(1, 'no check', ENFORCED)]\n")
        result = run_invariants(self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("no callable check" in e for e in result["errors"]))

    def test_unknown_status_fails(self):
        self._module(
            "from invariants import Invariant\n"
            "INVARIANTS = [Invariant(1, 'weird', 'MAYBE')]\n")
        result = run_invariants(self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("unknown status" in e for e in result["errors"]))

    def test_check_that_raises_is_an_error_not_silent(self):
        self._module(
            "from invariants import Invariant, ENFORCED\n"
            "def boom(repo):\n"
            "    raise RuntimeError('kaboom')\n"
            "INVARIANTS = [Invariant(1, 'explodes', ENFORCED, boom)]\n")
        result = run_invariants(self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("kaboom" in e for e in result["errors"]))

    def test_check_returning_non_list_is_an_error(self):
        self._module(
            "from invariants import Invariant, ENFORCED\n"
            "def bad(repo):\n"
            "    return 'not a list'\n"
            "INVARIANTS = [Invariant(1, 'bad return', ENFORCED, bad)]\n")
        result = run_invariants(self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("expected a list" in e for e in result["errors"]))

    def test_one_bad_entry_does_not_hide_others(self):
        # A malformed entry is surfaced, but sibling checks still run.
        self._module(
            "from invariants import Invariant, ENFORCED\n"
            "def ok(repo):\n"
            "    return []\n"
            "INVARIANTS = [\n"
            "    Invariant(1, 'unknown status', 'HUH'),\n"
            "    Invariant(2, 'good', ENFORCED, ok),\n"
            "]\n")
        result = run_invariants(self.root)
        self.assertFalse(result["ok"])          # the bad entry fails the gate
        self.assertEqual(len(result["passed"]), 1)  # but the good one still ran
        self.assertTrue(result["errors"])

    def test_explicit_config_path(self):
        self._service()
        alt = os.path.join(self.root, "checks", "custom.py")
        _write(self.root, "checks/custom.py", _CHECK_MODULE)
        result = run_invariants(self.root, config_path=alt)
        self.assertTrue(result["ok"], result)

    def test_missing_explicit_config_fails_closed(self):
        # An explicitly requested config that doesn't exist is a surfaced
        # failure — a typo'd --invariants-config must never go silently green.
        # Only auto-discovery finding nothing is an opt-out.
        missing = os.path.join(self.root, "nope.py")
        result = run_invariants(self.root, config_path=missing)
        self.assertFalse(result["ok"])
        self.assertEqual(result["config"], missing)
        self.assertTrue(any("not found" in e for e in result["errors"]))


class RepoHelperTests(unittest.TestCase):
    """The Repo helpers are the ported scaffolding — test them directly."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.repo = Repo(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_read_and_exists(self):
        _write(self.root, "a/b.py", "x = 1\n")
        self.assertTrue(self.repo.exists("a/b.py"))
        self.assertFalse(self.repo.exists("a/missing.py"))
        self.assertEqual(self.repo.read("a/b.py"), "x = 1\n")

    def test_function_source(self):
        _write(self.root, "m.py", "def foo():\n    return 1\n\ndef bar():\n    return 2\n")
        src = self.repo.function_source("m.py", "bar")
        self.assertIn("return 2", src)
        self.assertNotIn("return 1", src)
        self.assertIsNone(self.repo.function_source("m.py", "nope"))

    def test_except_body_source(self):
        _write(self.root, "m.py",
               "def f():\n"
               "    try:\n"
               "        risky()\n"
               "    except Exception:\n"
               "        return False\n")
        body = self.repo.except_body_source("m.py", "f")
        self.assertIn("return False", body)
        # a function with no try/except yields None, not a false match
        _write(self.root, "g.py", "def g():\n    return 1\n")
        self.assertIsNone(self.repo.except_body_source("g.py", "g"))

    def test_has_required_str_param(self):
        _write(self.root, "m.py",
               "def a(system, prompt):\n    pass\n"
               "def b(prompt, system=None):\n    pass\n"
               "def c(*, system):\n    pass\n"
               "def d(*, system=None):\n    pass\n")
        self.assertTrue(self.repo.has_required_str_param("m.py", "a", "system"))
        self.assertFalse(self.repo.has_required_str_param("m.py", "b", "system"))
        self.assertTrue(self.repo.has_required_str_param("m.py", "c", "system"))   # required kwonly
        self.assertFalse(self.repo.has_required_str_param("m.py", "d", "system"))  # kwonly w/ default
        self.assertFalse(self.repo.has_required_str_param("m.py", "missing", "system"))

    def test_iter_py_orders_and_skips_dunder_dirs(self):
        _write(self.root, "pkg/__init__.py")
        _write(self.root, "pkg/z.py")
        _write(self.root, "pkg/a.py")
        _write(self.root, "pkg/__pycache__/cached.py")  # must be skipped
        _write(self.root, "pkg/notes.txt")               # non-py, skipped
        rels = list(self.repo.iter_py("pkg"))
        self.assertEqual(rels, ["pkg/__init__.py", "pkg/a.py", "pkg/z.py"])

    def test_iter_lines(self):
        _write(self.root, "pkg/m.py", "line1\nline2\n")
        got = list(self.repo.iter_lines("pkg"))
        self.assertIn(("pkg/m.py", 1, "line1"), got)
        self.assertIn(("pkg/m.py", 2, "line2"), got)

    def test_function_node_pure_helper(self):
        import ast
        tree = ast.parse("async def h():\n    pass\n")
        node = function_node(tree, "h")
        self.assertIsNotNone(node)
        self.assertEqual(node.name, "h")
        self.assertIsNone(function_node(tree, "absent"))


class LoadModuleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_two_check_modules_load_independently(self):
        # Distinct files get distinct module names, so one can't shadow the other.
        _write(self.root, "one.py", "INVARIANTS = ['A']\n")
        _write(self.root, "two.py", "INVARIANTS = ['B']\n")
        m1, e1 = load_check_module(os.path.join(self.root, "one.py"))
        m2, e2 = load_check_module(os.path.join(self.root, "two.py"))
        self.assertIsNone(e1)
        self.assertIsNone(e2)
        self.assertEqual(m1.INVARIANTS, ["A"])
        self.assertEqual(m2.INVARIANTS, ["B"])


if __name__ == "__main__":
    unittest.main()
