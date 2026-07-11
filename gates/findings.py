"""
findings.py — Structured review findings: schema, storage, dedup, rendering.

The single source of truth for a review run is docs/review_findings.json.
Markdown reports (code_review_report.md and the consolidated report) are
rendered views of that JSON. fix.py consumes the JSON directly — no regex
parsing of prose reports.

Finding schema (dict; `file` and `description` required, rest optional):
    file          relative path from project root (forward slashes)
    line          int, 1-based line number
    severity      "bug" | "security" | "rule" | "style" (free-form tolerated)
    rule_label    short label of the violated rule
    quote         exact code snippet backing the finding (grounding target)
    description   what is wrong and why
    source        "llm" | "ruff" | "semgrep"
    confidence    1.0 for deterministic sources, absent for LLM findings
    layer         layer key the file belongs to
    chunk_part    "2/3" when found while reviewing a chunked large file
    parsed        "prose" when recovered by the fallback prose parser
    coverage      "partial" when the file was only partially reviewed
    fix_rejected  "test_regression" when an applied fix was reverted
"""

import hashlib
import json
import os
import re
from collections import OrderedDict

FINDINGS_NAME = "review_findings.json"

SEVERITY_ORDER = {"bug": 0, "security": 1, "rule": 2, "style": 3}


# ---------------------------------------------------------------------------
# Normalization and identity
# ---------------------------------------------------------------------------
def normalize_quote(quote):
    """Collapse all whitespace runs so trivial reformatting keeps identity."""
    return re.sub(r"\s+", " ", (quote or "").strip())


def finding_hash(finding):
    """Stable identity: sha256(file + rule_label + normalized quote)."""
    key = (finding.get("file", "") + "\x00"
           + finding.get("rule_label", "") + "\x00"
           + normalize_quote(finding.get("quote", "")))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def validate_finding(obj, default_file=None):
    """Coerce a model-emitted object into a valid finding dict, or None.

    Requires a file path (falls back to *default_file*) and a description.
    Line numbers are coerced to int; junk values are dropped, not fatal.
    """
    if not isinstance(obj, dict):
        return None
    f = {}
    file_ = obj.get("file") or default_file
    desc = obj.get("description") or obj.get("issue") or obj.get("message")
    if not file_ or not isinstance(file_, str) or not desc or not isinstance(desc, str):
        return None
    f["file"] = file_.replace("\\", "/")
    f["description"] = desc.strip()

    line = obj.get("line")
    try:
        f["line"] = int(line)
    except (TypeError, ValueError):
        pass

    for key in ("severity", "rule_label", "quote", "source", "layer",
                "chunk_part", "parsed", "coverage", "fix_rejected"):
        val = obj.get(key)
        if isinstance(val, str) and val:
            f[key] = val
    conf = obj.get("confidence")
    if isinstance(conf, (int, float)):
        f["confidence"] = float(conf)
    return f


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------
def dedup_findings(findings):
    """Drop duplicate findings; deterministic sources win over LLM.

    Two findings are duplicates when they share a hash (file + rule + quote),
    or when an LLM finding lands on the same file+line as a deterministic
    (ruff/semgrep) finding — the deterministic one is authoritative.
    Returns (kept, dropped_count).
    """
    det_locations = {
        (f.get("file"), f.get("line"))
        for f in findings
        if f.get("source") in ("ruff", "semgrep") and f.get("line") is not None
    }
    seen_hashes = set()
    kept, dropped = [], 0
    for f in findings:
        h = finding_hash(f)
        is_llm = f.get("source", "llm") == "llm"
        if h in seen_hashes:
            dropped += 1
            continue
        if is_llm and (f.get("file"), f.get("line")) in det_locations:
            dropped += 1
            continue
        seen_hashes.add(h)
        kept.append(f)
    return kept, dropped


def sort_findings(findings):
    """Severity-ranked, then by file and line, for stable diffable output."""
    return sorted(findings, key=lambda f: (
        SEVERITY_ORDER.get(f.get("severity", ""), 9),
        f.get("file", ""),
        f.get("line", 0) or 0,
    ))


# ---------------------------------------------------------------------------
# Storage — docs/review_findings.json
# ---------------------------------------------------------------------------
def findings_path(root):
    return os.path.join(root, "docs", FINDINGS_NAME)


