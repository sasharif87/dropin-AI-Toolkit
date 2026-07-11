"""Tests for claims.py — the doc-claim checker (re-derive, then flag divergence).

Stdlib unittest only: the toolkit tests itself on bare Python with no pip
install, matching the air-gapped sovereignty constraint. Also collectable by
pytest if it happens to be present.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claims import (
    extract_claims, derive_test_count, derive_loc, find_claim_issues,
    _count_test_methods, _significant, _to_int,
)


def _write(root, rel, content=""):
    path = os.path.join(root, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


class DeriveTestCountTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_counts_methods_across_files(self):
        _write(self.root, "tests/test_a.py",
               "import unittest\n"
               "class T(unittest.TestCase):\n"
               "    def test_one(self): pass\n"
               "    def test_two(self): pass\n")
        _write(self.root, "tests/test_b.py",
               "class S(unittest.TestCase):\n"
               "    def test_three(self): pass\n")
        self.assertEqual(derive_test_count(self.root), 3)

    def test_duplicate_method_name_counts_once(self):
        # A redefined method runs once — the runner sees one, so do we.
        _write(self.root, "tests/test_dup.py",
               "class T(unittest.TestCase):\n"
               "    def test_x(self): pass\n"
               "    def test_x(self): pass\n")
        self.assertEqual(derive_test_count(self.root), 1)

    def test_counts_module_level_functions(self):
        _write(self.root, "tests/test_mod.py",
               "def test_alpha(): pass\n"
               "def test_beta(): pass\n"
               "def helper(): pass\n")
        self.assertEqual(derive_test_count(self.root), 2)

    def test_custom_base_class_still_counted(self):
        # Recognising only unittest.TestCase would undercount custom bases and
        # then falsely flag an honest claim — any test-prefixed method counts.
        _write(self.root, "tests/test_custom.py",
               "class T(MyBaseCase):\n"
               "    def test_one(self): pass\n"
               "    def test_two(self): pass\n")
        self.assertEqual(derive_test_count(self.root), 2)

    def test_no_test_files_returns_none(self):
        _write(self.root, "app.py", "x = 1\n")
        self.assertIsNone(derive_test_count(self.root))

    def test_only_test_prefixed_files_scanned(self):
        # A non-test file with a test_ method must not inflate the count.
        _write(self.root, "helpers.py",
               "class T:\n    def test_ignored(self): pass\n")
        _write(self.root, "tests/test_real.py",
               "class T(unittest.TestCase):\n    def test_one(self): pass\n")
        self.assertEqual(derive_test_count(self.root), 1)

    def test_syntax_error_file_contributes_zero(self):
        _write(self.root, "tests/test_broken.py", "def (:\n")
        _write(self.root, "tests/test_ok.py",
               "def test_one(): pass\n")
        self.assertEqual(derive_test_count(self.root), 1)


class ExtractClaimsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _values(self, kind=None):
        return [c["value"] for c in extract_claims(self.root)
                if kind is None or c["kind"] == kind]

    def test_plain_test_claim(self):
        _write(self.root, "README.md", "This project has 119 tests.\n")
        self.assertIn(119, self._values("test-count"))

    def test_comma_grouped_number(self):
        _write(self.root, "README.md", "A whopping 2,492 tests.\n")
        self.assertIn(2492, self._values("test-count"))

    def test_qualifier_words_between(self):
        _write(self.root, "README.md", "We ship 42 passing tests here.\n")
        self.assertIn(42, self._values("test-count"))

    def test_colon_form(self):
        _write(self.root, "README.md", "tests: 88\n")
        self.assertIn(88, self._values("test-count"))

    def test_floor_marker_recorded(self):
        _write(self.root, "README.md", "100+ tests\n")
        claims = extract_claims(self.root)
        self.assertTrue(any(c["floor"] for c in claims))

    def test_loc_claim_variants(self):
        _write(self.root, "README.md",
               "580 LOC across the app; about 2k lines of code total.\n")
        vals = self._values("loc")
        self.assertIn(580, vals)
        self.assertIn(2000, vals)

    def test_docs_dir_is_scanned(self):
        # detect.SKIP_DIRS excludes docs/, but claims live there.
        _write(self.root, "docs/STATUS.md", "500 tests\n")
        self.assertIn(500, self._values("test-count"))

    def test_vendored_dir_not_scanned(self):
        _write(self.root, "node_modules/dep/README.md", "9999 tests\n")
        self.assertEqual(self._values(), [])

    def test_non_doc_file_not_scanned(self):
        _write(self.root, "code.py", "# 9999 tests\n")
        self.assertEqual(self._values(), [])


class SignificanceTests(unittest.TestCase):
    def test_exact_match_not_significant(self):
        self.assertFalse(_significant(119, 119, 0.15, floor=False))

    def test_small_diff_within_absolute_floor(self):
        self.assertFalse(_significant(121, 119, 0.15, floor=False))

    def test_gross_inflation_significant(self):
        self.assertTrue(_significant(2492, 580, 0.15, floor=False))

    def test_none_derived_never_significant(self):
        self.assertFalse(_significant(500, None, 0.15, floor=False))

    def test_zero_derived_never_significant(self):
        self.assertFalse(_significant(500, 0, 0.15, floor=False))

    def test_floor_claim_below_reality_ok(self):
        # "100+ tests" with 250 real tests is honest — not flagged.
        self.assertFalse(_significant(100, 250, 0.15, floor=True))

    def test_floor_claim_above_reality_flagged(self):
        # "100+ tests" with only 3 real tests violates the floor.
        self.assertTrue(_significant(100, 3, 0.15, floor=True))


class FindClaimIssuesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _reasons(self):
        return [i["reason"] for i in find_claim_issues(self.root)]

    def test_honest_claim_not_flagged(self):
        _write(self.root, "tests/test_a.py",
               "class T(unittest.TestCase):\n"
               "    def test_one(self): pass\n"
               "    def test_two(self): pass\n"
               "    def test_three(self): pass\n")
        _write(self.root, "README.md", "3 tests, all green.\n")
        self.assertEqual(find_claim_issues(self.root), [])

    def test_inflated_claim_flagged(self):
        _write(self.root, "tests/test_a.py",
               "def test_one(): pass\n")
        _write(self.root, "README.md", "2,492 tests.\n")
        issues = find_claim_issues(self.root)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["claimed"], 2492)
        self.assertEqual(issues[0]["derived"], 1)
        self.assertIn("overstates", issues[0]["reason"])

    def test_claim_with_no_tests_in_repo_not_flagged(self):
        # No test files -> nothing to derive -> no false positive.
        _write(self.root, "README.md", "500 tests.\n")
        self.assertEqual(find_claim_issues(self.root), [])

    def test_understated_claim_flagged_as_stale(self):
        _write(self.root, "README.md", "5 tests.\n")
        src = "".join(f"    def test_{i}(self): pass\n" for i in range(50))
        _write(self.root, "tests/test_a.py",
               "class T(unittest.TestCase):\n" + src)
        issues = find_claim_issues(self.root)
        self.assertEqual(len(issues), 1)
        self.assertIn("understates", issues[0]["reason"])

    def test_empty_project_returns_empty(self):
        self.assertEqual(find_claim_issues(self.root), [])


class HelperTests(unittest.TestCase):
    def test_to_int_strips_commas(self):
        self.assertEqual(_to_int("2,492"), 2492)

    def test_to_int_k_suffix(self):
        self.assertEqual(_to_int("10", "k"), 10000)

    def test_count_test_methods_direct(self):
        self.assertEqual(
            _count_test_methods(
                "class T(unittest.TestCase):\n"
                "    def test_a(self): pass\n"
                "    def not_a_test(self): pass\n"),
            1)


if __name__ == "__main__":
    unittest.main()
