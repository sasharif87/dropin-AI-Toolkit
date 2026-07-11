# Toolkit self-tests

The toolkit that refuses to apply unverified fixes to other repos now has tests
of its own. These are **stdlib `unittest` only** — no pytest, no pip install —
so the suite runs on the same bare, air-gapped Python the toolkit itself runs
on (the sovereignty constraint in `../TODO.md`). pytest will also collect them
if it happens to be installed.

Run from the repo root:

```sh
python -m unittest discover -s tests
```

Coverage today (the silent-failure surfaces named in the TODO) — 281 tests:

- `test_orphans.py`  — zero-caller / scaffold-residue detection (`orphans.py`)
- `test_claims.py`   — doc-claim checker: re-derive test counts / LOC from the
                        repo and flag only divergence beyond tolerance
                        (`claims.py`)
- `test_golden.py`   — golden-file runner: bank known-good output for real input
                        fixtures and diff on change; fail-closed on missing
                        golden or zero-match glob (`golden.py`)
- `test_layers.py`   — architectural layer gate: no upward imports into the top
                        layer + layer-map completeness, config-driven and
                        fail-closed (`layers.py`)
- `test_invariants.py` — pluggable design-invariant harness: ENFORCED/GAP states,
                        fail-closed loader/runner, each error class surfaced
                        (`invariants.py`)
- `test_triggers.py` — local trigger installer: pre-commit hook render/install/
                        backup/refresh and the CI workflow (`triggers.py`)
- `test_validate.py` — the default path: the aggregate `ok` is the AND of the
                        blocking gates, and orphans/claims/findings stay advisory
                        (never flip the verdict) (`validate.py`)
- `test_wiring.py`   — wiring-test stub emitter: deterministic pytest stubs for
                        scaffolded routes/modules, compile-verified and ASCII;
                        the `*_unverified` tripwire fails until wiring is
                        confirmed (`wiring.py`, `develop.py` integration)
- `test_findings.py` — the review→fix JSON contract: validate, dedup, hash,
                        sort, save/load/update round-trip (`findings.py`)
- `test_testgate.py` — fix-verification status parsing:
                        pass/fail/no_tests/unavailable, failure-line regexes,
                        affected-test heuristic (`testgate.py`)
- `test_engine.py`   — untrusted-output parsers and the path-traversal guard:
                        `extract_json`, `strip_fences`, `safe_abs_path`,
                        `chunk_text`, `fmt_time` (`engine.py`)
- `test_catalog.py`  — cache-load + remote-fetch `except Exception` fallbacks;
                        the happy path is pinned so a wrong-kwarg regression
                        fails loudly instead of silently using the builtin
                        catalog (`catalog.py`, network stubbed)
- `test_hardware.py` — `detect_gpu` / `installed_models` / `pull_model` broad-
                        except probes, nvidia-smi parsing, and the "never
                        raises" contract (`hardware.py`, subprocess/HTTP stubbed)

The broad-except probes are tested both ways on purpose: the fallback returns
the documented safe value (`[]`, `None` budget, `False`) *and* the success path
is asserted, so a genuine bug hiding behind `except Exception` surfaces as a
failing test.
