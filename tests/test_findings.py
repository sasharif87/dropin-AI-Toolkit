"""Tests for findings.py — the review/fix JSON contract.

The findings JSON is the single source of truth fix.py consumes without prose
parsing, so a silent round-trip or dedup regression corrupts the whole
review→fix pipeline. Stdlib unittest only.
"""

import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "gates"), os.path.join(_ROOT, "generation")]

import findings


class NormalizeAndHashTests(unittest.TestCase):
    def test_normalize_collapses_whitespace(self):
        self.assertEqual(findings.normalize_quote("  a\t b\n  c "), "a b c")

    def test_normalize_handles_none(self):
        self.assertEqual(findings.normalize_quote(None), "")

    def test_hash_is_stable_across_whitespace_reformatting(self):
        a = {"file": "x.py", "rule_label": "r", "quote": "foo(  bar )"}
        b = {"file": "x.py", "rule_label": "r", "quote": "foo( bar )"}
        self.assertEqual(findings.finding_hash(a), findings.finding_hash(b))

    def test_hash_differs_on_file(self):
        a = {"file": "x.py", "rule_label": "r", "quote": "q"}
        b = {"file": "y.py", "rule_label": "r", "quote": "q"}
        self.assertNotEqual(findings.finding_hash(a), findings.finding_hash(b))


class ValidateFindingTests(unittest.TestCase):
    def test_valid_finding_kept(self):
        f = findings.validate_finding({"file": "a.py", "description": "bad", "line": 3})
        self.assertEqual(f["file"], "a.py")
        self.assertEqual(f["description"], "bad")
        self.assertEqual(f["line"], 3)

    def test_default_file_used_when_missing(self):
        f = findings.validate_finding({"description": "bad"}, default_file="d.py")
        self.assertEqual(f["file"], "d.py")

    def test_missing_description_rejected(self):
        self.assertIsNone(findings.validate_finding({"file": "a.py"}))

    def test_missing_file_and_no_default_rejected(self):
        self.assertIsNone(findings.validate_finding({"description": "bad"}))

    def test_non_dict_rejected(self):
        self.assertIsNone(findings.validate_finding("not a dict"))

    def test_alternate_description_keys(self):
        for key in ("issue", "message"):
            f = findings.validate_finding({"file": "a.py", key: "text"})
            self.assertEqual(f["description"], "text")

    def test_backslash_paths_normalized(self):
        f = findings.validate_finding({"file": "a\\b\\c.py", "description": "x"})
        self.assertEqual(f["file"], "a/b/c.py")

    def test_non_int_line_dropped_not_fatal(self):
        f = findings.validate_finding({"file": "a.py", "description": "x", "line": "NaN"})
        self.assertNotIn("line", f)

    def test_confidence_coerced_to_float(self):
        f = findings.validate_finding(
            {"file": "a.py", "description": "x", "confidence": 1})
        self.assertEqual(f["confidence"], 1.0)


class DedupTests(unittest.TestCase):
    def test_identical_hash_deduped(self):
        f = {"file": "a.py", "rule_label": "r", "quote": "q", "description": "d"}
        kept, dropped = findings.dedup_findings([dict(f), dict(f)])
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, 1)

    def test_deterministic_source_wins_over_llm_same_location(self):
        det = {"file": "a.py", "line": 5, "source": "ruff",
               "rule_label": "E501", "quote": "x", "description": "line too long"}
        llm = {"file": "a.py", "line": 5, "source": "llm",
               "rule_label": "style", "quote": "y", "description": "long line"}
        kept, dropped = findings.dedup_findings([det, llm])
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["source"], "ruff")
        self.assertEqual(dropped, 1)

    def test_distinct_findings_both_kept(self):
        a = {"file": "a.py", "rule_label": "r1", "quote": "q1", "description": "d"}
        b = {"file": "b.py", "rule_label": "r2", "quote": "q2", "description": "d"}
        kept, dropped = findings.dedup_findings([a, b])
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped, 0)


class SortTests(unittest.TestCase):
    def test_severity_then_file_then_line(self):
        items = [
            {"file": "b.py", "line": 1, "severity": "style"},
            {"file": "a.py", "line": 9, "severity": "bug"},
            {"file": "a.py", "line": 2, "severity": "bug"},
        ]
        ordered = findings.sort_findings(items)
        self.assertEqual(
            [(f["file"], f["line"]) for f in ordered],
            [("a.py", 2), ("a.py", 9), ("b.py", 1)],
        )

    def test_unknown_severity_sorts_last(self):
        items = [{"file": "a.py", "severity": "mystery"},
                 {"file": "a.py", "severity": "bug"}]
        ordered = findings.sort_findings(items)
        self.assertEqual(ordered[0]["severity"], "bug")


class StorageRoundTripTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_save_then_load_preserves_findings(self):
        data_in = [{"file": "a.py", "description": "d", "severity": "bug"}]
        findings.save_findings(self.root, data_in, meta={"tool": "test"},
                               stats={"count": 1})
        loaded = findings.load_findings(self.root)
        self.assertEqual(loaded["findings"], data_in)
        self.assertEqual(loaded["meta"]["tool"], "test")
        self.assertEqual(loaded["stats"]["count"], 1)

    def test_load_missing_returns_none(self):
        self.assertIsNone(findings.load_findings(self.root))

    def test_load_invalid_json_returns_none(self):
        path = findings.findings_path(self.root)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ not valid json")
        self.assertIsNone(findings.load_findings(self.root))

    def test_update_findings_rewrites_matched_by_hash(self):
        original = {"file": "a.py", "rule_label": "r", "quote": "q",
                    "description": "d"}
        findings.save_findings(self.root, [dict(original)])
        patched = dict(original)
        patched["fix_rejected"] = "test_regression"
        self.assertTrue(findings.update_findings(self.root, [patched]))
        loaded = findings.load_findings(self.root)
        self.assertEqual(loaded["findings"][0]["fix_rejected"], "test_regression")

    def test_update_findings_without_store_returns_false(self):
        self.assertFalse(findings.update_findings(self.root, [{"file": "a.py"}]))


if __name__ == "__main__":
    unittest.main()
