# dropin-AI-Toolkit — TODO

From portfolio audit (2026-07-03). The TestGate philosophy (fail-closed, explicit
`--unverified` / `--allow-red-baseline` opt-ins, batch revert) is the strongest gate
in the portfolio — these items convert audit findings from other repos into toolkit
leverage, since dropin is the factory for initial builds.

**Sovereignty constraint (non-negotiable):** local Ollama only. No cloud-model
integration, no Claude Code coupling. The long-term goal is moving _away_ from cloud
models — this toolkit is the sustainable, air-gapped path, and every item below must
work on a fully local stack. Cloud tools may consume the findings JSON if they want;
the harness never depends on them.

## Reframe: validation-first harness — landed 2026-07-11

The 2026-07-11 reorg made the split physical: `gates/` (validation half, stdlib,
air-gapped) vs `generation/` (Ollama half), drivers at the root.

## Open

### Own house — wire the gates on the toolkit itself

The toolkit dogfoods its _wiring_ (CI runs the three gates against itself and they
opt out cleanly) but ships no gate config of its own — the pack currently enforces
nothing here. Now that the `gates/` vs `generation/` boundary exists, there's a real
invariant to hold:

- [ ] **Fix the air-gap boundary leak.** `gates/orphans.py` imports `SKIP_DIRS`
      from `generation/detect.py` — the only gates→generation import. Move
      `SKIP_DIRS` into the gates half (or a gates-local constant) so the validation
      half never depends on the generation half. Prerequisite for the invariant
      below, which would fail on this today.
- [ ] **Ship the toolkit's own `.invariants.py`.** At minimum: (1) _air-gap
      boundary_ — no module under `gates/` imports a module under `generation/`;
      (2) _air-gapped dispatch_ — `drop.py` dispatches `validate`/`golden`/
      `layers`/`invariants`/`hooks` before constructing `Engine`; (3) _fail-closed
      prompts_ — `timed_input` still defaults to `'n'`. The gate pack finally
      enforces something on its own repo instead of opting out.
- [ ] **Make `gates/` + `drop.py` vendorable.** `drop.py` imports
      `engine`/`detect`/`rules`/`catalog`/`config` at module load, so even
      `drop.py validate` needs `generation/` on disk. Move the generation imports
      into `main()` after the air-gapped dispatch (a local `log` shim covers the
      gate commands). Then strategy (B) in the CI template — vendor just the
      validation half — becomes real, and the `.invariants.py` above should pin it.

### Portability — gates beyond Python

`layers`, `orphans`, and the wiring stubs are Python-only (`ast`); other languages
get no findings rather than a guess — honest, but it caps who the pack serves.
`golden` (any command) and `invariants` (arbitrary check code) are already
language-agnostic; `claims` counts only Python tests.

- [ ] **JS/TS import graph** for `layers` + `orphans` (the audit's follow-up,
      deferred twice). Constraint: stdlib-only, so no real JS parser — regex-based
      import/require extraction, biased to false negatives like the Python path.
- [ ] **JS/TS wiring-test stubs** (jest/vitest shape) from the build plan.
- [ ] **`claims` test-count derivation for JS/TS** (`test(...)`/`it(...)`
      occurrences, same gross-divergence-only bias).
- [ ] **Language-agnostic `Repo` helpers** in `invariants.py` — the text helpers
      (`read`/`iter_lines`/`exists`) already work on any file; document that split
      so non-Python repos know they can still write invariants.

### Consolidated deferred follow-ups

Carried out of the landed items (each was noted "deferred" at landing):

- [ ] **Findings-ledger parity** — emit gate violations and advisory residue
      (orphans, claims, golden regressions, layer/invariant failures) into the
      findings JSON ledger, not just stdout (deferred from five separate items).
- [ ] `golden`: capture exit code as part of the snapshot; `--only <case>` filter.
- [ ] `invariants`: `--only <id>` filter.
- [ ] `layers`: resolve relative imports against a package root; multiple/tiered
      top layers.
- [ ] `claims`: `deployed`/`in production`/coverage-% claims (needs a runner or
      deploy probe, not static re-derivation).
- [ ] `triggers`: optional pre-push hook for a slower/full set; surface the
      orphans/claims residue in CI via an air-gapped entry point.
- [ ] `orphans`: duplicate-implementation clustering (the divergent-schema case).
- [ ] `wiring`: introspect the app object to auto-verify route registration
      (framework-specific).
- [ ] `mcp_server.py`: expose `validate` as a tool (the local bridge for any
      consumer).
