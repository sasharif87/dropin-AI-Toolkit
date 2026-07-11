"""Tests for triggers.py — local enforcement (git hooks + CI snippet).

Stdlib unittest only, hermetic temp repos (a synthetic ``.git/hooks`` tree — no
real git needed). Mirrors the other gate tests' style.
"""

import os
import stat
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "gates"), os.path.join(_ROOT, "generation")]

from triggers import (
    render_precommit,
    render_ci,
    install_precommit,
    install_ci,
    run_hooks,
    _hooks_dir,
    MARKER,
    GATES,
)


def _write(root, rel, content=""):
    path = os.path.join(root, rel.replace("/", os.sep))
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)


class RenderTests(unittest.TestCase):
    def test_precommit_bakes_in_paths_and_gates(self):
        body = render_precommit("/usr/bin/python3", "/opt/toolkit/drop.py")
        self.assertTrue(body.startswith("#!/bin/sh"))
        self.assertIn("/usr/bin/python3", body)
        self.assertIn("/opt/toolkit/drop.py", body)
        for gate, _cfgs in GATES:
            self.assertIn(f"run_gate {gate}", body)
        self.assertIn("DROPIN_SKIP", body)
        self.assertIn(MARKER, body)

    def test_precommit_leaves_no_unsubstituted_placeholders(self):
        body = render_precommit("py", "drop")
        self.assertNotIn("{python}", body)
        self.assertNotIn("{drop}", body)
        self.assertNotIn("{marker}", body)
        # The shell ${...} / { } must survive untouched.
        self.assertIn("${DROPIN_SKIP:-0}", body)
        self.assertIn("run_gate() {", body)

    def test_ci_lists_the_three_gates(self):
        ci = render_ci()
        self.assertIn("name: dropin-gates", ci)
        for gate in ("layers", "invariants", "golden"):
            self.assertIn(f"drop.py\" {gate}", ci)


class InstallPrecommitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        os.makedirs(os.path.join(self.root, ".git", "hooks"))

    def tearDown(self):
        self._tmp.cleanup()

    def _read_hook(self):
        with open(os.path.join(self.root, ".git", "hooks", "pre-commit"),
                  "r", encoding="utf-8", newline="") as fh:
            return fh.read()

    def test_installs_executable_lf_hook(self):
        r = install_precommit(self.root, "python3", "/t/drop.py")
        self.assertTrue(r["ok"], r)
        body = self._read_hook()
        self.assertTrue(body.startswith("#!/bin/sh"))
        self.assertIn(MARKER, body)
        self.assertNotIn("\r", body)  # LF only — CRLF breaks the shebang
        if os.name != "nt":
            mode = os.stat(r["path"]).st_mode
            self.assertTrue(mode & stat.S_IXUSR, "hook should be executable")

    def test_idempotent_refresh_no_backup(self):
        install_precommit(self.root, "python3", "/t/drop.py")
        r2 = install_precommit(self.root, "python3", "/t/drop.py")  # ours already
        self.assertTrue(r2["ok"])
        self.assertIsNone(r2["backed_up"])
        # exactly one backup-free hook exists
        self.assertFalse(os.path.exists(r2["path"] + ".dropin-backup"))

    def test_foreign_hook_refused_without_force(self):
        _write(self.root, ".git/hooks/pre-commit", "#!/bin/sh\necho custom\n")
        r = install_precommit(self.root, "python3", "/t/drop.py")
        self.assertFalse(r["ok"])
        self.assertIn("force", r["error"])
        # the foreign hook is untouched
        self.assertIn("echo custom", self._read_hook())

    def test_foreign_hook_backed_up_with_force(self):
        _write(self.root, ".git/hooks/pre-commit", "#!/bin/sh\necho custom\n")
        r = install_precommit(self.root, "python3", "/t/drop.py", force=True)
        self.assertTrue(r["ok"])
        self.assertTrue(r["backed_up"])
        with open(r["backed_up"], "r", encoding="utf-8") as fh:
            self.assertIn("echo custom", fh.read())
        self.assertIn(MARKER, self._read_hook())

    def test_not_a_git_repo_fails(self):
        with tempfile.TemporaryDirectory() as plain:
            r = install_precommit(plain, "python3", "/t/drop.py")
            self.assertFalse(r["ok"])
            self.assertIn("not a git repository", r["error"])

    def test_worktree_gitdir_pointer_resolved(self):
        # .git as a "gitdir:" pointer file (worktree/submodule layout).
        with tempfile.TemporaryDirectory() as base:
            realgit = os.path.join(base, "realgit")
            os.makedirs(os.path.join(realgit, "hooks"))
            wt = os.path.join(base, "wt")
            os.makedirs(wt)
            _write(wt, ".git", f"gitdir: {realgit}\n")
            r = install_precommit(wt, "python3", "/t/drop.py")
            self.assertTrue(r["ok"], r)
            self.assertEqual(_hooks_dir(wt), os.path.join(realgit, "hooks"))
            self.assertTrue(os.path.exists(os.path.join(realgit, "hooks", "pre-commit")))


class InstallCiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_writes_workflow(self):
        r = install_ci(self.root)
        self.assertTrue(r["ok"])
        self.assertTrue(os.path.exists(
            os.path.join(self.root, ".github", "workflows", "dropin-gates.yml")))

    def test_existing_workflow_needs_force(self):
        install_ci(self.root)
        r2 = install_ci(self.root)
        self.assertFalse(r2["ok"])
        self.assertIn("force", r2["error"])
        r3 = install_ci(self.root, force=True)
        self.assertTrue(r3["ok"])


class RunHooksTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        os.makedirs(os.path.join(self.root, ".git", "hooks"))

    def tearDown(self):
        self._tmp.cleanup()

    def test_hook_only_by_default(self):
        r = run_hooks(self.root, "python3", "/t/drop.py")
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["results"]), 1)

    def test_ci_flag_adds_workflow(self):
        r = run_hooks(self.root, "python3", "/t/drop.py", ci=True)
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["results"]), 2)
        self.assertTrue(os.path.exists(
            os.path.join(self.root, ".github", "workflows", "dropin-gates.yml")))

    def test_aggregate_ok_false_when_a_step_fails(self):
        # A foreign hook makes the pre-commit step fail without force -> ok False.
        _write(self.root, ".git/hooks/pre-commit", "#!/bin/sh\necho custom\n")
        r = run_hooks(self.root, "python3", "/t/drop.py")
        self.assertFalse(r["ok"])


if __name__ == "__main__":
    unittest.main()
