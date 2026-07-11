"""
golden.py — Golden-file runner: bank known-good outputs, diff on change.

Live discovery (review, plausibility checks, a human noticing bad output) is the
*finding* mechanism; golden files are the *banking* mechanism. Every bug found
live converts to a permanent regression gate: you fix it, run
``drop.py golden --update`` to snapshot the now-correct output for a set of real
input fixtures (ride files, FIT exports, machining outputs), and from then on
any change to that output fails the gate. The priority target is a
safety-critical validator/plausibility layer where bad output damages hardware —
exactly where a silent output change must never slip through.

Fail-closed, like the rest of the toolkit: a configured case whose golden does
not exist yet fails (you must explicitly bank it with ``--update``), and a case
that matches zero input fixtures fails rather than silently checking nothing.

Deterministic and local — no Ollama. The transform is any local command the
project configures; the runner only executes it, captures a chosen stream, and
compares. Each repo carries a ``.golden.json`` config (the ``.layer_rules.json``
pattern, generalized); the runner lives here so every project gets it for free.

Config schema (``.golden.json`` at the project root)::

    {
      "cases": [
        {
          "name": "ride-validator",              # report + golden subdir name
          "command": "{python} validate.py {input}",  # {python}, {input} placeholders
          "inputs": ["fixtures/rides/*.fit"],     # globs, relative to project root
          "golden_dir": "tests/golden",           # optional (default: tests/golden)
          "capture": "stdout",                    # stdout | stderr | combined
          "timeout": 60,                          # optional per-run seconds
          "scrub": [                              # optional volatile-content masks
            {"pattern": "\\d{4}-\\d{2}-\\d{2}T[\\d:.]+", "replace": "<TS>"}
          ]
        }
      ]
    }

The command is parsed as argv with POSIX ``shlex`` rules (no shell, so no pipes
or redirects — that keeps a fixture filename from ever being interpreted as
shell). ``{python}`` expands to the running interpreter, ``{input}`` to the
fixture path; both are substituted into already-split tokens so a path with
spaces stays one argument.
"""

import difflib
import glob
import json
import os
import shlex
import subprocess
import sys

CONFIG_NAMES = (".golden.json", "golden.json")
DEFAULT_GOLDEN_DIR = "tests/golden"
DEFAULT_TIMEOUT = 60


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def find_config(root, config_path=None):
    """Return the path to the golden config, or None if there isn't one."""
    if config_path:
        return config_path if os.path.isfile(config_path) else None
    for name in CONFIG_NAMES:
        candidate = os.path.join(root, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def load_config(path):
    """Load and lightly validate the config. Returns (cases, error_str)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        return None, f"could not read config: {e}"
    cases = data.get("cases") if isinstance(data, dict) else None
    if not isinstance(cases, list):
        return None, "config has no 'cases' list"
    return cases, None


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------
def _normalize(text):
    """Universal newlines + exactly one trailing newline, for stable diffs."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.rstrip("\n") + "\n" if text else ""


def _apply_scrubs(text, scrubs):
    """Mask volatile content (timestamps, temp paths, PIDs) before comparison."""
    import re
    for s in scrubs or []:
        pattern = s.get("pattern")
        if not pattern:
            continue
        try:
            text = re.sub(pattern, s.get("replace", ""), text)
        except re.error:
            # A bad scrub regex must not mask a real regression by crashing —
            # skip it and leave the text unscrubbed (fail toward showing diffs).
            continue
    return text


# ---------------------------------------------------------------------------
# Golden paths + running
# ---------------------------------------------------------------------------
def _flatten(rel):
    """fixtures/rides/a.fit -> fixtures__rides__a.fit (unique, one dir deep)."""
    return rel.replace("\\", "/").replace("/", "__")


def _golden_path(root, golden_dir, case_name, input_rel):
    return os.path.join(root, golden_dir, case_name,
                        _flatten(input_rel) + ".golden")


def _build_argv(command, input_path):
    argv = []
    for token in shlex.split(command, posix=True):
        token = token.replace("{python}", sys.executable)
        token = token.replace("{input}", input_path)
        argv.append(token)
    return argv


def _run_command(argv, cwd, capture, timeout):
    """Run argv; return (output_text, error_str). error_str is None on success."""
    kwargs = dict(cwd=cwd, text=True, encoding="utf-8", errors="replace",
                  timeout=timeout)
    try:
        if capture == "combined":
            proc = subprocess.run(argv, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, **kwargs)
            out = proc.stdout or ""
        else:
            proc = subprocess.run(argv, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, **kwargs)
            out = (proc.stderr if capture == "stderr" else proc.stdout) or ""
    except FileNotFoundError:
        return None, f"command not found: {argv[0] if argv else '(empty)'}"
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout}s"
    except OSError as e:
        return None, f"could not run command: {e}"
    return out, None


