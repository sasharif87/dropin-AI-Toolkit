# Drop-in AI Toolkit

## About

A **validation-first harness** you drop into any project. It ships a pack of
deterministic, fail-closed gates that catch the failure classes scaffolding
tends to generate — dead modules, unwired routes, drifted golden output, broken
architectural layers, and docs that overstate the repo — and it can *also*
generate code, tests, reviews, and fixes with a local LLM.

The emphasis is deliberate. Validation is the **default** path and runs fully
air-gapped (no Ollama, no network); generation is **opt-in** and the only part
that talks to a model. That split is the point: the gates are the durable
value, and they judge *all* output the same way — whether a model, a teammate,
or you typed it.

> **Sovereignty constraint (non-negotiable):** local [Ollama](https://ollama.com/)
> only. No cloud-model integration, no external service. The generation half is
> a convenience; the harness never depends on it, and every gate works on a
> fully local, air-gapped stack.

## The two halves

| | **Validation** (default) | **Generation** (opt-in) |
| --- | --- | --- |
| Command | `drop.py` (no args) | `drop.py develop` / `test` / `review` / `fix` |
| Needs Ollama? | **No** — deterministic, air-gapped | Yes |
| Writes files? | No (read-only reports) | Only with `--apply` |
| What it does | Runs the gate pack + advisory reports, returns one pass/fail verdict | Scaffolds code, generates tests, reviews, applies fixes |

## Quick start

```bash
# Validate: run every configured gate + advisory report (air-gapped, no Ollama).
# Exit code is the verdict — 0 green, 1 if any blocking gate fails.
python drop.py

# Install the gates as a pre-commit hook so they fire without anyone remembering.
python drop.py hooks           # add --ci to also emit a GitHub Actions workflow

# Generation (needs Ollama running):
python drop.py detect          # detect stack/layers, show a plan
python drop.py develop --apply # scaffold from docs/ARCHITECTURE.md
python drop.py test --apply    # generate test suites
python drop.py review          # layer-aware code review -> findings JSON
python drop.py fix --apply     # apply the review's fixes (fail-closed)
```

## Validation: the gate pack

Running `drop.py` with no command aggregates everything below into a single
verdict. Each gate opts out cleanly when its config file is absent, so the same
command is safe to run in any repo — but a gate that *is* configured and can't
run is a surfaced failure, never a silent pass.

### Blocking gates (fail-closed — decide the exit code)

| Gate | Config file | Catches |
| --- | --- | --- |
| **Layers** (`layers.py`) | `.layers.json` | Upward imports (a lower layer importing the top/API layer) and layer-map gaps (a package with no entry in the layer rules). |
| **Invariants** (`invariants.py`) | `.invariants.py` | Design invariants that can't reduce to config — "this except-block must fail closed", "this column must never exist". The repo ships the checks; the toolkit ships the harness. |
| **Golden files** (`golden.py`) | `.golden.json` | Regressions in a command's real output. Runs a project command over input fixtures and diffs against banked snapshots. `drop.py golden --update` banks or re-blesses. |

### Advisory reports (informational — never block)

Print-only by design (biased toward false negatives so they never cry wolf).
They inform; they don't gate.

| Report | Source | Surfaces |
| --- | --- | --- |
| **Orphans** (`orphans.py`) | import graph | Modules nothing imports that have no entry point — scaffold residue to delete, wire up, or confirm. |
| **Claims** (`claims.py`) | docs vs repo | Numeric doc claims (test counts, LOC) the repo contradicts beyond tolerance — an unverified number is a credibility leak. |
| **Findings ledger** (`findings.py`) | last `review` run | A summary of `docs/review_findings.json`, the structured output of the last code review. |

### Why fail-closed matters

The gates are only leverage if they *fail loudly*. A missing golden snapshot, a
zero-match input glob, a broken invariant-check module, an unparseable source
file — all fail the gate rather than quietly passing. The whole philosophy is
that **a gate which silently checks nothing is the anti-pattern**, so the
toolkit refuses to be silently green.

## Wiring gates to fire

Gates that depend on a human remembering to run them don't hold a line.
`drop.py hooks` installs the fast, air-gapped gates as enforcement:

```bash
python drop.py hooks           # pre-commit hook (blocks a bad commit locally)
python drop.py hooks --ci      # also write .github/workflows/dropin-gates.yml
```

The pre-commit hook runs each gate only if that gate's config is present, needs
no network, and can be bypassed once with `DROPIN_SKIP=1 git commit ...`. An
existing foreign hook is backed up (with `--force`), never clobbered.

## Generation (needs Ollama)

| Command | Module | What it does |
| --- | --- | --- |
| `detect` | `detect.py` | Detect stack, framework, and layers; print a plan. |
| `develop` | `develop.py` | Scaffold code from `docs/ARCHITECTURE.md`, and drop wiring-test stubs (`wiring.py`) that turn a Potemkin route (200-OK wired to nothing) loud-red until a human verifies it. |
| `test` | `testgen.py` | Generate unit and integration test suites for existing code. |
| `review` | `review.py` | Layer-aware code review against generated rules; writes structured findings to `docs/review_findings.json`. |
| `fix` | `fix.py` | Apply the review's fixes — fail-closed: it won't apply on a red baseline (override with `--allow-red-baseline`) or when tests can't run (`--unverified`), and reverts a fix that breaks the suite. |
| `all` / `full` | — | Non-interactive / interactive pipelines chaining the above. |
| `setup` | `hardware.py`, `catalog.py` | Model-install wizard: connection, hardware/VRAM, catalog, and pulls. |

Writes are always gated. Nothing lands without `--apply`; interactive apply
prompts default to **`n`** on timeout, and `--yes` opts into unattended apply.

## Configuration

### Per-repo gate config

Each gate reads a small config file the consuming repo carries (generalizing the
`.layer_rules.json` pattern) — commit these to enable a gate:

- **`.layers.json`** — `source_root`, `top_layer`, `exclude`, and the layer-rules map path.
- **`.invariants.py`** — `from invariants import Invariant, ENFORCED, GAP`, define `check(repo)` functions, and export a module-level `INVARIANTS` list.
- **`.golden.json`** — a list of `cases`, each with a `command` (`{python}`/`{input}` placeholders, parsed as argv — no shell), `inputs` globs, and optional `capture` / `timeout` / `scrub`.

### Model configuration

```bash
python drop.py --url http://remote_host:11434   # point at a remote Ollama host
```

The engine routes tasks to different local models by complexity — a reasoning
model for architecture, a code model for generation and fixes, a quick model for
classification. Pin any of them with `--reason-model` / `--code-model` /
`--quick-model`, or save defaults via `drop.py setup`.

## Own house

The tool that refuses to apply unverified fixes to other repos is verified
itself:

- **Test suite** — stdlib `unittest` only (no pytest, so it runs on the same
  bare air-gapped Python as the toolkit). Run it with:

  ```bash
  python -m unittest discover -s tests
  ```

- **Dogfood CI** — `.github/workflows/ci.yml` runs the full suite plus the three
  gates pointed at the toolkit itself on every push/PR. The toolkit ships no gate
  config, so those steps opt out cleanly (exit 0) — proving the wiring runs
  end-to-end without inventing a false failure.

## Requirements

- Python 3.10+
- [Ollama](https://ollama.com/) — **only** for the generation commands. The
  validation gates and their hooks need neither Ollama nor a network.
