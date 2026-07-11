# dropin-AI-Toolkit — TODO

From portfolio audit (2026-07-03). The TestGate philosophy (fail-closed, explicit
`--unverified` / `--allow-red-baseline` opt-ins, batch revert) is the strongest gate
in the portfolio — these items convert audit findings from other repos into toolkit
leverage, since dropin is the factory for initial builds.

## Reframe: validation-first harness, not generation-first scaffolder (2026-07-08)

Direction, not a single task. Today the toolkit is generation-first with validation
bolted on (develop/testgen → review/fix). The harness inversion: `testgate.py` +
`findings.py` become the core; `develop.py` becomes one pluggable generator among
several. The July 1 work (findings JSON as the review/fix contract, safe unattended
defaults) already points this way — this section names the destination.

**Sovereignty constraint (non-negotiable):** local Ollama only. No cloud-model
integration, no Claude Code coupling. The long-term goal is moving *away* from cloud
models — this toolkit is the sustainable, air-gapped path, and every item below must
work on a fully local stack. Cloud tools may consume the findings JSON if they want;
the harness never depends on them.

A true harness = the existing components plus three properties the setup lacks:

1. **Cross-project.** Gates currently live inside single repos (IME's
   `scripts/check_invariants.py`, `scripts/check_layers.py`, `docs/.layer_rules.json`);
   other projects get nothing. Gate pack lives here; each repo carries only a config
   file (the `.layer_rules.json` pattern, generalized).
2. **Event-driven.** Nothing runs unless a human remembers. Enforcement moves to
   pre-commit hooks + CI + optional file-watch — all local, no cloud trigger.
3. **Generator-independent.** Same gates judge all output regardless of who typed it —
   qwen3:32b, a cloud model, or the human. Findings JSON is the interchange;
   `mcp_server.py` is the local bridge for any consumer.

Tasks, roughly in leverage order:

