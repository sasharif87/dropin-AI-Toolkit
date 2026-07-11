"""Tests for catalog.py — the base model-catalog layer.

catalog.py has broad `except Exception: pass` fallbacks around cache load and
remote fetch: a wrong kwarg or a parse regression there would silently fall
back to the builtin catalog forever. These tests pin the happy path (so such a
regression fails loudly) alongside the intended graceful degradation. Network
is stubbed; no real host is contacted. Stdlib unittest.
"""

import json
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "gates"), os.path.join(_ROOT, "generation")]

import catalog


class _FakeResp:
    """Minimal context-manager stand-in for a urlopen response."""
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class LoadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = os.path.join(self._tmp.name, "cat.json")

    def tearDown(self):
        self._tmp.cleanup()

    def test_valid_cache_is_used_not_builtin(self):
        custom = {"version": "test-1", "preferences": {"code": ["m"]}}
        with open(self.cache, "w", encoding="utf-8") as fh:
            json.dump(custom, fh)
        got = catalog.load(self.cache)
        self.assertEqual(got["version"], "test-1")

    def test_missing_cache_falls_back_to_builtin(self):
        self.assertIs(catalog.load(self.cache), catalog.BUILTIN_CATALOG)

    def test_invalid_json_falls_back_to_builtin(self):
        with open(self.cache, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        self.assertIs(catalog.load(self.cache), catalog.BUILTIN_CATALOG)

    def test_cache_missing_preferences_key_falls_back(self):
        with open(self.cache, "w", encoding="utf-8") as fh:
            json.dump({"version": "x"}, fh)  # no 'preferences'
        self.assertIs(catalog.load(self.cache), catalog.BUILTIN_CATALOG)


class FetchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = os.path.join(self._tmp.name, "cat.json")
        self._orig_urlopen = catalog.urllib.request.urlopen
        self._orig_catalog = catalog._catalog

    def tearDown(self):
        catalog.urllib.request.urlopen = self._orig_urlopen
        catalog._catalog = self._orig_catalog
        self._tmp.cleanup()

    def _stub(self, resp=None, raises=None):
        def fake(*a, **k):
            if raises is not None:
                raise raises
            return resp
        catalog.urllib.request.urlopen = fake

    def test_valid_fetch_returns_and_caches(self):
        payload = {"version": "remote-1", "preferences": {"code": ["m"]}}
        self._stub(_FakeResp(json.dumps(payload).encode("utf-8")))
        got = catalog.fetch("http://host/catalog.json", cache_path=self.cache)
        self.assertEqual(got["version"], "remote-1")
        # Cache written and in-memory catalog updated.
        self.assertTrue(os.path.isfile(self.cache))
        self.assertIs(catalog._catalog, got)

    def test_network_error_raises_valueerror(self):
        self._stub(raises=OSError("connection refused"))
        with self.assertRaises(ValueError):
            catalog.fetch("http://host/catalog.json", cache_path=self.cache)

    def test_invalid_json_raises_valueerror(self):
        self._stub(_FakeResp(b"{ not json"))
        with self.assertRaises(ValueError):
            catalog.fetch("http://host/catalog.json", cache_path=self.cache)

    def test_missing_preferences_raises_valueerror(self):
        self._stub(_FakeResp(json.dumps({"version": "x"}).encode("utf-8")))
        with self.assertRaises(ValueError):
            catalog.fetch("http://host/catalog.json", cache_path=self.cache)


class AccessorTests(unittest.TestCase):
    def setUp(self):
        self._orig = catalog._catalog
        catalog._catalog = catalog.BUILTIN_CATALOG  # deterministic, ignore machine cache

    def tearDown(self):
        catalog._catalog = self._orig

    def test_preferences_all_roles(self):
        prefs = catalog.preferences()
        self.assertIn("reason", prefs)
        self.assertIn("code", prefs)
        self.assertIn("quick", prefs)

    def test_preferences_single_role_returns_list(self):
        self.assertIsInstance(catalog.preferences("code"), list)

    def test_preferences_unknown_role_returns_empty(self):
        self.assertEqual(catalog.preferences("nonexistent"), [])

    def test_ctx_windows_and_sizes_are_dicts(self):
        self.assertIsInstance(catalog.ctx_windows(), dict)
        self.assertIsInstance(catalog.sizes(), dict)

    def test_version_present(self):
        self.assertTrue(catalog.catalog_version())

    def test_active_caches_after_first_call(self):
        catalog._catalog = None
        first = catalog.active()
        self.assertIsNotNone(first)
        self.assertIs(catalog.active(), first)


if __name__ == "__main__":
    unittest.main()
