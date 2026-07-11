"""
layers.py — Architectural layer enforcement (config-driven).

Two deterministic checks, generalized from IME's ``scripts/check_layers.py`` so
any repo gets IME-grade layer gating by shipping a small ``.layers.json``
instead of a bespoke script:

  1. **No upward imports.** A designated top layer (e.g. the API/route layer)
     sits at the top of the dependency stack; no module *below* it may import
     it — "dependencies flow downward only". Business logic must never depend on
     the route/handler layer.

  2. **Layer-map completeness.** Every package under the source root must have
     an entry in the layer-rules map (``docs/.layer_rules.json`` by default), so
     a new layer can't be added without declaring its rules.

Deterministic and local — no Ollama; the import graph is parsed with :mod:`ast`.
Python package layout only, matching orphans/wiring/claims (other languages get
no findings rather than a guess). Fail-closed: a configured gate whose source
root or rules map is missing *fails* rather than silently passing; a project
with no ``.layers.json`` opts out (there are no declared layers to enforce).

Config schema (``.layers.json`` at the project root)::

    {
      "source_root": "backend",                 # dir whose packages are layers
      "top_layer": "backend.api",               # dotted module nothing below may import
      "exclude": ["cli", "scripts"],            # child dirs that aren't layers —
                                                #   skipped by BOTH checks (entry
                                                #   points may import the top layer)
      "layer_rules": "docs/.layer_rules.json"   # the package -> rules map (default shown)
    }

``source_root`` is a path relative to the project root; ``top_layer`` is the
dotted module path *as it resolves from the project root* (``backend.api`` for
``backend/api/``). The ``.layers.json`` gate config is separate from the
``.layer_rules.json`` map it references: the map is the (often large,
generated) package→rules text; this config is the small gate metadata.
"""

import ast
import json
import os

