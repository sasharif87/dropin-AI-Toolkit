"""
rules.py — Auto-generate layer-specific review/development rules.

Two modes:
  1. Pattern-based: generates rules from detected project patterns (no LLM needed)
  2. Architecture-aware: reads the arch doc with reasoning model for deeper rules

The pattern rules reflect production engineering standards:
  - Error isolation (one bad item never kills a batch)
  - Read-only by default until explicit approval
  - Provenance / audit logging on state changes
  - Abstract interfaces with concrete implementations
  - Multi-tenancy (tenant_id where applicable)
  - No hardcoded secrets
  - Consistent error responses
  - Never expose raw tracebacks
  - Layer boundaries respected
"""

import json
import os

from engine import Engine, extract_json, read_file

# ---------------------------------------------------------------------------
# Pattern → rule templates
# ---------------------------------------------------------------------------
# These are opinionated rules based on real production patterns.
# Each maps a detected pattern (or layer type) to concrete rules.

UNIVERSAL_RULES = """
- Secrets must come from environment variables or .env — never hardcoded in source
- Error handling: catch specific exceptions, never bare `except:` or `except Exception:`
  that silently swallows errors without logging
- No print() in production code — use proper logging module
- All database tables must include tenant_id if multi-tenancy is a requirement
- Never return raw dicts from API endpoints — use typed response models
"""

RULE_TEMPLATES = {
    # ── Data / DB layers ──
    "has_models": """
DATA MODELS RULES:
- All schema changes must go through migrations (Alembic/Prisma) — never raw CREATE TABLE
- Core tables MUST include: id, created_at, updated_at
- Add tenant_id to all tables that store user/org data
- Use proper column types — no VARCHAR for dates, no TEXT for booleans
- Foreign keys must have ON DELETE behavior specified (CASCADE, SET NULL, RESTRICT)
- Index any column used in WHERE clauses or JOINs
""",

    "has_migrations": """
MIGRATION RULES:
- Every migration must be reversible (include downgrade/rollback)
- Never modify a deployed migration — create a new one
- Migration filenames must be sequential and descriptive
- Test migrations against a copy of production data before deploying
""",

    # ── API / Route layers ──
    "has_routes": """
API ROUTE RULES:
- All endpoints must be async (if framework supports it)
- Use typed request/response models (Pydantic v2, Zod, etc.) — never raw dicts
- Consistent error response format: {{"error": "message", "code": "ERROR_CODE", "detail": {{}}}}
- Never expose raw exception tracebacks in responses
- Authentication/authorization check on every non-public endpoint
- Rate limiting on write endpoints
- Input validation before any business logic
- Return appropriate HTTP status codes (201 for created, 404 for not found, etc.)
""",

    "has_schemas": """
SCHEMA RULES:
- Request schemas must validate all required fields with proper types
- Response schemas should not leak internal fields (internal IDs, passwords, etc.)
- Use schema inheritance to avoid duplication (CreateTask, UpdateTask, TaskResponse)
- DateTime fields should specify timezone handling
""",

    "has_middleware": """
MIDDLEWARE RULES:
- Auth middleware must run before any data-access middleware
- Error-handling middleware must be outermost (catches everything)
- Never modify request body in middleware — only headers/context
- Middleware must be stateless — no instance variables that persist between requests
- CORS configuration must be explicit — never allow-all in production
""",

    # ── Service / Business logic layers ──
    "has_services": """
SERVICE LAYER RULES:
- Services contain business logic — routes should be thin (validate → delegate → respond)
- Services receive dependencies via injection — never create DB connections internally
- Services must NOT import from the API/route layer (dependency flows downward only)
- ERROR ISOLATION: one bad item must NEVER kill a batch job — wrap per-item calls in try/except
- All external API calls must use retry with exponential backoff — never bare requests
- Audit log all state-changing operations (create, update, delete, approve, reject)
""",

    # ── Abstract interface layers ──
    "has_abstractions": """
ABSTRACTION LAYER RULES:
- Base class defines the COMPLETE interface — all methods, all return types
- Every concrete implementation MUST implement ALL methods — no partial implementations
- Never add a method to one implementation without updating the base class AND all others
- Data models crossing the abstraction boundary must be defined in the base module (dataclasses, not ORM models)
- Changes to the interface affect every consumer — flag anything that breaks the contract
- Integration tests must exist for each concrete implementation
""",

    # ── Config layers ──
    "has_config": """
CONFIGURATION RULES:
- All config from environment variables via a central config module
- Config module should validate required vars at startup — fail fast, not at runtime
- Sensitive values (API keys, DB passwords) must never appear in logs
- Default values for non-sensitive settings — no config = still works in dev
- DEPLOYMENT_MODE or equivalent env var controls behavior, not code branches
""",
}

