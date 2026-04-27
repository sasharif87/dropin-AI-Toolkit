"""
review.py — Layer-aware code review with grounding check.

Uses the code model for per-file review, reasoning model for consolidation.
Auto-generates rules if none exist. Strips hallucinated findings via grounding check.

Usage:
    python review.py                           # review all
    python review.py --layer api,db            # specific layers
    python review.py --file backend/api/main.py
    python review.py --skip-consolidation
"""

import argparse
import os
import re
import sys
import time
from collections import OrderedDict
from datetime import datetime

from engine import Engine, read_file, fmt_time, log
from detect import detect, print_detection
from rules import build_all_rules, load_rules, save_rules

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
REVIEW_PROMPT = """You are a senior principal engineer reviewing code for the {project_name} project.

REVIEW RULES — follow strictly:
1. DO NOT summarize what the code does.
2. DO NOT give generic advice ("add type hints", "add docstrings") unless there's a concrete bug.
3. DO check for violations of the layer rules below.
4. DO check for real bugs, logic errors, security issues, race conditions, data integrity problems.
5. If the file is clean, respond with EXACTLY "OK" and nothing else.
6. Each finding MUST include:
   a. Line number (e.g. "Line 42:")
   b. Backtick-quoted snippet of the EXACT problematic code
   c. What's wrong and which rule it violates
7. CRITICAL: If you cannot quote the exact code, DO NOT report it.
8. Top 5 max. Priority: bugs > security > rule violations > style.

LAYER RULES:
{rules}

File: {filepath}

```
{code}
```
"""

CONSOLIDATION_PROMPT = """You are a senior principal engineer doing a second-pass review consolidation.

Given raw first-pass findings, produce a clean consolidated review:
1. Deduplicate same-root-cause findings across files
2. Cross-reference related findings between files
3. Remove noise (generic advice, summaries, empty __init__.py)
4. Rank: bugs > security > architecture > style
5. End with 1-2 sentence health assessment

OUTPUT FORMAT:
### [Layer Name]
**Health**: [one-line assessment]
**Findings** (ranked):
- **[severity]** `file` — description
...

If no real findings: "**Health**: Clean — no actionable issues."

RAW FINDINGS:
{findings}
"""


# ---------------------------------------------------------------------------
# Grounding check — strip hallucinated findings
# ---------------------------------------------------------------------------
def ground_findings(text, source):
    bullet_re = re.compile(r"^(\s*[-*•]\s+)", re.MULTILINE)
    quote_re = re.compile(r"`([^`]{6,})`")
    segments = bullet_re.split(text)
    output, kept, dropped = [], 0, 0

    if segments:
        output.append(segments[0])

    i = 1
    while i + 1 < len(segments):
        marker, body = segments[i], segments[i + 1]
        full = marker + body
        i += 2

        quotes = quote_re.findall(full)
        if any(q.strip() in source for q in quotes):
            output.append(full)
            kept += 1
        else:
            dropped += 1

    return "".join(output), kept, dropped


