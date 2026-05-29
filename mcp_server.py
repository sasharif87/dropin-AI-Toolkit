#!/usr/bin/env python3
"""
mcp_server.py — MCP server for dropin-AI-Toolkit.

Exposes dropin's pipeline as MCP tools so a frontier model (Claude Code)
can orchestrate the iteration loop while local Ollama handles generation.

Tools:
    dropin_detect    — project structure + stack as JSON
    dropin_rules     — layer rules (cached or freshly generated)
    dropin_generate  — raw Ollama inference (any role)
    dropin_develop   — scaffold from architecture doc
    dropin_review    — code review with grounding check
    dropin_fix       — apply review fixes
    dropin_test      — generate test suites

Usage (stdio transport — add to Claude Code MCP config):
    python mcp_server.py
"""

import json
import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from mcp.server.fastmcp import FastMCP
import config

mcp = FastMCP("dropin")

DROP_SCRIPT = os.path.join(SCRIPT_DIR, "drop.py")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_cmd(cfg, project_dir, command):
    """Build the base drop.py subprocess command."""
    cmd = [sys.executable, DROP_SCRIPT, command,
           "--project", os.path.abspath(project_dir)]
    if cfg.get("url"):
        cmd += ["--url", cfg["url"]]
    cmd += ["--code-url", config.effective_code_url(cfg)]
    return cmd


def _run(cmd):
    """Run a drop.py command, capturing all output. stdin is closed so
    interactive prompts auto-fail (empty string → not 'y' → no accidental apply)."""
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        cwd=SCRIPT_DIR,
    )
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    combined = out + ("\n" + err if err else "")
    return combined.strip()


def _engine():
    """Return a connected Engine using the active config."""
    from engine import Engine
    cfg = config.load()
    engine = Engine(
        url=cfg.get("url", "http://localhost:11434"),
        code_url=cfg.get("code_url"),
        models=cfg.get("models", {}),
        role_ctx_caps=cfg.get("role_ctx_caps") or {},
    )
    ok, _, msg = engine.test()
    if not ok:
        raise RuntimeError(f"Cannot reach Ollama: {msg}")
    return engine


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def dropin_detect(project_dir: str) -> str:
    """Detect project structure, stack, layers, and architecture doc.

    Returns JSON with name, stack, layers (file counts + patterns), arch_doc path,
    and total file count. Use this first to understand what you're working with.
    """
    from detect import detect
    abs_dir = os.path.abspath(project_dir)
    info = detect(abs_dir)
    return json.dumps({
        "name": info["name"],
        "root": info["root"],
        "stack": info["stack"],
        "arch_doc": info["arch_doc"],
        "has_tests": info["has_tests"],
        "file_count": info["file_count"],
        "layers": {
            k: {
                "file_count": v["file_count"],
                "patterns": v["patterns"],
                "files": v["files"],
            }
            for k, v in info["layers"].items()
        },
        "config_files": info["config_files"],
    }, indent=2)


@mcp.tool()
def dropin_rules(project_dir: str) -> str:
    """Get layer rules for the project.

    Loads cached rules from docs/.layer_rules.json if present, otherwise
    generates them from detected patterns + architecture doc (requires Ollama).
    Returns JSON mapping layer key -> rule text.
    """
    from detect import detect
    from rules import load_rules, build_all_rules

    abs_dir = os.path.abspath(project_dir)
    rules_path = os.path.join(abs_dir, "docs", ".layer_rules.json")

    if os.path.isfile(rules_path):
        rules = load_rules(rules_path)
        return json.dumps({"source": "cached", "path": rules_path, "rules": rules}, indent=2)

    info = detect(abs_dir)
    try:
        engine = _engine()
    except RuntimeError as e:
        return json.dumps({"error": str(e)})

    use_llm = bool(info.get("arch_doc"))
    rules, _ = build_all_rules(engine, info, use_llm=use_llm)
    return json.dumps({"source": "generated", "rules": rules}, indent=2)


@mcp.tool()
def dropin_generate(prompt: str, role: str = "code", project_dir: str = "") -> str:
    """Send a prompt directly to local Ollama and return the raw response.

    role: 'code' (generation/fixes), 'reason' (architecture/analysis),
          'quick' (classification/short tasks).
    Use this for surgical one-off generation — e.g. regenerating a single file
    or asking the local model to explain a finding.
    """
    try:
        engine = _engine()
    except RuntimeError as e:
        return f"ERROR: {e}"
    try:
        return engine.generate(prompt, role=role)
    except Exception as e:
        return f"ERROR: {e}"


@mcp.tool()
def dropin_develop(
    project_dir: str,
    layer: str = "",
    apply: bool = False,
    plan_only: bool = False,
) -> str:
    """Scaffold source files from the project's architecture doc.

    layer     — comma-separated layer keys to target (e.g. 'api,db'). Empty = all.
    apply     — write files to disk. False = dry-run (preview written to tmp/preview/).
    plan_only — just show the build plan, generate nothing.

    Requires docs/ARCHITECTURE.md (or equivalent) to exist in project_dir.
    """
    cfg = config.load()
    cmd = _base_cmd(cfg, project_dir, "develop")
    if layer:
        cmd += ["--layer", layer]
    if apply:
        cmd.append("--apply")
    if plan_only:
        cmd.append("--plan-only")
    return _run(cmd)


@mcp.tool()
def dropin_review(
    project_dir: str,
    layer: str = "",
    file: str = "",
    skip_consolidation: bool = False,
) -> str:
    """Run a layer-aware code review with grounding check.

    layer              — target specific layers (comma-separated). Empty = all.
    file               — target a single file path (relative to project_dir).
    skip_consolidation — skip the reasoning-model consolidation pass (faster).

    Report is written to docs/code_review_report.md and
    docs/code_review_report_consolidated.md inside project_dir.
    Returns the full review output including findings.
    """
    cfg = config.load()
    cmd = _base_cmd(cfg, project_dir, "review")
    if layer:
        cmd += ["--layer", layer]
    if file:
        cmd += ["--file", file]
    if skip_consolidation:
        cmd.append("--skip-consolidation")
    return _run(cmd)


@mcp.tool()
def dropin_fix(
    project_dir: str,
    layer: str = "",
    file: str = "",
    apply: bool = False,
) -> str:
    """Apply fixes from the last review report.

    Reads docs/code_review_report_consolidated.md (falls back to raw report).
    Validates fixes before applying: syntax check, size ratio, public API
    preservation, comment retention.

    layer  — only fix files in these layers.
    file   — only fix this specific file.
    apply  — write fixes to disk. False = preview in tmp/preview/fixes/.
    """
    cfg = config.load()
    cmd = _base_cmd(cfg, project_dir, "fix")
    if layer:
        cmd += ["--layer", layer]
    if file:
        cmd += ["--file", file]
    if apply:
        cmd.append("--apply")
    return _run(cmd)


@mcp.tool()
def dropin_test(
    project_dir: str,
    layer: str = "",
    file: str = "",
    apply: bool = False,
    integration: bool = False,
) -> str:
    """Generate test suites for source files.

    Uses the quick model to plan what tests are needed, code model to write them.
    Generates a conftest.py with shared fixtures.

    layer       — target specific layers.
    file        — target a single source file.
    apply       — write test files to disk.
    integration — include integration tests (marked @pytest.mark.integration).
    """
    cfg = config.load()
    cmd = _base_cmd(cfg, project_dir, "test")
    if layer:
        cmd += ["--layer", layer]
    if file:
        cmd += ["--file", file]
    if apply:
        cmd.append("--apply")
    if integration:
        cmd.append("--integration")
    return _run(cmd)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
