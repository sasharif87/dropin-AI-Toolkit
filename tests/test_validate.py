"""Tests for validate.py — the default path that aggregates gates + findings.

Stdlib unittest only: the toolkit tests itself on bare Python with no pip
install, matching the air-gapped sovereignty constraint. Also collectable by
pytest if it happens to be present.

validate.py is an *aggregator*, so these tests build real temp repos with real
gate configs (borrowing the same shapes the per-gate suites use) and assert the
two properties the inversion turns on: the aggregate ``ok`` is the AND of the
*blocking* gates (a configured gate that fails blocks; an unconfigured gate opts
out), and the *advisory* reports (orphans/claims/findings) never flip the
verdict. The gate internals themselves are covered by test_layers/_invariants/
_golden — here we only prove they're wired together correctly.
"""

import io
import json
import os
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from validate import run_validate, print_validate
from golden import run_golden


def _write(root, rel, content=""):
    path = os.path.join(root, rel.replace("/", os.sep))
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)


# A passing invariants check module (mirrors test_invariants' shape).
_INV_OK = '''\
from invariants import Invariant, ENFORCED, GAP


def check_ok(repo):
    return []


INVARIANTS = [
    Invariant(1, "always holds", ENFORCED, check_ok),
    Invariant(2, "future work", GAP, gap_note="not built yet"),
]
'''

# A failing invariants check module (the ENFORCED check reports a problem).
_INV_FAIL = '''\
from invariants import Invariant, ENFORCED


def check_broken(repo):
    return ["this invariant is violated"]


INVARIANTS = [
    Invariant(1, "always fails", ENFORCED, check_broken),
]
'''

# A deterministic transform used by the golden gate: uppercase stdin file.
_UPPER = "import sys\nprint(open(sys.argv[1]).read().upper(), end='')\n"


class ValidateHelpersMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    # ── gate config builders ───────────────────────────────────────────
    def _layers_ok(self):
        _write(self.root, ".layers.json", json.dumps({
            "source_root": "backend",
            "top_layer": "backend.api",
            "exclude": [],
            "layer_rules": "docs/.layer_rules.json",
        }))
        _write(self.root, "backend/__init__.py")
        _write(self.root, "backend/api/__init__.py")
        _write(self.root, "backend/api/routes.py",
               "from backend.services import svc\n")
        _write(self.root, "backend/services/__init__.py")
        _write(self.root, "backend/services/svc.py", "x = 1\n")
        _write(self.root, "docs/.layer_rules.json", json.dumps(
            {"backend/api": "rules", "backend/services": "rules"}))

    def _layers_broken(self):
        """A lower layer imports the top layer — an upward-import violation."""
        self._layers_ok()
        _write(self.root, "backend/services/svc.py",
               "from backend.api import routes\n")

    def _invariants(self, content=_INV_OK):
        _write(self.root, ".invariants.py", content)

    def _golden(self, bank=False):
        _write(self.root, "transform.py", _UPPER)
        _write(self.root, "fixtures/a.txt", "hello\n")
        _write(self.root, ".golden.json", json.dumps({"cases": [{
            "name": "upper",
            "command": "{python} transform.py {input}",
            "inputs": ["fixtures/*.txt"],
        }]}))
        if bank:
            # Bank the current output as the golden so the gate later passes.
            res = run_golden(self.root, update=True)
            self.assertTrue(res["ok"], res)


