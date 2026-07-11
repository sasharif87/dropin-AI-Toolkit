"""Tests for layers.py — the config-driven architectural layer gate.

Stdlib unittest only: the toolkit tests itself on bare Python with no pip
install, matching the air-gapped sovereignty constraint. Also collectable by
pytest if it happens to be present.

Every case builds a synthetic project tree in a temp dir (the IME source itself
isn't shipped), so the checks are exercised against real files, not mocks.
"""

import json
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "gates"), os.path.join(_ROOT, "generation")]

from layers import (
    run_layers,
    find_upward_import_violations,
    find_missing_layer_entries,
    _module_name,
    _imports,
)


def _write(root, rel, content=""):
    path = os.path.join(root, rel.replace("/", os.sep))
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)


class LayerGateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _config(self, **overrides):
        cfg = {
            "source_root": "backend",
            "top_layer": "backend.api",
            "exclude": ["cli", "scripts"],
            "layer_rules": "docs/.layer_rules.json",
        }
        cfg.update(overrides)
        _write(self.root, ".layers.json", json.dumps(cfg))

    def _rules(self, mapping):
        _write(self.root, "docs/.layer_rules.json", json.dumps(mapping))

    # ── clean project ──────────────────────────────────────────────────
    def _clean_project(self):
        _write(self.root, "backend/__init__.py")
        _write(self.root, "backend/api/__init__.py")
        _write(self.root, "backend/api/routes.py", "from backend.services import svc\n")
        _write(self.root, "backend/services/__init__.py")
        _write(self.root, "backend/services/svc.py", "x = 1\n")
        self._rules({"backend/api": "rules", "backend/services": "rules"})

    def test_clean_project_passes(self):
        self._config()
        self._clean_project()
        result = run_layers(self.root)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["upward"], [])
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["errors"], [])

    # ── upward-import check ────────────────────────────────────────────
    def test_upward_import_flagged(self):
        self._config()
        self._clean_project()
        # services (a lower layer) imports the api layer — forbidden.
        _write(self.root, "backend/services/bad.py",
               "from backend.api import routes\n")
        result = run_layers(self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["upward"]), 1)
        self.assertIn("backend/services/bad.py", result["upward"][0])

    def test_top_layer_may_import_itself(self):
        self._config()
        self._clean_project()
        _write(self.root, "backend/api/handlers.py",
               "from backend.api import routes\n")
        result = run_layers(self.root)
        self.assertEqual(result["upward"], [])
        self.assertTrue(result["ok"])

    def test_from_package_import_layer_is_caught(self):
        # `from backend import api` imports the api *package* — must be caught,
        # not only the dotted `import backend.api` form.
        self._config()
        self._clean_project()
        _write(self.root, "backend/services/sneaky.py",
               "from backend import api\n")
        result = run_layers(self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("sneaky.py" in v for v in result["upward"]))

    def test_one_violation_per_import_line(self):
        _write(self.root, "backend/__init__.py")
        _write(self.root, "backend/api/__init__.py")
        _write(self.root, "backend/services/__init__.py")
        _write(self.root, "backend/services/multi.py",
               "import backend.api.routes, backend.api.models\n")
        up, err = find_upward_import_violations(self.root, "backend", "backend.api")
        self.assertEqual(err, [])
        self.assertEqual(len(up), 1)  # one offending line -> one violation

    def test_relative_import_not_flagged(self):
        # Relative imports aren't resolved to the top layer under the
        # package-from-root convention; parity with IME.
        self._config()
        self._clean_project()
        _write(self.root, "backend/services/rel.py", "from . import svc\n")
        result = run_layers(self.root)
        self.assertEqual(result["upward"], [])

    def test_unparseable_file_is_an_error_not_silent(self):
        self._config()
        self._clean_project()
        _write(self.root, "backend/services/broken.py", "def (:\n")  # syntax error
        result = run_layers(self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("broken.py" in e for e in result["errors"]))

    # ── completeness check ─────────────────────────────────────────────
    def test_missing_layer_entry_flagged(self):
        self._config()
        self._clean_project()
        # New package with no rules entry.
        _write(self.root, "backend/workers/__init__.py")
        _write(self.root, "backend/workers/run.py", "x = 1\n")
        result = run_layers(self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("backend/workers" in m for m in result["missing"]))

    def test_excluded_dirs_need_no_entry(self):
        self._config()
        self._clean_project()
        _write(self.root, "backend/cli/__init__.py")
        _write(self.root, "backend/cli/main.py", "x = 1\n")
        _write(self.root, "backend/scripts/__init__.py")
        result = run_layers(self.root)
        self.assertEqual(result["missing"], [])
        self.assertTrue(result["ok"])

    def test_excluded_dir_may_import_top_layer(self):
        # `exclude` names dirs that aren't layers (CLIs, scripts) — entry
        # points that legitimately import the top layer. They're skipped by
        # the upward-import scan too, not just the completeness check.
        self._config()
        self._clean_project()
        _write(self.root, "backend/cli/__init__.py")
        _write(self.root, "backend/cli/main.py",
               "from backend.api import routes\n")
        result = run_layers(self.root)
        self.assertEqual(result["upward"], [])
        self.assertTrue(result["ok"], result)

    def test_exclude_applies_only_to_top_level_dirs(self):
        # A nested dir that happens to share an excluded name is still part of
        # its layer — exclude prunes direct children of source_root only.
        self._config()
        self._clean_project()
        _write(self.root, "backend/services/cli/__init__.py")
        _write(self.root, "backend/services/cli/tool.py",
               "from backend.api import routes\n")
        result = run_layers(self.root)
        self.assertEqual(len(result["upward"]), 1)
        self.assertFalse(result["ok"])

    def test_non_package_dir_needs_no_entry(self):
        # A dir without __init__.py isn't an architectural package.
        self._config()
        self._clean_project()
        _write(self.root, "backend/assets/logo.txt", "not python\n")
        result = run_layers(self.root)
        self.assertEqual(result["missing"], [])
        self.assertTrue(result["ok"])

    # ── fail-closed config handling ────────────────────────────────────
    def test_no_config_opts_out(self):
        self._clean_project()  # tree present, but no .layers.json
        result = run_layers(self.root)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["config"])
        self.assertIn("nothing to check", result["note"])

    def test_malformed_config_fails(self):
        _write(self.root, ".layers.json", "{ not valid json")
        result = run_layers(self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(result["errors"])

    def test_missing_source_root_key_fails(self):
        _write(self.root, ".layers.json", json.dumps({"top_layer": "backend.api"}))
        result = run_layers(self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("source_root" in e for e in result["errors"]))

    def test_missing_top_layer_key_fails(self):
        _write(self.root, ".layers.json", json.dumps({"source_root": "backend"}))
        _write(self.root, "backend/__init__.py")
        result = run_layers(self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("top_layer" in e for e in result["errors"]))

    def test_absent_source_root_dir_fails(self):
        self._config(source_root="nonexistent")
        result = run_layers(self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("source_root not found" in e for e in result["errors"]))

    def test_missing_rules_map_fails_closed(self):
        self._config()
        _write(self.root, "backend/__init__.py")
        _write(self.root, "backend/api/__init__.py")
        # No docs/.layer_rules.json written.
        result = run_layers(self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("layer-rules map not found" in e for e in result["errors"]))

    def test_default_rules_path_used_when_unset(self):
        self._config(layer_rules=None)  # falls back to docs/.layer_rules.json
        self._clean_project()
        result = run_layers(self.root)
        self.assertTrue(result["ok"], result)

    def test_explicit_config_path(self):
        self._clean_project()
        alt = os.path.join(self.root, "custom.layers.json")
        _write(self.root, "custom.layers.json", json.dumps({
            "source_root": "backend", "top_layer": "backend.api",
            "exclude": ["cli", "scripts"],
        }))
        result = run_layers(self.root, config_path=alt)
        self.assertTrue(result["ok"], result)

    def test_missing_explicit_config_fails_closed(self):
        # An explicitly requested config that doesn't exist is a surfaced
        # failure — a typo'd --layers-config must never go silently green.
        # Only auto-discovery finding nothing is an opt-out.
        self._clean_project()
        missing = os.path.join(self.root, "typo.layers.json")
        result = run_layers(self.root, config_path=missing)
        self.assertFalse(result["ok"])
        self.assertEqual(result["config"], missing)
        self.assertTrue(any("not found" in e for e in result["errors"]))

    # ── unit helpers ───────────────────────────────────────────────────
    def test_module_name(self):
        self.assertEqual(_module_name("backend/api/routes.py"), "backend.api.routes")

    def test_imports_expands_from_targets(self):
        pairs = _imports("from backend import api\nimport os\n", "x.py")
        names = {n for n, _ in pairs}
        self.assertIn("backend", names)
        self.assertIn("backend.api", names)
        self.assertIn("os", names)


if __name__ == "__main__":
    unittest.main()
