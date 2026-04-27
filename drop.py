#!/usr/bin/env python3
"""
drop.py — Drop into any project and build it.

This is the single entry point. It:
  1. Auto-detects your project structure (language, framework, layers)
  2. Auto-generates layer rules matching production patterns
  3. Reads your architecture doc (if present) for project-specific rules
  4. Scaffolds code, generates tests, reviews, fixes — whatever you ask

Model switching happens automatically:
  - Reasoning model for architecture analysis and rule generation
  - Code model for file generation and fixes
  - Quick model for classification and yes/no decisions

Usage:
    python drop.py                             # detect + show plan
    python drop.py develop                     # scaffold from arch doc
    python drop.py develop --apply             # write scaffolded files
    python drop.py test                        # generate test suites
    python drop.py test --apply                # write test files
    python drop.py review                      # code review
    python drop.py fix --apply                 # apply review fixes
    python drop.py all                         # full pipeline (dry-run)
    python drop.py all --apply                 # full pipeline (write)

    python drop.py --layer api,db develop      # target specific layers
    python drop.py --reason-model deepseek-r1:14b develop

    python drop.py --url http://remote_host:11434  # use remote host instead
"""

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime

# Ensure the script directory is on the path so imports work
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from engine import Engine, fmt_time, log, timed_input
from detect import detect, print_detection
from rules import build_all_rules, save_rules, load_rules


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------
def cmd_detect(args, engine, info, rules):
    """Just detect and show what we found."""
    print_detection(info)
    print(f"\n  Rules generated for {len(rules)} layers.")
    for key, r in rules.items():
        count = len([l for l in r.split("\n") if l.strip().startswith("-")])
        print(f"    {key:<30} {count:>2} rules")
    print(f"\n  Next steps:")
    if info.get("arch_doc"):
        print(f"    python {os.path.basename(__file__)} develop          # scaffold from arch doc")
    else:
        print(f"    Create docs/ARCHITECTURE.md first, then:")
        print(f"    python {os.path.basename(__file__)} develop          # scaffold from arch doc")
    if info["file_count"] > 0:
        print(f"    python {os.path.basename(__file__)} test             # generate tests")
        print(f"    python {os.path.basename(__file__)} review           # code review")


