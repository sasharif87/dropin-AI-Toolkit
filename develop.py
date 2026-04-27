"""
develop.py — Read architecture doc and generate project code.

Uses the code model to generate actual implementation files based on:
  - The architecture document's specifications
  - Auto-detected project structure
  - Generated layer rules (so code follows the rules from day one)

Usage:
    python develop.py                           # dry-run
    python develop.py --apply                   # write files
    python develop.py --layer api,db            # specific layers
    python develop.py --plan-only               # just show what would be generated
"""

import argparse
import json
import os
import sys
import time

from engine import Engine, extract_json, strip_fences, read_file, fmt_time, log
from detect import detect, print_detection
from rules import build_all_rules, save_rules

# ---------------------------------------------------------------------------
# Architecture analysis prompt (uses reasoning model)
# ---------------------------------------------------------------------------
ANALYZE_ARCH_PROMPT = """You are a senior software architect. Analyze this architecture document
and return a detailed build plan as ONLY valid JSON — no markdown, no commentary.

Schema:
{{
  "layers": [
    {{
      "key": "path/prefix",
      "name": "Human Layer Name",
      "files": [
        {{
          "path": "full/relative/path/to/file.py",
          "purpose": "what this file does",
          "key_classes": ["ClassName"],
          "key_functions": ["function_name"],
          "depends_on": ["other/file.py"]
        }}
      ]
    }}
  ],
  "data_models": [
    {{
      "name": "ModelName",
      "layer_key": "backend/db",
      "fields": [{{"name": "id", "type": "UUID", "required": true, "notes": "primary key"}}],
      "relationships": ["OtherModel"]
    }}
  ],
  "api_routes": [
    {{
      "method": "GET",
      "path": "/api/v1/resource",
      "handler_file": "backend/api/routes/resource.py",
      "handler_function": "list_resources",
      "request_model": "null or SchemaName",
      "response_model": "ResourceListResponse",
      "auth_required": true
    }}
  ],
  "config_files": [
    {{"path": "docker-compose.yml", "purpose": "container orchestration"}},
    {{"path": ".env.example", "purpose": "environment variable template"}}
  ]
}}

DETECTED PROJECT STRUCTURE:
{detected_structure}

ARCHITECTURE DOCUMENT:
{arch_doc}
"""

# ---------------------------------------------------------------------------
# File generation prompt (uses code model)
# ---------------------------------------------------------------------------
GENERATE_FILE_PROMPT = """You are a senior {lang} developer. Generate the COMPLETE source code for this file.

This is project scaffolding with real implementation patterns — not empty stubs. Include:
- All imports
- Class/function signatures with type hints
- Core logic where the pattern is clear (CRUD operations, validation, etc.)
- TODO comments only for domain-specific business logic that needs human input
- Proper error handling
- Configuration from environment variables

STRUCTURE RULES — these are mandatory, not optional:
1. First line MUST be a path comment: `# {file_path}`
2. Second block MUST be a module docstring (triple-quoted) that describes:
   - What this module does in one sentence
   - The key classes/functions it exposes
   - Usage example if non-obvious
3. Before EVERY logical section (imports, constants, each class, each group of related
   functions, entry point) add a separator block:
   # ---------------------------------------------------------------------------
   # Section name
   # ---------------------------------------------------------------------------
   Common section names: Imports, Configuration, Models, {key_items}, Helpers, Main
4. Inside classes, mark method groups with a short inline header:
   # ── Group name ───────────────────────────────────────────────────────────

Project: {project_name} | Stack: {stack_summary}
Layer: {layer_name}

RULES FOR THIS LAYER (code must follow these):
{rules}

File: {file_path}
Purpose: {purpose}
Key classes/functions: {key_items}
Dependencies: {dependencies}

Data models (if relevant):
{models_json}

API routes (if relevant):
{routes_json}

Return ONLY the source code — no markdown fences, no explanations.
"""


