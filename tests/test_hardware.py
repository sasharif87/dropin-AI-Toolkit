"""Tests for hardware.py — GPU/VRAM detection and Ollama model helpers.

The broad `except Exception` blocks in detect_gpu / installed_models / pull_model
exist so the toolkit degrades gracefully on a client machine with no GPU tools
or no reachable Ollama. But the same blocks would swallow a genuine bug (a wrong
kwarg, a parse regression) forever. These tests lock the happy path so such a
bug fails loudly, and confirm the fallbacks return the documented safe values.
subprocess and network are stubbed. Stdlib unittest.
"""

import contextlib
import io
import json
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "gates"), os.path.join(_ROOT, "generation")]

import catalog
import hardware


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakePullResp:
    """readline()-driven stand-in for the streaming /api/pull response."""
    def __init__(self, lines):
        self._lines = list(lines)

    def readline(self):
        return self._lines.pop(0) if self._lines else b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ModelSizeTests(unittest.TestCase):
    def setUp(self):
        self._orig = catalog._catalog
        catalog._catalog = catalog.BUILTIN_CATALOG  # ignore any machine-local cache

    def tearDown(self):
        catalog._catalog = self._orig

    def test_exact_match(self):
        self.assertEqual(hardware.model_size_gb("qwen3:32b"), 19.0)

    def test_base_name_match(self):
        # Unknown exact tag, but the base name resolves.
        self.assertEqual(hardware.model_size_gb("qwen3-coder:custom-q4"), 17.0)

    def test_unknown_returns_none(self):
        self.assertIsNone(hardware.model_size_gb("nonexistent-model:1b"))


class RecommendTests(unittest.TestCase):
    def setUp(self):
        self._orig = catalog._catalog
        catalog._catalog = catalog.BUILTIN_CATALOG

    def tearDown(self):
        catalog._catalog = self._orig

    def test_high_budget_picks_first_preference(self):
        prefs = {"code": ["qwen2.5:72b", "llama3.2:3b"]}  # 41 GB, 2 GB
        rec = hardware.recommend_models(100, set(), prefs)
        self.assertEqual(rec["code"]["model"], "qwen2.5:72b")

    def test_low_budget_falls_to_smaller_model(self):
        prefs = {"code": ["qwen2.5:72b", "llama3.2:3b"]}  # 41 GB, 2 GB
        rec = hardware.recommend_models(5, set(), prefs)  # budget 4.5
        self.assertEqual(rec["code"]["model"], "llama3.2:3b")

    def test_unknown_size_model_treated_as_fitting(self):
        prefs = {"code": ["mystery-model:xl"]}  # size None -> chosen
        rec = hardware.recommend_models(5, set(), prefs)
        self.assertEqual(rec["code"]["model"], "mystery-model:xl")

    def test_installed_flag_by_base_name(self):
        prefs = {"code": ["qwen2.5-coder:32b"]}
        rec = hardware.recommend_models(100, {"qwen2.5-coder:7b"}, prefs)
        self.assertTrue(rec["code"]["installed"])


class IsInstalledTests(unittest.TestCase):
    def test_exact(self):
        self.assertTrue(hardware._is_installed("qwen2.5:32b", {"qwen2.5:32b"}))

    def test_base_name(self):
        self.assertTrue(
            hardware._is_installed("qwen2.5-coder:32b", {"qwen2.5-coder:7b"}))

    def test_absent(self):
        self.assertFalse(hardware._is_installed("qwen2.5:32b", {"llama3.2:3b"}))


class InstalledModelsTests(unittest.TestCase):
    def setUp(self):
        self._orig = hardware.urllib.request.urlopen

    def tearDown(self):
        hardware.urllib.request.urlopen = self._orig

    def _stub(self, resp=None, raises=None):
        def fake(*a, **k):
            if raises is not None:
                raise raises
            return resp
        hardware.urllib.request.urlopen = fake

    def test_parses_models(self):
        payload = {"models": [{"name": "qwen3:32b", "size": 19_000_000_000}]}
        self._stub(_FakeResp(json.dumps(payload).encode("utf-8")))
        got = hardware.installed_models("http://host")
        self.assertEqual(got, [{"name": "qwen3:32b", "size_gb": 19.0}])

    def test_network_error_returns_empty(self):
        self._stub(raises=OSError("unreachable"))
        self.assertEqual(hardware.installed_models("http://host"), [])