# Layer-type heuristic rules (applied based on directory name patterns)
LAYER_TYPE_RULES = {
    "ingestion": """
INGESTION PIPELINE RULES:
- ERROR ISOLATION: one bad file/record must NEVER kill the entire job
- Every per-document/record call must be wrapped in try/except with logging
- Rate limiting on external API calls (use semaphores for concurrency limits)
- All external API calls use retry with exponential backoff — never bare sleep()
- Default mode should be incremental (process only changes) — not full re-processing
- Track ingestion state: which items succeeded, which failed, which were skipped
- Staging/validation step before committing to permanent storage
""",

    "hygiene": """
HYGIENE/CLEANUP ENGINE RULES:
- CRITICAL: engine is READ-ONLY until admin explicitly approves a proposed action
- All proposed changes go to a proposals/queue table with status 'pending'
- Write operations ONLY execute after status is 'approved'
- Every approval/rejection is audit-logged — never delete proposal records
- Stale detection thresholds must be configurable, not hardcoded
""",

    "graph": """
KNOWLEDGE GRAPH RULES:
- PROVENANCE IS MANDATORY: every node needs source reference, contributor, confidence, timestamp
- A node without provenance must raise an error — never silently persisted
- Use content hashing for deduplication — add_node() is always an upsert
- Raw content stays in the document store — graph stores summaries/metadata only
- Never hard-delete nodes — use tombstoning (source_deleted=True)
""",

    "knowledge": """
KNOWLEDGE EXTRACTION RULES:
- LLM calls: always instruct to return valid JSON only — no markdown fences, no prose
- Include confidence score (0.0-1.0) in every extracted item
- Extraction output must conform to defined schemas — validate before persisting
- When primary LLM is unavailable, fall back to secondary automatically
- Never re-fetch from source when re-processing — read from local storage
""",

    "cache": """
CACHE RULES:
- Cache invalidation MUST trigger when underlying data changes
- Never serve stale data after a write — invalidate-on-write, not TTL-only
- Cache keys must be deterministic and include version/tenant context
- Graceful degradation: if cache is down, fall back to source (slower, not broken)
""",

    "pii": """
PII SCANNING RULES:
- PII scan must run BEFORE ingesting content into any persistent store
- Raw document content must never appear in logs
- Configurable allowlist for domains/folders/patterns to exclude
- PII detection must be testable with known fixtures
""",

    "consent": """
CONSENT RULES:
- Consent records must be audit-logged with timestamps
- "decide later" / no response should be treated as opted OUT (conservative default)
- Consent can be revoked — revocation must cascade to downstream data
""",

    "generation": """
DOCUMENT GENERATION RULES:
- PII gate must run before publishing any generated content
- Publishing requires explicit review/approval — never auto-publish
- Generated content must reference source material (citations/provenance)
""",

    "workers": """
BACKGROUND WORKER RULES:
- Workers must be idempotent — safe to retry on failure
- Every job must have a timeout — no infinite hangs
- Dead letter queue for jobs that fail after max retries
- Worker health check endpoint for monitoring
- Graceful shutdown: finish current job before exiting
""",

    "frontend": """
FRONTEND RULES:
- Functional components only — no class components
- API calls go through the api/ client module — never inline fetch()
- Every data display must handle loading, error, and empty states
- Type all props and API responses — no `any` types
- User-facing errors should be human-readable, not raw error codes
""",

    "api": """
API SURFACE RULES:
- All endpoints async
- Typed request/response models — never raw dicts
- Consistent error format: {{"error": "...", "code": "...", "detail": {{}}}}
- Never expose stack traces in responses
- Check authorization on every non-public endpoint
""",

    "db": """
DATABASE LAYER RULES:
- All schema changes through migrations — never raw DDL in application code
- All tables include tenant_id if multi-tenant
- Session management centralized — never create sessions in business logic
- Indexes on foreign keys and commonly filtered columns
""",

    "infra": """
INFRASTRUCTURE RULES:
- docker-compose must include all required services with health checks
- Production config must have resource limits and restart policies
- All data volumes must be explicitly mounted (no anonymous volumes)
- No cloud vendor lock-in — must work with DEPLOYMENT_MODE env var
""",

    "tests": """
TEST SUITE RULES:
- Unit tests for every public function/method in business logic
- Integration tests for external service interactions
- Golden test fixtures with known expected outputs
- API tests must cover happy path + error cases + auth failures
- Each abstraction implementation needs its own integration test
""",
}


# ---------------------------------------------------------------------------
# Pattern-based rule generator (no LLM)
# ---------------------------------------------------------------------------
def generate_rules_from_patterns(layers, stack):
    """Generate rules for each layer based on detected patterns. No LLM needed."""
    all_rules = {}

    for key, layer in layers.items():
        rules_parts = [UNIVERSAL_RULES.strip()]

        # Apply pattern-based rules
        for pattern in layer.get("patterns", []):
            if pattern in RULE_TEMPLATES:
                rules_parts.append(RULE_TEMPLATES[pattern].strip())

        # Apply layer-type heuristic rules (match directory name)
        layer_name_lower = key.lower().replace("/", "_")
        for type_key, type_rules in LAYER_TYPE_RULES.items():
            if type_key in layer_name_lower:
                rules_parts.append(type_rules.strip())
                break
        else:
            # Check the last path component too
            last_part = key.split("/")[-1].lower()
            for type_key, type_rules in LAYER_TYPE_RULES.items():
                if type_key in last_part:
                    rules_parts.append(type_rules.strip())
                    break

        all_rules[key] = "\n\n".join(rules_parts)

    return all_rules