def _read_golden(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def _write_golden(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _diff(golden, actual, golden_rel):
    lines = difflib.unified_diff(
        golden.splitlines(), actual.splitlines(),
        fromfile=f"golden/{golden_rel}", tofile="actual", lineterm="")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def _run_case(root, case, update):
    """Run one case over its matched inputs. Returns a case-result dict."""
    name = case.get("name") or "unnamed"
    command = case.get("command")
    golden_dir = case.get("golden_dir") or DEFAULT_GOLDEN_DIR
    capture = case.get("capture") or "stdout"
    timeout = case.get("timeout") or DEFAULT_TIMEOUT
    scrubs = case.get("scrub") or []

    raw_inputs = case.get("inputs")
    if isinstance(raw_inputs, str):
        raw_inputs = [raw_inputs]

    result = {"name": name, "results": []}

    if not command:
        result["results"].append({
            "input": None, "status": "error",
            "detail": "case has no 'command'"})
        return result
    if not raw_inputs:
        result["results"].append({
            "input": None, "status": "error",
            "detail": "case has no 'inputs'"})
        return result

    # Expand globs (relative to root), de-duped and sorted for determinism.
    matched = []
    for pattern in raw_inputs:
        abs_pat = pattern if os.path.isabs(pattern) else os.path.join(root, pattern)
        matched.extend(glob.glob(abs_pat, recursive=True))
    matched = sorted({os.path.abspath(p) for p in matched if os.path.isfile(p)})

    if not matched:
        # A gate that silently checks nothing is the anti-pattern — fail closed.
        result["results"].append({
            "input": None, "status": "error",
            "detail": f"no input files matched: {', '.join(raw_inputs)}"})
        return result

    for input_abs in matched:
        input_rel = os.path.relpath(input_abs, root).replace("\\", "/")
        argv = _build_argv(command, input_abs)
        out, err = _run_command(argv, root, capture, timeout)
        if err is not None:
            result["results"].append({
                "input": input_rel, "status": "error", "detail": err})
            continue

        actual = _apply_scrubs(_normalize(out), scrubs)
        gpath = _golden_path(root, golden_dir, name, input_rel)
        grel = os.path.relpath(gpath, root).replace("\\", "/")
        golden = _read_golden(gpath)

        if golden is None:
            if update:
                _write_golden(gpath, actual)
                status, detail = "banked", grel
            else:
                status, detail = "new", grel
            result["results"].append({
                "input": input_rel, "status": status,
                "golden": grel, "detail": detail})
            continue

        golden_norm = _apply_scrubs(_normalize(golden), scrubs)
        if actual == golden_norm:
            result["results"].append({
                "input": input_rel, "status": "pass", "golden": grel})
        elif update:
            _write_golden(gpath, actual)
            result["results"].append({
                "input": input_rel, "status": "updated", "golden": grel})
        else:
            result["results"].append({
                "input": input_rel, "status": "regression", "golden": grel,
                "diff": _diff(golden_norm, actual, grel)})

    return result


def run_golden(root, config_path=None, update=False):
    """Run every configured golden case under *root*.

    Returns a dict: ``config`` (path or None), ``cases`` (per-case results),
    ``counts`` (status tally), and ``ok`` (True when the gate passes). In check
    mode ``ok`` is True only with no regression / new / error; in update mode
    regressions and new cases are banked, so ``ok`` reflects only errors. An
    explicitly passed *config_path* that doesn't exist fails (only
    auto-discovery finding nothing is an opt-out; a typo'd path must never go
    silently green).
    """
    root = os.path.abspath(root)
    if config_path and not os.path.isfile(config_path):
        return {"config": config_path, "cases": [], "counts": {}, "ok": False,
                "note": f"golden config not found: {config_path}"}
    path = find_config(root, config_path)
    result = {"config": path, "cases": [], "counts": {}, "ok": True}
    if not path:
        result["note"] = "no golden config (.golden.json) — nothing to check"
        return result

    cases, err = load_config(path)
    if err:
        result["ok"] = False
        result["note"] = err
        return result

    counts = {"pass": 0, "regression": 0, "new": 0,
              "error": 0, "banked": 0, "updated": 0}
    for case in cases:
        cres = _run_case(root, case, update)
        result["cases"].append(cres)
        for r in cres["results"]:
            counts[r["status"]] = counts.get(r["status"], 0) + 1

    result["counts"] = counts
    result["ok"] = (counts["error"] == 0
                    and counts["regression"] == 0
                    and counts["new"] == 0)
    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def print_golden(result):
    """Pretty-print the golden run for `drop.py golden`."""
    if not result.get("config"):
        print(f"\n  Golden files: {result.get('note', 'no config')}")
        return
    if not result.get("cases") and result.get("note"):
        print(f"\n  Golden config error: {result['note']}")
        return

    c = result["counts"]
    print(f"\n  Golden files — {os.path.basename(result['config'])}")
    for case in result["cases"]:
        print(f"\n  [{case['name']}]")
        for r in case["results"]:
            where = r.get("input") or "(case)"
            if r["status"] == "pass":
                print(f"    ok         {where}")
            elif r["status"] == "banked":
                print(f"    banked     {where}  -> {r.get('detail')}")
            elif r["status"] == "updated":
                print(f"    updated    {where}  -> {r.get('golden')}")
            elif r["status"] == "new":
                print(f"    NO GOLDEN  {where}  (run: drop.py golden --update)")
            elif r["status"] == "error":
                print(f"    ERROR      {where}  {r.get('detail')}")
            elif r["status"] == "regression":
                print(f"    REGRESSION {where}")
                for line in (r.get("diff") or "").splitlines():
                    print(f"        {line}")

    tally = (f"{c['pass']} ok, {c['regression']} regressions, {c['new']} missing, "
             f"{c['error']} errors")
    if c["banked"] or c["updated"]:
        tally += f", {c['banked']} banked, {c['updated']} updated"
    print(f"\n  {tally}")
    if result["ok"]:
        print("  Golden gate: PASS")
    else:
        print("  Golden gate: FAIL (fix the regression, or bank with --update if intended)")
