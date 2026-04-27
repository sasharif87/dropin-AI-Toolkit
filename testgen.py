"""
testgen.py — Generate test suites from existing source code.

Uses quick model to analyze what tests are needed, code model to write them.

Usage:
    python testgen.py                    # dry-run
    python testgen.py --apply            # write tests
    python testgen.py --layer api        # specific layer
    python testgen.py --integration      # include integration tests
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

from engine import Engine, extract_json, strip_fences, read_file, fmt_time, log
from detect import detect, print_detection
from rules import build_all_rules, load_rules

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
ANALYZE_PROMPT = """Analyze this {lang} file and return ONLY valid JSON — what tests does it need?

{{
  "testable": true,
  "test_file": "tests/path/test_filename.py",
  "unit_tests": [
    {{"name": "test_something", "description": "what it tests", "mocks": ["dep_to_mock"]}}
  ],
  "integration_tests": [
    {{"name": "test_integration_something", "description": "what it tests", "fixtures": ["db"]}}
  ],
  "edge_cases": ["empty input", "None values"],
  "fixtures": [
    {{"name": "fixture_name", "scope": "function", "description": "what it provides"}}
  ]
}}

File: {filepath}
Layer: {layer_name} | Rules: {rules_summary}

```
{code}
```
"""

GENERATE_PROMPT = """Write a COMPLETE {test_framework} test file for this source module.

Rules:
1. Group tests by class (TestClassName)
2. Happy-path AND error/edge-case tests
3. Mock external dependencies — never hit real services
4. Use parametrize for multiple inputs
5. Mark integration tests with @pytest.mark.integration
6. AAA pattern: Arrange, Act, Assert
7. Each test tests ONE thing

Project: {project_name} | Layer: {layer_name}
Layer rules: {rules}
Test plan: {test_plan}

Source ({filepath}):
```
{code}
```

Return ONLY the test file code — no fences, no explanations.
"""

CONFTEST_PROMPT = """Generate a {test_framework} conftest.py with shared fixtures.

Stack: {stack_summary}
Required fixtures: {fixtures_json}
Layer structure: {layers_summary}

Rules:
- DB fixtures use transactions that rollback after each test
- API fixtures use TestClient
- Session-scoped for expensive setup, function-scoped for isolation

