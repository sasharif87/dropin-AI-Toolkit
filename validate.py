"""
validate.py — The default path: run every deterministic gate + advisory report
over a project, air-gapped, and return one aggregate verdict.

This is the CLI inversion (TODO.md, "validation-first harness"): validation is
what ``drop.py`` does by default, generation is opt-in. ``drop.py`` with no
command dispatches here — and, like the individual gates, it needs no Ollama and
no network. The sovereignty constraint applies to the toolkit's own front door:
you can validate a project on a fully local, air-gapped stack.

Two tiers, matching the split ``triggers.py`` already draws for the commit hook:

  - **Blocking gates** (fail-closed): ``layers``, ``invariants``, ``golden``.
    Each opts out cleanly when its config is absent, but a *configured* gate that
    can't run is a surfaced failure, never a silent pass. The aggregate ``ok`` is
    the AND of these — and it's the exit code, so a broken gate blocks CI or the
    commit hook exactly as running the gate alone would.
  - **Advisory reports** (never block): ``orphans``, ``claims``, and a summary of
    the last review's findings ledger (``docs/review_findings.json``). Print-only
    by design — their false-negative bias (never flag a used module, only gross
    claim divergence) means they inform, they don't gate. Surfacing them here is
    the "findings" half of "gates + findings"; keeping them out of ``ok`` is the
    same call ``triggers.py`` makes for the hook.

Deterministic and local throughout — the gate runners are ``ast``/text only and
the advisory finders re-derive from the repo; nothing here reaches for a model.
"""

import os

from layers import run_layers, print_layers
from invariants import run_invariants, print_invariants
from golden import run_golden, print_golden
from orphans import find_orphans, print_orphans
from claims import find_claim_issues, print_claims
from findings import load_findings, findings_path, SEVERITY_ORDER


# Blocking gate name -> runner. Each runner takes (root, config_path=None) and
# returns a dict with an ``ok`` key and a ``config`` (None when the repo opted
# out). Kept in the same order triggers.py installs them.
GATE_RUNNERS = (
    ("layers", run_layers),
    ("invariants", run_invariants),
    ("golden", run_golden),
)

GATE_PRINTERS = {
    "layers": print_layers,
    "invariants": print_invariants,
    "golden": print_golden,
}


# ---------------------------------------------------------------------------
# Advisory: last review's findings ledger
# ---------------------------------------------------------------------------
def _findings_summary(root):
    """Summarize docs/review_findings.json, or None when there's no ledger.

    Advisory only — the ledger is a *past* review's output, surfaced so the
    validation view is complete. It never affects the verdict.
    """
    data = load_findings(root)
    if not data:
        return None
    findings = data.get("findings", [])
    by_severity = {}
    for f in findings:
        sev = f.get("severity", "?")
        by_severity[sev] = by_severity.get(sev, 0) + 1
    return {
        "total": len(findings),
        "by_severity": by_severity,
        "path": os.path.relpath(findings_path(root), root),
    }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def run_validate(root, layers_config=None, invariants_config=None,
                 golden_config=None):
    """Run every gate + advisory report under *root*. Returns an aggregate dict.

    Keys: ``root``; ``gates`` (list of ``{"name", "result"}`` for each blocking
    gate, in install order); ``configured`` (names of gates the repo opted into);
    ``advisories`` (``orphans`` / ``claims`` finding lists + ``findings`` ledger
    summary or None); and ``ok`` — the AND of the blocking gates' ``ok``. An
    unconfigured gate is ``ok`` (opt-out), so a repo with no gates validates green
    but ``configured`` is empty, which ``print_validate`` calls out loudly (a gate
    pack that enforces nothing is the anti-pattern, not a pass to celebrate).
    """
    root = os.path.abspath(root)
    config_for = {
        "layers": layers_config,
        "invariants": invariants_config,
        "golden": golden_config,
    }
    gates = [
        {"name": name, "result": runner(root, config_path=config_for[name])}
        for name, runner in GATE_RUNNERS
    ]
    configured = [g["name"] for g in gates if g["result"].get("config")]

    advisories = {
        "orphans": find_orphans(root),
        "claims": find_claim_issues(root),
        "findings": _findings_summary(root),
    }

    return {
        "root": root,
        "gates": gates,
        "configured": configured,
        "advisories": advisories,
        "ok": all(g["result"]["ok"] for g in gates),
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def _print_findings_summary(summary):
    if not summary:
        print("\n  Review findings: none on record (run `drop.py review`)")
        return
    ranked = sorted(summary["by_severity"].items(),
                    key=lambda kv: SEVERITY_ORDER.get(kv[0], 9))
    parts = ", ".join(f"{n} {sev}" for sev, n in ranked)
    detail = f" — {parts}" if parts else ""
    print(f"\n  Review findings: {summary['total']} on record{detail}  "
          f"({summary['path']})")


def print_validate(result):
    """Pretty-print the aggregate run for `drop.py validate` (the default)."""
    # Blocking gates first, each via its own printer (so `drop.py validate` and
    # `drop.py layers` render a gate identically).
    for g in result["gates"]:
        GATE_PRINTERS[g["name"]](g["result"])

    # Advisory reports — informational, never flip the verdict.
    adv = result["advisories"]
    print_orphans(adv["orphans"])
    print_claims(adv["claims"])
    _print_findings_summary(adv["findings"])

    # Aggregate verdict.
    configured = result["configured"]
    print("\n" + "-" * 60)
    if not configured:
        print("  Validation: no blocking gates configured — nothing is enforced.")
        print("  Add a .layers.json / .invariants.py / .golden.json to opt in,")
        print("  then `drop.py hooks` to run them on every commit.")
    elif result["ok"]:
        print(f"  Validation: PASS — {len(configured)} gate(s) green "
              f"({', '.join(configured)}).")
    else:
        failed = [g["name"] for g in result["gates"] if not g["result"]["ok"]]
        print(f"  Validation: FAIL — {', '.join(failed)} "
              f"(see the gate output above).")
