"""Tests for golden.py — the golden-file regression runner (bank, then diff).

Stdlib unittest only: the toolkit tests itself on bare Python with no pip
install, matching the air-gapped sovereignty constraint. Commands run through
the ``{python}`` placeholder (-> sys.executable) so the tests are hermetic and
need no tool on PATH. Also collectable by pytest if it happens to be present.
"""

import json
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "gates"), os.path.join(_ROOT, "generation")]

from golden import (
    run_golden, find_config, load_config, _normalize, _apply_scrubs,
    _flatten, _build_argv,
)

# A deterministic transform: uppercase the input file to stdout.
UPPER = (
    "import sys\n"
    "print(open(sys.argv[1]).read().upper(), end='')\n"
)
LOWER = (
    "import sys\n"
    "print(open(sys.argv[1]).read().lower(), end='')\n"
)


def _write(root, rel, content=""):
    path = os.path.join(root, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


class GoldenRunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        _write(self.root, "transform.py", UPPER)
        _write(self.root, "fixtures/a.txt", "hello\n")

    def tearDown(self):
        self._tmp.cleanup()

    def _config(self, **overrides):
        case = {
            "name": "upper",
            "command": "{python} transform.py {input}",
            "inputs": ["fixtures/*.txt"],
        }
        case.update(overrides)
        _write(self.root, ".golden.json", json.dumps({"cases": [case]}))

    def _statuses(self, result):
        return [r["status"] for c in result["cases"] for r in c["results"]]

    def test_missing_golden_fails_closed(self):
        self._config()
        result = run_golden(self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(self._statuses(result), ["new"])

    def test_update_banks_the_golden(self):
        self._config()
        result = run_golden(self.root, update=True)
        self.assertTrue(result["ok"])
        self.assertEqual(self._statuses(result), ["banked"])
        gpath = os.path.join(self.root, "tests", "golden", "upper",
                             "fixtures__a.txt.golden")
        self.assertTrue(os.path.isfile(gpath))
        with open(gpath) as fh:
            self.assertEqual(fh.read(), "HELLO\n")

    def test_banked_then_passes(self):
        self._config()
        run_golden(self.root, update=True)
        result = run_golden(self.root)
        self.assertTrue(result["ok"])
        self.assertEqual(self._statuses(result), ["pass"])

    def test_output_change_is_a_regression(self):
        self._config()
        run_golden(self.root, update=True)
        _write(self.root, "transform.py", LOWER)  # now lowercases -> differs
        result = run_golden(self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(self._statuses(result), ["regression"])
        diff = result["cases"][0]["results"][0]["diff"]
        self.assertIn("-HELLO", diff)
        self.assertIn("+hello", diff)

    def test_update_reblesses_a_regression(self):
        self._config()
        run_golden(self.root, update=True)
        _write(self.root, "transform.py", LOWER)
        result = run_golden(self.root, update=True)
        self.assertTrue(result["ok"])
        self.assertEqual(self._statuses(result), ["updated"])
        result2 = run_golden(self.root)
        self.assertEqual(self._statuses(result2), ["pass"])

    def test_command_not_found_is_error(self):
        self._config(command="nonesuch_binary_xyz {input}")
        result = run_golden(self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(self._statuses(result), ["error"])
        self.assertIn("not found", result["cases"][0]["results"][0]["detail"])

    def test_zero_matched_inputs_fails_closed(self):
        self._config(inputs=["does/not/exist/*.txt"])
        result = run_golden(self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(self._statuses(result), ["error"])
        self.assertIn("no input files matched",
                      result["cases"][0]["results"][0]["detail"])

    def test_missing_command_is_error(self):
        _write(self.root, ".golden.json",
               json.dumps({"cases": [{"name": "x", "inputs": ["fixtures/*.txt"]}]}))
        result = run_golden(self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(self._statuses(result), ["error"])

    def test_scrub_masks_volatile_output(self):
        # Transform emits a changing first line; the scrub masks it so two runs
        # with different volatile content still compare equal.
        _write(self.root, "transform.py",
               "import sys, os\n"
               "print('PID:', os.getpid())\n"
               "print(open(sys.argv[1]).read().upper(), end='')\n")
        self._config(scrub=[{"pattern": r"PID: \d+", "replace": "PID: <N>"}])
        run_golden(self.root, update=True)
        result = run_golden(self.root)  # new PID, but masked
        self.assertEqual(self._statuses(result), ["pass"])

    def test_bad_scrub_regex_does_not_crash(self):
        self._config(scrub=[{"pattern": "([unclosed", "replace": "x"}])
        result = run_golden(self.root, update=True)  # must not raise
        self.assertEqual(self._statuses(result), ["banked"])

    def test_multiple_inputs_each_get_a_golden(self):
        _write(self.root, "fixtures/b.txt", "world\n")
        self._config()
        result = run_golden(self.root, update=True)
        self.assertEqual(sorted(self._statuses(result)), ["banked", "banked"])
        base = os.path.join(self.root, "tests", "golden", "upper")
        self.assertTrue(os.path.isfile(os.path.join(base, "fixtures__a.txt.golden")))
        self.assertTrue(os.path.isfile(os.path.join(base, "fixtures__b.txt.golden")))

    def test_crlf_normalized_so_no_spurious_regression(self):
        self._config()
        run_golden(self.root, update=True)
        # Hand-rewrite the golden with CRLF line endings; normalization should
        # make it still compare equal to the LF actual output.
        gpath = os.path.join(self.root, "tests", "golden", "upper",
                             "fixtures__a.txt.golden")
        with open(gpath, "wb") as fh:
            fh.write(b"HELLO\r\n")
        result = run_golden(self.root)
        self.assertEqual(self._statuses(result), ["pass"])


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_config_is_ok_nothing_to_check(self):
        result = run_golden(self.root)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["config"])
        self.assertIn("no golden config", result["note"])

    def test_find_config_prefers_dotfile(self):
        _write(self.root, ".golden.json", "{}")
        self.assertTrue(find_config(self.root).endswith(".golden.json"))

    def test_explicit_config_path(self):
        _write(self.root, "custom.json", "{}")
        p = os.path.join(self.root, "custom.json")
        self.assertEqual(find_config(self.root, p), p)

    def test_missing_explicit_config_fails_closed(self):
        # An explicitly requested config that doesn't exist is a surfaced
        # failure — a typo'd --golden-config must never go silently green.
        # Only auto-discovery finding nothing is an opt-out.
        missing = os.path.join(self.root, "typo.golden.json")
        result = run_golden(self.root, config_path=missing)
        self.assertFalse(result["ok"])
        self.assertEqual(result["config"], missing)
        self.assertIn("not found", result["note"])

    def test_malformed_json_fails_closed(self):
        _write(self.root, ".golden.json", "{not json")
        result = run_golden(self.root)
        self.assertFalse(result["ok"])
        self.assertIn("could not read config", result["note"])

    def test_config_without_cases_list_fails(self):
        _write(self.root, ".golden.json", json.dumps({"foo": 1}))
        result = run_golden(self.root)
        self.assertFalse(result["ok"])
        self.assertIn("no 'cases'", result["note"])


class HelperTests(unittest.TestCase):
    def test_normalize_crlf_and_trailing(self):
        self.assertEqual(_normalize("a\r\nb\r\n\n"), "a\nb\n")

    def test_normalize_empty(self):
        self.assertEqual(_normalize(""), "")

    def test_apply_scrubs(self):
        self.assertEqual(
            _apply_scrubs("time=12:30:59 done", [{"pattern": r"time=[\d:]+",
                                                  "replace": "time=<T>"}]),
            "time=<T> done")

    def test_flatten(self):
        self.assertEqual(_flatten("fixtures/rides/a.fit"), "fixtures__rides__a.fit")

    def test_build_argv_substitutes_placeholders(self):
        argv = _build_argv("{python} run.py {input}", "/path/to/in.txt")
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[1], "run.py")
        self.assertEqual(argv[2], "/path/to/in.txt")

    def test_build_argv_keeps_spaced_path_as_one_arg(self):
        argv = _build_argv("tool {input}", "/a b/in.txt")
        self.assertEqual(argv, ["tool", "/a b/in.txt"])


if __name__ == "__main__":
    unittest.main()