def save_findings(root, findings, meta=None, coverage=None, stats=None):
    """Write the findings JSON (source of truth). Returns the path."""
    path = findings_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {
        "meta": meta or {},
        "stats": stats or {},
        "coverage": coverage or {},
        "findings": findings,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return path


def load_findings(root):
    """Load the findings JSON. Returns the data dict, or None if absent/invalid."""
    path = findings_path(root)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("findings"), list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return None


def update_findings(root, updated):
    """Rewrite stored findings matched by hash with *updated* versions."""
    data = load_findings(root)
    if not data:
        return False
    by_hash = {finding_hash(f): f for f in updated}
    merged = []
    for f in data["findings"]:
        merged.append(by_hash.get(finding_hash(f), f))
    data["findings"] = merged
    with open(findings_path(root), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return True


# ---------------------------------------------------------------------------
# Markdown rendering — reports are views of the JSON
# ---------------------------------------------------------------------------
def _finding_line(f):
    parts = []
    sev = f.get("severity")
    if sev:
        parts.append(f"**[{sev}]**")
    if f.get("line") is not None:
        parts.append(f"Line {f['line']}:")
    if f.get("quote"):
        q = f["quote"].replace("`", "'")
        if len(q) > 120:
            q = q[:117] + "..."
        parts.append(f"`{q}`")
    parts.append("—")
    parts.append(f.get("description", ""))
    tags = []
    if f.get("rule_label"):
        tags.append(f.get("rule_label"))
    if f.get("source") and f.get("source") != "llm":
        tags.append(f["source"])
    if f.get("chunk_part"):
        tags.append(f"chunk {f['chunk_part']}")
    if f.get("parsed") == "prose":
        tags.append("prose-parsed")
    if f.get("fix_rejected"):
        tags.append(f"fix rejected: {f['fix_rejected']}")
    if tags:
        parts.append(f"*({'; '.join(tags)})*")
    return "- " + " ".join(parts)


def render_markdown(data, title):
    """Render the raw per-file report from the findings JSON data dict."""
    meta = data.get("meta", {})
    out = [f"# {title}", ""]
    for k, v in meta.items():
        out.append(f"**{k}**: {v}  ")
    out.append("")
    out.append("---")
    out.append("")

    by_file = OrderedDict()
    for f in sort_findings(data.get("findings", [])):
        by_file.setdefault(f.get("file", "?"), []).append(f)

    for file_, items in by_file.items():
        out.append(f"## `{file_}`")
        out.append("")
        for f in items:
            out.append(_finding_line(f))
        out.append("")

    stats = data.get("stats", {})
    if stats:
        out.append("---")
        out.append("")
        out.append("# Summary")
        out.append("")
        out.append("| Metric | Count |")
        out.append("|---|---|")
        for k, v in stats.items():
            out.append(f"| {k} | {v} |")
        out.append("")

    coverage = data.get("coverage", {})
    if coverage:
        out.append("## Coverage")
        out.append("")
        out.append("| Status | Files |")
        out.append("|---|---|")
        for status in ("full", "partial", "timeout", "skipped", "error"):
            files = coverage.get(status, [])
            if files:
                out.append(f"| {status} | {len(files)}: {', '.join(files[:10])}"
                           f"{' …' if len(files) > 10 else ''} |")
            else:
                out.append(f"| {status} | 0 |")
        out.append("")
    return "\n".join(out) + "\n"


def render_consolidated(data, title):
    """Render the consolidated report: health assessments + ranked findings."""
    meta = data.get("meta", {})
    out = [f"# {title}", ""]
    for k, v in meta.items():
        out.append(f"**{k}**: {v}  ")
    out.append("")
    out.append("---")
    out.append("")
    health = data.get("health", {})
    if health:
        out.append("## Health")
        out.append("")
        for layer, text in health.items():
            out.append(f"- **{layer}**: {text}")
        out.append("")
    out.append("## Findings (ranked)")
    out.append("")
    findings = sort_findings(data.get("findings", []))
    if not findings:
        out.append("Clean — no actionable issues.")
    for f in findings:
        out.append(_finding_line(f) + f"  \n  in `{f.get('file', '?')}`")
    out.append("")
    return "\n".join(out) + "\n"
