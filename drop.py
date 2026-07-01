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

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Ensure the script directory is on the path so imports work
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from engine import Engine, fmt_time, log, timed_input
from detect import detect, print_detection
from rules import build_all_rules, save_rules, load_rules
import catalog
import config


# ---------------------------------------------------------------------------
# Prompt gating
# ---------------------------------------------------------------------------
# Prompt call-site audit (all interactive prompts in the toolkit):
#   drop.py cmd_setup       input()      read-only (config/URLs/model pulls; empty ≠ 'y')
#   drop.py cmd_develop     apply_prompt WRITE-GATING (source files, tests)
#   drop.py cmd_test        apply_prompt WRITE-GATING (test files)
#   drop.py cmd_review      apply_prompt WRITE-GATING (fix may run with --apply)
#   drop.py cmd_full        apply_prompt WRITE-GATING (source files, tests)
#   fix.py  main            apply prompt WRITE-GATING (fix apply)
# All write-gating prompts default to 'n' on timeout (see engine.timed_input)
# and honour --yes for intentional unattended apply.
def apply_prompt(args, prompt):
    """Gate a file-writing action: --yes answers 'y', timeouts default to 'n'."""
    if getattr(args, "yes", False):
        log(f"{prompt} y (--yes)")
        return "y"
    return timed_input(prompt, args.timeout)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------
