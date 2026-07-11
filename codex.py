#!/usr/bin/env python3
"""
codex.py — run a TODO item through your local Ollama coder and get back
the file(s), the exact steps, and the commands to run.

Stdlib only. Talks to the Ollama HTTP API (same ethos as dropin-AI-Toolkit).
Nothing is written into your projects unless you pass --apply: by default the
generated files land in a timestamped run dir so you review first, then apply.

Each run dir also doubles as your seam log — it keeps the prompt, the raw
model response, and the parsed result, so you have a record of what local
actually produced for a given task.

Examples
--------
  # free-text task, default model (qwen3-coder:30b) on the default host
  python codex.py "Add a healthcheck to the gluetun compose service"

  # pull a specific line out of a TODO file as the task
  python codex.py --todo homelab/Docs/TODO.md:126

  # give the model an existing file to edit in-context
  python codex.py "Add a restart policy" --context homelab/compose/jellyfin/docker-compose.yml

  # write the result straight into the repo (relative to --root, default cwd)
  python codex.py "..." --apply

  # see what local would do without calling out for a model change
  python codex.py "..." --model qwen2.5-coder:32b
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

def _load_dotenv():
    """Fill os.environ from the .env next to this script (real env vars win).

    Machine-specific host addresses (LAN IPs, remote Ollama boxes) belong in
    .env — which is gitignored — never in this file. See .env.example.
    Only reads the toolkit's own directory, not the cwd, so a checked-out
    repo can't inject a host.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path, "r", encoding="utf-8") as f:
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

DEFAULT_HOST = os.environ.get("CODEX_HOST") or os.environ.get("OLLAMA_HOST") or "http://localhost:11434"
LOCAL_HOST = os.environ.get("CODEX_LOCAL_HOST") or "http://localhost:11434"
DEFAULT_MODEL = os.environ.get("CODEX_MODEL") or "qwen3-coder:30b"
DEFAULT_TIMEOUT = int(os.environ.get("CODEX_TIMEOUT", "600"))  # 70B cold loads are slow
CODER_HINTS = ("coder", "code", "qwen", "deepseek", "starcoder", "codestral")

SYSTEM_PROMPT = """You are a senior engineer producing production-ready output for a self-hosted, \
local-first homelab and app portfolio (Python/FastAPI, Node/Hono, React+Vite, Docker Compose, \
Proxmox). You write code that matches the surrounding stack and conventions.

Respond with a SINGLE JSON object and nothing else, matching this schema exactly:

{
  "summary": "<one sentence: what this does>",
  "assumptions": ["<assumption you made because the task was underspecified>", ...],
  "files": [
    {"path": "<relative/path/to/file>", "language": "<yaml|python|js|...>", "content": "<COMPLETE file contents, no placeholders, no truncation, no '...'>"}
  ],
  "steps": ["<exact, ordered step a human follows to apply this>", ...],
  "commands": ["<exact shell command to run, in order>", ...],
  "notes": "<caveats, risks, what to verify, or what you were unsure about>"
}

Rules:
- File `content` must be the entire file, ready to save. Never abbreviate or use ellipses.
- `steps` are concrete and ordered (e.g. "Save the file to X", "Run command Y", "Verify Z").
- If you are uncertain or the task is ambiguous, say so in `assumptions`/`notes` rather than guessing silently.
- Prefer the smallest correct change. Do not invent files that weren't asked for."""


def http_request(url, payload=None, timeout=DEFAULT_TIMEOUT):
    """POST json if payload given, else GET. Returns parsed JSON."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def list_models(host):
    try:
        tags = http_request(f"{host}/api/tags", timeout=15)
        return [m["name"] for m in tags.get("models", [])]
    except Exception:
        return []


def host_reachable(host):
    return bool(list_models(host))


def pick_host(explicit, local_only):
    """Routing: explicit --host wins; else --local forces CODEX_LOCAL_HOST; else
    try the primary host (CODEX_HOST) first and fall back to the local one."""
    if explicit:
        return explicit
    if local_only:
        return LOCAL_HOST
    # primary host first, local fallback second
    if host_reachable(DEFAULT_HOST):
        return DEFAULT_HOST
    if host_reachable(LOCAL_HOST):
        print(f"[codex] host {DEFAULT_HOST} unreachable, falling back to {LOCAL_HOST}", file=sys.stderr)
        return LOCAL_HOST
    print(f"[codex] no configured host reachable; trying {DEFAULT_HOST} anyway", file=sys.stderr)
    return DEFAULT_HOST


def resolve_model(host, requested):
    """Confirm the model exists; if not, fall back to a coder-ish one and warn."""
    available = list_models(host)
    if not available:
        # Can't reach /api/tags — let the chat call surface the real error.
        return requested
    if requested in available:
        return requested
    # tolerate ":latest" omission and vice versa
    base = requested.split(":")[0]
    for m in available:
        if m == base or m.split(":")[0] == base:
            print(f"[codex] '{requested}' not found, using '{m}'", file=sys.stderr)
            return m
    coders = [m for m in available if any(h in m.lower() for h in CODER_HINTS)]
    pick = coders[0] if coders else available[0]
    print(f"[codex] '{requested}' not installed. Available: {', '.join(available)}", file=sys.stderr)
    print(f"[codex] falling back to '{pick}' (or run: ollama pull {requested})", file=sys.stderr)
    return pick


def read_todo(spec):
    """spec is 'path:linenumber' — return that line's text as the task."""
    if ":" not in spec:
        raise SystemExit(f"--todo expects PATH:LINE, got '{spec}'")
    path, _, lineno = spec.rpartition(":")
    if not lineno.isdigit():
        raise SystemExit(f"--todo line must be a number, got '{lineno}'")
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    idx = int(lineno) - 1
    if idx < 0 or idx >= len(lines):
        raise SystemExit(f"{path} has no line {lineno}")
    return lines[idx].strip().lstrip("-*[ ]x").strip()