class DetectGpuTests(unittest.TestCase):
    def setUp(self):
        self._orig = {
            "nvidia": hardware._probe_nvidia,
            "rocm": hardware._probe_rocm,
            "metal": hardware._probe_metal,
            "cpu": hardware._probe_cpu,
        }

    def tearDown(self):
        hardware._probe_nvidia = self._orig["nvidia"]
        hardware._probe_rocm = self._orig["rocm"]
        hardware._probe_metal = self._orig["metal"]
        hardware._probe_cpu = self._orig["cpu"]

    def test_first_successful_probe_wins(self):
        hardware._probe_nvidia = lambda: {
            "source": "nvidia", "gpu_name": "RTX", "vram_gb": 24.0, "gpus": []}
        hardware._probe_rocm = lambda: (_ for _ in ()).throw(AssertionError("unreached"))
        got = hardware.detect_gpu()
        self.assertEqual(got["source"], "nvidia")
        self.assertEqual(got["vram_gb"], 24.0)

    def test_all_tools_missing_returns_unknown_with_none_budget(self):
        missing = lambda: (_ for _ in ()).throw(FileNotFoundError())
        hardware._probe_nvidia = missing
        hardware._probe_rocm = missing
        hardware._probe_metal = missing
        # _probe_cpu must NOT be consulted when tools are merely missing.
        hardware._probe_cpu = lambda: (_ for _ in ()).throw(
            AssertionError("cpu probe should be skipped"))
        got = hardware.detect_gpu()
        self.assertEqual(got["source"], "unknown")
        self.assertIsNone(got["vram_gb"])

    def test_generic_probe_error_falls_through_to_cpu(self):
        boom = lambda: (_ for _ in ()).throw(RuntimeError("weird gpu error"))
        hardware._probe_nvidia = boom
        hardware._probe_rocm = boom
        hardware._probe_metal = boom
        hardware._probe_cpu = lambda: {
            "source": "cpu", "gpu_name": "CPU only", "vram_gb": 9.6, "gpus": []}
        got = hardware.detect_gpu()
        self.assertEqual(got["source"], "cpu")

    def test_never_raises(self):
        boom = lambda: (_ for _ in ()).throw(ValueError("chaos"))
        hardware._probe_nvidia = boom
        hardware._probe_rocm = boom
        hardware._probe_metal = boom
        hardware._probe_cpu = boom
        # Contract: detect_gpu never raises, whatever the probes do.
        got = hardware.detect_gpu()
        self.assertIn("source", got)


class ProbeNvidiaParsingTests(unittest.TestCase):
    def setUp(self):
        self._orig = hardware.subprocess.check_output

    def tearDown(self):
        hardware.subprocess.check_output = self._orig

    def test_single_gpu(self):
        hardware.subprocess.check_output = lambda *a, **k: b"NVIDIA RTX 4090, 24576\n"
        got = hardware._probe_nvidia()
        self.assertEqual(got["source"], "nvidia")
        self.assertAlmostEqual(got["vram_gb"], 24.0)
        self.assertEqual(got["gpu_name"], "NVIDIA RTX 4090")

    def test_multi_gpu_sums_vram(self):
        hardware.subprocess.check_output = (
            lambda *a, **k: b"A100, 40960\nA100, 40960\n")
        got = hardware._probe_nvidia()
        self.assertAlmostEqual(got["vram_gb"], 80.0)
        self.assertIn("2x", got["gpu_name"])

    def test_empty_output_returns_none(self):
        hardware.subprocess.check_output = lambda *a, **k: b"\n"
        self.assertIsNone(hardware._probe_nvidia())


class PullModelTests(unittest.TestCase):
    def setUp(self):
        self._orig = hardware.urllib.request.urlopen

    def tearDown(self):
        hardware.urllib.request.urlopen = self._orig

    @staticmethod
    def _pull(*args):
        # pull_model streams a progress bar to stdout; swallow it in tests.
        with contextlib.redirect_stdout(io.StringIO()):
            return hardware.pull_model(*args)

    def test_success_stream_returns_true(self):
        lines = [
            json.dumps({"status": "pulling", "total": 100, "completed": 50}).encode() + b"\n",
            json.dumps({"status": "success"}).encode() + b"\n",
        ]
        hardware.urllib.request.urlopen = lambda *a, **k: _FakePullResp(lines)
        self.assertTrue(self._pull("m", "http://host"))

    def test_malformed_line_is_skipped_not_fatal(self):
        lines = [b"not json\n",
                 json.dumps({"status": "success"}).encode() + b"\n"]
        hardware.urllib.request.urlopen = lambda *a, **k: _FakePullResp(lines)
        self.assertTrue(self._pull("m", "http://host"))

    def test_network_error_returns_false(self):
        def boom(*a, **k):
            raise OSError("connection reset")
        hardware.urllib.request.urlopen = boom
        self.assertFalse(self._pull("m", "http://host"))


if __name__ == "__main__":
    unittest.main()
