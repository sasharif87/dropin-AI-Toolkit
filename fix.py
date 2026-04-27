"""
fix.py — Apply code review fixes via Ollama.

Usage:
    python fix.py                         # dry-run
    python fix.py --apply                 # write fixes
    python fix.py --layer api --apply     # one layer
"""

import argparse
import difflib
import json
import os
import py_compile
import re
import sys
import tempfile
import time
from collections import OrderedDict
from datetime import datetime

from engine import Engine, strip_fences, read_file, fmt_time, log, timed_input

# ---------------------------------------------------------------------------
# Fix prompt
# ---------------------------------------------------------------------------
FIX_PROMPT = """You are a senior engineer applying code review fixes.

Return the COMPLETE corrected file — no explanations, no fences, no commentary.
Only change what's needed. Preserve structure, imports, indentation. Skip issues
that need more context or changes to other files.

PRESERVATION RULES — these are not optional:
1. Do NOT remove or shorten module-level docstrings (the triple-quoted block at the top).
2. Do NOT remove section separator comments (lines like # ---...--- or # ===...===).
3. Do NOT remove inline comments or function/class docstrings. Only touch a comment
   if a finding explicitly names it as the problem.
4. Do NOT add @retry or retry logic to database session calls (session.execute,
   session.flush, session.commit) — they manage their own connection pool and
   retrying mid-transaction causes duplicate writes.
5. Preserve the exact indentation style (spaces/tabs) of the original file.

ISSUES:
{issues}

ORIGINAL FILE ({filepath}):
{code}
"""


# ---------------------------------------------------------------------------
# Report parser
# ---------------------------------------------------------------------------
def parse_report(path):
    """Parse review report → list of (rel_path, issues_text)."""
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    pattern = r"^## `([^`]+)`\s*\n(.*?)(?=^## `|^# Summary|^---\s*\n# |\Z)"
    matches = re.findall(pattern, content, re.MULTILINE | re.DOTALL)

    entries = []
    for rel, issues in matches:
        issues = issues.strip()
        if issues.startswith("**Skipped**") or issues.startswith("**Error**") or not issues:
            continue
        entries.append((rel.strip(), issues))
    return entries


# ---------------------------------------------------------------------------
# Validation helpers — run before applying any fix to catch automation damage
# ---------------------------------------------------------------------------
def _count_comment_lines(text):
    """Count lines that are comments or contain docstring delimiters."""
    return sum(1 for ln in text.splitlines()
               if ln.strip().startswith("#") or '"""' in ln or "'''" in ln)


