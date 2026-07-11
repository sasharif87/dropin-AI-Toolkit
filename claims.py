"""
claims.py — Claims checker: re-derive doc claims from the repo, flag contradictions.

Credibility docs (READMEs, portfolio pages, status sections) accumulate numeric
claims — "119 tests", "2,492 tests", "580 LOC" — that drift from reality because
nothing re-derives them. The portfolio audit's worst finding was exactly this:
a credibility doc claimed 2,492 tests where the repos actually held ~580, an
unverified-aggregation failure that survived because no gate re-computed the
number. This gate makes that class structurally hard: it extracts numeric claims
from docs and re-derives them from the repository, flagging only the ones the
repo clearly contradicts.

Deterministic and local — no Ollama. Consumes only the project's files.

Bias, inherited from ``orphans.py``: false negatives over false positives. A
static test count can never exactly match a runner (duplicate method names
collapse, helpers named ``test_*`` don't run), so the derived value is an
*estimate* and a claim is flagged only when it diverges beyond a tolerance —
never for the honest rounding of a fair claim. The failure being targeted is
gross inflation (4x), not a doc that says 119 when the runner says 121.
"""

import ast
import os
import re

# Directories that are not part of the project's own source or docs. Unlike
# ``detect.SKIP_DIRS`` this keeps ``docs`` — that is where claims live — but
# still skips vendored trees so a dependency's README is never scanned.
_VENDOR_DIRS = {
    "node_modules", ".venv", "venv", "env", ".git", "__pycache__",
    ".next", "dist", "build", ".mypy_cache", ".pytest_cache", ".tox",
    "htmlcov", ".idea", ".vscode", "eggs",
}

DOC_EXTENSIONS = {".md", ".rst", ".txt"}
CODE_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs"}

# A claim is flagged only when |claimed - derived| exceeds BOTH an absolute
# floor and a fraction of the derived value. Test counts are re-derivable
# fairly precisely, so their fraction is tight; "LOC" means wildly different
# things (with/without blanks, comments, tests) so its fraction is loose.
_TOL_ABS = 5
_TEST_TOL_FRAC = 0.15
_LOC_TOL_FRAC = 0.40


# ---------------------------------------------------------------------------
# Claim extraction from docs
# ---------------------------------------------------------------------------
# Number immediately before a "test"/"tests" keyword, allowing a short list of
# qualifier words in between ("119 passing tests", "42 unit tests"). A trailing
# "+" ("100+ tests") marks the number as a floor, handled at comparison time.
_TEST_CLAIM_RE = re.compile(
    r"(?<![\w.])(\d[\d,]*)(\+?)\s*"
    r"(?:(?:passing|unit|integration|automated|total|green)\s+){0,2}"
    r"tests?\b",
    re.IGNORECASE,
)
# "tests: 119" / "test count = 119" colon/equals form.
_TEST_COLON_RE = re.compile(
    r"\btests?\b[^\n\d]{0,12}?[:=]\s*(\d[\d,]*)(\+?)",
    re.IGNORECASE,
)
# Lines of code: "580 LOC", "10k lines of code", "~1,200 lines of Python".
_LOC_CLAIM_RE = re.compile(
    r"(?<![\w.])(\d[\d,]*)(\+?)\s*([kK]?)\s*"
    r"(?:LOC\b|lines?\s+of\s+(?:code|python|source)\b)",
    re.IGNORECASE,
)


def _to_int(digits, k=""):
    n = int(digits.replace(",", ""))
    return n * 1000 if k.lower() == "k" else n


def _iter_files(root, extensions, skip_docs=False):
    """Yield (abs_path, rel_path) for files with the given extensions."""
    skip = _VENDOR_DIRS | ({"docs"} if skip_docs else set())
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
        for f in files:
            if os.path.splitext(f)[1].lower() in extensions:
                abs_path = os.path.join(dirpath, f)
                rel = os.path.relpath(abs_path, root).replace("\\", "/")
                yield abs_path, rel


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            return fh.read()
    except OSError:
        return ""


def extract_claims(root):
    """Return the numeric claims found in the project's documentation.

    Each claim is ``{"file", "line", "text", "kind", "value", "floor"}`` where
    ``kind`` is ``"test-count"`` or ``"loc"`` and ``floor`` is True for an
    ``N+`` claim (a lower bound, not an exact count).
    """
    claims = []
    for abs_path, rel in _iter_files(root, DOC_EXTENSIONS):
        for lineno, line in enumerate(_read(abs_path).splitlines(), 1):
            for m in _TEST_CLAIM_RE.finditer(line):
                claims.append({
                    "file": rel, "line": lineno, "text": m.group(0).strip(),
                    "kind": "test-count", "value": _to_int(m.group(1)),
                    "floor": m.group(2) == "+",
                })
            for m in _TEST_COLON_RE.finditer(line):
                claims.append({
                    "file": rel, "line": lineno, "text": m.group(0).strip(),
                    "kind": "test-count", "value": _to_int(m.group(1)),
                    "floor": m.group(2) == "+",
                })
            for m in _LOC_CLAIM_RE.finditer(line):
                claims.append({
                    "file": rel, "line": lineno, "text": m.group(0).strip(),
                    "kind": "loc", "value": _to_int(m.group(1), m.group(3)),
                    "floor": m.group(2) == "+",
                })
    return claims