# ---------------------------------------------------------------------------
# Post-processing helpers
# ---------------------------------------------------------------------------
def _ensure_section_headers(code: str, file_path: str) -> str:
    """
    Guarantee the two structural markers every generated file must have:
      1. `# path/to/file.py` as the very first line
      2. A module-level docstring immediately after

    Section separator comments inside the body are left to the LLM — the
    prompt instructs it to add them.  This function only enforces the two
    headers that are trivially checkable without parsing the AST.
    """
    lines = code.splitlines()
    if not lines:
        return code

    # ── 1. Path comment ──────────────────────────────────────────────────────
    path_comment = f"# {file_path}"
    if not lines[0].strip().startswith("#"):
        # Model omitted it entirely — prepend
        lines.insert(0, path_comment)
    elif lines[0].strip() != path_comment:
        # Model wrote a different comment (e.g. shebang or wrong path) — replace
        lines[0] = path_comment

    # ── 2. Module docstring ──────────────────────────────────────────────────
    # Find where imports/code starts (skip the path comment line)
    has_docstring = any('"""' in l or "'''" in l for l in lines[1:6])
    if not has_docstring:
        # Insert a minimal docstring after the path comment
        module_name = file_path.split("/")[-1].replace(".py", "").replace("_", " ").title()
        stub = f'"""\n{module_name}.\n\nTODO: add module description.\n"""'
        lines.insert(1, stub)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Developer
