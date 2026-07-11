"""
invariants.py — Design-invariant conformance harness (pluggable checks).

The companion to ``layers.py``. Where the layer gate is *declarative* (a repo
ships a ``.layers.json`` and the toolkit knows how to read it), invariant checks
are **code, not config** — "this except-block must fail closed", "this column
must never exist on that model", "this function must keep a required parameter".
They can't reduce to a JSON file, so this gate generalizes as a *pluggable-check
harness*:

  - the **toolkit** ships the scaffolding — a :class:`Repo` of cheap AST/text
    helpers, the :class:`Invariant` model with its ``ENFORCED`` / ``GAP`` states,
    and a fail-closed loader + runner;
  - each **consuming repo** ships a ``.invariants.py`` that imports
    ``Invariant, ENFORCED, GAP`` from here and defines its own ``check`` functions
    plus a module-level ``INVARIANTS`` list binding them together.

IME's ``scripts/check_invariants.py`` (17 checks) is the reference example of what
such a module looks like; those checks stay in IME — only the scaffolding lives
here.

Two invariant states, matching IME:

  ENFORCED  A structural check runs and must pass; a problem fails the gate.
            The check guards the *shape* of a guarantee (a function/constant/grant
            still exists and still reads the way it did when verified) — it does
            not re-run business logic (that's the repo's test suite).
  GAP       The enforcing code doesn't exist yet — tracked future work, printed
            so the backlog stays visible but never failing the run.

Deterministic and local — no Ollama; the repo is inspected with :mod:`ast` and
plain text. Fail-closed throughout: a configured-but-broken check module fails
(a syntax error, a missing ``INVARIANTS`` list, an ``ENFORCED`` entry with no
callable check, a check that raises or returns a non-list are all *surfaced
errors*, never a silent pass), while a repo with no ``.invariants.py`` opts out
(there are no declared invariants to enforce).

The check contract — each ``check`` is ``check(repo: Repo) -> list[str]``, where
an empty list means the invariant holds and each string is a human-readable
problem. Example ``.invariants.py`` at a consuming repo's root::

    from invariants import Invariant, ENFORCED, GAP

    def check_egress_fails_closed(repo):
        problems = []
        body = repo.except_body_source("app/pii/firewall.py", "firewall_egress")
        if not body or "return False" not in body:
            problems.append("firewall_egress: except-block no longer fails closed")
        return problems

    def check_router_keeps_system(repo):
        if not repo.has_required_str_param("app/inference/router.py", "chat", "system"):
            return ["InferenceRouter.chat(): 'system' is no longer required"]
        return []

    INVARIANTS = [
        Invariant(1, "Egress fails closed", ENFORCED, check_egress_fails_closed),
        Invariant(2, "System framing never suppressed", ENFORCED, check_router_keeps_system),
        Invariant(3, "Notification orchestration fail-closed", GAP,
                  gap_note="domain is schema-only — no orchestrator reads it yet"),
    ]
"""

import ast
import hashlib
import importlib.util
import os
import sys
from dataclasses import dataclass

CONFIG_NAMES = (".invariants.py",)

ENFORCED = "ENFORCED"
GAP = "GAP"


# ---------------------------------------------------------------------------
# Invariant model — one entry per design invariant, same shape as IME's
# ---------------------------------------------------------------------------
@dataclass
class Invariant:
    id: object                # int or str — printed as-is
    name: str
    status: str               # ENFORCED | GAP
    check: object = None      # Callable[[Repo], list[str]] | None (required for ENFORCED)
    gap_note: str = ""        # shown for GAP entries


