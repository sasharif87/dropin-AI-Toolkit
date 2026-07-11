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
- [x] **Extract IME's layer gate** (Gate A) (2026-07-11): `layers.py` ports IME's
      `scripts/check_layers.py` behind a per-repo `.layers.json`, so homeForge and the
      training app get IME-grade layer gating without a bespoke script. Two deterministic
      checks (no Ollama, `ast` only): **no upward imports** — a configured `top_layer` (e.g.
      the API/route layer) may not be imported by any module below it — and **layer-map
      completeness** — every package under `source_root` must have an entry in the
      `.layer_rules.json` map. The four IME hardcodes (source root, top layer, non-layer
      exclusions, rules-map path) all became config keys; `from pkg import layer` is caught,
      not only `import pkg.layer`. New subcommand `drop.py layers`, dispatched air-gapped
      before engine setup (sovereignty constraint) like `golden`. Fail-closed throughout: a
      configured gate with a missing source root or rules map fails, an unparseable source
      file is a *surfaced error* (never a silent skip), and a repo with no `.layers.json`
      opts out. Covered by `tests/test_layers.py` (20 hermetic temp-repo cases).
      Python package layout only (matches orphans/wiring/claims). The reference source was
      staged in `_ime_ref/` (gitignored) — folder-scoped so the port never had to open the
      IME repo root; deleted once Gate B landed (see below). Follow-ups deferred: resolving
      relative imports against a package root; multiple/tiered top layers; emitting
      violations into the findings JSON ledger.
- [x] **Extract IME's invariant harness** (Gate B) (2026-07-11): `invariants.py` ports IME's
      `scripts/check_invariants.py` *scaffolding* as a **pluggable-check harness**. Unlike the
      declarative layer gate (a repo ships a `.layers.json` the toolkit reads), invariant checks
      are *code, not config* — "this except-block must fail closed", "this column must never
      exist on that model" — so the toolkit ships the machinery and each repo ships the checks:
      the `Invariant` model with `ENFORCED`/`GAP` states, a `Repo` of cheap AST/text helpers
      (`read`/`parse`/`function_source`/`except_body_source`/`has_required_str_param`/
      `iter_py`/`iter_lines` — the ported lines 44–104), a fail-closed importlib loader, and a
      runner (the ported registry/runner 317–392). A consuming repo writes a `.invariants.py`
      that does `from invariants import Invariant, ENFORCED, GAP` and defines `check(repo)`
      functions plus a module-level `INVARIANTS` list; IME's 17 `check_N_*` functions stay in
      IME as the reference example (never ported). New subcommand `drop.py invariants`,
      dispatched air-gapped before engine setup (sovereignty constraint) like `golden`/`layers`.
      Fail-closed throughout: a missing/opt-out `.invariants.py` passes with a note, but a syntax
      error, a missing `INVARIANTS` list, an `ENFORCED` entry with no callable check, an unknown
      status, a check that *raises*, and a check returning a non-list are all *surfaced errors*
      that fail the gate (a broken check can never hide a real violation). Deterministic, local,
      no Ollama (`ast` + text only). Covered by `tests/test_invariants.py` (24 hermetic temp-repo
      cases: happy path, each failure/error class, gaps-never-fail, opt-out, and direct `Repo`
      helper unit tests). With Gate B landed the reference `_ime_ref/` was deleted (both gates
      ported). Follow-ups deferred: emitting violations into the findings JSON ledger (parity
      with orphans/claims, which also only print); a `--only <id>` filter.
- [x] **Wire local triggers** (2026-07-11): `triggers.py` + `drop.py hooks` install the
      fast, air-gapped, fail-closed gates (`layers`, `invariants`, `golden`) as a **pre-commit
      hook**, so enforcement stops depending on memory (the "event-driven" property from the
      reframe). The hook is a POSIX `sh` script (runs under Git for Windows too) with the
      interpreter + `drop.py` path baked in at install time; it runs each gate *only* if that
      gate's config is present (`.layers.json` / `.invariants.py` / `.golden.json`), so an
      unconfigured repo sees no noise and the same hook is safe to install everywhere. Verified
      end-to-end: a failing gate blocks the commit (exit 1, clear message), `DROPIN_SKIP=1`
      bypasses once, a fixed gate passes, and Windows backslash paths execute fine under Git
      Bash. Fail-closed install: a foreign existing hook is backed up (not clobbered) only with
      `--force`, a prior dropin hook is refreshed in place (idempotent), and a non-git dir is an
      error. `drop.py hooks --ci` also emits a `.github/workflows/dropin-gates.yml` running the
      three gates (honest about the one deployment choice it can't make: how a consuming repo's
      CI gets `drop.py` on disk — checkout vs vendor). Deterministic, local, no Ollama (dispatched
      air-gapped before engine setup like the gates it installs). Covered by
      `tests/test_triggers.py` (14 hermetic cases: render/substitution, install/backup/refresh,
      worktree `gitdir:` pointer, CI write, aggregate ok). **Own house:** the toolkit now dogfoods
      its own enforcement via `.github/workflows/ci.yml` — the full unittest suite plus the three
      gates pointed at itself (they opt out cleanly, proving the wiring). The advisory reports
      (orphans/claims via `detect`) stay *out* of the blocking path: they're print-only by design
      (false-negative bias) and `detect` needs the engine, so they don't belong in an air-gapped
      commit hook. Follow-ups deferred: a pre-push hook for a slower/full set; surfacing the
      orphans/claims residue report in CI via an air-gapped entry point.
- [x] **Invert the CLI** (2026-07-11): `validate.py` + `drop.py validate` make validation
      the **default** path — `drop.py` with no command now runs the gates + advisory
      reports (the "gates + findings" destination of this section), and generation
      (`develop`/`test`/`review`/`fix`) is opt-in. `validate` aggregates the three
      deterministic gates (`layers`, `invariants`, `golden`) via their `run_*` functions and
      the advisory finders (`orphans`, `claims`, plus a one-line summary of the last review's
      `docs/review_findings.json` ledger) into one verdict. The aggregate `ok` is the **AND of
      the blocking gates only** — advisory reports print but never flip the verdict (same
      false-negative-biased split `triggers.py` already draws for the commit hook). Dispatched
      air-gapped before engine setup (sovereignty constraint) like the gates it wraps, so the
      toolkit's own front door needs no Ollama and no network. Fail-closed inheritance: each
      gate opts out cleanly when unconfigured, a *configured* gate that can't run fails, and a
      repo with zero gates validates green but the output loudly says "nothing is enforced"
      (a gate pack that enforces nothing is the anti-pattern, not a pass to celebrate). The
      one behavioral change is the default command (`detect` → `validate`); `detect` stays as
      the Ollama-coupled project overview. Covered by `tests/test_validate.py` (19 hermetic
      cases: opt-out, each gate wired/failing, one-bad-gate-fails-the-aggregate, advisories
      never block, findings summary present/absent, explicit config paths, and print
      rendering for pass/fail/no-config). README rewritten around the validation-first framing
      (the two halves, the gate pack table, wiring gates to fire). Follow-ups deferred:
      folding the advisory residue into a machine-readable findings JSON (parity with the
      review ledger); `mcp_server.py` exposing `validate` as the local bridge for any consumer.

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
      Run: `python -m unittest discover -s tests`. **257 tests**, all green. Coverage of
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
        - the new `orphans.py`, `claims.py`, `golden.py`, `wiring.py`, `layers.py`, and
          `invariants.py` gates, plus `triggers.py` (the pre-commit/CI installer)