# ---------------------------------------------------------------------------
class Developer:
    def __init__(self, engine, project_info, rules):
        self.engine = engine
        self.info = project_info
        self.rules = rules
        self.plan = None
        self.generated = {}  # path → content
        self.errors = []

    def run(self, apply=False, layer_filter=None, plan_only=False):
        start = time.time()

        # Phase 1: Analyze architecture → build plan
        log("Phase 1 — Analyzing architecture document")
        self.plan = self._analyze_arch()
        if not self.plan:
            log("ERROR: Could not build a plan from the architecture doc.")
            return False

        self._print_plan()
        if plan_only:
            return True

        # Phase 2: Generate files
        log("Phase 2 — Generating source files")
        self._generate_files(layer_filter)

        # Phase 3: Generate __init__.py / index.ts files
        log("Phase 3 — Generating package init files")
        self._generate_inits()

        # Phase 4: Generate config files
        log("Phase 4 — Generating config files")
        self._generate_configs()

        # Phase 5: Write or preview
        if apply:
            log("Phase 5 — Writing files to disk")
            self._write_files()
        else:
            log("Phase 5 — Dry run preview")
            self._preview()

        elapsed = time.time() - start
        log(f"\nDone — {len(self.generated)} files, {len(self.errors)} errors, {fmt_time(elapsed)}")
        if not apply and self.generated:
            log("Dry run — use --apply to write files.")
        return True

    def _analyze_arch(self):
        arch_doc_rel = self.info.get("arch_doc")
        if not arch_doc_rel:
            log("No architecture doc found. Create docs/ARCHITECTURE.md first.")
            return None

        arch_path = os.path.join(self.info["root"], arch_doc_rel)
        content, err = read_file(arch_path, max_chars=50_000)
        if err:
            log(f"Cannot read {arch_path}: {err}")
            return None

        # If reason and code resolved to the same model, skip the reason role for
        # arch analysis — using the same large model twice in parallel stalls Ollama.
        # Fall back to code model with a tighter context budget instead.
        reason_model = self.engine.model_for("reason")
        code_model = self.engine.model_for("code")
        role = "reason" if reason_model != code_model else "code"
        ctx = 16384 if role == "reason" else 8192

        log(f"  Arch doc: {arch_doc_rel} ({len(content):,} chars)")
        log(f"  Sending to {role} model ({self.engine.model_for(role)})...")

        detected = json.dumps({
            "stack": self.info["stack"],
            "layers": {k: {"prefix": v["prefix"], "patterns": v.get("patterns", []),
                           "files": v.get("files", [])[:5]}  # sample files — keep prompt lean
                       for k, v in self.info["layers"].items()},
        }, indent=2)

        prompt = ANALYZE_ARCH_PROMPT.format(
            arch_doc=content[:10_000],  # cap arch doc — model doesn't need the full text to plan
            detected_structure=detected,
        )

        t0 = time.time()
        try:
            response = self.engine.generate(prompt, role=role, num_ctx=ctx, timeout=1800)
            plan = extract_json(response)
            log(f"  Plan extracted in {fmt_time(time.time() - t0)}")
            return plan
        except Exception as e:
            log(f"  ERROR: {e}")
            return None

    def _print_plan(self):
        layers = self.plan.get("layers", [])
        total_files = sum(len(l.get("files", [])) for l in layers)
        models = self.plan.get("data_models", [])
        routes = self.plan.get("api_routes", [])
        configs = self.plan.get("config_files", [])

        log(f"\n  Plan: {len(layers)} layers, {total_files} source files, "
            f"{len(models)} models, {len(routes)} routes, {len(configs)} config files")
        for layer in layers:
            log(f"    {layer.get('key', '?'):<30} {len(layer.get('files', [])):>3} files")

    def _generate_files(self, layer_filter=None):
        layers = self.plan.get("layers", [])
        stack = self.info["stack"]
        lang = stack.get("backend", "python")
        stack_summary = " + ".join(f for f in [
            stack.get("backend"), stack.get("api"), stack.get("frontend"),
            stack.get("database"), stack.get("orm"),
        ] if f)

        if layer_filter:
            filter_set = {l.strip().lower() for l in layer_filter.split(",")}
            layers = [l for l in layers
                      if l.get("key", "").lower() in filter_set
                      or l.get("key", "").split("/")[-1].lower() in filter_set]

        total = sum(len(l.get("files", [])) for l in layers)
        counter = 0

        for layer in layers:
            layer_key = layer.get("key", "unknown")
            layer_name = layer.get("name", layer_key)
            layer_rules = self.rules.get(layer_key, "")

            # Try fuzzy match if exact key not in rules
            if not layer_rules:
                for rk, rv in self.rules.items():
                    if layer_key.endswith(rk) or rk.endswith(layer_key.split("/")[-1]):
                        layer_rules = rv
                        break

            files = layer.get("files", [])
            if not files:
                continue

            print(f"\n  {'-'*55}")
            print(f"  {layer_name} ({len(files)} files) -> model: {self.engine.model_for('code')}")
            print(f"  {'-'*55}")

            for fspec in files:
                counter += 1
                fpath = fspec.get("path", "unknown")
                purpose = fspec.get("purpose", "")
                key_items = ", ".join(
                    fspec.get("key_classes", []) + fspec.get("key_functions", [])
                )
                deps = ", ".join(fspec.get("depends_on", []))

                # Related models/routes
                related_models = [m for m in self.plan.get("data_models", [])
                                  if m.get("layer_key", "") == layer_key]
                related_routes = [r for r in self.plan.get("api_routes", [])
                                  if r.get("handler_file", "").startswith(layer_key)]

                print(f"    [{counter}/{total}] {fpath}...", end=" ", flush=True)

                prompt = GENERATE_FILE_PROMPT.format(
                    lang=lang,
                    project_name=self.info["name"],
                    stack_summary=stack_summary,
                    layer_name=layer_name,
                    rules=layer_rules or "(no specific rules)",
                    file_path=fpath,
                    purpose=purpose,
                    key_items=key_items or "(determine from purpose)",
                    dependencies=deps or "(none specified)",
                    models_json=json.dumps(related_models, indent=2)[:3000] if related_models else "n/a",
                    routes_json=json.dumps(related_routes, indent=2)[:3000] if related_routes else "n/a",
                )

                t0 = time.time()
                try:
                    code = self.engine.generate(prompt, role="code")
                    code = strip_fences(code)
                    code = _ensure_section_headers(code, fpath)
                    elapsed = time.time() - t0

                    if code and len(code.strip()) > 30:
                        self.generated[fpath] = code
                        print(f"OK ({fmt_time(elapsed)}, {len(code):,} chars)")
                    else:
                        self.errors.append((fpath, "empty response"))
                        print(f"EMPTY ({fmt_time(elapsed)})")
                except Exception as e:
                    self.errors.append((fpath, str(e)))
                    print(f"ERROR ({e})")

    def _generate_inits(self):
        lang = self.info["stack"].get("backend", "python")
        if lang != "python":
            return

        dirs_needing_init = set()
        for path in self.generated:
            if path.endswith(".py"):
                parts = path.split("/")
                for i in range(1, len(parts)):
                    pkg = "/".join(parts[:i])
                    init = f"{pkg}/__init__.py"
                    if init not in self.generated:
                        dirs_needing_init.add(pkg)

        for pkg in sorted(dirs_needing_init):
            init_path = f"{pkg}/__init__.py"
            module = pkg.replace("/", ".")
            self.generated[init_path] = f'"""{module} package."""\n'

        if dirs_needing_init:
            log(f"  Added {len(dirs_needing_init)} __init__.py files")

    def _generate_configs(self):
        configs = self.plan.get("config_files", [])
        if not configs:
            return

        for cfg in configs:
            path = cfg.get("path", "")
            purpose = cfg.get("purpose", "")
            print(f"    {path}...", end=" ", flush=True)

            prompt = (
                f"Generate the complete content for {path} ({purpose}) "
                f"for a {' + '.join(f for f in [self.info['stack'].get('backend'), self.info['stack'].get('database')] if f)} project. "
                f"Return ONLY the file content — no fences, no explanations."
            )

            try:
                content = self.engine.generate(prompt, role="code")
                content = strip_fences(content)
                if content and len(content.strip()) > 10:
                    self.generated[path] = content
                    print("OK")
                else:
                    print("EMPTY")
            except Exception as e:
                print(f"ERROR ({e})")

    def _write_files(self):
        root = self.info["root"]
        written = skipped = 0

        for rel_path, content in sorted(self.generated.items()):
            abs_path = os.path.join(root, rel_path)
            if os.path.isfile(abs_path):
                print(f"    SKIP (exists): {rel_path}")
                skipped += 1
                continue

            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"    WROTE: {rel_path}")
            written += 1

        log(f"\n  Written: {written} | Skipped (existing): {skipped}")

    def _preview(self):
        root = self.info["root"]
        preview_dir = os.path.join(root, "tmp", "preview")
        os.makedirs(preview_dir, exist_ok=True)

        log(f"\n  Files that would be created ({len(self.generated)}):\n")
        for path, content in sorted(self.generated.items()):
            lines = content.count("\n") + 1
            exists = " (EXISTS)" if os.path.isfile(os.path.join(root, path)) else ""
            print(f"    {path:<55} {lines:>4} lines{exists}")

            preview_path = os.path.join(preview_dir, path.replace("/", os.sep))
            os.makedirs(os.path.dirname(preview_path), exist_ok=True)
            with open(preview_path, "w", encoding="utf-8") as f:
                f.write(content)

        log(f"\n  Preview written to tmp/preview/ — inspect in IDE before applying.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Generate project code from architecture doc")
    parser.add_argument("--apply", action="store_true", help="Write files to disk")
    parser.add_argument("--layer", type=str, help="Comma-separated layers to generate")
    parser.add_argument("--plan-only", action="store_true", help="Just show the build plan")
    parser.add_argument("--ollama-url", type=str, default="http://localhost:11434")
    parser.add_argument("--code-url", type=str, default="http://localhost:11434",
                        help="Ollama URL for code role")
    parser.add_argument("--reason-model", type=str, help="Pin reasoning model")
    parser.add_argument("--code-model", type=str, help="Pin code model")
    parser.add_argument("project_dir", nargs="?", default=".", help="Project root directory")
    args = parser.parse_args()

    # Setup
    models = {}
    if args.reason_model: models["reason"] = args.reason_model
    if args.code_model: models["code"] = args.code_model

    engine = Engine(url=args.ollama_url, code_url=args.code_url, models=models)
    ok, available, msg = engine.test()
    print(f"  Ollama: {msg}")
    if not ok:
        sys.exit(1)
    engine.print_model_map()

    # Detect project
    info = detect(os.path.abspath(args.project_dir))
    print_detection(info)

    if not info.get("arch_doc"):
        print("\n  No architecture doc found. Create docs/ARCHITECTURE.md first.")
        print("  (See docs/ARCHITECTURE_TEMPLATE.md for a starting point)")
        sys.exit(1)

    # Generate rules
    rules, _ = build_all_rules(engine, info, use_llm=True)

    # Save rules for reuse by review.py
    rules_path = os.path.join(info["root"], "docs", ".layer_rules.json")
    os.makedirs(os.path.dirname(rules_path), exist_ok=True)
    save_rules(rules, rules_path)
    log(f"  Rules saved to {rules_path}")

    # Develop
    dev = Developer(engine, info, rules)
    success = dev.run(apply=args.apply, layer_filter=args.layer, plan_only=args.plan_only)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
