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
import urllib.error
import urllib.request
from datetime import datetime

from catalog import preferences as _cat_prefs, ctx_windows as _cat_ctx

# ---------------------------------------------------------------------------
# Model role defaults — ordered by preference (first available wins)
# Resolved from the active catalog so the data lives in catalog.py.
# Module-level aliases keep all existing call sites working unchanged.
# ---------------------------------------------------------------------------
MODEL_PREFERENCES = _cat_prefs()
MODEL_CTX_WINDOWS = _cat_ctx()

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
# Context-window registry and chunking config
# ---------------------------------------------------------------------------

# Conservative chars-per-token ratio for mixed code/prose content.
CHARS_PER_TOKEN = 3.0

# Per-role KV-cache token caps.  Raise these if your GPU has VRAM to spare;
# lower them if Ollama is OOM-ing.  ctx_for_role() uses these as a ceiling
# even when the chosen model's architecture supports a larger window.
ROLE_CTX_CAPS = {
    "reason": 65_536,   # large enough for arch docs + consolidation
    "code":   32_768,   # safe default for most desktop setups
    "quick":   8_192,   # fast classification needs little context
}


# ---------------------------------------------------------------------------
# Engine — Ollama client with automatic model selection per task role
# ---------------------------------------------------------------------------
class Engine:

    def __init__(self, url="http://localhost:11434",
                 code_url="http://localhost:11434", models=None, role_ctx_caps=None):
        """
        Args:
            url:           Ollama URL for quick + reason roles.
            code_url:      Ollama URL for code role. Falls back to `url` if omitted.
            models:        Optional dict pinning roles to specific model names.
            role_ctx_caps: Optional dict overriding per-role KV-cache caps (tokens),
                           e.g. {"code": 65536}.  Merges with ROLE_CTX_CAPS defaults.
        """
        self.url = url.rstrip("/")
        self.code_url = (code_url or url).rstrip("/")
        self.pinned = models or {}
        self._role_ctx_caps = dict(ROLE_CTX_CAPS)
        if role_ctx_caps:
            self._role_ctx_caps.update({r: int(v) for r, v in role_ctx_caps.items()})
        self._available = None        # models on url (quick/reason host)
        self._available_code = None   # models on code_url
        self._resolved = {}           # role -> model name
        self._resolved_hosts = {}     # role -> host URL (tracks where the model actually lives)
        self._probed_ctx = {}         # role -> context_length from /api/show (live probe)
        self._hardware = None         # result of hardware.detect_gpu()

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
            # Detect hardware before resolving so preference sorting can use it.
            try:
                from hardware import detect_gpu
                self._hardware = detect_gpu()
            except Exception:
                self._hardware = None
            self._resolve_models()
            self._probe_all_model_ctx()
        return ok, models, msg

    def _probe(self, url):
        """Query /api/tags on a host. Returns (ok, model_list, message)."""
        try:
            req = urllib.request.Request(f"{url}/api/tags")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                models = [m["name"] for m in data.get("models", [])]
                return True, models, f"Connected ({url}) - {len(models)} model(s)"
        except Exception as e:
            return False, [], f"Cannot reach {url}: {e}"

    def _query_running_models(self, host):
        """Return list of {name, size_gb, vram_gb} for models Ollama currently has loaded.

        Uses /api/ps (Ollama ≥0.1.33).  Returns [] on failure or older Ollama.
        """
        try:
            req = urllib.request.Request(f"{host}/api/ps")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            result = []
            for m in data.get("models", []):
                size_gb = m.get("size", 0) / 1e9
                vram_gb = m.get("size_vram", 0) / 1e9
                result.append({
                    "name": m.get("name", "?"),
                    "size_gb": size_gb,
                    "vram_gb": vram_gb,
                    "ram_gb": size_gb - vram_gb,
                })
            return result
        except Exception:
            return []

    def _probe_model_ctx(self, model, host):
        """Return the model's actual context_length via /api/show, or None on failure.

        Ollama ≥0.1.33 exposes model_info with architecture-specific keys like
        'llama.context_length' or 'qwen2.context_length'.  Older versions may
        expose a num_ctx PARAMETER in the Modelfile parameters string instead.
        """
        try:
            data = json.dumps({"model": model}).encode("utf-8")
            req = urllib.request.Request(
                f"{host}/api/show",
                data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                info = json.loads(resp.read().decode("utf-8"))

            # Preferred: model_info dict (Ollama ≥0.1.33)
            for key, val in info.get("model_info", {}).items():
                if "context_length" in key:
                    return int(val)

            # Fallback: PARAMETER num_ctx line in the Modelfile parameters blob
            for line in info.get("parameters", "").splitlines():
                parts = line.strip().lower().split()
                if parts and parts[0] == "num_ctx" and len(parts) >= 2:
                    return int(parts[-1])
        except Exception:
            pass
        return None

    def _probe_all_model_ctx(self):
        """Call /api/show for every resolved model and cache context lengths.

        Results take priority over MODEL_CTX_WINDOWS in ctx_for_role().
        Duplicate model/host pairs are probed only once.
        """
        self._probed_ctx = {}
        seen = {}  # (model, host) -> ctx
        for role in ("reason", "code", "quick"):
            model = self._resolved.get(role)
            if not model:
                continue
            host = self._resolved_hosts.get(role, self.url)
            key = (model, host)
            if key not in seen:
                seen[key] = self._probe_model_ctx(model, host)
            if seen[key]:
                self._probed_ctx[role] = seen[key]

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

            hw_prefs = self._hw_sorted_prefs(role)

            def _pick(search_pool, prefs=hw_prefs):
                for pref in prefs:
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

        hw = getattr(self, "_hardware", None)
        if hw:
            from hardware import print_hardware
            print_hardware(hw)

        try:
            from hardware import model_size_gb as _msz
        except ImportError:
            _msz = lambda _: None  # noqa: E731

        print(f"\n  Model assignments:")
        for role in ("reason", "code", "quick"):
            model = self._resolved.get(role, "?")
            pinned = " (pinned)" if role in self.pinned else " (auto)"
            host = self._resolved_hosts.get(role, self.url)
            ctx = self.ctx_for_role(role)
            budget = self.content_budget(role)

            if self._probed_ctx.get(role):
                ctx_src = "probed"
            elif self._model_ctx(model):
                ctx_src = "registry"
            else:
                ctx_src = "cap"

            size = _msz(model)
            vram = hw["vram_gb"] if hw else None
            if hw and size and vram is not None:
                fits = "ok" if size <= vram * 0.90 else "!!"
                size_tag = f"  [{fits} {size:.1f}GB]"
            elif size:
                size_tag = f"  [{size:.1f}GB]"
            else:
                size_tag = ""

            print(f"    {role:<8} -> {model}{pinned}  [{host}]"
                  f"  ctx={ctx//1024}k ({ctx_src})  budget~{budget//1000}k chars{size_tag}")
        print()

    # ── Hardware-aware preference sorting ────────────────────────────────────

    def _hw_sorted_prefs(self, role):
        """Return MODEL_PREFERENCES[role] with hardware-fitting models first.

        Models whose catalogued VRAM requirement exceeds the detected budget are
        demoted to the end of the list.  Models not in the size catalog are left
        in place (we cannot safely exclude them).  When no hardware info is
        available the original preference order is returned unchanged.
        """
        hw = getattr(self, "_hardware", None)
        if not hw or hw.get("vram_gb") is None:
            return MODEL_PREFERENCES[role]
        try:
            from hardware import model_size_gb
        except ImportError:
            return MODEL_PREFERENCES[role]

        budget = hw["vram_gb"] * 0.90
        prefs = MODEL_PREFERENCES[role]
        fitting = [m for m in prefs
                   if (sz := model_size_gb(m)) is None or sz <= budget]
        non_fitting = [m for m in prefs if m not in set(fitting)]
        return fitting + non_fitting

    # ── Context-window helpers ────────────────────────────────────────────────

    def _model_ctx(self, model):
        """Context window (tokens) from registry; None if unknown."""
        if model in MODEL_CTX_WINDOWS:
            return MODEL_CTX_WINDOWS[model]
        base = model.split(":")[0]
        for key, ctx in MODEL_CTX_WINDOWS.items():
            if key.split(":")[0] == base:
                return ctx
        return None

    def ctx_for_role(self, role):
        """Token budget to request from Ollama for this role's model.

        Priority order:
          1. Live /api/show probe  — most accurate, reflects actual model weights
          2. MODEL_CTX_WINDOWS     — static registry for known model families
          3. ROLE_CTX_CAPS fallback

        Always capped by ROLE_CTX_CAPS to avoid giant KV-cache allocations on
        modest hardware.  Adjust ROLE_CTX_CAPS at the top of this file if you
        have more VRAM and want larger effective windows.
        """
        cap = self._role_ctx_caps.get(role, CTX_DEFAULTS.get(role, 32_768))
        probed = self._probed_ctx.get(role)
        if probed:
            return min(probed, cap)
        known = self._model_ctx(self.model_for(role))
        return min(known, cap) if known else cap

    def content_budget(self, role):
        """Max characters for source content in a single prompt for this role.

        Reserves ~28 % of the context for prompt scaffolding and the response.
        Text larger than this should be chunked before being sent to the model.
        """
        return int(self.ctx_for_role(role) * CHARS_PER_TOKEN * 0.72)

    # ── Generation ───────────────────────────────────────────────────────────

    def generate(self, prompt, *, role="code", temperature=None, num_ctx=None,
                 timeout=1800, retries=1):
        """Send prompt to Ollama. Model + host selected by role.

        Uses streaming so tokens flow continuously — the socket stays alive for
        slow/RAM-offloaded models and never times out mid-generation.
        timeout is the per-chunk idle timeout, not total generation time.
        Retries once at temperature=0 on empty responses or transient errors.
        """
        model = self.model_for(role)
        host = self._resolved_hosts.get(role, self.code_url if role == "code" else self.url)
        last_exc = None
        eff_ctx = num_ctx if num_ctx is not None else self.ctx_for_role(role)

        for attempt in range(1 + retries):
            if attempt > 0 and temperature is None:
                eff_temp = 0.0
            else:
                eff_temp = temperature if temperature is not None else TEMP_DEFAULTS.get(role, 0.1)

            options = {"temperature": eff_temp, "num_ctx": eff_ctx}
            data = {
                "model": model,
                "prompt": prompt,
                "stream": True,
                "options": options,
            }
            if "qwen3" in model.lower():
                data["think"] = False
            req = urllib.request.Request(
                f"{host}/api/generate",
                json.dumps(data).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            try:
                chunks = []
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    for raw_line in resp:
                        line = raw_line.strip()
                        if not line:
                            continue
                        chunk = json.loads(line.decode("utf-8"))
                        if chunk.get("response"):
                            chunks.append(chunk["response"])
                        if chunk.get("done"):
                            break
                response = "".join(chunks).strip()
                if response:
                    return response
                if attempt < retries:
                    log(f"  [retry {attempt + 1}/{retries}] empty response - retrying at temp=0")
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", errors="replace")
                    body_json = json.loads(body)
                    body = body_json.get("error", body)
                except Exception:
                    pass
                last_exc = Exception(f"HTTP {e.code}: {body or e.reason}")
                if e.code == 500 and "more system memory" in body:
                    running = self._query_running_models(host)
                    ram_detail = ""
                    if running:
                        lines = [f"    {r['name']}: {r['size_gb']:.1f} GB total "
                                 f"({r['vram_gb']:.1f} GB VRAM + {r['ram_gb']:.1f} GB RAM)"
                                 for r in running]
                        ram_detail = "\n  Models currently loaded on that host:\n" + "\n".join(lines)
                    raise Exception(
                        f"Model '{model}' needs more RAM than the Ollama host has free.\n"
                        f"  Ollama says: {body}{ram_detail}\n"
                        f"  Free RAM by running `ollama stop <model>` on the host, "
                        f"or use a smaller model with --reason-model <name>."
                    )
                if attempt < retries:
                    if e.code == 500:
                        eff_ctx = max(eff_ctx // 2, 2048)
                        log(f"  [retry {attempt + 1}/{retries}] HTTP 500: {body or e.reason} — retrying with ctx={eff_ctx}")
                    else:
                        log(f"  [retry {attempt + 1}/{retries}] HTTP {e.code}: {body or e.reason} — retrying")
                else:
                    raise last_exc
            except Exception as e:
                last_exc = e
                if attempt < retries:
                    log(f"  [retry {attempt + 1}/{retries}] {e} - retrying")
                else:
                    raise

        if last_exc:
            raise last_exc
        return ""

    def chat(self, messages, *, role="code", temperature=None, num_ctx=None,
             timeout=1800, retries=1):
        """Send chat messages to Ollama. Model selected by role. Uses streaming."""
        model = self.model_for(role)
        host = self._resolved_hosts.get(role, self.code_url if role == "code" else self.url)
        last_exc = None
        eff_ctx = num_ctx if num_ctx is not None else self.ctx_for_role(role)

        for attempt in range(1 + retries):
            if attempt > 0 and temperature is None:
                eff_temp = 0.0
            else:
                eff_temp = temperature if temperature is not None else TEMP_DEFAULTS.get(role, 0.1)

            options = {"temperature": eff_temp, "num_ctx": eff_ctx}
            data = {
                "model": model,
                "messages": messages,
                "stream": True,
                "options": options,
            }
            if "qwen3" in model.lower():
                data["think"] = False
            req = urllib.request.Request(
                f"{host}/api/chat",
                json.dumps(data).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            try:
                chunks = []
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    for raw_line in resp:
                        line = raw_line.strip()
                        if not line:
                            continue
                        chunk = json.loads(line.decode("utf-8"))
                        content = chunk.get("message", {}).get("content", "")
                        if content:
                            chunks.append(content)
                        if chunk.get("done"):
                            break
                response = "".join(chunks).strip()
                if response:
                    return response
                if attempt < retries:
                    log(f"  [retry {attempt + 1}/{retries}] empty response - retrying at temp=0")
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", errors="replace")
                    body_json = json.loads(body)
                    body = body_json.get("error", body)
                except Exception:
                    pass
                last_exc = Exception(f"HTTP {e.code}: {body or e.reason}")
                if e.code == 500 and "more system memory" in body:
                    raise Exception(
                        f"Model '{model}' needs more RAM than the Ollama host has available.\n"
                        f"  Ollama says: {body}\n"
                        f"  Try a smaller model with --reason-model <name>."
                    )
                if attempt < retries:
                    if e.code == 500:
                        eff_ctx = max(eff_ctx // 2, 2048)
                        log(f"  [retry {attempt + 1}/{retries}] HTTP 500: {body or e.reason} — retrying with ctx={eff_ctx}")
                    else:
                        log(f"  [retry {attempt + 1}/{retries}] HTTP {e.code}: {body or e.reason} — retrying")
                else:
                    raise last_exc
            except Exception as e:
                last_exc = e
                if attempt < retries:
                    log(f"  [retry {attempt + 1}/{retries}] {e} - retrying")
                else:
                    raise

        if last_exc:
            raise last_exc
        return ""


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


def read_file(path, max_chars=500_000):
    """Read file, return (content, error).

    Default limit raised to 500 k so callers can chunk rather than skip large
    files.  Pass a smaller max_chars to keep the old reject-early behaviour.
    """
    try:
        if os.path.getsize(path) > max_chars:
            return None, f"too large ({os.path.getsize(path):,} bytes)"
        with open(path, "r", encoding="utf-8") as f:
            return f.read(), None
    except UnicodeDecodeError:
        return None, "binary"
    except Exception as e:
        return None, str(e)


def safe_abs_path(root, rel):
    """Return a normalised absolute path only if rel stays inside root.

    Rejects absolute paths and directory-traversal sequences.
    Returns None when the path would escape the project root.
    """
    rel = rel.lstrip("/\\").replace("\\", "/")
    abs_p = os.path.normpath(os.path.join(root, rel))
    root_norm = os.path.normpath(root)
    if abs_p != root_norm and not abs_p.startswith(root_norm + os.sep):
        return None
    return abs_p


def chunk_text(text, max_chars, overlap=300):
    """Split *text* into overlapping chunks that each fit in *max_chars*.

    Breaks preferentially at newline boundaries in the second half of each
    window, so we never cut mid-line.  Consecutive chunks share *overlap*
    characters so that code near the seam is fully visible in at least one
    chunk.  Returns a list with one element when text already fits.
    """
    if len(text) <= max_chars:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            # Find the last newline in the second half of the window.
            nl = text.rfind("\n", start + max_chars // 2, end)
            if nl != -1:
                end = nl + 1
        chunks.append(text[start:end])
        # Overlap: next chunk re-reads the last `overlap` chars.
        start = max(end - overlap, start + 1)  # +1 guarantees progress
    return chunks


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
        print(f"(no response after {timeout}s - defaulting '{default}')")
        result[0] = default
    return result[0]