# ---------------------------------------------------------------------------
# Re-derivation from the repository
# ---------------------------------------------------------------------------
def _is_test_file(basename):
    return (
        basename.startswith("test_")
        or basename.endswith(("_test.py", ".test.py", "_tests.py"))
    )


def _count_test_methods(source):
    """Estimate the number of runnable tests in one Python test file.

    Counts ``test*`` methods on every class (any class in a test file with
    test-prefixed methods is a test case, whatever it subclasses — recognising
    only ``unittest.TestCase`` would undercount custom bases, and undercounting
    the derived value is what would falsely flag an honest claim) plus
    module-level ``test_*`` functions. Names are de-duplicated per scope
    because a redefined method runs once, matching the runner.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0
    total = 0
    module_funcs = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test"):
                module_funcs.add(node.name)
        elif isinstance(node, ast.ClassDef):
            methods = {
                m.name for m in node.body
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                and m.name.startswith("test")
            }
            total += len(methods)
    return total + len(module_funcs)


def derive_test_count(root):
    """Estimate the project's Python test count by static analysis.

    Returns None when the project has no Python test files — there is nothing
    to derive from, so no test-count claim can be judged (false-negative bias).
    """
    files = [abs_path for abs_path, rel in _iter_files(root, {".py"})
             if _is_test_file(os.path.basename(rel))]
    if not files:
        return None
    return sum(_count_test_methods(_read(p)) for p in files)


def derive_loc(root):
    """Count non-blank source lines across the project's code files.

    "LOC" is inherently fuzzy (blanks? comments? tests?); this counts non-blank
    lines in recognised code files and pairs it with a loose tolerance. Returns
    None when no code files are found.
    """
    total = 0
    found = False
    for abs_path, _rel in _iter_files(root, CODE_EXTENSIONS, skip_docs=True):
        found = True
        for line in _read(abs_path).splitlines():
            if line.strip():
                total += 1
    return total if found else None


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------
def _significant(claimed, derived, frac, floor):
    """True when a claim diverges from the derived value beyond tolerance.

    ``floor`` claims ("100+ tests") are only violated when the repo has *fewer*
    than claimed — an "N+" lower bound is honest however far reality exceeds it.
    """
    if derived is None or derived <= 0:
        return False
    tol = max(_TOL_ABS, frac * derived)
    if floor:
        return (claimed - derived) > tol
    return abs(claimed - derived) > tol


def find_claim_issues(root):
    """Return findings for doc claims the repository contradicts.

    Each finding is ``{"file", "line", "kind", "claim", "claimed", "derived",
    "reason"}``. Returns an empty list when nothing is contradicted (including
    projects with no derivable claims). Deterministic; no Ollama.
    """
    claims = extract_claims(root)
    if not claims:
        return []

    derived = {}
    if any(c["kind"] == "test-count" for c in claims):
        derived["test-count"] = derive_test_count(root)
    if any(c["kind"] == "loc" for c in claims):
        derived["loc"] = derive_loc(root)

    tol_frac = {"test-count": _TEST_TOL_FRAC, "loc": _LOC_TOL_FRAC}
    noun = {"test-count": "tests", "loc": "source lines"}

    issues = []
    for c in claims:
        d = derived.get(c["kind"])
        if not _significant(c["value"], d, tol_frac[c["kind"]], c["floor"]):
            continue
        direction = "overstates" if c["value"] > d else "understates"
        issues.append({
            "file": c["file"], "line": c["line"], "kind": c["kind"],
            "claim": c["text"], "claimed": c["value"], "derived": d,
            "reason": f"doc {direction} {noun[c['kind']]}: "
                      f"claims {c['value']}, repo has ~{d}",
        })
    return issues


def print_claims(issues):
    """Pretty-print the claims report as part of `drop.py detect` output."""
    if not issues:
        print("\n  Doc claims: none contradicted")
        return
    print(f"\n  Doc claims contradicted ({len(issues)}) — docs disagree with the repo:")
    for i in issues:
        loc = f"{i['file']}:{i['line']}"
        print(f"    {loc:<40} {i['reason']}")
    print("    (fix the doc, or the repo — an unverified number is a credibility leak)")