def cmd_setup(args, engine, info, rules):
    """Interactive 4-step setup wizard: connection, hardware, catalog, model install."""
    import hardware as hw_mod

    cfg = config.load()

    # ── Step 1 — Ollama connection ────────────────────────────────────────────
    print("\n  ── Step 1: Ollama connection ──")
    current_url = cfg.get("url", "http://localhost:11434")
    print(f"  Current URL: {current_url}")
    try:
        new_url = input(f"  Ollama URL [{current_url}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        new_url = ""
    if new_url and new_url != current_url:
        cfg["url"] = new_url
        args.url = new_url
        # Re-test with new URL
        ok, _, msg = engine._probe(new_url)
        print(f"  Connection: {msg}")
    else:
        print(f"  Connection: using {current_url}")

    current_code_url = cfg.get("code_url") or ""
    print(f"  Current code-model URL: {current_code_url or '(same as primary)'}")
    try:
        new_code_url = input(
            f"  Separate code-model host? [{current_code_url or 'leave blank to skip'}]: "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        new_code_url = ""
    if new_code_url:
        cfg["code_url"] = new_code_url
    elif not current_code_url:
        cfg["code_url"] = None

    # ── Step 2 — Hardware ────────────────────────────────────────────────────
    print("\n  ── Step 2: Hardware ──")
    hardware = getattr(engine, "_hardware", None) or hw_mod.detect_gpu()
    # Hardware summary was already printed by print_model_map() in main(); nothing extra needed.
    vram = hardware["vram_gb"]
    print(f"  VRAM budget: {vram:.1f} GB")

    # ── Step 3 — Model catalog ───────────────────────────────────────────────
    print("\n  ── Step 3: Model catalog ──")
    cat_ver = catalog.catalog_version()
    # Determine source of currently active catalog
    import catalog as _cat_mod
    if _cat_mod._catalog is not None and _cat_mod._catalog is not _cat_mod.BUILTIN_CATALOG:
        cat_source = "cached"
    else:
        import os as _os
        cat_source = "cached" if _os.path.isfile(_cat_mod.CATALOG_CACHE) else "builtin"
    print(f"  Catalog version: {cat_ver}  (source: {cat_source})")

    configured_cat_url = cfg.get("catalog_url", "")
    if configured_cat_url:
        try:
            answer = input(
                f"  Refresh catalog from {configured_cat_url}? [y/N]: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer == "y":
            try:
                new_cat = catalog.fetch(configured_cat_url)
                _cat_mod._catalog = new_cat   # update module-level cache
                print(f"  Catalog updated — version: {new_cat.get('version', 'unknown')}")
            except ValueError as e:
                print(f"  Catalog fetch failed: {e}")
    else:
        try:
            new_cat_url = input(
                "  Enter catalog URL to enable updates (leave blank to skip): "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            new_cat_url = ""
        if new_cat_url:
            try:
                new_cat = catalog.fetch(new_cat_url)
                _cat_mod._catalog = new_cat
                cfg["catalog_url"] = new_cat_url
                print(f"  Catalog fetched — version: {new_cat.get('version', 'unknown')}")
            except ValueError as e:
                print(f"  Catalog fetch failed: {e}")

    # ── Step 4 — Model installation ──────────────────────────────────────────
    print("\n  ── Step 4: Model installation ──")
    current = hw_mod.installed_models(args.url)
    installed_names = {m["name"] for m in current}

    rec = hw_mod.recommend_models(vram, installed_names, catalog.preferences())
    hw_mod.print_recommendation_table(rec, current)

    to_pull = [role_info["model"] for role_info in rec.values() if not role_info["installed"]]
    to_update = [role_info["model"] for role_info in rec.values() if role_info["installed"]]

    if not to_pull and not getattr(args, "update", False):
        log("\n  All recommended models are already installed.")
        log("  Run with --update to re-pull and check for newer versions.")
    else:
        if to_pull:
            total_gb = sum(hw_mod.model_size_gb(m) or 0 for m in to_pull)
            print(f"\n  Models to download ({len(to_pull)}):  ~{total_gb:.1f} GB total")
            for m in to_pull:
                sz = hw_mod.model_size_gb(m)
                print(f"    {m:<35} {'~' + str(sz) + ' GB' if sz else '?'}")

        if getattr(args, "update", False) and to_update:
            print(f"\n  Models to update ({len(to_update)}):")
            for m in to_update:
                print(f"    {m}")

        targets = to_pull + (to_update if getattr(args, "update", False) else [])
        if targets:
            try:
                answer = input(f"\n  Pull {len(targets)} model(s) now? [y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = ""

            if answer != "y":
                log("  Skipped — re-run without changes when ready.")
            else:
                ok_count = fail_count = 0
                for model in targets:
                    if hw_mod.pull_model(model, args.url):
                        ok_count += 1
                    else:
                        fail_count += 1
                log(f"\n  Done — {ok_count} pulled, {fail_count} failed.")
                if ok_count:
                    log("  Re-run any drop.py command to use the updated models.")

    # ── Save config ──────────────────────────────────────────────────────────
    saved_path = config.save(cfg)
    print(f"\n  Config saved to {saved_path}")


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
        answer = apply_prompt(args, "\n  Apply source files? [y/N]:")
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
            answer = apply_prompt(args, "\n  Apply tests? [y/N]:")
            if answer == "y":
                tgen._write()


def cmd_test(args, engine, info, rules):
    """Generate test suites."""
    from testgen import TestGenerator
    gen = TestGenerator(engine, info, rules)
    gen.run(apply=args.apply, layer_filter=args.layer,
            file_filter=args.file, integration=args.integration)

    if not args.apply and gen.generated:
        answer = apply_prompt(args, "\n  Apply tests? [y/N]:")
        if answer == "y":
            gen._write()


def cmd_review(args, engine, info, rules):
    """Run code review."""
    from review import Reviewer
    reviewer = Reviewer(engine, info, rules)
    reviewer.run(layer_filter=args.layer, file_filter=args.file,
                 skip_consolidation=args.skip_consolidation)

    # Write-gating when --apply is set (fix subprocess inherits it) — default 'n'.
    answer = apply_prompt(args, "\n  Run fix now? [y/N]:")
    if answer == "y":
        cmd_fix(args, engine, info, rules)


def cmd_fix(args, engine, info, rules):
    """Apply review fixes."""
    # Delegate to fix.py with forwarded args
    cmd = [sys.executable, os.path.join(SCRIPT_DIR, "fix.py")]
    if args.apply: cmd.append("--apply")
    if getattr(args, "yes", False): cmd.append("--yes")
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
        answer = apply_prompt(args, "\n  Apply source files? [y/N]:")
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
        answer = apply_prompt(args, "\n  Apply tests? [y/N]:")
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
                        help="Seconds to wait at each prompt before auto-proceeding with the "
                             "safe default 'n' (0 = wait forever)")
    parser.add_argument("--yes", action="store_true",
                        help="Answer 'y' to all apply prompts (intentional unattended apply)")

    # Subcommand-specific flags
    parser.add_argument("--plan-only", action="store_true", help="[develop] Just show plan")
    parser.add_argument("--integration", action="store_true", help="[test] Include integration tests")
    parser.add_argument("--skip-consolidation", action="store_true", help="[review] Skip pass 2")
    parser.add_argument("--skip-tests", action="store_true", help="[all] Skip test run phases")
    parser.add_argument("--update", action="store_true",
                        help="[setup] Re-pull already-installed models to check for updates")

    parser.add_argument("command", nargs="?", default="detect",
                        choices=["detect", "develop", "test", "review", "fix", "all", "full",
                                 "setup", "install"],
                        help="What to do (default: detect)")

    args = parser.parse_args()

    # ── Load config (CLI args override config file values) ──
    cfg = config.load()

    # Apply config defaults where CLI args were not explicitly set.
    # argparse defaults are indistinguishable from user-supplied values, so we
    # check against the parser defaults and only substitute config values when
    # the user left the arg at its default.
    _parser_defaults = {
        "url": "http://localhost:11434",
        "code_url": "http://localhost:11434",
    }
    if args.url == _parser_defaults["url"] and cfg.get("url"):
        args.url = cfg["url"]
    if args.code_url == _parser_defaults["code_url"]:
        # null code_url in config means "same as primary url"
        args.code_url = cfg.get("code_url") or args.url

    # Apply catalog URL from config if set
    if cfg.get("catalog_url"):
        import catalog as _cat_mod
        _cat_mod.CATALOG_URL = cfg["catalog_url"]

    # ── Setup engine ──
    # Start from config-defined model pins, then let explicit CLI flags override.
    models = dict(cfg.get("models", {}))
    if args.reason_model: models["reason"] = args.reason_model
    if args.code_model: models["code"] = args.code_model
    if args.quick_model: models["quick"] = args.quick_model

    engine = Engine(url=args.url, models=models, code_url=args.code_url,
                    role_ctx_caps=cfg.get("role_ctx_caps") or {})

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

    # setup/install only need Ollama connectivity — skip project detection and rules.
    if args.command in ("setup", "install"):
        cmd_setup(args, engine, info=None, rules=None)
        return

    # ── Detect project ──
    project_root = os.path.abspath(args.project)
    info = detect(project_root)

    # ── Build rules ──
    rules_path = os.path.join(info["root"], "docs", ".layer_rules.json")
    plan_only_mode = getattr(args, "plan_only", False)
    if os.path.isfile(rules_path) and (args.command != "develop" or plan_only_mode):
        log(f"  Loading saved rules from docs/.layer_rules.json")
        rules = load_rules(rules_path)
    elif plan_only_mode:
        # plan_only never calls _generate_files so rules are unused — skip LLM call
        rules = {}
    else:
        use_llm = not args.no_llm_rules and bool(info.get("arch_doc"))
        rules, _ = build_all_rules(engine, info, use_llm=use_llm)
        os.makedirs(os.path.dirname(rules_path), exist_ok=True)
        save_rules(rules, rules_path)
        log(f"  Rules saved to docs/.layer_rules.json")

    # ── Dispatch ──
    commands = {
        "detect":  cmd_detect,
        "develop": cmd_develop,
        "test":    cmd_test,
        "review":  cmd_review,
        "fix":     cmd_fix,
        "all":     cmd_all,
        "full":    cmd_full,
    }

    handler = commands.get(args.command, cmd_detect)
    handler(args, engine, info, rules)


if __name__ == "__main__":
    main()