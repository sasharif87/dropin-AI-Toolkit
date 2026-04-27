"""
detect.py — Auto-detect project structure, stack, and layers.

Drop this into any project and it figures out:
  - Language/framework (Python+FastAPI, Node+Express, etc.)
  - Directory layout → layer classification
  - Existing patterns (models, routes, tests, config)

No config needed. Returns a structured dict that other scripts consume.
"""

import os
import re
from collections import OrderedDict

SKIP_DIRS = {
    "node_modules", ".venv", "venv", "env", ".next", "dist", "build",
    "__pycache__", ".git", ".github", ".agents", "docs", "__snapshots__",
    ".mypy_cache", ".pytest_cache", ".tox", "htmlcov", "coverage",
    ".idea", ".vscode", "eggs", "*.egg-info",
}

CODE_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs"}
CONFIG_FILES = {
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile",
    "package.json", "tsconfig.json", "pnpm-lock.yaml", "yarn.lock",
    "go.mod", "Cargo.toml",
    "docker-compose.yml", "docker-compose.yaml", "Dockerfile",
    "alembic.ini", "prisma/schema.prisma",
    ".env", ".env.example", ".env.template",
    "pytest.ini", "jest.config.js", "vitest.config.ts",
    "nginx.conf", "Makefile",
}


def detect(root_dir):
    """Auto-detect everything about a project. Returns a structured dict."""
    root = os.path.abspath(root_dir)
    result = {
        "root": root,
        "name": os.path.basename(root),
        "stack": _detect_stack(root),
        "layers": _detect_layers(root),
        "config_files": _find_config_files(root),
        "arch_doc": _find_arch_doc(root),
        "has_tests": _has_tests(root),
        "file_count": 0,
    }

    # Count source files
    for layer in result["layers"].values():
        result["file_count"] += layer.get("file_count", 0)

    return result


# ---------------------------------------------------------------------------
# Stack detection
# ---------------------------------------------------------------------------
def _detect_stack(root):
    stack = {
        "backend": None,
        "frontend": None,
        "database": None,
        "orm": None,
        "api": None,
        "test_framework": None,
    }

    files_at_root = set(os.listdir(root))

    # ── Backend language ──
    if any(f in files_at_root for f in ("pyproject.toml", "setup.py", "requirements.txt", "Pipfile")):
        stack["backend"] = "python"
        # Check for specific frameworks
        for marker_file in ("pyproject.toml", "requirements.txt", "setup.py"):
            content = _read_if_exists(os.path.join(root, marker_file))
            if content:
                if "fastapi" in content.lower():
                    stack["api"] = "fastapi"
                elif "flask" in content.lower():
                    stack["api"] = "flask"
                elif "django" in content.lower():
                    stack["api"] = "django"
                if "sqlalchemy" in content.lower():
                    stack["orm"] = "sqlalchemy"
                elif "tortoise" in content.lower():
                    stack["orm"] = "tortoise"
                if "pytest" in content.lower():
                    stack["test_framework"] = "pytest"
                if "psycopg" in content.lower() or "asyncpg" in content.lower():
                    stack["database"] = "postgres"
                elif "sqlite" in content.lower():
                    stack["database"] = "sqlite"

    elif "package.json" in files_at_root:
        pkg = _read_if_exists(os.path.join(root, "package.json"))
        if pkg:
            if "express" in pkg:
                stack["backend"] = "node"
                stack["api"] = "express"
            elif "hono" in pkg:
                stack["backend"] = "node"
                stack["api"] = "hono"
            elif "fastify" in pkg:
                stack["backend"] = "node"
                stack["api"] = "fastify"
            if "prisma" in pkg:
                stack["orm"] = "prisma"
            elif "drizzle" in pkg:
                stack["orm"] = "drizzle"
            elif "typeorm" in pkg:
                stack["orm"] = "typeorm"
            if "jest" in pkg:
                stack["test_framework"] = "jest"
            elif "vitest" in pkg:
                stack["test_framework"] = "vitest"
            if "react" in pkg:
                stack["frontend"] = "react"
            elif "vue" in pkg:
                stack["frontend"] = "vue"
            elif "svelte" in pkg:
                stack["frontend"] = "svelte"

    elif "go.mod" in files_at_root:
        stack["backend"] = "go"
    elif "Cargo.toml" in files_at_root:
        stack["backend"] = "rust"

    # ── Frontend (check subdirectories too) ──
    if not stack["frontend"]:
        for candidate in ("frontend", "client", "web", "ui"):
            pkg_path = os.path.join(root, candidate, "package.json")
            if os.path.isfile(pkg_path):
                content = _read_if_exists(pkg_path)
                if content:
                    if "react" in content: stack["frontend"] = "react"
                    elif "vue" in content: stack["frontend"] = "vue"
                    elif "svelte" in content: stack["frontend"] = "svelte"
                    if "jest" in content and not stack["test_framework"]:
                        stack["test_framework"] = "jest"
                    elif "vitest" in content and not stack["test_framework"]:
                        stack["test_framework"] = "vitest"

    # ── Database from docker-compose ──
    if not stack["database"]:
        for dc in ("docker-compose.yml", "docker-compose.yaml"):
            content = _read_if_exists(os.path.join(root, dc))
            if content:
                if "postgres" in content.lower():
                    stack["database"] = "postgres"
                elif "mysql" in content.lower() or "mariadb" in content.lower():
                    stack["database"] = "mysql"
                elif "mongo" in content.lower():
                    stack["database"] = "mongo"

    # ── Defaults ──
    if not stack["test_framework"]:
        if stack["backend"] == "python":
            stack["test_framework"] = "pytest"
        elif stack["backend"] == "node":
            stack["test_framework"] = "jest"

    return stack


