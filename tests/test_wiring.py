"""Tests for wiring.py — the deterministic wiring-test stub emitter.

Stdlib unittest only: the toolkit tests itself on bare Python with no pip
install, matching the air-gapped sovereignty constraint. The emitted stubs are
pytest-style (for the target project), so these tests only *compile* them —
they never import pytest, which the toolkit does not depend on.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wiring import (
    build_wiring_tests, _module_path, _flatten, _slug, _clean, _norm_rel,
)


def _plan(routes=None, layers=None):
    return {"api_routes": routes or [], "layers": layers or []}


def _route(method, path, handler_file, fn, **kw):
    r = {"method": method, "path": path, "handler_file": handler_file,
         "handler_function": fn}
    r.update(kw)
    return r


class BuildWiringTests(unittest.TestCase):
    def test_route_produces_stub_file(self):
        plan = _plan([_route("GET", "/api/rides", "api/rides.py", "list_rides")])
        stubs = build_wiring_tests(plan)
        self.assertIn("tests/wiring/test_wiring_api_rides.py", stubs)

    def test_every_emitted_file_is_valid_python(self):
        plan = _plan(
            routes=[
                _route("GET", "/api/rides", "api/rides.py", "list_rides",
                       response_model="RideList"),
                _route("POST", "/api/rides/{id}", "api/rides.py", "create_ride",
                       request_model="RideCreate", response_model="Ride"),
                _route("DELETE", "/api/rides/{id}", "api/rides.py", "delete_ride"),
            ],
            layers=[{"key": "svc", "files": [
                {"path": "svc/ride_service.py", "key_classes": ["RideService"]}]}],
        )
        stubs = build_wiring_tests(plan)
        self.assertTrue(stubs)
        for path, content in stubs.items():
            compile(content, path, "exec")  # syntax must be valid

    def test_emitted_content_is_ascii(self):
        # Stubs land in arbitrary downstream repos — keep them encoding-safe.
        plan = _plan(
            routes=[_route("POST", "/api/rides/{id}", "api/rides.py", "create_ride",
                           request_model="RideCreate")],
            layers=[{"key": "svc", "files": [
                {"path": "svc/s.py", "key_functions": ["go"]}]}],
        )
        for content in build_wiring_tests(plan).values():
            content.encode("ascii")  # raises if any non-ASCII slipped in

    def test_handlers_deduped_and_ordered(self):
        plan = _plan([
            _route("GET", "/r", "api/r.py", "handler_a"),
            _route("POST", "/r", "api/r.py", "handler_b"),
            _route("GET", "/r/2", "api/r.py", "handler_a"),  # dup
        ])
        content = build_wiring_tests(plan)["tests/wiring/test_wiring_api_r.py"]
        self.assertIn('HANDLERS = ["handler_a", "handler_b"]', content)

    def test_write_route_mentions_persistence(self):
        plan = _plan([_route("POST", "/api/rides", "api/rides.py", "create_ride")])
        content = build_wiring_tests(plan)["tests/wiring/test_wiring_api_rides.py"]
        self.assertIn("PERSISTS a row", content)
        self.assertIn("persist a row", content)  # in the pytest.fail message

    def test_delete_route_mentions_removal(self):
        plan = _plan([_route("DELETE", "/api/rides/{id}", "api/rides.py", "delete_ride")])
        content = build_wiring_tests(plan)["tests/wiring/test_wiring_api_rides.py"]
        self.assertIn("REMOVES the target row", content)

    def test_read_route_mentions_response_model(self):
        plan = _plan([_route("GET", "/api/rides", "api/rides.py", "list_rides",
                             response_model="RideList")])
        content = build_wiring_tests(plan)["tests/wiring/test_wiring_api_rides.py"]
        self.assertIn("RideList", content)
        self.assertNotIn("PERSISTS a row", content)

    def test_tripwire_fails_by_design(self):
        plan = _plan([_route("GET", "/api/rides", "api/rides.py", "list_rides")])
        content = build_wiring_tests(plan)["tests/wiring/test_wiring_api_rides.py"]
        self.assertIn("pytest.fail(", content)
        self.assertIn("_unverified", content)

    def test_null_models_omitted(self):
        plan = _plan([_route("POST", "/api/rides", "api/rides.py", "create_ride",
                             request_model="null", response_model="none")])
        content = build_wiring_tests(plan)["tests/wiring/test_wiring_api_rides.py"]
        self.assertNotIn("null", content)
        self.assertNotIn("none", content)

    def test_generated_paths_filter_excludes_unscaffolded(self):
        plan = _plan([
            _route("GET", "/a", "api/a.py", "h_a"),
            _route("GET", "/b", "api/b.py", "h_b"),
        ])
        stubs = build_wiring_tests(plan, generated_paths={"api/a.py"})
        self.assertIn("tests/wiring/test_wiring_api_a.py", stubs)
        self.assertNotIn("tests/wiring/test_wiring_api_b.py", stubs)

    def test_service_module_stub(self):
        plan = _plan(layers=[{"key": "svc", "files": [
            {"path": "svc/ride_service.py", "key_classes": ["RideService"],
             "key_functions": ["summarize"]}]}])
        content = build_wiring_tests(plan)["tests/wiring/test_wiring_modules.py"]
        self.assertIn('"svc.ride_service"', content)
        self.assertIn("RideService", content)
        self.assertIn("summarize", content)

    def test_route_handler_not_duplicated_as_module(self):
        # A file that is a route handler is covered by the route stub, so it must
        # not also appear in the module-reachability stub.
        plan = _plan(
            routes=[_route("GET", "/r", "api/r.py", "h")],
            layers=[{"key": "api", "files": [{"path": "api/r.py",
                                              "key_functions": ["h"]}]}],
        )
        stubs = build_wiring_tests(plan)
        self.assertNotIn("tests/wiring/test_wiring_modules.py", stubs)

    def test_packaging_files_skipped_as_modules(self):
        plan = _plan(layers=[{"key": "p", "files": [
            {"path": "pkg/__init__.py"}, {"path": "conftest.py"}]}])
        stubs = build_wiring_tests(plan)
        self.assertEqual(stubs, {})

    def test_custom_tests_dir(self):
        plan = _plan([_route("GET", "/r", "api/r.py", "h")])
        stubs = build_wiring_tests(plan, tests_dir="test")
        self.assertIn("test/wiring/test_wiring_api_r.py", stubs)
        self.assertIn("pytest test/wiring", list(stubs.values())[0])

    def test_non_python_returns_empty(self):
        plan = _plan([_route("GET", "/r", "api/r.js", "h")])
        self.assertEqual(build_wiring_tests(plan, lang="typescript"), {})

    def test_empty_plan_returns_empty(self):
        self.assertEqual(build_wiring_tests({}), {})

    def test_route_without_py_handler_ignored(self):
        plan = _plan([_route("GET", "/r", "api/r.rb", "h")])
        self.assertEqual(build_wiring_tests(plan), {})

    def test_duplicate_tripwire_names_disambiguated(self):
        # Same method+path twice (e.g. two content types) must not collide into
        # one function def (which would be a redefinition, silently dropping one).
        plan = _plan([
            _route("GET", "/r", "api/r.py", "h1"),
            _route("GET", "/r", "api/r.py", "h2"),
        ])
        content = build_wiring_tests(plan)["tests/wiring/test_wiring_api_r.py"]
        self.assertIn("test_wiring_GET_r_unverified", content)
        self.assertIn("test_wiring_GET_r_unverified_x", content)
        compile(content, "x", "exec")


class DeveloperIntegrationTests(unittest.TestCase):
    """develop.py's phase merges stubs in without overwriting scaffolded files."""

    def _dev(self, generated):
        from develop import Developer
        info = {"root": "/x", "name": "demo", "stack": {"backend": "python"},
                "has_tests": "tests", "layers": {}}
        dev = Developer(engine=None, project_info=info, rules={})
        dev.plan = {"api_routes": [
            {"method": "POST", "path": "/api/rides", "handler_file": "api/rides.py",
             "handler_function": "create_ride"}], "layers": []}
        dev.generated = dict(generated)
        return dev

    def test_stub_added_for_scaffolded_route(self):
        dev = self._dev({"api/rides.py": "# handler\n"})
        dev._generate_wiring_tests()
        self.assertIn("tests/wiring/test_wiring_api_rides.py", dev.generated)

    def test_existing_entry_not_overwritten(self):
        dev = self._dev({
            "api/rides.py": "# handler\n",
            "tests/wiring/test_wiring_api_rides.py": "PRE-EXISTING"})
        dev._generate_wiring_tests()
        self.assertEqual(dev.generated["tests/wiring/test_wiring_api_rides.py"],
                         "PRE-EXISTING")

    def test_no_stub_when_route_file_not_generated(self):
        # Route handler wasn't scaffolded this run -> no stub for it.
        dev = self._dev({"other/thing.py": "x = 1\n"})
        dev._generate_wiring_tests()
        self.assertNotIn("tests/wiring/test_wiring_api_rides.py", dev.generated)


class HelperTests(unittest.TestCase):
    def test_module_path(self):
        self.assertEqual(_module_path("a/b/c.py"), "a.b.c")

    def test_flatten(self):
        self.assertEqual(_flatten("backend/api/routes/rides.py"),
                         "backend_api_routes_rides")

    def test_slug_sanitizes_path(self):
        self.assertEqual(_slug("/api/v1/rides/{id}"), "api_v1_rides_id")

    def test_clean_rejects_placeholders(self):
        self.assertIsNone(_clean("null"))
        self.assertIsNone(_clean("None"))
        self.assertIsNone(_clean(""))
        self.assertEqual(_clean("Ride"), "Ride")

    def test_norm_rel_strips_leading(self):
        self.assertEqual(_norm_rel("./api/r.py"), "api/r.py")
        self.assertEqual(_norm_rel("\\api\\r.py"), "api/r.py")
        self.assertIsNone(_norm_rel(""))


if __name__ == "__main__":
    unittest.main()