# ---------------------------------------------------------------------------
# Reviewer
# ---------------------------------------------------------------------------
class Reviewer:
    def __init__(self, engine, project_info, rules):
        self.engine = engine
        self.info = project_info
        self.rules = rules

    def run(self, layer_filter=None, file_filter=None, skip_consolidation=False):
        start = time.time()
        root = self.info["root"]
        layers = self.info["layers"]
        name = self.info["name"]

        # Collect files
        all_files = []
        for key, layer in layers.items():
            for fpath in layer.get("files", []):
                all_files.append((fpath, key))

        if layer_filter:
            fset = {l.strip().lower() for l in layer_filter.split(",")}
            all_files = [(f, k) for f, k in all_files
                         if k in fset or k.split("/")[-1] in fset]
        if file_filter:
            all_files = [(f, k) for f, k in all_files if f == file_filter]

        log(f"  Reviewing {len(all_files)} files with {self.engine.model_for('code')}")

        # Setup reports
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        docs_dir = os.path.join(root, "docs")
        os.makedirs(docs_dir, exist_ok=True)
        report_path = os.path.join(docs_dir, "code_review_report.md")
        layer_dir = os.path.join(docs_dir, "review_by_layer")
        os.makedirs(layer_dir, exist_ok=True)

        stats = {"total": 0, "issues": 0, "clean": 0, "skipped": 0, "grounding_dropped": 0}
        layer_content = OrderedDict()
        current_layer = None

        with open(report_path, "w", encoding="utf-8") as rpt:
            rpt.write(f"# {name} — Code Review\n\n**Date**: {ts}  \n")
            rpt.write(f"**Code model**: `{self.engine.model_for('code')}`  \n\n---\n\n")

            for i, (rel, layer_key) in enumerate(all_files, 1):
                if layer_key != current_layer:
                    current_layer = layer_key
                    layer_content.setdefault(layer_key, [])
                    print(f"\n  {'-'*55}")
                    print(f"  {self.info['layers'].get(layer_key, {}).get('name', layer_key)}")
                    print(f"  {'-'*55}")

                print(f"    [{i}/{len(all_files)}] {rel}...", end=" ", flush=True)
                stats["total"] += 1

                abs_path = os.path.join(root, rel)
                code, err = read_file(abs_path)
                if err:
                    stats["skipped"] += 1
                    print(f"SKIP ({err})")
                    continue
                if not code.strip() or os.path.basename(rel) in ("__init__.py", "conftest.py"):
                    stats["clean"] += 1
                    print("SKIP")
                    continue

                layer_rules = self.rules.get(layer_key, "")
                if not layer_rules:
                    for rk, rv in self.rules.items():
                        if layer_key.endswith(rk) or rk.endswith(layer_key.split("/")[-1]):
                            layer_rules = rv
                            break

                result, grounding_dropped = self._review_file(rel, code, layer_rules)
                stats["grounding_dropped"] += grounding_dropped

                if result is None:
                    stats["clean"] += 1
                    print("OK")
                else:
                    stats["issues"] += 1
                    rpt.write(result + "---\n\n")
                    layer_content[layer_key].append(result)
                    print("ISSUES")

            # Summary
            rpt.write(f"\n---\n\n# Summary\n\n| Metric | Count |\n|---|---|\n")
            for k, v in stats.items():
                rpt.write(f"| {k} | {v} |\n")

        # Per-layer reports
        for lk, entries in layer_content.items():
            if entries:
                lname = self.info["layers"].get(lk, {}).get("name", lk)
                lpath = os.path.join(layer_dir, f"review_{lk.replace('/', '_')}.md")
                with open(lpath, "w", encoding="utf-8") as f:
                    f.write(f"# {lname} — Review\n\n---\n\n")
                    for e in entries:
                        f.write(e + "---\n\n")

        log(f"\n  Pass 1: {stats['issues']} issues, {stats['clean']} clean, "
            f"{stats['skipped']} skipped, {stats['grounding_dropped']} grounding drops")
        log(f"  Report: {report_path}")

        # Pass 2: Consolidation
        if not skip_consolidation and any(layer_content.values()):
            log("\n  Pass 2 — Consolidation (reasoning model)")
            con_path = report_path.replace(".md", "_consolidated.md")
            self._consolidate(layer_content, con_path, ts)
            log(f"  Consolidated: {con_path}")

        log(f"\n  Total: {fmt_time(time.time() - start)}")

    def _review_file(self, rel, code, rules):
        prompt = REVIEW_PROMPT.format(
            project_name=self.info["name"],
            rules=rules or "(no layer rules)",
            filepath=rel,
            code=code[:30_000],
        )

        try:
            reply = self.engine.generate(prompt, role="code")
        except Exception as e:
            return f"## `{rel}`\n\n**Error**: {e}\n\n", 0

        if not reply or reply.strip() == "OK":
            return None, 0

        lines = reply.splitlines()
        bullets = sum(1 for l in lines if l.strip().startswith(("-", "*", "•")))
        if bullets == 0 and len(lines) > 5:
            return None, 0

        grounded, kept, dropped = ground_findings(reply, code)
        if dropped:
            grounded = grounded.rstrip() + f"\n\n> *Grounding dropped {dropped} unverifiable finding(s).*\n"
        grounded = grounded.strip()
        if not grounded or not any(l.strip().startswith(("-","*","•")) for l in grounded.splitlines()):
            return None, dropped

        return f"## `{rel}`\n\n{grounded}\n\n", dropped

    def _consolidate(self, layer_content, output_path, ts):
        entries = [(k, v) for k, v in layer_content.items() if v]
        layer_reviews = {}

        for i, (lk, findings) in enumerate(entries, 1):
            lname = self.info["layers"].get(lk, {}).get("name", lk)
            text = "\n\n---\n\n".join(findings)[:12_000]
            print(f"    [{i}/{len(entries)}] {lname}...", end=" ", flush=True)
            try:
                prompt = CONSOLIDATION_PROMPT.format(findings=text)
                result = self.engine.generate(prompt, role="reason", num_ctx=8192)
                layer_reviews[lk] = result or f"### {lname}\nNo output."
                print("done")
            except Exception as e:
                layer_reviews[lk] = f"### {lname}\nFailed: {e}"
                print(f"ERROR")

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(f"# {self.info['name']} — Consolidated Review\n\n")
            f.write(f"**Date**: {ts}  \n**Reason model**: `{self.engine.model_for('reason')}`  \n\n---\n\n")
            for lk in layer_reviews:
                f.write(layer_reviews[lk] + "\n\n---\n\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Code review with grounding check")
    parser.add_argument("--layer", type=str)
    parser.add_argument("--file", type=str)
    parser.add_argument("--skip-consolidation", action="store_true")
    parser.add_argument("--ollama-url", type=str, default="http://localhost:11434")
    parser.add_argument("--code-url", type=str, default="http://localhost:11434",
                        help="Ollama URL for code role")
    parser.add_argument("--code-model", type=str, help="Pin review model")
    parser.add_argument("--reason-model", type=str, help="Pin consolidation model")
    parser.add_argument("project_dir", nargs="?", default=".")
    args = parser.parse_args()

    models = {}
    if args.code_model: models["code"] = args.code_model
    if args.reason_model: models["reason"] = args.reason_model

    engine = Engine(url=args.ollama_url, code_url=args.code_url, models=models)
    ok, _, msg = engine.test()
    print(f"  Ollama: {msg}")
    if not ok: sys.exit(1)
    engine.print_model_map()

    info = detect(os.path.abspath(args.project_dir))
    print_detection(info)

    # Load rules — prefer saved, fallback to generate
    rules_path = os.path.join(info["root"], "docs", ".layer_rules.json")
    if os.path.isfile(rules_path):
        log(f"  Loading saved rules from {rules_path}")
        rules = load_rules(rules_path)
    else:
        log("  No saved rules — generating from patterns + architecture doc")
        rules, _ = build_all_rules(engine, info, use_llm=bool(info.get("arch_doc")))
        save_rules(rules, rules_path)

    reviewer = Reviewer(engine, info, rules)
    reviewer.run(layer_filter=args.layer, file_filter=args.file,
                 skip_consolidation=args.skip_consolidation)


if __name__ == "__main__":
    main()