- [x] **Golden-file runner** (new gate type) (2026-07-10): `golden.py` runs a
      project-configured local command over real input fixtures, captures a chosen
      stream, and diffs it against a banked snapshot. New subcommand `drop.py golden`
      (checks; fail-closed) and `drop.py golden --update` (banks/re-blesses). Live
      discovery stays the *finding* mechanism; golden files are the *banking*
      mechanism — fix a live-found bug, `--update` to snapshot the correct output, and
      any later change to it fails the gate. Config is a per-repo `.golden.json` (the
      `.layer_rules.json` pattern, generalized): each case has a `command` (with
      `{python}`/`{input}` placeholders, parsed as argv via shlex — no shell, so a
      fixture filename can't be interpreted), `inputs` globs, optional `golden_dir`,
      `capture` (stdout/stderr/combined), `timeout`, and `scrub` regexes to mask
      volatile content (timestamps, PIDs, temp paths). Deterministic, local, no Ollama
      — dispatches in `drop.py` *before* engine setup so it runs air-gapped. Fail-closed
      throughout: a missing golden and a zero-match input glob both fail (a gate that
      silently checks nothing is the anti-pattern). Covered by `tests/test_golden.py`
      (bank → pass → regression → re-bless → error → scrub → CRLF-normalize, all
      hermetic via `{python}`). Priority target remains homeForge's validator/
      plausibility layer (safety-critical — bad output damages hardware); the runner is
      generic, so pointing it there is just a `.golden.json` + fixtures.
      Follow-ups deferred: capturing exit code as part of the golden; a `--only <case>`
      filter; emitting regressions into the findings JSON ledger.
- [x] **Claims checker** (new gate type) (2026-07-10): `claims.py` extracts numeric
      claims from docs (`.md`/`.rst`/`.txt`, incl. `docs/`) and re-derives them from
      the repo, flagging only claims the repo contradicts beyond tolerance. Wired into
      `drop.py detect` via `print_claims`. Deterministic, local, no Ollama. Two claim
      types today: **test counts** (derived by AST-counting `test*` methods on any
      class in a test file + module-level `test_*`, de-duped per scope so it matches
      the runner — verified: derives exactly 119/150 on this repo) and **LOC** (loose
      tolerance, since "a line" is ambiguous). Bias mirrors `orphans.py`: false
      negatives over false positives — a static count can't match a runner exactly, so
      only gross divergence (the 2,492-vs-580 class) fires, never honest rounding;
      `N+` claims are floors, violated only when the repo has fewer. Covered by
      `tests/test_claims.py`. Rationale: the portfolio test-count inflation (2,492
      claimed vs 580 real) was an unverified-aggregation-in-credibility-doc failure;
      this makes that class structurally hard instead of a checklist item.
      Follow-ups deferred: `deployed`/`in production`/coverage-% claims (not
      statically re-derivable — would need a runner or deploy probe); emitting into
      the findings JSON ledger (parity with `orphans.py`, which also only prints).
- [ ] **Extract IME's gates into the toolkit**: parameterize `check_invariants.py` /
      `check_layers.py` behind per-project config so homeForge and the training app
      get IME-grade gating for free.
- [ ] **Wire local triggers**: pre-commit hook template + CI snippet that run the fast
      gates automatically; full gate set on push. Enforcement stops depending on memory.
- [ ] **Invert the CLI**: validation is the default path (`drop.py` → gates + findings),
      generation is opt-in. Mostly rearrangement of existing code, least urgent —
      do after the two new gate types exist.

## Highest leverage — catch scaffold residue at scaffold time

- [x] **Orphan / zero-caller report** (2026-07-10): `orphans.py` parses the Python
      import graph with `ast` and flags modules nothing imports that have no entry
      point (no `__main__` guard, not a conventional runner); wired into
      `drop.py detect` output via `print_orphans`. Deterministic, local, no Ollama.
      Tests counted as importers (a module used only by its test isn't dead), so the
      bias is toward false negatives — never flag a used module. Covered by
      `tests/test_orphans.py`. Rationale: the Personal Training App audit found 5
      scaffold-era orphan modules, including a duplicate implementation with a
      divergent DB schema (`season_planner` vs `event_extractor`). This failure class
      is *generated* at scaffold time — cheapest to detect there, in every future
      project.
      Follow-ups deferred: JS/TS import graph (Python-only today); duplicate-
      implementation clustering (the divergent-schema case needs deeper analysis).
- [x] **`develop.py` emits a wiring-test stub** (2026-07-10): new `wiring.py` builds
      pytest-style stubs deterministically from the build plan's `api_routes` and file
      specs (no LLM), and `develop.py` drops them under `<tests>/wiring/` as a phase
      after scaffolding (only for files generated this run; never overwrites an existing
      file). Two stub kinds: **route** stubs assert the handler module imports and each
      handler function exists (runs immediately, catching broken/empty scaffolds) plus a
      `*_unverified` tripwire per route that **fails on purpose** with precise text
      (method, path, handler, models) until a human confirms the route is registered
      and — for writes — persists/removes a row; **module** stubs assert each scaffolded
      service imports and exposes its declared classes/functions. So a Potemkin route
      (200-OK wired to nothing) is loud-red-unverified instead of silently-green.
      Emitted code is ASCII-clean and compile-verified (it lands in arbitrary downstream
      repos). Covered by `tests/test_wiring.py`, including a `develop.py` integration
      test and a compile check on every emitted file. Python only for now (matches
      `orphans.py`). Follow-ups deferred: introspect the app object to auto-verify route
      registration (framework-specific); JS/TS stubs.

## Own house

- [x] **Toolkit test suite** — the tool that refuses to apply unverified fixes to other
      repos now has tests of its own (2026-07-10). Stdlib `unittest` only, so the suite
      runs on the same bare air-gapped Python as the toolkit — no pytest dependency.
      Run: `python -m unittest discover -s tests`. **199 tests**, all green. Coverage of
      every silent-failure surface the audit named:
        - `testgate.py` status parsing (pass/fail/no_tests/unavailable + failure-line
          regexes + affected-test heuristic), subprocess stubbed
        - `findings.py` ledger round-trip (validate/dedup/hash/sort/save/load/update)
        - `engine.py` JSON-parse fallbacks (`extract_json`/`strip_fences`/`chunk_text`)
          plus the `safe_abs_path` traversal guard
        - `catalog.py` cache-load + remote-fetch `except Exception` fallbacks (happy path
          pinned so a wrong-kwarg regression fails loudly), network stubbed
        - `hardware.py` `detect_gpu` / `installed_models` / `pull_model` broad-except
          probes + nvidia-smi parsing + the "never raises" contract, subprocess/HTTP stubbed
        - the new `orphans.py`, `claims.py`, `golden.py`, and `wiring.py` gates