# ---------------------------------------------------------------------------
# Pure AST helper (no repo root needed)
# ---------------------------------------------------------------------------
def function_node(tree, name):
    """The first function/method named *name* in *tree*, or None."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


# ---------------------------------------------------------------------------
# Repo — cheap AST/text helpers bound to a project root, passed to each check.
# Same spirit as IME's module-level helpers, generalized so no check needs a
# hardcoded REPO_ROOT: paths are project-root-relative, posix-style ("/").
# ---------------------------------------------------------------------------
class Repo:
    def __init__(self, root):
        self.root = os.path.abspath(root)

    def _abs(self, relpath):
        return os.path.join(self.root, relpath.replace("/", os.sep))

    def exists(self, relpath):
        """True if *relpath* exists under the repo root."""
        return os.path.exists(self._abs(relpath))

    def read(self, relpath):
        """Text of *relpath* (utf-8)."""
        with open(self._abs(relpath), "r", encoding="utf-8") as fh:
            return fh.read()

    def parse(self, relpath):
        """Parsed :class:`ast.Module` for *relpath*."""
        return ast.parse(self.read(relpath), filename=relpath)

    def function_source(self, relpath, name):
        """Source text of the first function/method named *name* in *relpath*."""
        source = self.read(relpath)
        tree = ast.parse(source, filename=relpath)
        node = function_node(tree, name)
        return ast.get_source_segment(source, node) if node else None

    def except_body_source(self, relpath, func_name):
        """Source of the first except-handler body inside *func_name*.

        Precise where a substring search over the whole function would false-pass
        (unrelated code later in the function can accidentally contain the marker
        text a fail-closed check looks for). Returns None if the function or an
        except-handler isn't found.
        """
        source = self.read(relpath)
        tree = ast.parse(source, filename=relpath)
        func = function_node(tree, func_name)
        if func is None:
            return None
        for node in ast.walk(func):
            if isinstance(node, ast.Try) and node.handlers:
                handler = node.handlers[0]
                segments = [ast.get_source_segment(source, stmt) for stmt in handler.body]
                return "\n".join(s for s in segments if s)
        return None

    def has_required_str_param(self, relpath, func_name, param):
        """True if *func_name* has *param* as a required (no-default) argument."""
        tree = ast.parse(self.read(relpath), filename=relpath)
        node = function_node(tree, func_name)
        if node is None:
            return False
        args = node.args
        positional = args.posonlyargs + args.args
        names = [a.arg for a in positional]
        n_defaults = len(args.defaults)
        required = names[: len(names) - n_defaults] if n_defaults else names
        if param in required:
            return True
        for kwarg, default in zip(args.kwonlyargs, args.kw_defaults):
            if kwarg.arg == param and default is None:
                return True
        return False

    def iter_py(self, subdir="."):
        """Yield repo-relative posix paths of every .py under *subdir*, sorted."""
        base = self.root if subdir in (".", "", None) else self._abs(subdir)
        for dirpath, dirs, files in os.walk(base):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "__pycache__")
            for f in sorted(files):
                if f.endswith(".py"):
                    abs_path = os.path.join(dirpath, f)
                    yield os.path.relpath(abs_path, self.root).replace("\\", "/")

    def iter_lines(self, subdir="."):
        """Yield (relpath, lineno, line) over every .py line under *subdir*.

        The IME grep-style scans (hard-delete / auto-verify / immutable-column
        checks) are all this shape.
        """
        for rel in self.iter_py(subdir):
            for lineno, line in enumerate(self.read(rel).splitlines(), start=1):
                yield rel, lineno, line


# ---------------------------------------------------------------------------
# Config discovery + load
# ---------------------------------------------------------------------------
def find_config(root, config_path=None):
    """Return the path to the check module, or None if there isn't one."""
    if config_path:
        return config_path if os.path.isfile(config_path) else None
    for name in CONFIG_NAMES:
        candidate = os.path.join(root, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def load_check_module(path):
    """Import the repo's check module from *path*. Returns (module, error_str).

    Fail-closed: any import-time failure (syntax error, bad import, exception at
    module scope) is surfaced as an error string rather than swallowed.
    """
    mod_name = "dropin_invariants__" + hashlib.md5(
        os.path.abspath(path).encode("utf-8")).hexdigest()[:12]
    try:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            return None, f"could not create import spec for {path}"
        module = importlib.util.module_from_spec(spec)
        # Register before exec so a check module defining its own dataclass (or
        # doing any sys.modules introspection) resolves — matches IME's loader.
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
    except Exception as e:  # noqa: BLE001 — a broken check module must fail loudly
        sys.modules.pop(mod_name, None)
        return None, f"could not import check module: {e.__class__.__name__}: {e}"
    return module, None


def _validate_entry(inv):
    """Return an error string for a malformed INVARIANTS entry, or None."""
    for attr in ("id", "name", "status"):
        if not hasattr(inv, attr):
            return f"invalid INVARIANTS entry (missing '{attr}'): {inv!r}"
    if inv.status not in (ENFORCED, GAP):
        return (f"#{inv.id} {inv.name}: unknown status {inv.status!r} "
                f"(expected {ENFORCED} or {GAP})")
    if inv.status == ENFORCED and not callable(getattr(inv, "check", None)):
        return f"#{inv.id} {inv.name}: ENFORCED invariant has no callable check"
    return None


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def run_invariants(root, config_path=None):
    """Run the invariant harness under *root*.

    Returns a dict: ``config`` (path or None), ``passed`` / ``failures`` /
    ``gaps`` / ``errors`` (lists), and ``ok`` (True when the gate passes). A repo
    with no ``.invariants.py`` passes with a note (opt-out); any load error, a
    broken registry entry, a check that raises, or a check returning a non-list
    fails the gate — as does an explicitly passed *config_path* that doesn't
    exist (only auto-discovery finding nothing is an opt-out; a typo'd path must
    never go silently green).
    """
    root = os.path.abspath(root)
    if config_path and not os.path.isfile(config_path):
        return {"config": config_path, "ok": False,
                "passed": [], "failures": [], "gaps": [],
                "errors": [f"invariants config not found: {config_path}"]}
    path = find_config(root, config_path)
    result = {"config": path, "ok": True,
              "passed": [], "failures": [], "gaps": [], "errors": []}
    if not path:
        result["note"] = "no invariants config (.invariants.py) — nothing to check"
        return result

    module, err = load_check_module(path)
    if err:
        result["ok"] = False
        result["errors"].append(err)
        return result

    invariants = getattr(module, "INVARIANTS", None)
    if invariants is None:
        result["ok"] = False
        result["errors"].append("check module defines no INVARIANTS list")
        return result
    if not isinstance(invariants, (list, tuple)):
        result["ok"] = False
        result["errors"].append(
            f"INVARIANTS is {type(invariants).__name__}, expected a list")
        return result

    repo = Repo(root)
    for inv in invariants:
        entry_err = _validate_entry(inv)
        if entry_err:
            result["errors"].append(entry_err)
            continue
        if inv.status == GAP:
            result["gaps"].append(
                {"id": inv.id, "name": inv.name, "note": inv.gap_note or ""})
            continue
        # ENFORCED
        try:
            problems = inv.check(repo)
        except Exception as e:  # noqa: BLE001 — a check that blows up can't pass silently
            result["errors"].append(
                f"#{inv.id} {inv.name}: check raised {e.__class__.__name__}: {e}")
            continue
        if not isinstance(problems, (list, tuple)):
            result["errors"].append(
                f"#{inv.id} {inv.name}: check returned {type(problems).__name__}, "
                f"expected a list of strings")
            continue
        problems = [str(p) for p in problems]
        if problems:
            result["failures"].append(
                {"id": inv.id, "name": inv.name, "problems": problems})
        else:
            result["passed"].append({"id": inv.id, "name": inv.name})

    result["ok"] = not result["failures"] and not result["errors"]
    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def print_invariants(result):
    """Pretty-print the invariant run for `drop.py invariants`."""
    if not result.get("config"):
        print(f"\n  Invariants: {result.get('note', 'no config')}")
        return

    passed = result.get("passed") or []
    failures = result.get("failures") or []
    gaps = result.get("gaps") or []
    errors = result.get("errors") or []
    enforced = len(passed) + len(failures)

    print(f"\n  Invariants — {os.path.basename(result['config'])}")
    print(f"    {enforced + len(gaps)} registered — "
          f"{enforced} enforced, {len(gaps)} gap(s)\n")

    for f in failures:
        print(f"    [FAILED] #{str(f['id']):>2} {f['name']}")
        for p in f["problems"]:
            print(f"               - {p}")
    for p in passed:
        print(f"    [OK]     #{str(p['id']):>2} {p['name']}")
    for g in gaps:
        print(f"    [GAP]    #{str(g['id']):>2} {g['name']}")
        if g["note"]:
            print(f"               {g['note']}")
    if errors:
        print(f"\n    Errors ({len(errors)}):")
        for e in errors:
            print(f"      - {e}")

    if result["ok"]:
        print(f"\n    Invariant gate: PASS — {len(passed)}/{enforced} enforced "
              f"invariant(s) OK, {len(gaps)} gap(s) tracked")
    else:
        print(f"\n    Invariant gate: FAIL "
              f"({len(failures)} enforced invariant(s) broken, {len(errors)} error(s))")
