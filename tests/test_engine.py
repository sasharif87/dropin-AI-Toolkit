"""Tests for engine.py's pure utilities.

These parse untrusted model output and guard file writes, so a silent
regression here corrupts every downstream consumer. Only the stdlib-only,
network-free helpers are exercised (importing engine touches no host; the
Engine class reaches Ollama on instantiation, not import). Stdlib unittest.
"""

import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "gates"), os.path.join(_ROOT, "generation")]

import engine


class ExtractJsonTests(unittest.TestCase):
    def test_clean_object(self):
        self.assertEqual(engine.extract_json('{"a": 1}'), {"a": 1})

    def test_clean_array(self):
        self.assertEqual(engine.extract_json('[1, 2, 3]'), [1, 2, 3])

    def test_fenced_json(self):
        self.assertEqual(engine.extract_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_object_embedded_in_prose(self):
        text = 'Here is the result:\n{"file": "x.py", "line": 3}\nHope that helps.'
        self.assertEqual(engine.extract_json(text), {"file": "x.py", "line": 3})

    def test_nested_object_not_truncated(self):
        text = 'noise {"outer": {"inner": [1, 2]}} trailing'
        self.assertEqual(engine.extract_json(text), {"outer": {"inner": [1, 2]}})

    def test_unparseable_returns_none(self):
        self.assertIsNone(engine.extract_json("no json here at all"))

    def test_empty_returns_none(self):
        self.assertIsNone(engine.extract_json(""))


class StripFencesTests(unittest.TestCase):
    def test_removes_language_fence(self):
        self.assertEqual(engine.strip_fences("```python\ncode()\n```"), "code()")

    def test_removes_bare_fence(self):
        self.assertEqual(engine.strip_fences("```\nx\n```"), "x")

    def test_plain_text_unchanged(self):
        self.assertEqual(engine.strip_fences("just text"), "just text")


class SafeAbsPathTests(unittest.TestCase):
    def setUp(self):
        self.root = os.path.normpath("/project")

    def test_normal_relative_path_allowed(self):
        got = engine.safe_abs_path(self.root, "src/app.py")
        self.assertEqual(got, os.path.normpath("/project/src/app.py"))

    def test_parent_traversal_rejected(self):
        self.assertIsNone(engine.safe_abs_path(self.root, "../secret.txt"))

    def test_deep_traversal_rejected(self):
        self.assertIsNone(engine.safe_abs_path(self.root, "src/../../etc/passwd"))

    def test_leading_slash_is_stripped_not_escaped(self):
        # A leading slash is treated as project-relative, not absolute.
        got = engine.safe_abs_path(self.root, "/app.py")
        self.assertEqual(got, os.path.normpath("/project/app.py"))

    def test_root_itself_allowed(self):
        self.assertEqual(engine.safe_abs_path(self.root, ""), self.root)


class ChunkTextTests(unittest.TestCase):
    def test_short_text_single_chunk(self):
        self.assertEqual(engine.chunk_text("short", 100), ["short"])

    def test_long_text_splits_and_respects_max(self):
        text = "\n".join(f"line {i}" for i in range(500))
        chunks = engine.chunk_text(text, 200, overlap=20)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), 200)

    def test_chunks_cover_all_content(self):
        text = "".join(f"{i}\n" for i in range(300))
        chunks = engine.chunk_text(text, 150, overlap=20)
        # Every original line appears in at least one chunk.
        joined = "".join(chunks)
        for i in range(300):
            self.assertIn(f"{i}\n", joined)

    def test_always_makes_progress_on_unbroken_text(self):
        text = "x" * 1000  # no newlines to break on
        chunks = engine.chunk_text(text, 100, overlap=20)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(len(text), len("x") * 1000)


class FmtTimeTests(unittest.TestCase):
    def test_seconds_only(self):
        self.assertEqual(engine.fmt_time(5), "5s")

    def test_minutes_and_seconds(self):
        self.assertEqual(engine.fmt_time(65), "1m 5s")

    def test_hours_minutes_seconds(self):
        self.assertEqual(engine.fmt_time(3661), "1h 1m 1s")


if __name__ == "__main__":
    unittest.main()
