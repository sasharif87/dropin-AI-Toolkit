"""
engine.py — Ollama client with model switching.

Different tasks need different models:
  - "reason"  → deep analysis, architecture parsing, rule generation
  - "code"    → file generation, fix application
  - "quick"   → fast classification, yes/no decisions, small edits

The engine auto-detects available models and picks the best one per role,
or you can pin models explicitly.
"""

import json
import os
import re
import sys
import threading
import urllib.request
from datetime import datetime

# ---------------------------------------------------------------------------
# Model role defaults — ordered by preference (first available wins)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Model role defaults — ordered by preference (first available wins)
# ---------------------------------------------------------------------------
MODEL_PREFERENCES = {
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
}

# Context windows and temperature defaults per role
CTX_DEFAULTS = {
    "reason": 32768,
    "code": 32768,
    "quick": 8192,
}

TEMP_DEFAULTS = {
    "reason": 0.15,
    "code": 0.1,
    "quick": 0.05,
}


# ---------------------------------------------------------------------------
# Engine — Ollama client with automatic model selection per task role
# ---------------------------------------------------------------------------
class Engine:

    def __init__(self, url="http://localhost:11434",
                 code_url="http://localhost:11434", models=None):
        """
        Args:
            url:      Ollama URL for quick + reason roles.
                      Default: localhost
                        quick  -> qwen2.5-coder:7b
                        reason -> qwen2.5:72b
            code_url: Ollama URL for code role. Falls back to `url` if omitted.
                      Default: localhost
                        code   -> qwen2.5-coder:32b
            models:   Optional dict pinning roles to specific model names,
                      e.g. {"reason": "qwen2.5:72b", "code": "qwen2.5-coder:32b"}
        """
        self.url = url.rstrip("/")
        self.code_url = (code_url or url).rstrip("/")
        self.pinned = models or {}
        self._available = None        # models on url (quick/reason host)
        self._available_code = None   # models on code_url
        self._resolved = {}           # role -> model name
        self._resolved_hosts = {}     # role -> host URL (tracks where the model actually lives)

    # ── Connection & model discovery ─────────────────────────────────────────

    def test(self):
        """Test both hosts. Returns (ok, available_models, message) based on primary host."""
        ok, models, msg = self._probe(self.url)
        if ok:
            self._available = models
        # Probe code host separately (may differ from primary)
        if self.code_url != self.url:
            code_ok, code_models, _ = self._probe(self.code_url)
            if code_ok:
                self._available_code = code_models
        else:
            self._available_code = self._available
        if ok:
            self._resolve_models()
        return ok, models, msg

    def _probe(self, url):
        """Query /api/tags on a host. Returns (ok, model_list, message)."""
        try:
            req = urllib.request.Request(f"{url}/api/tags")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                models = [m["name"] for m in data.get("models", [])]
                return True, models, f"Connected ({url}) — {len(models)} model(s)"
        except Exception as e:
            return False, [], f"Cannot reach {url}: {e}"

    def _resolve_models(self):
        """Pick the best available model for each role.

        Routing (all localhost by default):
          reason -> largest available (qwen2.5:72b preferred)
          code   -> qwen2.5-coder:32b preferred
          quick  -> qwen2.5-coder:7b preferred
        """
        if self._available is None:
            self.test()
            if self._available is None:
                return

        for role in ("reason", "code", "quick"):
            # Check pinned first
            if role in self.pinned:
                self._resolved[role] = self.pinned[role]
                # Pinned code models go to code_url; everything else to url
                self._resolved_hosts[role] = self.code_url if role == "code" else self.url
                continue

            # Route code+reason to code_url host; quick stays on primary url.
            # reason falls back to primary url pool if nothing usable on code_url host.
            code_host_available = bool(self._available_code)
            if role == "code":
                pool = self._available_code if code_host_available else (self._available or [])
                preferred_host = self.code_url if code_host_available else self.url
                fallback_pool, fallback_host = [], None
            elif role == "reason":
                # Prefer code host for consolidation quality; fall back to primary host
                if code_host_available:
                    pool = self._available_code
                    preferred_host = self.code_url
                    fallback_pool = self._available or []
                    fallback_host = self.url
                else:
                    pool = self._available or []
                    preferred_host = self.url
                    fallback_pool, fallback_host = [], None
            else:  # quick
                pool = self._available or []
                preferred_host = self.url
                fallback_pool, fallback_host = [], None

            if not pool:
                pool = self._available or []
                preferred_host = self.url

            def _pick(search_pool):
                for pref in MODEL_PREFERENCES[role]:
                    if pref in search_pool:
                        return pref
                    pref_base, pref_size = (pref.split(":", 1) + [""])[:2]
                    if not pref_size:
                        for avail in search_pool:
                            if pref_base in avail:
                                return avail
                return None

            found = _pick(pool)
            if found:
                self._resolved[role] = found
                self._resolved_hosts[role] = preferred_host
            elif fallback_pool:
                found = _pick(fallback_pool)
                if found:
                    self._resolved[role] = found
                    self._resolved_hosts[role] = fallback_host

            # Ultimate fallback — use whatever's available
            if role not in self._resolved:
                if pool:
                    self._resolved[role] = pool[0]
                    self._resolved_hosts[role] = preferred_host
                elif fallback_pool:
                    self._resolved[role] = fallback_pool[0]
                    self._resolved_hosts[role] = fallback_host

    def model_for(self, role):
        """Get the resolved model name for a role."""
        if not self._resolved:
            self._resolve_models()
        return self._resolved.get(role, self.pinned.get("code", "llama3.1:8b"))

    def print_model_map(self):
        """Print which model is assigned to which role and which host."""
        if not self._resolved:
            self._resolve_models()
        print(f"\n  Model assignments:")
        for role in ("reason", "code", "quick"):
            model = self._resolved.get(role, "?")
            pinned = " (pinned)" if role in self.pinned else " (auto)"
            host = self._resolved_hosts.get(role, self.url)
            print(f"    {role:<8} -> {model}{pinned}  [{host}]")
        print()

    # ── Generation ───────────────────────────────────────────────────────────

    def generate(self, prompt, *, role="code", temperature=None, num_ctx=None,
                 timeout=1800):
        """Send prompt to Ollama. Model + host selected by role."""
        model = self.model_for(role)
        host = self._resolved_hosts.get(role, self.code_url if role == "code" else self.url)
        data = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature if temperature is not None else TEMP_DEFAULTS.get(role, 0.1),
                "num_ctx": num_ctx or CTX_DEFAULTS.get(role, 16384),
            },
        }
        req = urllib.request.Request(
            f"{host}/api/generate",
            json.dumps(data).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("response", "").strip()

    def chat(self, messages, *, role="code", temperature=None, num_ctx=None,
             timeout=1800):
        """Send chat messages to Ollama. Model selected by role."""
        model = self.model_for(role)
        data = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature if temperature is not None else TEMP_DEFAULTS.get(role, 0.1),
                "num_ctx": num_ctx or CTX_DEFAULTS.get(role, 16384),
            },
        }
        host = self._resolved_hosts.get(role, self.code_url if role == "code" else self.url)
        req = urllib.request.Request(
            f"{host}/api/chat",
            json.dumps(data).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("message", {}).get("content", "").strip()


# ---------------------------------------------------------------------------
# Utilities used everywhere
# ---------------------------------------------------------------------------
def strip_fences(text):
    """Remove markdown code fences."""
    text = re.sub(r"^```[\w]*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    return text


def extract_json(text):
    """Extract JSON from model output that may have wrapping text."""
    text = strip_fences(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for pattern in [r'\{[\s\S]*\}', r'\[[\s\S]*\]']:
        m = re.search(pattern, text)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                continue
    return None


def read_file(path, max_chars=80_000):
    """Read file, return (content, error)."""
    try:
        if os.path.getsize(path) > max_chars:
            return None, f"too large ({os.path.getsize(path):,} chars)"
        with open(path, "r", encoding="utf-8") as f:
            return f.read(), None
    except UnicodeDecodeError:
        return None, "binary"
    except Exception as e:
        return None, str(e)


def fmt_time(seconds):
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h: return f"{h}h {m}m {s}s"
    if m: return f"{m}m {s}s"
    return f"{s}s"


def ts():
    return datetime.now().strftime("%H:%M:%S")


def log(msg):
    print(f"[{ts()}] {msg}")


def timed_input(prompt, timeout=0, default="y"):
    """Prompt for input. If timeout > 0 and no response arrives, returns default."""
    if timeout <= 0:
        try:
            return input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return ""

    print(prompt, end=" ", flush=True)
    result = [None]

    def _read():
        try:
            result[0] = sys.stdin.readline().strip().lower()
        except Exception:
            result[0] = default

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout)
    if result[0] is None:
        print(f"(no response after {timeout}s — defaulting '{default}')")
        result[0] = default
    return result[0]