class ValidateAggregateTests(ValidateHelpersMixin, unittest.TestCase):
    # ── opt-out ────────────────────────────────────────────────────────
    def test_no_config_opts_out_but_passes(self):
        result = run_validate(self.root)
        self.assertTrue(result["ok"])
        self.assertEqual(result["configured"], [])
        for g in result["gates"]:
            self.assertIsNone(g["result"]["config"])
            self.assertTrue(g["result"]["ok"])  # opt-out is a pass

    def test_gate_order_is_stable(self):
        result = run_validate(self.root)
        self.assertEqual([g["name"] for g in result["gates"]],
                         ["layers", "invariants", "golden"])

    # ── single-gate wiring ─────────────────────────────────────────────
    def test_layers_only_configured_and_green(self):
        self._layers_ok()
        result = run_validate(self.root)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["configured"], ["layers"])

    def test_failing_layers_gate_blocks(self):
        self._layers_broken()
        result = run_validate(self.root)
        self.assertFalse(result["ok"])
        failed = [g["name"] for g in result["gates"] if not g["result"]["ok"]]
        self.assertEqual(failed, ["layers"])

    def test_invariants_pass(self):
        self._invariants()
        result = run_validate(self.root)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["configured"], ["invariants"])

    def test_invariants_failure_blocks(self):
        self._invariants(_INV_FAIL)
        result = run_validate(self.root)
        self.assertFalse(result["ok"])

    def test_golden_missing_bank_blocks(self):
        self._golden(bank=False)  # no banked golden -> "new" -> fail closed
        result = run_validate(self.root)
        self.assertFalse(result["ok"])
        self.assertIn("golden", result["configured"])

    def test_golden_banked_passes(self):
        self._golden(bank=True)
        result = run_validate(self.root)
        self.assertTrue(result["ok"], result)
        self.assertIn("golden", result["configured"])

    # ── all three together ─────────────────────────────────────────────
    def test_all_three_configured_green(self):
        self._layers_ok()
        self._invariants()
        self._golden(bank=True)
        result = run_validate(self.root)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["configured"], ["layers", "invariants", "golden"])

    def test_one_failing_gate_fails_the_aggregate(self):
        self._layers_ok()
        self._invariants(_INV_FAIL)  # this one breaks
        self._golden(bank=True)
        result = run_validate(self.root)
        self.assertFalse(result["ok"])
        failed = [g["name"] for g in result["gates"] if not g["result"]["ok"]]
        self.assertEqual(failed, ["invariants"])

    # ── advisories never block ─────────────────────────────────────────
    def test_orphans_are_advisory_not_blocking(self):
        # A green gate plus a guaranteed orphan module (nothing imports it, no
        # __main__ guard). The orphan must surface but must NOT flip the verdict.
        self._layers_ok()
        _write(self.root, "stray.py", "value = 1\n")
        result = run_validate(self.root)
        orphan_files = {o["file"] for o in result["advisories"]["orphans"]}
        self.assertIn("stray.py", orphan_files)
        self.assertTrue(result["ok"], result)

    def test_claims_are_advisory_not_blocking(self):
        # A doc claims 999 tests; the repo has ~0. Contradicted, but advisory.
        _write(self.root, "README.md", "This suite has 999 tests.\n")
        _write(self.root, "tests/test_x.py",
               "def test_a():\n    pass\n")
        result = run_validate(self.root)
        self.assertTrue(result["advisories"]["claims"])
        self.assertTrue(result["ok"])  # no gate configured -> still green

    # ── findings ledger summary ────────────────────────────────────────
    def test_findings_summary_absent(self):
        result = run_validate(self.root)
        self.assertIsNone(result["advisories"]["findings"])

    def test_findings_summary_present(self):
        _write(self.root, "docs/review_findings.json", json.dumps({
            "meta": {}, "stats": {}, "coverage": {},
            "findings": [
                {"file": "a.py", "description": "bug here", "severity": "bug"},
                {"file": "b.py", "description": "style nit", "severity": "style"},
            ],
        }))
        result = run_validate(self.root)
        summary = result["advisories"]["findings"]
        self.assertIsNotNone(summary)
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["by_severity"], {"bug": 1, "style": 1})
        self.assertEqual(summary["path"], os.path.join("docs", "review_findings.json"))

    # ── explicit config paths ──────────────────────────────────────────
    def test_explicit_golden_config_path(self):
        self._golden(bank=True)
        # Move the config to a non-default name and point validate at it.
        moved = os.path.join(self.root, "custom.golden.json")
        os.rename(os.path.join(self.root, ".golden.json"), moved)
        # Without the explicit path, the gate opts out (no default config found).
        opted_out = run_validate(self.root)
        self.assertNotIn("golden", opted_out["configured"])
        # With it, the gate runs and passes.
        result = run_validate(self.root, golden_config=moved)
        self.assertIn("golden", result["configured"])
        self.assertTrue(result["ok"], result)

    def test_missing_explicit_config_fails_the_aggregate(self):
        # A typo'd explicit config path is a failing gate, not an opt-out —
        # the aggregate goes red and the gate counts as configured.
        missing = os.path.join(self.root, "typo.golden.json")
        result = run_validate(self.root, golden_config=missing)
        self.assertFalse(result["ok"])
        self.assertIn("golden", result["configured"])
        failed = [g["name"] for g in result["gates"] if not g["result"]["ok"]]
        self.assertEqual(failed, ["golden"])


class ValidatePrintTests(ValidateHelpersMixin, unittest.TestCase):
    def _render(self, result):
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_validate(result)
        return buf.getvalue()

    def test_print_no_config_calls_out_nothing_enforced(self):
        out = self._render(run_validate(self.root))
        self.assertIn("nothing is enforced", out)
        # Advisory sections still render (they're informational, always shown).
        self.assertIn("Review findings", out)

    def test_print_pass_shows_green_gates(self):
        self._layers_ok()
        out = self._render(run_validate(self.root))
        self.assertIn("Validation: PASS", out)
        self.assertIn("layers", out)

    def test_print_fail_names_the_failing_gate(self):
        self._layers_broken()
        out = self._render(run_validate(self.root))
        self.assertIn("Validation: FAIL", out)
        self.assertIn("layers", out)

    def test_print_findings_summary_line(self):
        _write(self.root, "docs/review_findings.json", json.dumps({
            "findings": [{"file": "a.py", "description": "x", "severity": "bug"}],
        }))
        out = self._render(run_validate(self.root))
        self.assertIn("Review findings: 1 on record", out)


if __name__ == "__main__":
    unittest.main()