def _syntax_check_py(content):
    """Compile-check Python content. Returns (ok, error_str)."""
    with tempfile.NamedTemporaryFile(suffix=".py", delete=False,
                                    mode="w", encoding="utf-8") as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        py_compile.compile(tmp_path, doraise=True)
        return True, None
    except py_compile.PyCompileError as e:
        return False, str(e)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Apply review fixes via Ollama")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--layer", type=str)
    parser.add_argument("--file", type=str)
    parser.add_argument("--report", type=str)
    parser.add_argument("--timeout", type=int, default=0,
                        help="Seconds to wait at prompt before auto-proceeding with 'y'")
    parser.add_argument("--ollama-url", type=str, default="http://localhost:11434")
    parser.add_argument("--code-url", type=str, default=None,
                        help="Ollama URL for code role. Defaults to --ollama-url.")
    parser.add_argument("--code-model", type=str)
    parser.add_argument("project_dir", nargs="?", default=".")
    args = parser.parse_args()

    root = os.path.abspath(args.project_dir)
    docs = os.path.join(root, "docs")

    models = {}
    if args.code_model: models["code"] = args.code_model
    engine = Engine(url=args.ollama_url, models=models, code_url=getattr(args, "code_url", None))
    ok, _, msg = engine.test()
    print(f"  Ollama: {msg}")
    if not ok: sys.exit(1)

    # Find report
    report = args.report
    if not report:
        con = os.path.join(docs, "code_review_report_consolidated.md")
        raw = os.path.join(docs, "code_review_report.md")
        report = con if os.path.isfile(con) else raw if os.path.isfile(raw) else None
    if not report or not os.path.isfile(report):
        log("No review report found. Run review.py first.")
        return

    entries = parse_report(report)
    if not entries:
        log("No issues in report.")
        return

    if args.file:
        entries = [(p, i) for p, i in entries if p == args.file]
    if args.layer:
        fset = {l.strip().lower() for l in args.layer.split(",")}
        entries = [(p, i) for p, i in entries
                   if any(p.startswith(l) or p.split("/")[-2] in fset for l in fset)]

    mode = "APPLY" if args.apply else "DRY-RUN"
    log(f"  Mode: {mode} | Files: {len(entries)} | Model: {engine.model_for('code')}")

    patch_dir = os.path.join(docs, "review_patches")
    os.makedirs(patch_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    patch_path = os.path.join(patch_dir, f"fixes_{ts}.patch")

    stats = {"fixed": 0, "no_change": 0, "skipped": 0, "errors": 0}
    pending = {}  # rel → fixed content, for apply-after-preview

    with open(patch_path, "w", encoding="utf-8") as pf:
        pf.write(f"# Fix patches — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")

        for i, (rel, issues) in enumerate(entries, 1):
            print(f"  [{i}/{len(entries)}] {rel}...", end=" ", flush=True)

            abs_path = os.path.join(root, rel)
            original, err = read_file(abs_path)
            if err or not original:
                stats["skipped"] += 1
                print(f"SKIP ({err or 'empty'})")
                continue

            prompt = FIX_PROMPT.format(issues=issues, filepath=rel, code=original)
            try:
                fixed = engine.generate(prompt, role="code")
                fixed = strip_fences(fixed)
            except Exception as e:
                stats["errors"] += 1
                print(f"ERROR ({e})")
                continue

            if not fixed.strip():
                stats["skipped"] += 1
                print("EMPTY")
                continue

            ratio = len(fixed) / max(len(original), 1)
            if ratio < 0.5 or ratio > 2.0:
                stats["skipped"] += 1
                print(f"SIZE_MISMATCH ({ratio:.2f})")
                continue

            # Syntax check Python files before accepting the fix
            if rel.endswith(".py"):
                ok, err_msg = _syntax_check_py(fixed)
                if not ok:
                    stats["errors"] += 1
                    print(f"SYNTAX_ERROR ({err_msg[:80]})")
                    continue

            # Comment/docstring preservation check — catch aggressive stripping
            orig_comments = _count_comment_lines(original)
            fixed_comments = _count_comment_lines(fixed)
            if orig_comments > 0 and fixed_comments / orig_comments < 0.75:
                stats["errors"] += 1
                print(f"COMMENT_STRIP ({fixed_comments}/{orig_comments} comment lines kept)")
                continue

            diff = list(difflib.unified_diff(
                original.splitlines(keepends=True), fixed.splitlines(keepends=True),
                fromfile=f"a/{rel}", tofile=f"b/{rel}",
            ))

            if not diff:
                stats["no_change"] += 1
                print("NO CHANGES")
                continue

            diff_text = "".join(diff)
            pf.write(diff_text + "\n")
            stats["fixed"] += 1

            if args.apply:
                with open(abs_path, "w", encoding="utf-8") as f:
                    f.write(fixed)
                print(f"FIXED ({len(diff)} diff lines)")
            else:
                preview_path = os.path.join(root, "tmp", "preview", "fixes",
                                            rel.replace("/", os.sep))
                os.makedirs(os.path.dirname(preview_path), exist_ok=True)
                with open(preview_path, "w", encoding="utf-8") as pf2:
                    pf2.write(fixed)
                pending[rel] = fixed
                print(f"PENDING ({len(diff)} diff lines)")

    if stats["fixed"] == 0 and os.path.isfile(patch_path):
        os.remove(patch_path)

    log(f"\n  Fixed: {stats['fixed']} | No change: {stats['no_change']} | "
        f"Skipped: {stats['skipped']} | Errors: {stats['errors']}")

    if pending:
        log(f"  Preview written to tmp/preview/fixes/ — inspect in IDE before applying.")
        answer = timed_input("  Apply now? [y/N]:", args.timeout)
        if answer == "y":
            for rel, content in pending.items():
                with open(os.path.join(root, rel), "w", encoding="utf-8") as f:
                    f.write(content)
            log(f"  Applied {len(pending)} fixes.")
        else:
            log(f"  Skipped — re-run with --apply to write.")
    elif not args.apply and stats["fixed"]:
        log(f"  Review patches in {patch_dir}/, then --apply")


if __name__ == "__main__":
    main()