def build_messages(task, context_files):
    parts = [f"TASK:\n{task}"]
    for cf in context_files:
        p = Path(cf)
        if not p.exists():
            raise SystemExit(f"--context file not found: {cf}")
        parts.append(f"\nEXISTING FILE `{cf}` (edit in place, return the full updated file):\n```\n{p.read_text(encoding='utf-8')}\n```")
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(parts)},
    ]


def call_ollama(host, model, messages, temperature, timeout):
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "format": "json",
        "options": {"temperature": temperature},
    }
    try:
        resp = http_request(f"{host}/api/chat", payload, timeout=timeout)
    except urllib.error.URLError as e:
        raise SystemExit(f"[codex] cannot reach Ollama at {host}: {e}\n"
                         f"        set CODEX_HOST or pass --host (current default: {DEFAULT_HOST})")
    return resp.get("message", {}).get("content", "")


def extract_json(text):
    """format:json should give clean JSON, but guard against stray prose."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
    return None


def write_files(files, base_dir):
    written = []
    for f in files:
        rel = f.get("path")
        if not rel:
            continue
        dest = (base_dir / rel).resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f.get("content", ""), encoding="utf-8")
        written.append(dest)
    return written


def print_report(result, written, run_dir):
    def section(title):
        print(f"\n\033[1m{title}\033[0m")

    print(f"\n\033[1;36m{result.get('summary', '(no summary)')}\033[0m")

    if result.get("assumptions"):
        section("Assumptions")
        for a in result["assumptions"]:
            print(f"  - {a}")

    section("Files")
    for d in written:
        print(f"  + {d}")
    if not written:
        print("  (none)")

    if result.get("steps"):
        section("Steps")
        for i, s in enumerate(result["steps"], 1):
            print(f"  {i}. {s}")

    if result.get("commands"):
        section("Commands")
        for c in result["commands"]:
            print(f"  $ {c}")

    if result.get("notes"):
        section("Notes")
        print(f"  {result['notes']}")

    print(f"\n\033[2mRun saved to: {run_dir}\033[0m")


def main():
    ap = argparse.ArgumentParser(description="Run a TODO item through your local Ollama coder.")
    ap.add_argument("task", nargs="?", help="The task / prompt (free text).")
    ap.add_argument("--todo", help="Pull the task from a TODO file: PATH:LINE")
    ap.add_argument("--task-file", help="Read the whole task prompt from a file.")
    ap.add_argument("--context", action="append", default=[], help="Existing file to give the model (repeatable).")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"Ollama model (default: {DEFAULT_MODEL})")
    ap.add_argument("--host", default=None, help=f"Force a specific Ollama host (default: {DEFAULT_HOST}, falling back to {LOCAL_HOST})")
    ap.add_argument("--local", action="store_true", help="Force the local host (CODEX_LOCAL_HOST) instead of the primary one.")
    ap.add_argument("--temperature", type=float, default=0.2, help="Sampling temperature (default: 0.2)")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"Request timeout sec (default: {DEFAULT_TIMEOUT})")
    ap.add_argument("--apply", action="store_true", help="Write files into --root instead of the run dir.")
    ap.add_argument("--root", default=".", help="Where --apply writes files (default: cwd).")
    ap.add_argument("--out", default="codex-runs", help="Where run records are stored (default: ./codex-runs).")
    args = ap.parse_args()

    # Resolve the task from exactly one source.
    sources = [bool(args.task), bool(args.todo), bool(args.task_file)]
    if sum(sources) != 1:
        ap.error("provide exactly one of: a task argument, --todo, or --task-file")
    if args.todo:
        task = read_todo(args.todo)
    elif args.task_file:
        task = Path(args.task_file).read_text(encoding="utf-8").strip()
    else:
        task = args.task
    print(f"[codex] task: {task}", file=sys.stderr)

    host = pick_host(args.host, args.local)
    model = resolve_model(host, args.model)
    messages = build_messages(task, args.context)

    print(f"[codex] calling {model} @ {host} ...", file=sys.stderr)
    t0 = time.time()
    content = call_ollama(host, model, messages, args.temperature, args.timeout)
    elapsed = time.time() - t0
    print(f"[codex] responded in {elapsed:.1f}s", file=sys.stderr)

    result = extract_json(content)

    run_dir = Path(args.out) / time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "prompt.txt").write_text(task, encoding="utf-8")
    (run_dir / "raw_response.txt").write_text(content, encoding="utf-8")
    meta = {"model": model, "host": host, "elapsed_sec": round(elapsed, 1),
            "temperature": args.temperature, "context": args.context, "task": task}
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    if result is None:
        print(f"[codex] model did not return valid JSON. Raw output saved to {run_dir/'raw_response.txt'}",
              file=sys.stderr)
        print("[codex] (this is itself a seam signal — note which model/task it happened on.)", file=sys.stderr)
        sys.exit(2)

    (run_dir / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")

    target = Path(args.root) if args.apply else (run_dir / "files")
    written = write_files(result.get("files", []), target)

    print_report(result, written, run_dir)
    if not args.apply and written:
        print("\033[2m(review, then re-run with --apply, or copy the files into place)\033[0m")


if __name__ == "__main__":
    main()
