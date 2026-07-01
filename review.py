"""
review.py — Layer-aware code review with structured findings.

Pass 1 uses the code model per file and asks for findings as JSON objects.
Grounding, dedup, and consolidation all operate on the JSON list —
docs/review_findings.json is the source of truth; the markdown reports are
rendered views of it. Hallucinated findings (quote not present in the
source) are stripped by the grounding check. A prose fallback parser
recovers findings when the model ignores the JSON instruction; those are
tagged "parsed": "prose".

Usage:
    python review.py                           # review all
    python review.py --layer api,db            # specific layers
    python review.py --file backend/api/main.py
    python review.py --skip-consolidation
"""

import argparse
import json
import os
import re
import sys
import time
from collections import OrderedDict
from datetime import datetime

from engine import Engine, extract_json, read_file, chunk_text, fmt_time, log
import config
import findings as fnd
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
5. Report at most 5 findings. Priority: bugs > security > rule violations > style.
6. Every finding MUST quote the EXACT problematic code, copied verbatim from the file.
   CRITICAL: if you cannot quote the exact code, DO NOT report it.

Return ONLY a JSON array — no markdown fences, no commentary. Each element:
  {{"line": <line number>,
   "severity": "bug" | "security" | "rule" | "style",
   "rule_label": "<short name of the violated rule, or 'bug'>",
   "quote": "<exact code copied from the file>",
   "description": "<what is wrong and why>"}}

If the file is clean, return exactly: []

LAYER RULES:
{rules}

File: {filepath}

```
{code}
```
"""

CONSOLIDATION_PROMPT = """You are a senior principal engineer doing a second-pass review consolidation.

The input is a JSON array of first-pass code review findings. Produce a
consolidated set:
1. Deduplicate same-root-cause findings (keep one; mention other affected files in its description)
2. Cross-reference related findings between files in the descriptions
3. Remove noise: generic advice, code summaries, findings that are not actionable
4. Rank: bugs > security > rule violations > style
5. Keep the original "file", "line", "quote", "rule_label" fields of every finding you keep — never invent new files or quotes.

Return ONLY valid JSON — no markdown, no commentary:
{{"health": "<1-2 sentence health assessment of this code>",
 "findings": [<kept findings, same schema as the input, ranked most severe first>]}}

If nothing is actionable: {{"health": "Clean — no actionable issues.", "findings": []}}

