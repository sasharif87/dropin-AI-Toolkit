"""Tests for orphans.py — the zero-caller / scaffold-residue report.

Stdlib unittest only: the toolkit tests itself on bare Python with no pip
install, matching the air-gapped sovereignty constraint. Also collectable by
pytest if it happens to be present.
"""

import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "gates"), os.path.join(_ROOT, "generation")]

from orphans import find_orphans, _collect_references, _has_main_guard, _dotted_suffixes


def _write(root, rel, content=""):
    path = os.path.join(root, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


class OrphanDetectionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _orphan_files(self):
        return {o["file"] for o in find_orphans(self.root)}

    def test_flags_unimported_module(self):
        _write(self.root, "app.py", "import lib\n")
        _write(self.root, "lib.py", "x = 1\n")
        _write(self.root, "dead.py", "y = 2\n")  # nobody imports
        self.assertEqual(self._orphan_files(), {"dead.py"})

    def test_main_guard_is_not_an_orphan(self):
        _write(self.root, "runner.py",
               "def go():\n    pass\n\nif __name__ == '__main__':\n    go()\n")
        self.assertNotIn("runner.py", self._orphan_files())

    def test_entrypoint_basenames_excluded(self):
        for name in ("__init__.py", "conftest.py", "manage.py", "wsgi.py",
                     "app.py", "main.py"):
            _write(self.root, name, "x = 1\n")
        self.assertEqual(self._orphan_files(), set())

    def test_test_files_are_not_orphans_but_do_count_as_importers(self):
        # feature.py is imported only by its test — that still counts as used.
        _write(self.root, "feature.py", "value = 1\n")
        _write(self.root, "tests/test_feature.py", "import feature\n")
        self.assertEqual(self._orphan_files(), set())

    def test_from_import_of_submodule(self):
        _write(self.root, "app.py", "from services import used\n")
        _write(self.root, "services/__init__.py", "")
        _write(self.root, "services/used.py", "u = 1\n")
        _write(self.root, "services/orphan.py", "o = 1\n")
        self.assertEqual(self._orphan_files(), {"services/orphan.py"})

    def test_dotted_import_matches(self):
        _write(self.root, "app.py", "import pkg.deep.mod\n")
        _write(self.root, "pkg/__init__.py", "")
        _write(self.root, "pkg/deep/__init__.py", "")
        _write(self.root, "pkg/deep/mod.py", "m = 1\n")
        self.assertEqual(self._orphan_files(), set())

    def test_relative_import(self):
        _write(self.root, "pkg/__init__.py", "")
        _write(self.root, "pkg/a.py", "from . import b\n")
        _write(self.root, "pkg/b.py", "x = 1\n")
        # a.py has no importer, but it is the sole caller of b; a should be
        # flagged, b should not.
        self.assertEqual(self._orphan_files(), {"pkg/a.py"})

    def test_migrations_dir_excluded(self):
        _write(self.root, "dead.py", "x = 1\n")  # unimported -> orphan
        _write(self.root, "migrations/0001_initial.py", "op = 1\n")  # excluded
        self.assertEqual(self._orphan_files(), {"dead.py"})

    def test_non_python_project_returns_empty(self):
        _write(self.root, "index.js", "console.log(1)\n")
        _write(self.root, "package.json", "{}\n")
        self.assertEqual(find_orphans(self.root), [])

    def test_syntax_error_file_does_not_crash(self):
        _write(self.root, "app.py", "import lib\n")
        _write(self.root, "lib.py", "x = 1\n")
        _write(self.root, "broken.py", "def (:\n")  # unparseable
        # broken.py has no importer and unparseable -> reported, no exception.
        self.assertIn("broken.py", self._orphan_files())


class ImportParsingTests(unittest.TestCase):
    def test_plain_import(self):
        self.assertIn("a.b", _collect_references("import a.b\n"))

    def test_from_import_records_module_and_member(self):
        refs = _collect_references("from a.b import c\n")
        self.assertIn("a.b", refs)
        self.assertIn("a.b.c", refs)

    def test_relative_from_import_records_leaf(self):
        self.assertIn("c", _collect_references("from . import c\n"))

    def test_main_guard_detection(self):
        self.assertTrue(_has_main_guard("if __name__ == '__main__':\n    pass\n"))
        self.assertFalse(_has_main_guard("x = 1\n"))

    def test_main_guard_survives_syntax_error(self):
        # Fallback path: unparseable but clearly guarded.
        self.assertTrue(_has_main_guard("def (:\nif __name__ == '__main__':\n"))

    def test_dotted_suffixes(self):
        self.assertEqual(_dotted_suffixes("a/b/c.py"), {"a.b.c", "b.c", "c"})


if __name__ == "__main__":
    unittest.main()