# ---------------------------------------------------------------------------
# Layer detection
# ---------------------------------------------------------------------------
def _detect_layers(root):
    """Detect layers from directory structure. Returns OrderedDict."""
    layers = OrderedDict()

    for item in sorted(os.listdir(root)):
        full = os.path.join(root, item)
        if not os.path.isdir(full):
            continue
        if item in SKIP_DIRS or item.startswith("."):
            continue

        # Skip test dirs — they're consumers, not layers
        if item in ("tests", "test", "__tests__", "spec"):
            continue

        # Skip bootstrap tooling dirs — any dir (or subdir) containing drop.py
        has_drop = os.path.isfile(os.path.join(full, "drop.py")) or any(
            os.path.isfile(os.path.join(full, s, "drop.py"))
            for s in os.listdir(full)
            if os.path.isdir(os.path.join(full, s))
        )
        if has_drop:
            continue

        # Check if this is a container dir (backend/, frontend/) or a leaf layer
        subdirs = [s for s in os.listdir(full)
                    if os.path.isdir(os.path.join(full, s))
                    and s not in SKIP_DIRS and not s.startswith(".")]
        has_source = any(
            f.endswith(tuple(CODE_EXTS))
            for f in os.listdir(full)
            if os.path.isfile(os.path.join(full, f))
        )

        if subdirs and len(subdirs) >= 2:
            # Container dir — create sub-layers
            for sub in sorted(subdirs):
                sub_full = os.path.join(full, sub)
                key = f"{item}/{sub}"
                layer = _build_layer_info(root, key, sub_full)
                if layer["file_count"] > 0:
                    layers[key] = layer

            # Also capture loose files at the container level
            if has_source:
                layer = _build_layer_info(root, item, full, recurse=False)
                if layer["file_count"] > 0:
                    layers[item] = layer
        else:
            # Leaf dir — treat as a single layer
            layer = _build_layer_info(root, item, full)
            if layer["file_count"] > 0:
                layers[item] = layer

    return layers