# ---------------------------------------------------------------------------
# Architecture-aware rule generator (uses reasoning model)
# ---------------------------------------------------------------------------
ARCH_RULES_PROMPT = """You are a senior principal engineer. You've been given an architecture document
and the auto-detected project structure. Your job is to generate SPECIFIC, ACTIONABLE review rules
for each layer — rules that a code reviewer would check against.

DO NOT generate generic advice. Every rule must be:
1. Testable — a reviewer can look at code and say yes/no
2. Specific to this layer's role in the architecture
3. About correctness, security, data integrity, or architectural boundaries

The auto-generated pattern rules are provided below. Your job is to ADD rules that are specific
to THIS project's architecture — things the pattern rules can't know. For example:
- Specific field requirements on data models
- Which layers can call which other layers
- Specific validation requirements
- Business logic constraints
- Security boundaries specific to this domain

Return ONLY valid JSON — no markdown, no commentary:

{{
  "layer_key": {{
    "additional_rules": [
      "specific rule 1",
      "specific rule 2"
    ],
    "cross_layer_contracts": [
      "this layer → that layer: description of contract"
    ]
  }}
}}

ARCHITECTURE DOCUMENT:
{arch_doc}

DETECTED STRUCTURE:
{structure}

EXISTING PATTERN RULES (already applied — don't repeat these):
{pattern_rules_summary}
"""


def generate_rules_from_architecture(engine, arch_doc_path, layers, stack, pattern_rules):
    """Use reasoning model to generate architecture-specific rules."""
    content, err = read_file(arch_doc_path)
    if err:
        print(f"  Cannot read arch doc: {err}")
        return {}

    # Build a compact summary of structure and existing rules
    structure = json.dumps({
        "stack": stack,
        "layers": {k: {"prefix": v["prefix"], "patterns": v.get("patterns", []),
                        "file_count": v.get("file_count", 0)}
                   for k, v in layers.items()},
    }, indent=2)

    # Summarize pattern rules (just the rule headings, not full text)
    summary_lines = []
    for key, rules in pattern_rules.items():
        lines = [l.strip("- ").strip() for l in rules.split("\n")
                 if l.strip().startswith("-")]
        summary_lines.append(f"{key}: {len(lines)} rules covering: "
                           f"{', '.join(lines[:3])}...")
    pattern_summary = "\n".join(summary_lines)

    prompt = ARCH_RULES_PROMPT.format(
        arch_doc=content[:15_000],  # truncate if huge
        structure=structure,
        pattern_rules_summary=pattern_summary,
    )

    try:
        response = engine.generate(prompt, role="reason", temperature=0.15)
        result = extract_json(response)
        if result:
            return result
    except Exception as e:
        print(f"  Architecture rule generation failed: {e}")

    return {}


# ---------------------------------------------------------------------------
# Combined: pattern + architecture rules
# ---------------------------------------------------------------------------
def build_all_rules(engine, project_info, use_llm=True):
    """Build complete rule set: pattern rules + optional architecture-aware rules."""
    layers = project_info["layers"]
    stack = project_info["stack"]

    print(f"\n  Generating pattern-based rules for {len(layers)} layers...")
    pattern_rules = generate_rules_from_patterns(layers, stack)

    for key, rules in pattern_rules.items():
        rule_count = len([l for l in rules.split("\n") if l.strip().startswith("-")])
        print(f"    {key:<30} {rule_count:>2} rules")

    # If we have an arch doc and LLM is available, augment with architecture rules
    arch_rules = {}
    if use_llm and project_info.get("arch_doc"):
        arch_path = os.path.join(project_info["root"], project_info["arch_doc"])
        print(f"\n  Reading architecture doc for additional rules...")
        arch_rules = generate_rules_from_architecture(
            engine, arch_path, layers, stack, pattern_rules
        )
        if arch_rules:
            n = sum(len(v.get("additional_rules", [])) for v in arch_rules.values())
            print(f"  Architecture doc contributed {n} additional rules.")

    # Merge
    combined = {}
    for key in pattern_rules:
        combined[key] = pattern_rules[key]
        if key in arch_rules:
            extras = arch_rules[key].get("additional_rules", [])
            if extras:
                combined[key] += "\n\nARCHITECTURE-SPECIFIC RULES:\n"
                combined[key] += "\n".join(f"- {r}" for r in extras)

    return combined, arch_rules


# ---------------------------------------------------------------------------
# Save/load rules to file
# ---------------------------------------------------------------------------
def save_rules(rules, path):
    """Save rules dict to a JSON file for reuse."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rules, f, indent=2)


def load_rules(path):
    """Load rules dict from a JSON file."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
