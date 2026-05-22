"""
hardware.py — GPU/VRAM detection, model size catalog, and Ollama pull helpers.

Used in two ways:
  1. Engine calls detect_gpu() at startup so model selection prefers models
     that actually fit the hardware.
  2. `drop.py setup` calls the full interactive flow: detect → recommend →
     pull missing models (optionally update existing ones).

No external dependencies — stdlib + Ollama API only.
"""

import json
import os
import platform
import subprocess
import sys
import urllib.request

from catalog import sizes as _cat_sizes


def model_size_gb(model):
    """Approximate VRAM requirement in GB for a model name. None if unknown."""
    model_sizes = _cat_sizes()
    if model in model_sizes:
        return model_sizes[model]
    base = model.split(":")[0]
    for key, size in model_sizes.items():
        if key.split(":")[0] == base:
            return size
    return None


# ---------------------------------------------------------------------------
# Hardware detection
# ---------------------------------------------------------------------------

def detect_gpu():
    """Detect GPU and available VRAM.

    Returns a dict with:
        source   : "nvidia" | "rocm" | "metal" | "cpu"
        gpu_name : str
        vram_gb  : float  — usable budget (VRAM, or 60% of RAM for CPU)
        gpus     : list[{name, vram_gb}]

    Never raises — falls back gracefully to a CPU estimate.
    """
    for probe in (_probe_nvidia, _probe_rocm, _probe_metal, _probe_cpu):
        try:
            result = probe()
            if result:
                return result
        except Exception:
            pass
    return {"source": "cpu", "gpu_name": "unknown", "vram_gb": 4.0, "gpus": []}


def _probe_nvidia():
    out = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=name,memory.total",
         "--format=csv,noheader,nounits"],
        timeout=5, stderr=subprocess.DEVNULL,
    ).decode().strip()
    if not out:
        return None
    gpus = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2:
            gpus.append({"name": parts[0], "vram_gb": int(parts[1]) / 1024})
    if not gpus:
        return None
    total = sum(g["vram_gb"] for g in gpus)
    name = gpus[0]["name"] if len(gpus) == 1 else f"{len(gpus)}x {gpus[0]['name']}"
    return {"source": "nvidia", "gpu_name": name, "vram_gb": total, "gpus": gpus}


def _probe_rocm():
    out = subprocess.check_output(
        ["rocm-smi", "--showmeminfo", "vram", "--csv"],
        timeout=5, stderr=subprocess.DEVNULL,
    ).decode()
    gpus = []
    for line in out.splitlines():
        if "VRAM Total Memory" in line:
            parts = line.split(",")
            if len(parts) >= 2:
                vram_b = int(parts[-1].strip())
                gpus.append({"name": f"AMD GPU {len(gpus)}", "vram_gb": vram_b / 1e9})
    if not gpus:
        return None
    total = sum(g["vram_gb"] for g in gpus)
    name = f"{len(gpus)}x AMD GPU" if len(gpus) > 1 else "AMD GPU"
    return {"source": "rocm", "gpu_name": name, "vram_gb": total, "gpus": gpus}


def _probe_metal():
    """Apple Silicon: use 75% of unified memory as effective GPU budget."""
    if platform.system() != "Darwin":
        return None
    out = subprocess.check_output(
        ["sysctl", "-n", "hw.memsize"], timeout=3, stderr=subprocess.DEVNULL,
    ).decode().strip()
    total_gb = int(out) / 1_073_741_824
    usable = round(total_gb * 0.75, 1)
    return {
        "source": "metal",
        "gpu_name": f"Apple Silicon ({int(total_gb)} GB unified)",
        "vram_gb": usable,
        "gpus": [{"name": "Apple Silicon", "vram_gb": usable}],
    }


def _probe_cpu():
    """CPU-only fallback: use 60% of system RAM as model budget."""
    ram = _total_ram_gb()
    usable = round(ram * 0.6, 1)
    return {"source": "cpu", "gpu_name": "CPU only", "vram_gb": usable, "gpus": []}


def _total_ram_gb():
    try:
        if platform.system() == "Linux":
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        return int(line.split()[1]) / 1_048_576
        elif platform.system() == "Darwin":
            out = subprocess.check_output(
                ["sysctl", "-n", "hw.memsize"], timeout=3, stderr=subprocess.DEVNULL,
            ).decode().strip()
            return int(out) / 1_073_741_824
        elif platform.system() == "Windows":
            out = subprocess.check_output(
                ["wmic", "OS", "get", "TotalVisibleMemorySize", "/Value"],
                timeout=5, stderr=subprocess.DEVNULL,
            ).decode()
            for line in out.splitlines():
                if "TotalVisibleMemorySize" in line and "=" in line:
                    kb = int(line.split("=")[1].strip())
                    return kb / 1_048_576
    except Exception:
        pass
    return 8.0


