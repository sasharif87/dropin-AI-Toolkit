"""
catalog.py — Central model catalog: preferences, context windows, VRAM sizes.

This is the base layer — it has no imports from engine, hardware, config, or drop.
The catalog can be loaded from a local cache file (updated via fetch()) or falls
back to the BUILTIN_CATALOG compiled into this module.
"""

import json
import os
import urllib.request

# ---------------------------------------------------------------------------
# Built-in catalog (bundled with the package)
# ---------------------------------------------------------------------------

BUILTIN_CATALOG = {
    "version": "2026-05-22",
    "preferences": {
        "reason": [
            # Primary — largest available models for best consolidation quality
            "qwen2.5:72b", "llama3.3:70b",
            "deepseek-r1:32b", "qwen2.5:32b", "qwen3:32b",
            "qwen2.5-coder:32b",
            # Fallback — mid-range
            "deepseek-r1:14b", "qwen2.5:14b", "qwen3:14b",
            "mistral-small:latest", "gemma2:9b",
            "deepseek-coder-v2:16b",
        ],
        "code": [
            # Primary — qwen3-coder preferred (newer arch, ~30B); 2.5-coder:32b fallback
            "qwen3-coder", "qwen2.5-coder:32b",
            "qwen2.5:72b",
            # Mid-range fallback
            "qwen2.5-coder:14b", "qwen2.5-coder:7b",
            "deepseek-coder-v2:16b",
            "codellama:34b", "codellama:13b",
            "llama3.1:8b",
        ],
        "quick": [
            # Fast + code-aware
            "qwen2.5-coder:7b",
            "qwen2.5-coder:14b",
            "qwen2.5:14b", "qwen2.5:7b",
            "gemma2:9b", "llama3.2:3b",
            "phi3:mini",
            "deepseek-coder-v2:16b",
            "mistral:7b-instruct",
        ],
    },
    "ctx_windows": {
        "qwen3-coder":          131_072,
        "qwen3:32b":            131_072,
        "qwen3:14b":             40_960,
        "qwen3:8b":              32_768,
        "qwen2.5:72b":          131_072,
        "qwen2.5:32b":          131_072,
        "qwen2.5:14b":          131_072,
        "qwen2.5:7b":            32_768,
        "qwen2.5-coder:32b":    131_072,
        "qwen2.5-coder:14b":    131_072,
        "qwen2.5-coder:7b":      32_768,
        "llama3.3:70b":         131_072,
        "llama3.1:8b":          131_072,
        "llama3.2:3b":          131_072,
        "deepseek-r1:32b":      131_072,
        "deepseek-r1:14b":       65_536,
        "deepseek-coder-v2:16b": 65_536,
        "mistral-small":         32_768,
        "mistral":               32_768,
        "gemma2:9b":              8_192,
        "gemma2:27b":             8_192,
        "codellama:34b":         16_384,
        "codellama:13b":         16_384,
        "phi3:mini":            131_072,
        "phi3:medium":          131_072,
    },
    "sizes": {
        # qwen3 family
        "qwen3-coder":           17.0,   # MoE ~235B total, ~22B active
        "qwen3:32b":             19.0,
        "qwen3:14b":              8.5,
        "qwen3:8b":               5.0,
        # qwen2.5 family
        "qwen2.5:72b":           41.0,
        "qwen2.5:32b":           19.0,
        "qwen2.5:14b":            8.5,
        "qwen2.5:7b":             4.5,
        "qwen2.5-coder:32b":     19.0,
        "qwen2.5-coder:14b":      8.5,
        "qwen2.5-coder:7b":       4.5,
        # llama3 family
        "llama3.3:70b":          41.0,
        "llama3.1:8b":            5.0,
        "llama3.2:3b":            2.0,
        # deepseek family
        "deepseek-r1:32b":       19.0,
        "deepseek-r1:14b":        8.5,
        "deepseek-coder-v2:16b":  9.5,
        # mistral family
        "mistral-small:latest":  12.0,
        "mistral:7b-instruct":    4.5,
        # google
        "gemma2:9b":              6.0,
        "gemma2:27b":            16.0,
        # codellama
        "codellama:34b":         20.0,
        "codellama:13b":          8.0,
        # phi
        "phi3:mini":              2.4,
        "phi3:medium":            8.5,
    },
}

# ---------------------------------------------------------------------------
# Remote catalog config
# ---------------------------------------------------------------------------

CATALOG_URL = ""   # empty by default; user sets via config
CATALOG_CACHE = os.path.expanduser("~/.dropin_catalog.json")

# ---------------------------------------------------------------------------
# Module-level cache (populated on first call to active())
# ---------------------------------------------------------------------------

_catalog = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load(cache_path=CATALOG_CACHE):
    """Load catalog from cache file, falling back to BUILTIN_CATALOG.

    Returns a catalog dict with keys: version, preferences, ctx_windows, sizes.
    """
    if cache_path and os.path.isfile(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "preferences" in data:
                return data
        except Exception:
            pass
    return BUILTIN_CATALOG


def fetch(url, cache_path=CATALOG_CACHE):
    """Download catalog JSON from *url*, validate, save to cache, and return it.

    Raises ValueError if the response is not valid JSON or is missing the
    required 'preferences' key.
    """
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as e:
        raise ValueError(f"Failed to fetch catalog from {url}: {e}") from e

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Catalog at {url} returned invalid JSON: {e}") from e

    if "preferences" not in data:
        raise ValueError(
            f"Catalog at {url} is missing required 'preferences' key. "
            f"Got keys: {list(data.keys())}"
        )

    if cache_path:
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass  # non-fatal — cache write failure

    return data


def active():
    """Return the currently loaded catalog, loading it once and caching the result."""
    global _catalog
    if _catalog is None:
        _catalog = load()
    return _catalog


def preferences(role=None):
    """Return the full preferences dict, or the preference list for a single role."""
    prefs = active().get("preferences", BUILTIN_CATALOG["preferences"])
    if role is not None:
        return prefs.get(role, [])
    return prefs


def ctx_windows():
    """Return the context-window registry dict."""
    return active().get("ctx_windows", BUILTIN_CATALOG["ctx_windows"])


def sizes():
    """Return the model VRAM sizes dict."""
    return active().get("sizes", BUILTIN_CATALOG["sizes"])


def catalog_version():
    """Return the version string of the active catalog."""
    return active().get("version", "unknown")