FIRST-PASS FINDINGS:
{findings_json}
"""


# ---------------------------------------------------------------------------
# Prose fallback parser — for models that ignore the JSON instruction
# ---------------------------------------------------------------------------
def parse_prose_findings(text, rel):
    """Extract findings from bullet-style prose. Tagged "parsed": "prose"."""
    bullet_re = re.compile(r"^\s*[-*•]\s+", re.MULTILINE)
    quote_re = re.compile(r"`([^`]{6,})`")
    line_re = re.compile(r"[Ll]ine\s+(\d+)")

    results = []
    segments = bullet_re.split(text)[1:]  # drop preamble before first bullet
    for body in segments:
        body = body.strip()
        if not body:
            continue
        finding = {"file": rel, "description": body, "parsed": "prose"}
        m = quote_re.search(body)
        if m:
            finding["quote"] = m.group(1)
        m = line_re.search(body)
        if m:
            finding["line"] = int(m.group(1))
        results.append(finding)
    return results


# ---------------------------------------------------------------------------
# Grounding check — strip hallucinated findings
# ---------------------------------------------------------------------------
def ground_findings(findings_list, source):
    """Keep only findings whose quote actually appears in *source*.

    Comparison is exact substring first, then whitespace-normalized so
    wrapped quotes survive. Findings without any quote are dropped —
    an unquotable finding is unverifiable by construction.
    Returns (kept, dropped_count).
    """
    norm_source = fnd.normalize_quote(source)
    kept, dropped = [], 0
    for f in findings_list:
        quote = (f.get("quote") or "").strip()
        if quote and (quote in source or fnd.normalize_quote(quote) in norm_source):
            kept.append(f)
        else:
            dropped += 1
    return kept, dropped


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

        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        docs_dir = os.path.join(root, "docs")
        os.makedirs(docs_dir, exist_ok=True)
        report_path = os.path.join(docs_dir, "code_review_report.md")
        layer_dir = os.path.join(docs_dir, "review_by_layer")
        os.makedirs(layer_dir, exist_ok=True)

        stats = {"total": 0, "issues": 0, "clean": 0, "skipped": 0,
                 "grounding_dropped": 0, "dedup_dropped": 0, "errors": 0}
        coverage = {"full": [], "partial": [], "timeout": [], "skipped": [], "error": []}
        all_findings = []
        current_layer = None

        for i, (rel, layer_key) in enumerate(all_files, 1):
            if layer_key != current_layer:
                current_layer = layer_key
                print(f"\n  {'-'*55}")
                print(f"  {self.info['layers'].get(layer_key, {}).get('name', layer_key)}")
                print(f"  {'-'*55}")

            print(f"    [{i}/{len(all_files)}] {rel}...", end=" ", flush=True)
            stats["total"] += 1

            abs_path = os.path.join(root, rel)
            code, err = read_file(abs_path)
            if err:
                stats["skipped"] += 1
                coverage["skipped"].append(rel)
                print(f"SKIP ({err})")
                continue
            if not code.strip() or os.path.basename(rel) in ("__init__.py", "conftest.py"):
                stats["clean"] += 1
                coverage["skipped"].append(rel)
                print("SKIP")
                continue

            layer_rules = self.rules.get(layer_key, "")
            if not layer_rules:
                for rk, rv in self.rules.items():
                    if layer_key.endswith(rk) or rk.endswith(layer_key.split("/")[-1]):
                        layer_rules = rv
                        break

            file_findings, grounding_dropped, status = self._review_file(rel, code, layer_rules)
            stats["grounding_dropped"] += grounding_dropped

            if status == "error":
                stats["errors"] += 1
                coverage["error"].append(rel)
                print("ERROR")
                continue

            coverage["full"].append(rel)
            for f in file_findings:
                f["layer"] = layer_key
            if file_findings:
                stats["issues"] += 1
                all_findings.extend(file_findings)
                print(f"ISSUES ({len(file_findings)})")
            else:
                stats["clean"] += 1
                print("OK")

        # Dedup (identical findings across chunks/passes)
        all_findings, dedup_dropped = fnd.dedup_findings(all_findings)
        stats["dedup_dropped"] = dedup_dropped

        log(f"\n  Pass 1: {stats['issues']} files with issues, {stats['clean']} clean, "
            f"{stats['skipped']} skipped, {stats['grounding_dropped']} grounding drops, "
            f"{dedup_dropped} dedup drops")

        meta = {
            "project": name,
            "date": ts,
            "code_model": self.engine.model_for("code"),
            "reason_model": self.engine.model_for("reason"),
        }

        # Pass 2: Consolidation (JSON in, JSON out, validated)
        health = {}
        if not skip_consolidation and all_findings:
            log("\n  Pass 2 — Consolidation (reasoning model)")
            all_findings, health = self._consolidate(all_findings)

        # Save the source of truth, then render the markdown views from it.
        json_path = fnd.save_findings(root, fnd.sort_findings(all_findings),
                                      meta=meta, coverage=coverage, stats=stats)
        log(f"  Findings JSON: {json_path}")

        data = {"meta": meta, "stats": stats, "coverage": coverage,
                "findings": all_findings, "health": health}
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(fnd.render_markdown(data, f"{name} — Code Review"))
        log(f"  Report: {report_path}")

        if health or (not skip_consolidation and all_findings):
            con_path = report_path.replace(".md", "_consolidated.md")
            with open(con_path, "w", encoding="utf-8") as f:
                f.write(fnd.render_consolidated(data, f"{name} — Consolidated Review"))
            log(f"  Consolidated: {con_path}")

        # Per-layer views
        by_layer = OrderedDict()
        for f in all_findings:
            by_layer.setdefault(f.get("layer", "other"), []).append(f)
        for lk, items in by_layer.items():
            lname = self.info["layers"].get(lk, {}).get("name", lk)
            lpath = os.path.join(layer_dir, f"review_{lk.replace('/', '_')}.md")
            ldata = {"meta": {"layer": lname, "date": ts}, "findings": items}
            with open(lpath, "w", encoding="utf-8") as f:
                f.write(fnd.render_markdown(ldata, f"{lname} — Review"))

        log(f"\n  Total: {fmt_time(time.time() - start)}")
        return all_findings

    def _review_file(self, rel, code, rules):
        """Review *code*, chunking automatically when it exceeds the model's budget.

        Returns (findings_list, grounding_dropped, status) where status is
        "ok" or "error".
        """
        budget = self.engine.content_budget("code")
        total_lines = code.count("\n") + 1

        if len(code) <= budget:
            return self._do_review(rel, code, rules, full_source=code,
                                   line_start=1, total_lines=total_lines)

        # File is too large for one context window — review in overlapping chunks.
        chunks = chunk_text(code, budget, overlap=300)
        log(f"    (large file — {len(chunks)} chunks of ~{budget//1000}k chars each)")

        all_findings = []
        total_dropped = 0
        char_pos = 0
        had_error = False

        for idx, chunk in enumerate(chunks, 1):
            line_start = code[:char_pos].count("\n") + 1
            chunk_findings, dropped, status = self._do_review(
                rel, chunk, rules, full_source=code,
                line_start=line_start, total_lines=total_lines)
            total_dropped += dropped
            if status == "error":
                had_error = True
            for f in chunk_findings:
                f["chunk_part"] = f"{idx}/{len(chunks)}"
            all_findings.extend(chunk_findings)
            # Advance past this chunk; overlap means next chunk re-reads last 300 chars.
            char_pos = max(char_pos + len(chunk) - 300, char_pos + 1)

        status = "error" if (had_error and not all_findings) else "ok"
        return all_findings, total_dropped, status

    def _do_review(self, rel, code, rules, full_source, line_start=1, total_lines=None):
        """Send one code block to the model. Returns (findings, dropped, status)."""
        # When reviewing a chunk, tell the model which part of the file it sees
        # so line numbers in findings are anchored to the full file.
        if total_lines and line_start > 1:
            line_end = line_start + code.count("\n")
            chunk_header = (f"# [Reviewing lines {line_start}–{line_end} of {total_lines}."
                            f" Report line numbers relative to the FULL file (add {line_start - 1}"
                            f" to any line number you see here).]\n")
            code_for_prompt = chunk_header + code
        else:
            code_for_prompt = code

        prompt = REVIEW_PROMPT.format(
            project_name=self.info["name"],
            rules=rules or "(no layer rules)",
            filepath=rel,
            code=code_for_prompt,
        )

        try:
            reply = self.engine.generate(prompt, role="code")
        except Exception as e:
            log(f"    review failed for {rel}: {e}")
            return [], 0, "error"

        if not reply or reply.strip() in ("OK", "[]"):
            return [], 0, "ok"

        # Primary path: JSON findings. Fallback: prose bullets.
        parsed = extract_json(reply)
        raw_findings = []
        if isinstance(parsed, list):
            for obj in parsed:
                f = fnd.validate_finding(obj, default_file=rel)
                if f:
                    f["file"] = rel  # model must not redirect findings elsewhere
                    f["source"] = "llm"
                    raw_findings.append(f)
        elif isinstance(parsed, dict):
            # Some models wrap the array: {"findings": [...]}
            for obj in parsed.get("findings", []):
                f = fnd.validate_finding(obj, default_file=rel)
                if f:
                    f["file"] = rel
                    f["source"] = "llm"
                    raw_findings.append(f)
        else:
            for f in parse_prose_findings(reply, rel):
                f["source"] = "llm"
                raw_findings.append(f)

        # Ground against the full source so quotes near chunk seams can be verified.
        grounded, dropped = ground_findings(raw_findings, full_source)
        return grounded, dropped, "ok"

    def _consolidate(self, all_findings):
        """LLM consolidation over the JSON findings, grouped by layer for budget.

        The model must return {"health": ..., "findings": [...]}. The returned
        schema is validated finding-by-finding; on any validation failure the
        pre-consolidation findings for that group are kept — consolidation may
        reduce noise but must never lose data to a malformed reply.
        Returns (consolidated_findings, health_by_layer).
        """
        by_layer = OrderedDict()
        for f in all_findings:
            by_layer.setdefault(f.get("layer", "other"), []).append(f)

        budget = self.engine.content_budget("reason")
        consolidated, health = [], {}

        for i, (lk, group) in enumerate(by_layer.items(), 1):
            lname = self.info["layers"].get(lk, {}).get("name", lk)
            print(f"    [{i}/{len(by_layer)}] {lname} ({len(group)} findings)...",
                  end=" ", flush=True)
            findings_json = json.dumps(group, indent=1, ensure_ascii=False)[:budget]
            try:
                reply = self.engine.generate(
                    CONSOLIDATION_PROMPT.format(findings_json=findings_json),
                    role="reason")
                result = extract_json(reply)
            except Exception as e:
                print(f"ERROR ({e}) — keeping pre-consolidation findings")
                consolidated.extend(group)
                continue

            valid = []
            if isinstance(result, dict) and isinstance(result.get("findings"), list):
                for obj in result["findings"]:
                    f = fnd.validate_finding(obj)
                    if f is None:
                        valid = None
                        break
                    f.setdefault("layer", lk)
                    f.setdefault("source", "llm")
                    valid.append(f)
            else:
                valid = None

            if valid is None:
                print("invalid reply — keeping pre-consolidation findings")
                consolidated.extend(group)
            else:
                # Deterministic findings are never subject to LLM judgment.
                det = [f for f in group if f.get("source") in ("ruff", "semgrep")]
                merged, _ = fnd.dedup_findings(det + valid)
                consolidated.extend(merged)
                health[lname] = str(result.get("health", "")).strip() or "(no assessment)"
                print(f"done ({len(group)} -> {len(merged)})")

        return consolidated, health


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Code review with structured findings")
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

    cfg = config.load()
    if args.ollama_url == "http://localhost:11434" and cfg.get("url"):
        args.ollama_url = cfg["url"]
    if args.code_url == "http://localhost:11434":
        args.code_url = config.effective_code_url(cfg)

    models = dict(cfg.get("models", {}))
    if args.code_model: models["code"] = args.code_model
    if args.reason_model: models["reason"] = args.reason_model

    engine = Engine(url=args.ollama_url, code_url=args.code_url, models=models,
                    role_ctx_caps=cfg.get("role_ctx_caps") or {})
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
