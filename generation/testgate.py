"""
testgate.py — Run the project's own test suite around fix application.

The semantic guard the syntactic checks can't provide: a fix that compiles
but breaks behaviour is caught by re-running the tests that cover the fixed
file. detect.py identifies the framework; this module runs it via
subprocess (stdlib-only — pytest/jest are the *project's* dependencies,
never the toolkit's) and degrades gracefully with a loud log line when the
suite can't run.

Flow (driven by fix.py):
    baseline()            before any fix — record the failing set
    affected_tests(rel)   cheap heuristic: name match + import grep
    run(targets)          after each applied fix
    new_failures(failed)  failures not already red at baseline
"""

import os
import re
import subprocess
import sys

from engine import log

# Per-run subprocess timeout (seconds). Suites slower than this can't gate
# per-file fixes usefully anyway.
SUITE_TIMEOUT = 900

_FAILED_LINE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)  # pytest -rf
_FAIL_LINE_JS = re.compile(r"^FAIL\s+(\S+)", re.MULTILINE)             # jest/vitest


class TestGate:
    def __init__(self, root, stack, tests_dir=None):
        self.root = root
        self.framework = (stack or {}).get("test_framework")
        self.tests_dir = tests_dir
        self.baseline_failures = None   # set once baseline() has run
        self._unavailable_reason = None

    # ── Availability ─────────────────────────────────────────────────────────

    def available(self):
        """Return (ok, reason). ok=False means fixes cannot be verified."""
        if not self.framework:
            return False, "no test framework detected"
        if not self.tests_dir or not os.path.isdir(os.path.join(self.root, self.tests_dir)):
            return False, "no tests directory found"
        return True, ""

    # ── Running ──────────────────────────────────────────────────────────────

    def _command(self, targets):
        if self.framework == "pytest":
            cmd = [sys.executable, "-m", "pytest", "-x", "-q", "--tb=no", "-rf",
                   "-p", "no:cacheprovider"]
            cmd += targets if targets else [self.tests_dir]
            return cmd
        # jest / vitest — run through npx so the project's local install is used
        cmd = ["npx", self.framework, "--passWithNoTests"]
        if targets:
            cmd += targets
        return cmd

    def run(self, targets=None, first_fail_stop=True):
        """Run the suite (or *targets* subset).

        Returns (status, failed_set, output_tail) with status one of:
        "pass", "fail", "no_tests", "unavailable".
        """
        ok, reason = self.available()
        if not ok:
            self._unavailable_reason = reason
            return "unavailable", set(), reason

        cmd = self._command(targets)
        if self.framework == "pytest" and not first_fail_stop:
            cmd = [c for c in cmd if c != "-x"]

        try:
            result = subprocess.run(
                cmd, cwd=self.root, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=SUITE_TIMEOUT,
                stdin=subprocess.DEVNULL, shell=(self.framework != "pytest"
                                                 and sys.platform == "win32"),
            )
        except FileNotFoundError:
            self._unavailable_reason = f"{self.framework} not runnable on PATH"
            return "unavailable", set(), self._unavailable_reason
        except subprocess.TimeoutExpired:
            return "fail", {"<suite timeout>"}, f"suite exceeded {SUITE_TIMEOUT}s"

        out = (result.stdout or "") + "\n" + (result.stderr or "")
        failed_re = _FAILED_LINE if self.framework == "pytest" else _FAIL_LINE_JS
        failed = set(failed_re.findall(out))

        if self.framework == "pytest" and result.returncode == 5:
            return "no_tests", set(), out[-500:]
        if result.returncode == 0:
            return "pass", set(), out[-500:]
        if not failed:
            # Non-zero exit without parseable failures (collection error, etc.)
            failed = {f"<exit code {result.returncode}>"}
        return "fail", failed, out[-1500:]

    def baseline(self):
        """Run the full suite once and record the failing set.

        Returns (status, failed_set). Run without -x so the whole red set is
        known — later runs compare against it.
        """
        status, failed, out = self.run(first_fail_stop=False)
        if status in ("pass", "fail"):
            self.baseline_failures = failed
        if status == "fail":
            log(f"  Baseline test run: {len(failed)} failure(s) already present")
        elif status == "pass":
            log("  Baseline test run: green")
        return status, failed

    def new_failures(self, failed):
        """Failures not already present at baseline."""
        return set(failed) - (self.baseline_failures or set())

    # ── Affected-test heuristic ──────────────────────────────────────────────

    def affected_tests(self, fixed_rel):
        """Test files likely covering *fixed_rel*.

        Heuristic: test files whose name contains the fixed module's stem,
        plus any test file whose import lines mention the module (by stem or
        dotted path). Returns a list of paths relative to root, or None when
        detection is uncertain (caller should run the full suite).
        """
        if not self.tests_dir:
            return None
        stem = os.path.splitext(os.path.basename(fixed_rel))[0]
        dotted = os.path.splitext(fixed_rel)[0].replace("/", ".").replace("\\", ".")
        stem_word = re.compile(rf"\b{re.escape(stem)}\b")

        matches = []
        tests_root = os.path.join(self.root, self.tests_dir)
        for dirpath, dirs, files in os.walk(tests_root):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
            for f in files:
                if not (f.startswith("test_") or f.endswith(("_test.py", ".test.js",
                                                             ".test.ts", ".spec.js", ".spec.ts"))
                        or (f.startswith("test") and f.endswith(".py"))):
                    continue
                path = os.path.join(dirpath, f)
                rel = os.path.relpath(path, self.root).replace("\\", "/")
                if stem in f:
                    matches.append(rel)
                    continue
                try:
                    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                        for line in fh:
                            ls = line.strip()
                            if ls.startswith(("import ", "from ", "const ", "require(")) \
                                    and (dotted in ls or stem_word.search(ls)):
                                matches.append(rel)
                                break
                except OSError:
                    continue
        return matches or None