def cmd_develop(args, engine, info, rules):
    """Scaffold code from architecture doc."""
    from develop import Developer
    dev = Developer(engine, info, rules)
    dev.run(apply=args.apply, layer_filter=args.layer, plan_only=args.plan_only)

    applied = args.apply
    if not args.apply and not args.plan_only and dev.generated:
        answer = timed_input("\n  Apply source files? [y/N]:", args.timeout)
        if answer == "y":
            log("  Applying...")
            dev._write_files()
            applied = True

    if applied and not args.plan_only:
        from detect import detect as _redetect
        from testgen import TestGenerator
        log("\n  Generating tests for scaffolded files...")
        fresh_info = _redetect(info["root"])
        tgen = TestGenerator(engine, fresh_info, rules)
        tgen.run(apply=False, layer_filter=args.layer)
        if tgen.generated:
            try:
                answer = input("\n  Apply tests? [y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer == "y":
                tgen._write()


def cmd_test(args, engine, info, rules):
    """Generate test suites."""
    from testgen import TestGenerator
    gen = TestGenerator(engine, info, rules)
    gen.run(apply=args.apply, layer_filter=args.layer,
            file_filter=args.file, integration=args.integration)

    if not args.apply and gen.generated:
        answer = timed_input("\n  Apply tests? [y/N]:", args.timeout)
        if answer == "y":
            gen._write()


def cmd_review(args, engine, info, rules):
    """Run code review."""
    from review import Reviewer
    reviewer = Reviewer(engine, info, rules)
    reviewer.run(layer_filter=args.layer, file_filter=args.file,
                 skip_consolidation=args.skip_consolidation)

    answer = timed_input("\n  Run fix now? [y/N]:", args.timeout)
    if answer == "y":
        cmd_fix(args, engine, info, rules)


def cmd_fix(args, engine, info, rules):
    """Apply review fixes."""
    # Delegate to fix.py with forwarded args
    cmd = [sys.executable, os.path.join(SCRIPT_DIR, "fix.py")]
    if args.apply: cmd.append("--apply")
    if args.layer: cmd.extend(["--layer", args.layer])
    if args.file: cmd.extend(["--file", args.file])
    cmd.extend(["--ollama-url", args.url])
    if hasattr(args, "code_url") and args.code_url != args.url:
        cmd.extend(["--code-url", args.code_url])
    if args.timeout > 0:
        cmd.extend(["--timeout", str(args.timeout)])
    cmd.append(info["root"])
    subprocess.run(cmd)


def cmd_all(args, engine, info, rules):
    """Full pipeline: develop → test → review → fix."""
    start = time.time()
    phases = []

    # Phase 1: Develop (if arch doc exists and project is new-ish)
    if info.get("arch_doc"):
        log("=" * 60)
        log("  PHASE 1 — Develop from Architecture Doc")
        log("=" * 60)
        from develop import Developer
        dev = Developer(engine, info, rules)
        dev.run(apply=args.apply, layer_filter=args.layer)
        phases.append("develop")

        # Re-detect after scaffolding (new files exist now)
        if args.apply:
            info = detect(info["root"])

    # Phase 2: Generate tests
    log("\n" + "=" * 60)
    log("  PHASE 2 — Generate Test Suites")
    log("=" * 60)
    from testgen import TestGenerator
    gen = TestGenerator(engine, info, rules)
    gen.run(apply=args.apply, layer_filter=args.layer)
    phases.append("test")

    # Phase 3: Run existing tests
    if not args.skip_tests:
        log("\n" + "=" * 60)
        log("  PHASE 3 — Run Tests (baseline)")
        log("=" * 60)
        tf = info["stack"].get("test_framework", "pytest")
        td = info.get("has_tests") or "tests"
        if tf == "pytest":
            cmd = [sys.executable, "-m", "pytest", td, "-m", "not integration",
                   "--tb=short", "-q", "--no-header"]
        else:
            cmd = ["npx", tf, "--passWithNoTests"]
        result = subprocess.run(cmd, cwd=info["root"], capture_output=True, text=True)
        print(result.stdout[-500:] if result.stdout else "(no output)")
        phases.append("baseline_tests")

    # Phase 4: Code review
    log("\n" + "=" * 60)
    log("  PHASE 4 — Code Review")
    log("=" * 60)
    from review import Reviewer
    reviewer = Reviewer(engine, info, rules)
    reviewer.run(layer_filter=args.layer)
    phases.append("review")

    # Phase 5: Apply fixes
    if args.apply:
        log("\n" + "=" * 60)
        log("  PHASE 5 — Apply Fixes")
        log("=" * 60)
        fix_cmd = [sys.executable, os.path.join(SCRIPT_DIR, "fix.py"),
                   "--apply", "--ollama-url", args.url,
                   "--code-url", getattr(args, "code_url", args.url), info["root"]]
        if args.layer: fix_cmd.extend(["--layer", args.layer])
        subprocess.run(fix_cmd)
        phases.append("fix")

        # Phase 6: Post-fix tests
        if not args.skip_tests:
            log("\n" + "=" * 60)
            log("  PHASE 6 — Post-Fix Tests")
            log("=" * 60)
            if tf == "pytest":
                cmd = [sys.executable, "-m", "pytest", td, "-m", "not integration",
                       "--tb=short", "-q", "--no-header"]
            else:
                cmd = ["npx", tf, "--passWithNoTests"]
            result = subprocess.run(cmd, cwd=info["root"], capture_output=True, text=True)
            print(result.stdout[-500:] if result.stdout else "(no output)")
            phases.append("post_fix_tests")

    elapsed = time.time() - start
    log(f"\n{'='*60}")
    log(f"  COMPLETE — {', '.join(phases)} — {fmt_time(elapsed)}")
    log(f"{'='*60}")


def cmd_full(args, engine, info, rules):
    """Interactive pipeline: rules -> develop -> review -> fix -> test."""
    from detect import detect as _redetect
    from develop import Developer
    from review import Reviewer
    from testgen import TestGenerator
    from rules import build_all_rules, save_rules

    start = time.time()

    # Phase 1: Regenerate rules from arch doc
    log("=" * 60)
    log("  PHASE 1 — Regenerate Rules")
    log("=" * 60)
    rules, _ = build_all_rules(engine, info, use_llm=True)
    rules_path = os.path.join(info["root"], "docs", ".layer_rules.json")
    save_rules(rules, rules_path)
    log(f"  Rules saved.")

    # Phase 2: Develop
    log("\n" + "=" * 60)
    log("  PHASE 2 — Develop from Architecture Doc")
    log("=" * 60)
    dev = Developer(engine, info, rules)
    dev.run(apply=False, layer_filter=args.layer, plan_only=args.plan_only)

    dev_applied = False
    if not args.plan_only and dev.generated:
        answer = timed_input("\n  Apply source files? [y/N]:", args.timeout)
        if answer == "y":
            dev._write_files()
            dev_applied = True

    if not dev_applied and not args.plan_only:
        log("  Skipped develop — stopping pipeline.")
        return

    fresh_info = _redetect(info["root"])

    # Phase 3: Generate tests
    log("\n" + "=" * 60)
    log("  PHASE 3 — Generate Tests")
    log("=" * 60)
    tgen = TestGenerator(engine, fresh_info, rules)
    tgen.run(apply=False, layer_filter=args.layer, integration=args.integration)
    if tgen.generated:
        answer = timed_input("\n  Apply tests? [y/N]:", args.timeout)
        if answer == "y":
            tgen._write()

    # Phase 4: Code review
    log("\n" + "=" * 60)
    log("  PHASE 4 — Code Review")
    log("=" * 60)
    fresh_info = _redetect(info["root"])
    reviewer = Reviewer(engine, fresh_info, rules)
    reviewer.run(layer_filter=args.layer, skip_consolidation=args.skip_consolidation)

    # Phase 5: Fix
    log("\n" + "=" * 60)
    log("  PHASE 5 — Apply Fixes")
    log("=" * 60)
    cmd_fix(args, engine, fresh_info, rules)

    # Phase 6: Run tests
    log("\n" + "=" * 60)
    log("  PHASE 6 — Run Tests")
    log("=" * 60)
    fresh_info = _redetect(info["root"])
    td = fresh_info.get("has_tests") or "tests"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", td, "-m", "not integration",
         "--tb=short", "-q", "--no-header"],
        cwd=info["root"], capture_output=True, text=True,
    )
    print(result.stdout[-1000:] if result.stdout else "(no test output)")

    log(f"\n{'='*60}")
    log(f"  COMPLETE — {fmt_time(time.time() - start)}")
    log(f"{'='*60}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Drop-in project scaffolder, test generator, reviewer, and fixer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Global options
    parser.add_argument("--url", type=str, default="http://localhost:11434",
                        help="Ollama URL for quick+reason roles (default: http://localhost:11434)")
    parser.add_argument("--code-url", type=str, default="http://localhost:11434",
                        help="Ollama URL for code role (default: http://localhost:11434)")
    parser.add_argument("--reason-model", type=str, help="Pin reasoning model")
    parser.add_argument("--code-model", type=str, help="Pin code model")
    parser.add_argument("--quick-model", type=str, help="Pin quick model")
    parser.add_argument("--project", type=str, default=".", help="Project root directory")
    parser.add_argument("--layer", type=str, help="Target specific layers (comma-separated)")
    parser.add_argument("--file", type=str, help="Target a specific file")
    parser.add_argument("--apply", action="store_true", help="Write files (default: dry-run)")
    parser.add_argument("--no-llm-rules", action="store_true",
                        help="Skip LLM-based rule generation (pattern rules only)")
    parser.add_argument("--timeout", type=int, default=0,
                        help="Seconds to wait at each prompt before auto-proceeding with 'y' (0 = wait forever)")

    # Subcommand-specific flags
    parser.add_argument("--plan-only", action="store_true", help="[develop] Just show plan")
    parser.add_argument("--integration", action="store_true", help="[test] Include integration tests")
    parser.add_argument("--skip-consolidation", action="store_true", help="[review] Skip pass 2")
    parser.add_argument("--skip-tests", action="store_true", help="[all] Skip test run phases")

    parser.add_argument("command", nargs="?", default="detect",
                        choices=["detect", "develop", "test", "review", "fix", "all", "full"],
                        help="What to do (default: detect)")

    args = parser.parse_args()

    # ── Setup engine ──
    models = {}
    if args.reason_model: models["reason"] = args.reason_model
    if args.code_model: models["code"] = args.code_model
    if args.quick_model: models["quick"] = args.quick_model

    engine = Engine(url=args.url, models=models, code_url=args.code_url)

    log("=" * 60)
    log("  DROP — Project Scaffolder & Dev Toolkit")
    log("=" * 60)

    ok, available, msg = engine.test()
    print(f"\n  Ollama: {msg}")
    if not ok:
        print(f"\n  Cannot reach Ollama at {args.url}")
        print(f"  Make sure it's running: ollama serve")
        sys.exit(1)
    engine.print_model_map()

    # ── Detect project ──
    project_root = os.path.abspath(args.project)
    info = detect(project_root)

    # ── Build rules ──
    rules_path = os.path.join(info["root"], "docs", ".layer_rules.json")
    if os.path.isfile(rules_path) and args.command != "develop":
        log(f"  Loading saved rules from docs/.layer_rules.json")
        rules = load_rules(rules_path)
    else:
        use_llm = not args.no_llm_rules and bool(info.get("arch_doc"))
        rules, _ = build_all_rules(engine, info, use_llm=use_llm)
        os.makedirs(os.path.dirname(rules_path), exist_ok=True)
        save_rules(rules, rules_path)
        log(f"  Rules saved to docs/.layer_rules.json")

    # ── Dispatch ──
    commands = {
        "detect": cmd_detect,
        "develop": cmd_develop,
        "test": cmd_test,
        "review": cmd_review,
        "fix": cmd_fix,
        "all": cmd_all,
        "full": cmd_full,
    }

    handler = commands.get(args.command, cmd_detect)
    handler(args, engine, info, rules)


if __name__ == "__main__":
    main()