CONFIG_NAMES = (".layers.json", "layers.json")
DEFAULT_LAYER_RULES = "docs/.layer_rules.json"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def find_config(root, config_path=None):
    """Return the path to the layers config, or None if there isn't one."""
    if config_path:
        return config_path if os.path.isfile(config_path) else None
    for name in CONFIG_NAMES:
        candidate = os.path.join(root, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def load_config(path):
    """Load and lightly validate the config. Returns (config_dict, error_str)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        return None, f"could not read layers config: {e}"
    if not isinstance(data, dict):
        return None, "layers config is not a JSON object"
    return data, None


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------
def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _module_name(rel):
    """``backend/api/routes.py`` -> ``backend.api.routes`` (rel is posix)."""
    return os.path.splitext(rel)[0].replace("/", ".")


def _imports(source, filename):
    """Return (imported_dotted_name, lineno) for absolute imports in *source*.

    For ``from a.b import c`` both ``a.b`` and ``a.b.c`` are yielded, so that
    ``from backend import api`` (importing the api *package*) is caught, not
    only ``import backend.api``. Relative imports (``level > 0``) are skipped:
    under the package-from-root convention they can't name the top layer from
    below without going through an absolute path.
    """
    tree = ast.parse(source, filename=filename)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:  # absolute imports only
                out.append((node.module, node.lineno))
                for alias in node.names:
                    out.append((f"{node.module}.{alias.name}", node.lineno))
    return out


def _iter_py(src_abs, exclude=()):
    """Yield abs paths of every .py under *src_abs*, deterministically ordered.

    *exclude* prunes direct children of *src_abs* only — the ``.layers.json``
    ``exclude`` semantics (top-level dirs that aren't layers), same scope the
    completeness check applies.
    """
    for dirpath, dirs, files in os.walk(src_abs):
        skip = exclude if dirpath == src_abs else ()
        dirs[:] = sorted(d for d in dirs
                         if not d.startswith(".") and d != "__pycache__" and d not in skip)
        for f in sorted(files):
            if f.endswith(".py"):
                yield os.path.join(dirpath, f)


def _is_top(module, top_layer):
    return module == top_layer or module.startswith(top_layer + ".")


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
def find_upward_import_violations(root, source_root, top_layer, exclude=()):
    """Modules outside *top_layer* that import from it.

    Returns (violations, errors). *errors* holds files that couldn't be parsed —
    surfaced rather than silently skipped, so a broken source file can't hide a
    violation. Dirs in *exclude* are skipped: they aren't layers (CLIs, scripts)
    and as entry points may legitimately import the top layer.
    """
    violations = []
    errors = []
    src_abs = os.path.join(root, source_root.replace("/", os.sep))
    for abs_path in _iter_py(src_abs, exclude):
        rel = os.path.relpath(abs_path, root).replace("\\", "/")
        module = _module_name(rel)
        if _is_top(module, top_layer):
            continue  # the top layer may import itself
        try:
            imports = _imports(_read(abs_path), rel)
        except (OSError, SyntaxError) as e:
            errors.append(f"{rel}: could not parse ({e.__class__.__name__})")
            continue
        reported_lines = set()
        for imported, lineno in imports:
            if lineno in reported_lines:
                continue
            if _is_top(imported, top_layer):
                violations.append(
                    f"{rel}:{lineno}: {module} imports {imported} "
                    f"— lower layers must not import the top layer ({top_layer})")
                reported_lines.add(lineno)
    return violations, errors


def find_missing_layer_entries(root, source_root, exclude, layer_rules_path):
    """Packages under *source_root* with no entry in the layer-rules map.

    Returns (missing, errors). A missing or malformed rules map is an error
    (fail-closed): the completeness check can't run without it.
    """
    rules_abs = os.path.join(root, layer_rules_path.replace("/", os.sep))
    try:
        with open(rules_abs, "r", encoding="utf-8") as fh:
            rules = json.load(fh)
    except FileNotFoundError:
        return [], [f"layer-rules map not found: {layer_rules_path}"]
    except (OSError, json.JSONDecodeError) as e:
        return [], [f"could not read layer-rules map {layer_rules_path}: {e}"]
    if not isinstance(rules, dict):
        return [], [f"layer-rules map {layer_rules_path} is not a JSON object"]

    declared = set(rules.keys())
    missing = []
    src_abs = os.path.join(root, source_root.replace("/", os.sep))
    for child in sorted(os.listdir(src_abs)):
        child_abs = os.path.join(src_abs, child)
        if not os.path.isdir(child_abs) or child in exclude:
            continue
        if not os.path.isfile(os.path.join(child_abs, "__init__.py")):
            continue  # only architectural packages need a rules entry
        key = f"{source_root}/{child}"
        if key not in declared:
            missing.append(
                f"{key} has no entry in {layer_rules_path} (add its layer rules)")
    return missing, []


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def run_layers(root, config_path=None):
    """Run the layer gate under *root*.

    Returns a dict: ``config`` (path or None), ``upward`` / ``missing`` /
    ``errors`` (lists of strings), and ``ok`` (True when the gate passes). A
    project with no config passes with a note (opt-out); a configured gate whose
    source root or rules map is missing fails — as does an explicitly passed
    *config_path* that doesn't exist (only auto-discovery finding nothing is an
    opt-out; a typo'd path must never go silently green).
    """
    root = os.path.abspath(root)
    if config_path and not os.path.isfile(config_path):
        return {"config": config_path, "upward": [], "missing": [],
                "errors": [f"layers config not found: {config_path}"], "ok": False}
    path = find_config(root, config_path)
    result = {"config": path, "upward": [], "missing": [], "errors": [], "ok": True}
    if not path:
        result["note"] = "no layers config (.layers.json) — nothing to check"
        return result

    cfg, err = load_config(path)
    if err:
        result["ok"] = False
        result["errors"].append(err)
        return result

    source_root = cfg.get("source_root")
    top_layer = cfg.get("top_layer")
    exclude = set(cfg.get("exclude") or [])
    layer_rules = cfg.get("layer_rules") or DEFAULT_LAYER_RULES

    if not isinstance(source_root, str) or not source_root:
        result["ok"] = False
        result["errors"].append("config missing required 'source_root'")
        return result
    if not isinstance(top_layer, str) or not top_layer:
        result["ok"] = False
        result["errors"].append("config missing required 'top_layer'")
        return result

    src_abs = os.path.join(root, source_root.replace("/", os.sep))
    if not os.path.isdir(src_abs):
        result["ok"] = False
        result["errors"].append(f"source_root not found: {source_root}")
        return result

    result["source_root"] = source_root
    result["top_layer"] = top_layer
    result["layer_rules"] = layer_rules

    upward, up_err = find_upward_import_violations(root, source_root, top_layer, exclude)
    missing, miss_err = find_missing_layer_entries(root, source_root, exclude, layer_rules)
    result["upward"] = upward
    result["missing"] = missing
    result["errors"].extend(up_err)
    result["errors"].extend(miss_err)
    result["ok"] = not (upward or missing or result["errors"])
    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def print_layers(result):
    """Pretty-print the layer run for `drop.py layers`."""
    if not result.get("config"):
        print(f"\n  Layers: {result.get('note', 'no config')}")
        return

    print(f"\n  Layers — {os.path.basename(result['config'])}")
    if result.get("source_root"):
        print(f"    source root: {result['source_root']}    "
              f"top layer: {result.get('top_layer')}")

    upward = result.get("upward") or []
    missing = result.get("missing") or []
    errors = result.get("errors") or []

    if upward:
        print(f"\n    Upward-import violations ({len(upward)}) "
              f"— lower layer importing the top layer:")
        for v in upward:
            print(f"      - {v}")
    if missing:
        print(f"\n    Layer-map completeness ({len(missing)}):")
        for v in missing:
            print(f"      - {v}")
    if errors:
        print(f"\n    Errors ({len(errors)}):")
        for e in errors:
            print(f"      - {e}")

    if result["ok"]:
        print("\n    Layer gate: PASS — no upward imports, layer map complete")
    else:
        print(f"\n    Layer gate: FAIL "
              f"({len(upward)} upward, {len(missing)} missing, {len(errors)} error(s))")