def _build_layer_info(root, prefix, dir_path, recurse=True):
    """Build layer info dict by scanning a directory."""
    info = {
        "prefix": prefix,
        "name": _humanize(prefix),
        "dir": dir_path,
        "file_count": 0,
        "files": [],
        "patterns": [],  # detected patterns (has_models, has_routes, etc.)
        "subdirs": [],
    }

    if recurse:
        for dirpath, dirs, files in os.walk(dir_path):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            rel_dir = os.path.relpath(dirpath, os.path.dirname(dir_path)).replace("\\", "/")
            if rel_dir != prefix and rel_dir.startswith(prefix):
                sub = rel_dir[len(prefix):].strip("/")
                if sub and "/" not in sub:
                    info["subdirs"].append(sub)

            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext in CODE_EXTS:
                    info["file_count"] += 1
                    rel = os.path.relpath(os.path.join(dirpath, f), root).replace("\\", "/")
                    info["files"].append(rel)
    else:
        for f in os.listdir(dir_path):
            if os.path.isfile(os.path.join(dir_path, f)):
                ext = os.path.splitext(f)[1].lower()
                if ext in CODE_EXTS:
                    info["file_count"] += 1
                    rel = os.path.relpath(os.path.join(dir_path, f), root).replace("\\", "/")
                    info["files"].append(rel)

    # Detect patterns from file names
    file_names = {os.path.basename(f).lower() for f in info["files"]}
    dir_names = {s.lower() for s in info["subdirs"]}

    if any("model" in f for f in file_names) or "models" in dir_names:
        info["patterns"].append("has_models")
    if any("route" in f for f in file_names) or "routes" in dir_names:
        info["patterns"].append("has_routes")
    if any("schema" in f for f in file_names) or "schemas" in dir_names:
        info["patterns"].append("has_schemas")
    if any("test" in f for f in file_names):
        info["patterns"].append("has_tests")
    if any("migrat" in f for f in file_names) or "migrations" in dir_names:
        info["patterns"].append("has_migrations")
    if any("middle" in f for f in file_names) or "middleware" in dir_names:
        info["patterns"].append("has_middleware")
    if any("service" in f for f in file_names) or "services" in dir_names:
        info["patterns"].append("has_services")
    if any("base" in f or "abstract" in f or "interface" in f for f in file_names):
        info["patterns"].append("has_abstractions")
    if "config" in " ".join(file_names) or ".env" in " ".join(file_names):
        info["patterns"].append("has_config")

    return info


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _find_arch_doc(root):
    """Find the architecture document."""
    candidates = [
        "docs/ARCHITECTURE.md", "docs/architecture.md", "ARCHITECTURE.md",
        "docs/DESIGN.md", "docs/design.md", "docs/README.md",
        "docs/SPEC.md", "docs/spec.md",
    ]
    for c in candidates:
        path = os.path.join(root, c)
        if os.path.isfile(path):
            return c
    return None


def _find_config_files(root):
    """Find known config files at root level."""
    found = []
    for f in CONFIG_FILES:
        if os.path.isfile(os.path.join(root, f)):
            found.append(f)
    return found


def _has_tests(root):
    """Check if tests directory exists."""
    for d in ("tests", "test", "__tests__", "spec"):
        if os.path.isdir(os.path.join(root, d)):
            return d
    return None


def _read_if_exists(path, max_size=50_000):
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read(max_size)
    except Exception:
        return None


def _humanize(prefix):
    """Convert path prefix to human-readable layer name."""
    parts = prefix.replace("/", " / ").replace("_", " ").title()
    return parts


def print_detection(info):
    """Pretty-print the detection results."""
    print(f"\n  Project: {info['name']}")
    print(f"  Root:    {info['root']}")

    stack = info["stack"]
    parts = [f for f in [
        stack.get("backend"), stack.get("api"), stack.get("frontend"),
        stack.get("database"), stack.get("orm"),
    ] if f]
    print(f"  Stack:   {' + '.join(parts) if parts else '(could not detect)'}")
    print(f"  Tests:   {stack.get('test_framework', '?')} ({'found' if info['has_tests'] else 'none yet'})")
    print(f"  Arch doc: {info['arch_doc'] or '(none found)'}")
    print(f"  Files:   {info['file_count']} source files across {len(info['layers'])} layers")

    print(f"\n  Layers:")
    for key, layer in info["layers"].items():
        patterns = ", ".join(layer["patterns"]) if layer["patterns"] else "-"
        print(f"    {key:<30} {layer['file_count']:>3} files  [{patterns}]")
