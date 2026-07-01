"""
fix.py — Apply code review fixes via Ollama.

Reads structured findings from docs/review_findings.json (written by
review.py). The legacy markdown-report path survives only behind the
deprecated --report flag.

Usage:
    python fix.py                         # dry-run
    python fix.py --apply                 # write fixes
    python fix.py --layer api --apply     # one layer
"""

import argparse
import difflib
import os
import py_compile
import re
import sys
import tempfile
from collections import OrderedDict
from datetime import datetime

from engine import Engine, strip_fences, read_file, log, timed_input
import config
import findings as fnd

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
# Findings input — JSON is the contract with review.py
# ---------------------------------------------------------------------------
def _format_issue(f):
    """Render one finding as an issue line for the fix prompt."""
    parts = []
    if f.get("line") is not None:
        parts.append(f"Line {f['line']}:")
    tags = "/".join(t for t in (f.get("severity"), f.get("rule_label")) if t)
    if tags:
        parts.append(f"[{tags}]")
    parts.append(f.get("description", ""))
    if f.get("quote"):
        parts.append(f"(code: `{f['quote']}`)")
    return "- " + " ".join(parts)


def entries_from_findings(root):
    """Load docs/review_findings.json → list of (rel_path, issues_text, findings).

    Findings already rejected by the test gate are skipped so re-runs don't
    retry a fix that broke tests. Returns None when no findings file exists.
    """
    data = fnd.load_findings(root)
    if data is None:
        return None
    by_file = OrderedDict()
    for f in data["findings"]:
        if f.get("fix_rejected"):
            continue
        by_file.setdefault(f["file"], []).append(f)
    return [(rel, "\n".join(_format_issue(f) for f in items), items)
            for rel, items in by_file.items()]


def parse_report(path):
    """DEPRECATED legacy parser for prose markdown reports (--report only).

    Markdown reports are now rendered views of review_findings.json; this
    regex path exists only so old reports remain usable.
    Returns list of (rel_path, issues_text, []).
    """
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    pattern = r"^## `([^`]+)`\s*\n(.*?)(?=^## `|^# Summary|^---\s*\n# |\Z)"
    matches = re.findall(pattern, content, re.MULTILINE | re.DOTALL)

    entries = []
    for rel, issues in matches:
        issues = issues.strip()
        if issues.startswith("**Skipped**") or issues.startswith("**Error**") or not issues:
            continue
        entries.append((rel.strip(), issues, []))
    return entries


# ---------------------------------------------------------------------------
# Validation helpers — run before applying any fix to catch automation damage
# ---------------------------------------------------------------------------
def _count_comment_lines(text):
    """Count lines that are comments or contain docstring delimiters."""
    return sum(1 for ln in text.splitlines()
               if ln.strip().startswith("#") or '"""' in ln or "'''" in ln)


def _public_identifiers(source):
    """Return the set of public top-level def/class names in Python source."""
    return {
        m.group(1)
        for m in re.finditer(r"^(?:def|class)\s+([A-Za-z_]\w*)", source, re.MULTILINE)
        if not m.group(1).startswith("_")
    }


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
    parser.add_argument("--report", type=str,
                        help="DEPRECATED: parse a legacy markdown report instead of "
                             "docs/review_findings.json")
    parser.add_argument("--timeout", type=int, default=0,
                        help="Seconds to wait at prompt before auto-proceeding with the safe default 'n'")
    parser.add_argument("--yes", action="store_true",
                        help="Answer 'y' to the apply prompt (intentional unattended apply)")
    parser.add_argument("--ollama-url", type=str, default="http://localhost:11434")
    parser.add_argument("--code-url", type=str, default=None,
                        help="Ollama URL for code role. Defaults to --ollama-url.")
    parser.add_argument("--code-model", type=str)
    parser.add_argument("project_dir", nargs="?", default=".")
    args = parser.parse_args()

    root = os.path.abspath(args.project_dir)
    docs = os.path.join(root, "docs")

    cfg = config.load()
    if args.ollama_url == "http://localhost:11434" and cfg.get("url"):
        args.ollama_url = cfg["url"]
    code_url = getattr(args, "code_url", None)
    if not code_url or code_url == "http://localhost:11434":
        code_url = config.effective_code_url(cfg)

    models = dict(cfg.get("models", {}))
    if args.code_model: models["code"] = args.code_model
    engine = Engine(url=args.ollama_url, models=models, code_url=code_url,
                    role_ctx_caps=cfg.get("role_ctx_caps") or {})
    ok, _, msg = engine.test()
    print(f"  Ollama: {msg}")
    if not ok: sys.exit(1)

    # Load findings — JSON is the default; --report is the deprecated legacy path.
    if args.report:
        log(f"WARNING: --report is deprecated. Markdown reports are rendered views of "
            f"docs/{fnd.FINDINGS_NAME}; run a fresh review to use the JSON path.")
        if not os.path.isfile(args.report):
            log(f"Report not found: {args.report}")
            return
        entries = parse_report(args.report)
    else:
        entries = entries_from_findings(root)
        if entries is None:
            log(f"No docs/{fnd.FINDINGS_NAME} found. Run review.py first.")
            return
    if not entries:
        log("No open findings to fix.")
        return

    if args.file:
        entries = [(p, i, fs) for p, i, fs in entries if p == args.file]
    if args.layer:
        fset = {l.strip().lower() for l in args.layer.split(",")}
        entries = [(p, i, fs) for p, i, fs in entries
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

        for i, (rel, issues, file_findings) in enumerate(entries, 1):
            print(f"  [{i}/{len(entries)}] {rel}...", end=" ", flush=True)

            abs_path = os.path.join(root, rel)
            original, err = read_file(abs_path)
            if err or not original:
                stats["skipped"] += 1
                print(f"SKIP ({err or 'empty'})")
                continue

            # Guard: file must fit within the model's context budget.
            # Sending a truncated file would produce a partial fix that overwrites the original.
            if len(original) > engine.content_budget("code"):
                stats["skipped"] += 1
                print(f"TOO_LARGE ({len(original):,} chars > budget {engine.content_budget('code'):,})")
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

            # Public API preservation — catch model hallucinating renamed functions/classes
            if rel.endswith(".py"):
                orig_ids = _public_identifiers(original)
                fixed_ids = _public_identifiers(fixed)
                missing = orig_ids - fixed_ids
                if orig_ids and len(missing) / len(orig_ids) > 0.10:
                    stats["errors"] += 1
                    shown = ", ".join(sorted(missing)[:4])
                    print(f"API_LOSS ({len(missing)} public names removed: {shown}{'...' if len(missing) > 4 else ''})")
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
        # Write-gating prompt: timeout defaults to 'n'; --yes is the explicit opt-in.
        answer = "y" if args.yes else timed_input("  Apply now? [y/N]:", args.timeout)
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