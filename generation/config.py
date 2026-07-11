"""
config.py — Manages .dropin.json configuration files.

Search order: ./.dropin.json (current dir) then ~/.dropin.json
No imports from engine, hardware, catalog, or drop — config is a base layer.

Host precedence: CLI flag > .dropin.json > OLLAMA_HOST (env or the toolkit
root's .env) > localhost. Machine-specific addresses (LAN IPs) belong in the
gitignored .env — see .env.example — never in committed files.
"""

import json
import os

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

def _load_dotenv():
    """Fill os.environ from the toolkit root's .env (real env vars win).

    Only reads the toolkit's own directory, not the cwd, so a checked-out
    repo can't inject a host.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(root, ".env"), "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()

DEFAULT_CONFIG = {
    "version": 1,
    "url": os.environ.get("OLLAMA_HOST") or "http://localhost:11434",
    "code_url": None,
    "models": {},          # role -> model name, or empty
    "role_ctx_caps": {},   # role -> int, overrides engine.ROLE_CTX_CAPS
    "catalog_url": "",     # if set, used by catalog.fetch()
}

_SEARCH_PATHS = [
    os.path.join(os.getcwd(), ".dropin.json"),
    os.path.expanduser("~/.dropin.json"),
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _deep_merge(base, override):
    """Recursively merge *override* into a copy of *base*. Returns new dict."""
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def _find_config_file():
    """Return path of the first .dropin.json that exists, or None."""
    for path in _SEARCH_PATHS:
        if os.path.isfile(path):
            return path
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load():
    """Find and load the first .dropin.json that exists.

    Deep-merges with DEFAULT_CONFIG so callers always get all keys.
    Returns a config dict.
    """
    path = _find_config_file()
    if path is None:
        return dict(DEFAULT_CONFIG)

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return _deep_merge(DEFAULT_CONFIG, data)
    except Exception:
        return dict(DEFAULT_CONFIG)


def save(cfg, path=None):
    """Save *cfg* to *path*, defaulting to ~/.dropin.json."""
    if path is None:
        path = os.path.expanduser("~/.dropin.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return path


def config_path():
    """Return the path of the first .dropin.json that exists, or None."""
    return _find_config_file()


def engine_kwargs(cfg):
    """Extract url, code_url, and models from cfg as a dict for Engine()."""
    return {
        "url": cfg.get("url", DEFAULT_CONFIG["url"]),
        "code_url": cfg.get("code_url"),
        "models": cfg.get("models", {}),
    }


def effective_code_url(cfg):
    """Return the code model URL: explicit value, or primary url when code_url is null."""
    return cfg.get("code_url") or cfg.get("url", DEFAULT_CONFIG["url"])