# ---------------------------------------------------------------------------
# Model recommendation
# ---------------------------------------------------------------------------

def recommend_models(vram_gb, installed_names, preferences):
    """Choose the best fitting model per role given the hardware budget.

    Args:
        vram_gb        : float — usable VRAM from detect_gpu()
        installed_names: set[str] — model names currently in Ollama
        preferences    : dict role -> [ordered model names]

    Returns dict: role -> {model, size_gb, installed}
    """
    budget = vram_gb * 0.90   # 10% headroom for KV-cache and overhead
    result = {}

    for role, pref_list in preferences.items():
        chosen = None
        for model in pref_list:
            size = model_size_gb(model)
            if size is None or size <= budget:
                chosen = model
                break
        if chosen is None:
            chosen = pref_list[-1]  # smallest fallback

        size = model_size_gb(chosen)
        installed = _is_installed(chosen, installed_names)
        result[role] = {"model": chosen, "size_gb": size, "installed": installed}

    return result


def _is_installed(model, installed_names):
    """Check if model (or its base name) is in the installed set."""
    if model in installed_names:
        return True
    base = model.split(":")[0]
    return any(n.split(":")[0] == base for n in installed_names)


# ---------------------------------------------------------------------------
# Ollama model management
# ---------------------------------------------------------------------------

def installed_models(host="http://localhost:11434"):
    """Return list of {name, size_gb} for models installed on the Ollama host."""
    try:
        req = urllib.request.Request(f"{host}/api/tags")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        return [
            {"name": m["name"], "size_gb": m.get("size", 0) / 1e9}
            for m in data.get("models", [])
        ]
    except Exception:
        return []


def pull_model(model, host="http://localhost:11434"):
    """Pull (or update) a model via the Ollama API with a live progress bar.

    Returns True on success, False on failure.
    """
    print(f"  Pulling {model} ...", flush=True)
    data = json.dumps({"model": model, "stream": True}).encode()
    req = urllib.request.Request(
        f"{host}/api/pull", data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=3600) as resp:
            while True:
                line = resp.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode())
                except json.JSONDecodeError:
                    continue

                status = msg.get("status", "")
                total = msg.get("total", 0)
                completed = msg.get("completed", 0)

                if total > 0:
                    pct = completed / total * 100
                    done_gb = completed / 1e9
                    tot_gb = total / 1e9
                    filled = int(pct / 5)
                    bar = "#" * filled + "-" * (20 - filled)
                    print(f"\r  [{bar}] {done_gb:.1f}/{tot_gb:.1f} GB ({pct:.0f}%)",
                          end="", flush=True)
                elif status:
                    print(f"\r  {status:<55}", end="", flush=True)

                if status == "success":
                    print(f"\r  {model} — ready.{' ' * 50}")
                    return True
        print()
        return True
    except Exception as e:
        print(f"\n  Pull failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Setup report
# ---------------------------------------------------------------------------

def print_hardware(hw):
    """Print a one-line hardware summary."""
    src = hw["source"].upper()
    name = hw["gpu_name"]
    vram = hw["vram_gb"]
    print(f"  [{src}] {name}  —  {vram:.1f} GB usable")
    if len(hw.get("gpus", [])) > 1:
        for g in hw["gpus"]:
            print(f"          {g['name']}: {g['vram_gb']:.1f} GB")


def print_recommendation_table(rec, current_installed):
    """Print a recommendation table: role / model / size / status."""
    col_w = [8, 28, 9, 14]
    div = "+" + "+".join("-" * (w + 2) for w in col_w) + "+"
    def row(*cells):
        return "| " + " | ".join(str(c).ljust(w) for c, w in zip(cells, col_w)) + " |"

    print(f"\n  {div}")
    print(f"  {row('Role', 'Model', 'VRAM', 'Status')}")
    print(f"  {div}")
    for role, info in rec.items():
        model = info["model"]
        size = f"{info['size_gb']:.1f} GB" if info["size_gb"] else "?"
        if info["installed"]:
            # Find the exact installed variant
            match = next((m for m in current_installed if m["name"].split(":")[0] == model.split(":")[0]), None)
            status = f"installed ({match['size_gb']:.1f} GB)" if match else "installed"
        else:
            status = f"not installed"
        print(f"  {row(role, model, size, status)}")
    print(f"  {div}")