Return ONLY the code.
"""


# ---------------------------------------------------------------------------
# TestGenerator
# ---------------------------------------------------------------------------
class TestGenerator:
    def __init__(self, engine, project_info, rules):
        self.engine = engine
        self.info = project_info
        self.rules = rules
        self.stack = project_info["stack"]
        self.framework = self.stack.get("test_framework", "pytest")
        self.lang = self.stack.get("backend", "python")
        self.tests_dir = project_info.get("has_tests") or "tests"
        self.generated = {}
        self.all_fixtures = []
        self.plans = {}

    def run(self, apply=False, layer_filter=None, file_filter=None, integration=False):
        start = time.time()
        root = self.info["root"]

        # Collect source files (skip test dirs)
        all_files = []
        for key, layer in self.info["layers"].items():
            for fpath in layer.get("files", []):
                all_files.append((fpath, key))

        if layer_filter:
            fset = {l.strip().lower() for l in layer_filter.split(",")}
            all_files = [(f, k) for f, k in all_files
                         if k in fset or k.split("/")[-1] in fset]
        if file_filter:
            all_files = [(f, k) for f, k in all_files if f == file_filter]

        # Phase 1: Analyze files (quick model)
        log(f"Phase 1 — Analyzing {len(all_files)} files ({self.engine.model_for('quick')})")
        for i, (rel, lk) in enumerate(all_files, 1):
            abs_path = os.path.join(root, rel)
            code, err = read_file(abs_path)
            if err or not code or len(code) < 50:
                continue
            if os.path.basename(rel) in ("__init__.py", "conftest.py"):
                continue
            if "migration" in rel:
                continue

            print(f"    [{i}/{len(all_files)}] {rel}...", end=" ", flush=True)

            lname = self.info["layers"].get(lk, {}).get("name", lk)
            rules = self.rules.get(lk, "")
            rules_summary = rules[:200] + "..." if len(rules) > 200 else rules

            prompt = ANALYZE_PROMPT.format(
                lang=self.lang, filepath=rel, layer_name=lname,
                rules_summary=rules_summary, code=code[:10_000],
            )

            # If quick resolved to the same model as code, use code role to avoid
            # double-booking the same model and stalling Ollama.
            quick_role = "quick" if self.engine.model_for("quick") != self.engine.model_for("code") else "code"
            try:
                resp = self.engine.generate(prompt, role=quick_role, num_ctx=8192, timeout=1800)
                plan = extract_json(resp)
                if plan and plan.get("testable"):
                    self.plans[rel] = plan
                    self.all_fixtures.extend(plan.get("fixtures", []))
                    n = len(plan.get("unit_tests", [])) + len(plan.get("integration_tests", []))
                    print(f"{n} tests planned")
                else:
                    print("skip")
            except Exception as e:
                print(f"error ({e})")

        if not self.plans:
            log("No testable files found.")
            return True

        # Phase 2: Generate test files (code model)
        log(f"\nPhase 2 — Generating {len(self.plans)} test files ({self.engine.model_for('code')})")
        for i, (rel, plan) in enumerate(self.plans.items(), 1):
            test_file = plan.get("test_file", f"{self.tests_dir}/test_{os.path.basename(rel)}")
            print(f"    [{i}/{len(self.plans)}] {test_file}...", end=" ", flush=True)

            code, _ = read_file(os.path.join(root, rel))
            if not code:
                print("skip")
                continue

            lk = dict(all_files).get(rel, "other") if all_files else "other"
            lname = self.info["layers"].get(lk, {}).get("name", lk)
            rules = self.rules.get(lk, "")

            if not integration:
                plan = dict(plan)
                plan["integration_tests"] = []

            prompt = GENERATE_PROMPT.format(
                test_framework=self.framework,
                project_name=self.info["name"], layer_name=lname,
                rules=rules[:2000], test_plan=json.dumps(plan, indent=2),
                filepath=rel, code=code[:20_000],
            )

            try:
                result = self.engine.generate(prompt, role="code")
                result = strip_fences(result)
                if result and len(result) > 50:
                    self.generated[test_file] = result
                    print(f"OK ({len(result):,} chars)")
                else:
                    print("empty")
            except Exception as e:
                print(f"error ({e})")

        # Phase 3: Generate conftest
        log("\nPhase 3 — Generating conftest.py")
        self._gen_conftest()

        # Phase 4: Write or preview
        if apply:
            self._write()
        else:
            self._preview()

        total_tests = sum(
            len(p.get("unit_tests", [])) + len(p.get("integration_tests", []))
            for p in self.plans.values()
        )
        log(f"\nDone — {len(self.generated)} test files, ~{total_tests} test cases, "
            f"{fmt_time(time.time() - start)}")
        if not apply and self.generated:
            log("Use --apply to write.")

    def _gen_conftest(self):
        seen = set()
        unique = []
        for f in self.all_fixtures:
            name = f.get("name", "")
            if name and name not in seen:
                seen.add(name)
                unique.append(f)

        if not unique:
            unique = [{"name": "tmp_dir", "scope": "function", "description": "Temp directory"}]

        stack_summary = " + ".join(f for f in [
            self.stack.get("backend"), self.stack.get("api"), self.stack.get("database"),
        ] if f)
        layers_summary = "\n".join(
            f"- {k}: {v.get('name', k)}" for k, v in self.info["layers"].items()
        )

        prompt = CONFTEST_PROMPT.format(
            test_framework=self.framework, stack_summary=stack_summary,
            fixtures_json=json.dumps(unique, indent=2), layers_summary=layers_summary,
        )

        try:
            result = self.engine.generate(prompt, role="code")
            result = strip_fences(result)
            if result:
                self.generated[f"{self.tests_dir}/conftest.py"] = result
                log(f"  conftest.py generated ({len(result):,} chars)")
        except Exception as e:
            log(f"  conftest.py failed: {e}")

    def _write(self):
        root = self.info["root"]
        written = skipped = 0
        for rel, content in sorted(self.generated.items()):
            abs_path = os.path.join(root, rel)
            if os.path.isfile(abs_path):
                skipped += 1
                continue
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)
            written += 1
        log(f"  Written: {written} | Skipped: {skipped}")

    def _preview(self):
        root = self.info["root"]
        preview_dir = os.path.join(root, "tmp", "preview")
        os.makedirs(preview_dir, exist_ok=True)

        log(f"\n  Would create {len(self.generated)} test files:\n")
        for path, content in sorted(self.generated.items()):
            exists = " (EXISTS)" if os.path.isfile(os.path.join(root, path)) else ""
            print(f"    {path:<55} {content.count(chr(10))+1:>4} lines{exists}")

            preview_path = os.path.join(preview_dir, path.replace("/", os.sep))
            os.makedirs(os.path.dirname(preview_path), exist_ok=True)
            with open(preview_path, "w", encoding="utf-8") as f:
                f.write(content)

        log(f"\n  Preview written to tmp/preview/ — inspect in IDE before applying.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Generate test suites via Ollama")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--layer", type=str)
    parser.add_argument("--file", type=str)
    parser.add_argument("--integration", action="store_true")
    parser.add_argument("--ollama-url", type=str, default="http://localhost:11434")
    parser.add_argument("--code-model", type=str)
    parser.add_argument("--quick-model", type=str)
    parser.add_argument("project_dir", nargs="?", default=".")
    args = parser.parse_args()

    models = {}
    if args.code_model: models["code"] = args.code_model
    if args.quick_model: models["quick"] = args.quick_model

    engine = Engine(url=args.ollama_url, models=models)
    ok, _, msg = engine.test()
    print(f"  Ollama: {msg}")
    if not ok: sys.exit(1)
    engine.print_model_map()

    info = detect(os.path.abspath(args.project_dir))
    print_detection(info)

    rules_path = os.path.join(info["root"], "docs", ".layer_rules.json")
    if os.path.isfile(rules_path):
        rules = load_rules(rules_path)
    else:
        rules, _ = build_all_rules(engine, info, use_llm=False)

    gen = TestGenerator(engine, info, rules)
    gen.run(apply=args.apply, layer_filter=args.layer,
            file_filter=args.file, integration=args.integration)


if __name__ == "__main__":
    main()
