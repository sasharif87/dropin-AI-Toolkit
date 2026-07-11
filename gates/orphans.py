"""
orphans.py — Zero-caller / orphan module report.

Scaffold-residue detection: source modules that nothing imports and that expose
no entry point (no ``if __name__ == "__main__"`` guard, not a recognized
runner). These are the duplicate implementations and dead scaffolds that
survive because no gate looks for them at generation time — the Personal
Training App audit found five, including a duplicate with a divergent DB schema
(``season_planner`` vs ``event_extractor``). This failure class is *generated*
at scaffold time, so it is cheapest to catch here, in every future project.

Deterministic and local — no Ollama. Consumes only the project's file tree.
Python only for now: the import graph is parsed with :mod:`ast`. Other
languages return no findings rather than guessing (a noisy report gets ignored,
so the bias is toward false negatives — never flag something that is used).
"""

import ast
import os

from detect import SKIP_DIRS

# Files that are entry points by convention: run directly, imported by a
# framework, or collected by a test runner — never orphans even with no caller.
ENTRYPOINT_BASENAMES = {
    "__init__.py", "__main__.py", "conftest.py", "setup.py",
    "manage.py", "wsgi.py", "asgi.py", "app.py", "main.py",
    "gunicorn.conf.py", "celeryconfig.py",
}

# Directory names whose contents are entry points / not part of the import
# graph in the usual sense.
ENTRYPOINT_DIRS = {"migrations", "versions", "alembic"}


def _is_test_file(basename):
    return (
        basename.startswith("test_")
        or basename.endswith(("_test.py", ".test.py", "_tests.py"))
    )


def _iter_python_files(root):
    """Yield (abs_path, rel_path) for every .py file, skipping vendored dirs."""
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if f.endswith(".py"):
                abs_path = os.path.join(dirpath, f)
                rel = os.path.relpath(abs_path, root).replace("\\", "/")
                yield abs_path, rel


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            return fh.read()
    except OSError:
        return ""


def _collect_references(source):
    """Return the set of dotted names imported by *source*.

    ``import a.b`` → {"a.b"}. ``from a.b import c`` → {"a.b", "a.b.c"}.
    ``from . import c`` → {"c"}. Relative package prefixes are dropped (level is
    not resolved against a package root), which only makes matching looser —
    the safe direction for a report that must not flag used modules.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    refs = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                refs.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                refs.add(node.module)
                for alias in node.names:
                    refs.add(f"{node.module}.{alias.name}")
            else:
                # `from . import x` / `from .. import y`
                for alias in node.names:
                    refs.add(alias.name)
    return refs


def _has_main_guard(source):
    """True if the module has an ``if __name__ == "__main__":`` guard."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return '__name__' in source and '__main__' in source
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not isinstance(test, ast.Compare):
            continue
        names = {getattr(n, "id", None) for n in ast.walk(test)}
        consts = {n.value for n in ast.walk(test) if isinstance(n, ast.Constant)}
        if "__name__" in names and "__main__" in consts:
            return True
    return False


def _dotted_suffixes(rel_path):
    """Every dotted module path a file could be imported as.

    ``a/b/c.py`` → {"a.b.c", "b.c", "c"} so a match works regardless of which
    directory the importing code treats as its package root.
    """
    dotted = os.path.splitext(rel_path)[0].replace("/", ".")
    parts = dotted.split(".")
    return {".".join(parts[i:]) for i in range(len(parts))}


def find_orphans(root):
    """Return a list of orphan findings for the Python modules under *root*.

    Each finding is ``{"file": rel_path, "reason": str}``. A module is an
    orphan when no other file (including tests) imports it *and* it is not an
    entry point (no ``__main__`` guard, not a conventional runner). Returns an
    empty list for non-Python projects.
    """
    files = list(_iter_python_files(root))
    if not files:
        return []

    # Pass 1 — gather every dotted name referenced anywhere, tests included.
    # Tests count as importers: a module used only by its own test is still
    # "used", and flagging it would be a false positive.
    referenced_dotted = set()
    sources = {}
    for abs_path, rel in files:
        src = _read(abs_path)
        sources[rel] = src
        referenced_dotted |= _collect_references(src)
    referenced_leaves = {name.rsplit(".", 1)[-1] for name in referenced_dotted}

    # Pass 2 — flag candidate modules with no reference and no entry point.
    orphans = []
    for _abs_path, rel in files:
        basename = os.path.basename(rel)
        parts = rel.split("/")
        if basename in ENTRYPOINT_BASENAMES:
            continue
        if _is_test_file(basename):
            continue
        if any(p in ENTRYPOINT_DIRS for p in parts[:-1]):
            continue

        src = sources[rel]
        if _has_main_guard(src):
            continue

        candidates = _dotted_suffixes(rel)
        stem = os.path.splitext(basename)[0]
        imported = (
            bool(candidates & referenced_dotted)
            or stem in referenced_leaves
        )
        if not imported:
            orphans.append({
                "file": rel,
                "reason": "no importers, no __main__ guard",
            })
    return orphans


def print_orphans(orphans):
    """Pretty-print the orphan report as part of `drop.py detect` output."""
    if not orphans:
        print("\n  Orphan modules: none")
        return
    print(f"\n  Orphan modules ({len(orphans)}) — nothing imports these, no entry point:")
    for o in orphans:
        print(f"    {o['file']:<40} {o['reason']}")
    print("    (scaffold residue — delete, wire up, or confirm each is intentional)")
