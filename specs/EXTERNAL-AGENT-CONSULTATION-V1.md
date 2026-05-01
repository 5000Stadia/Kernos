# EXTERNAL-AGENT-CONSULTATION v1

**Status:** Draft for founder approval before implementation. Codex
deliberated D1–D7 architectural decisions during drafting (3 AGREE,
3 REVISE, 1 NEW reentrancy-policy seam). All folded.

**Substrate position:** rides on shipped CRB main v1, Drafter v2,
WLP, STS, WDP, MODEL-AND-STATUS-V1. Refactors existing
`kernos/kernel/builders/` into a unified `external_agents/` module
with `builders/` preserved as a compatibility facade so the existing
`KERNOS_BUILDER` env var keeps working unchanged.

## Why

Kernos's primary agent has no native way to reach external
coding-agent CLIs (Claude Code, Codex, Gemini, Aider) for
consultation, second-opinion review, or exploratory thinking.
Builders exist as an env-var-gated single-backend choice for
`code_exec`, but:

1. The agent can't choose backend per call.
2. There's no consultation mode — only task-execution.
3. Aider has been shipped untested for months because it's gated
   behind one env var nobody flips.
4. Gemini is missing entirely.

Founder direction (2026-04-30): "build it into Kernos" — this is a
first-class capability, not a lab tool. The spec generalizes the
shipped builder pattern into a single external-agent primitive
covering both task-execution (existing builders role) and
consultation (new role) for all four CLIs.

## Scope

### Ships in v1

* **Refactored module** at `kernos/kernel/external_agents/` with:
  * `harness.py` — `Harness` protocol with `build()` and
    `consult()` methods.
  * `registry.py` — `HarnessRegistry`.
  * `subprocess_substrate.py` — shared spawn/capture/scope/audit
    plumbing.
  * `harnesses/claude_code.py`, `codex.py`, `gemini.py`,
    `aider.py`, `native.py` — per-harness implementations.
  * `consultation_log.py` — durable consultation audit substrate.
  * `errors.py`.
  * `tool.py` — agent-facing `consult` tool.
* **`builders/` preserved as compatibility facade** (Codex D1
  refinement). `BuilderBackend`, `BuildResult`, `VALID_BUILDERS`,
  `get_builder()` re-export from new module. The `KERNOS_BUILDER`
  env var keeps working; `code_exec` calls remain unchanged.
* **Single `consult` agent tool** with `harness` parameter +
  `harness_options` dict (Codex D2 refinement).
* **Per-call backend choice for `code_exec`** — optional `backend`
  param. Default still `KERNOS_BUILDER` env var.
* **`consultation_log` table** keyed by `(instance_id, session_id)`
  with full audit shape (question, response, context,
  metadata_json, workspace_dir, timeout_seconds, truncated, etc.).
* **Kernos-owned opaque `session_id`** mapping internally to each
  CLI's native session mechanism (Codex D3 revision). No
  unscoped `codex resume --last`. Audit row records both Kernos
  `session_id` and native session ref.
* **Workspace-dir resolution** with config-driven default,
  detected-repo-root fallback, optional path allowlist (Codex D4
  revision).
* **Dispatch-gate classification `external-agent-read`** (Codex D5
  revision) — distinct from plain `read` because consultation
  discloses sensitive context AND triggers paid stateful
  subprocesses. New service `external_agent_consultation` with
  `consult` + `build` operations.
* **Reentrancy / context policy** (Codex D7 NEW) — consultation
  allowed during conversational + drafting + review paths; blocked
  during trigger evaluation, CRB approval/dispatch, and any
  recursive consult-within-consult path. Configurable depth guard.
* **Aider validation** — first integration tests for Aider land
  here; the existing implementation gets exercised end-to-end if
  Aider is installed (currently not on this system; tests skip
  with explicit marker rather than fail).
* **Agent-facing documentation** — template integration so the
  primary agent knows when and how to use these features.

### Defers to v1.x

* MCP-server transport for harnesses that support it (Claude Code's
  `claude mcp`, etc.). Subprocess transport ships v1; MCP swap
  is per-harness and earns its keep when streaming/structured-
  response benefits surface.
* ACP transport for harnesses with ACP modes. Same reasoning.
* `KERNOS-MCP-SERVER-V1` (Kernos exposing its own tools as MCP) —
  separate spec, complementary direction (consultation-IN vs
  consultation-OUT).
* Async / streaming consultation. v1 is sync subprocess; v1.x can
  add fire-and-receipt for long deliberations.
* Budget enforcement beyond per-call timeout (e.g., per-instance
  daily token cap with hard limit). v1 ships timeouts +
  optional daily call-count cap; full token budgeting deferred.

## Architectural decisions (D1–D7)

### D1 — Refactor `builders/` into unified `external_agents/`

The existing `builders/` module IS the harness-registry pattern,
shipped but partial. v1 extracts shared substrate (subprocess +
capture + scope + audit) into the new module; `builders/` becomes a
**compatibility facade** that re-exports the new types. `KERNOS_BUILDER`
keeps working; `code_exec` keeps working; existing operator workflows
unchanged.

### D2 — Single `consult` tool with `harness_options`

```yaml
consult:
  harness: claude_code | codex | gemini    # NOT aider — Aider's CLI is task-shaped, not Q&A-shaped; consult enum rejects aider with HarnessUnavailable
  question: str
  context: dict | str = ""           # optional plumbing
  session_id: str | None = None       # Kernos-owned opaque id; sanitized to hex-encoded SHA on the wire (see Session-id sanitization)
  workspace_dir: str | None = None    # falls back to instance config
  timeout_seconds: int = 600          # max 1800
  harness_options: dict = {}          # harness-specific knobs validated by registry
returns:
  response: str
  harness: str
  session_id: str
  metadata: dict   # tokens, duration_ms, exit_status, native_session_ref, truncated
```

**Aider note:** Aider participates in the build mode (see `code_exec`
backend choice) but NOT the consult mode. Calling `consult(harness="aider", ...)`
raises `HarnessUnavailable` with a clear message. The harness enum
above is build-vs-consult-aware; the registry exposes `consult_harnesses`
and `build_harnesses` separately.

### Session-id sanitization (Codex spec-review fold #7)

Agent-supplied `session_id` flows into filesystem paths under
`data/<instance>/consultations/<session_id>/`. Without sanitization
this is a path-injection vector. v1 sanitizes:

1. Trim whitespace, lowercase.
2. Reject if empty post-trim.
3. Hex-encode SHA-256 of the trimmed value; use the hex string as
   the on-disk session-id.
4. Audit row records BOTH the agent-supplied raw id (in
   `metadata_json`) and the sanitized hex (in `session_id`
   column). Triage queries can match either.

Maximum filesystem-path component is the 64-char hex; bounded.

Adding a new harness (cursor / opencode / kimi) is registry registration,
not a new tool descriptor. `harness_options` validated by the registry
per harness so harness-specific knobs don't force new tools.

### D3 — Kernos-owned opaque session_id

The agent picks an opaque string when threading a multi-call
consultation. The harness implementation maps it deterministically to
the CLI's native session mechanism:

* **Claude Code:** `--session-id <sanitized_hex_id>`. Direct map.
* **Codex:** Kernos uses `codex exec --thread <sanitized_hex_id>` /
  `codex exec resume <sanitized_hex_id>` per Codex CLI's session
  model. Kernos owns the id; Codex maps it to its own native
  session reference internally. Never uses `--last` (unscoped).
  Native session ref captured from `codex` output and recorded in
  `consultation_log.native_session_ref` for triage. Kick-back
  trigger #2 covers Codex CLI session-model surprises.
* **Gemini:** Gemini CLI's session model varies across releases.
  v1 starts from a "rebuild context per call" posture: Kernos
  persists conversation history per `session_id` in
  `data/<instance>/consultations/<sanitized_hex_id>/gemini.jsonl`
  and replays prior turns as part of the prompt on subsequent
  calls. If a stable Gemini CLI session-id flag is available, the
  harness uses it preferentially; otherwise prompt-replay is the
  fallback.
* **Aider:** N/A for v1 — consult mode not implemented.

Audit row records BOTH the Kernos `session_id` AND the harness's
native session reference for triage.

### D4 — Workspace-dir resolution

Resolution order:
1. Per-call `workspace_dir` param if supplied.
2. Per-instance config field `external_agents.default_workspace_dir`
   (added to instance.db settings).
3. Detected repo root (search for `.git` from `os.getcwd()` upward).
4. Process startup cwd as last-resort fallback.

All resolved paths canonicalized via `Path.resolve()`. Optional
allowlist enforcement: if `external_agents.workspace_allowlist` is
configured (list of paths), reject calls whose resolved path is
not under any allowlisted prefix. Default unlimited (no allowlist).

### D5 — Dispatch-gate classification `external-agent-read`

Distinct from plain `read` because:
* Consultation discloses Kernos's repo + state context to an
  external process.
* Triggers paid, stateful subprocesses.
* Per-call timeout + per-instance daily call cap configurable.

The gate is enforceable at the existing dispatch boundary in
`kernos/kernel/gate.py`. New gate value `external-agent-read` added
to the classification enum; the `consult` and `code_exec` (when
backend != native) tools both classify under it.

### D6 — `consultation_log` table

```sql
CREATE TABLE IF NOT EXISTS consultation_log (
    consultation_id      TEXT PRIMARY KEY,    -- uuid hex
    instance_id          TEXT NOT NULL,
    member_id            TEXT NOT NULL,
    harness              TEXT NOT NULL,
    session_id           TEXT,                 -- Kernos-owned opaque (sanitized hex)
    native_session_ref   TEXT,                 -- harness CLI's native session id/path
    question             TEXT NOT NULL,
    response             TEXT NOT NULL DEFAULT '',
    context              TEXT,                 -- caller-supplied context, JSON
    metadata_json        TEXT,                 -- tokens, duration, raw_session_id, etc.
    workspace_dir        TEXT,
    timeout_seconds      INTEGER NOT NULL,
    truncated            INTEGER NOT NULL DEFAULT 0,
    status               TEXT NOT NULL DEFAULT 'pending',  -- pending | succeeded | failed | timed_out
    started_at           TEXT NOT NULL,
    ended_at             TEXT,
    exit_status          INTEGER,
    error                TEXT,
    CHECK (harness IN ('claude_code', 'codex', 'gemini', 'aider')),
    CHECK (status IN ('pending', 'succeeded', 'failed', 'timed_out'))
);
CREATE INDEX IF NOT EXISTS idx_consultation_log_member
    ON consultation_log (instance_id, member_id, started_at);
CREATE INDEX IF NOT EXISTS idx_consultation_log_session
    ON consultation_log (session_id) WHERE session_id IS NOT NULL;
```

`response` is TEXT (SQLite handles MB-scale fine for v1). Truncation
applies when output exceeds 1MB (configurable); `truncated=1` flags
clipped responses.

### D7 — Reentrancy / context policy (NEW)

Codex pin: consultations CAN be called recursively (CC-from-Kernos
might internally call back into Kernos's MCP if/when that ships;
multi-step deliberations might consult more than once). Without
guardrails this fans out into recursive agent invocation, latency
spikes, and authority escalation through unaudited paths.

**v1 policy (defaults):**

| Calling context | Consultation allowed? | Depth limit |
|---|---|---|
| Conversational turn (user message reply) | Yes | 2 |
| Compaction / fact-harvest pipeline | No | 0 |
| Drafter cohort flow | Yes | 1 |
| CRB approval / dispatch | No | 0 |
| Trigger evaluation runtime | No | 0 |
| Workflow execution (WLP) | No | 0 |
| Recovery sweep | No | 0 |

**Enforcement: `contextvars.ContextVar`** (NOT thread-local —
asyncio uses `contextvars` for per-task isolation; thread-locals
leak across awaits). The `ConsultationContext` ContextVar carries
the calling-path label + current depth; `consult` tool reads it via
`get()` and rejects when current path is in the no-allow list OR
depth ≥ limit. Each calling-path entry uses
`var.set()` (and stores the token to reset on exit) so concurrent
async tasks have isolated context. Codex spec-review fold #2.

**Test pin (AC6 strengthened):** two concurrent async tasks
running through different paths see independent context;
allowlisted-path task succeeds while blocked-path task raises,
even when they share an event loop.

The blocked contexts are exactly the ones with strict latency,
deterministic-replay, or authority-escalation concerns. Allowed
contexts are the agent-driven flows where consultation is the
intended primitive.

## Public API

```python
# kernos/kernel/external_agents/harness.py

class Harness(Protocol):
    """One implementation per CLI. Lifecycle:
    1. registry.discover() probes the CLI binary on PATH.
    2. registry.get(name).consult(...) or .build(...) at call time.
    """

    name: str  # "claude_code" | "codex" | "gemini" | "aider"

    async def health_check(self) -> "HarnessHealth":
        """Probe binary presence + auth status. Idempotent + fast."""

    async def consult(
        self, *, question: str, context: dict | str,
        session_id: str | None,
        workspace_dir: Path,
        timeout_seconds: int,
        harness_options: dict,
    ) -> "ConsultResult": ...

    async def build(
        self, *, task: str, workspace_dir: Path,
        timeout_seconds: int,
        harness_options: dict,
    ) -> "BuildResult":
        """Optional. Aider implements; native + others vary."""
```

```python
# kernos/kernel/external_agents/registry.py

class HarnessRegistry:
    def discover(self) -> dict[str, "HarnessHealth"]:
        """Probe all registered harnesses. Returns
        per-harness installed/auth/version status."""

    def get(self, name: str) -> Harness: ...
    def list_available(self) -> list[str]: ...
```

```python
# kernos/kernel/external_agents/tool.py — agent-facing surface

# Tool descriptor in kernos/kernel/manifests/external_agents.json
# implements `consult` operation; service-bound to
# external_agent_consultation; classification external-agent-read.
```

## Reentrancy enforcement

```python
# kernos/kernel/external_agents/reentrancy.py

class ConsultationContext:
    """ContextVar-based depth + path tracker (asyncio-safe; per-task
    isolation — NOT thread-local).

    set(path: str)  — entered a calling context (e.g. "trigger_eval")
    enter()         — entering a consultation; raises if blocked
    exit()          — leaving a consultation
    """

# Calling-path sentinels set explicitly by:
# - kernos/messages/handler.py (conversational turn)
# - kernos/kernel/compaction.py (compaction)
# - kernos/kernel/cohorts/drafter/* (drafter)
# - kernos/kernel/crb/approval/flow.py (CRB)
# - kernos/kernel/triggers/runtime.py (trigger eval — when triggers spec lands)
# - kernos/kernel/workflows/execution_engine.py (WLP)
```

## Composition with shipped substrate

* **Service registry** — new `external_agent_consultation` service.
  Two operations: `consult` + `build`. `consult` tool service-bound;
  `code_exec` extended with optional `backend` param under same
  service (or kept on its current service with new auth, TBD in C5).
* **Authority** — `consult` authority required for the consult tool;
  `build` authority required for `code_exec` when `backend != "native"`.
* **Dispatch gate** — new classification `external-agent-read`.
* **Audit (action_log)** — every consultation and non-native build
  records a row in `consultation_log`. The existing
  `cohort_action_log` substrate is NOT reused (different key model
  + different lifecycle).
* **Receipt pattern** — `consultation.completed` event emitted on
  successful return; `consultation.failed` on error.
* **Compatibility facade** — `kernos/kernel/builders/__init__.py`
  re-exports the EXACT name-set existing callers import. Audited
  imports (Codex spec-review fold #2):
  * `kernos/kernel/code_exec.py` imports `UnknownBuilderError`,
    `get_builder`, `BuilderBackend`, `BuildResult`.
  * `kernos/kernel/setup/workspace_config.py` imports `BUILDER_TIER`,
    `VALID_BUILDERS`.
  * `kernos/kernel/builders/__init__.py` itself currently exports
    `BuilderBackend`, `BuildResult`, `VALID_BUILDERS`, `get_builder`.
  Facade re-exports ALL of: `BuilderBackend`, `BuildResult`,
  `VALID_BUILDERS`, `BUILDER_TIER`, `UnknownBuilderError`,
  `get_builder`. Test pin (AC9) verifies each import path.
  `KERNOS_BUILDER` env var fully preserved.

* **Tool descriptor + gate classification** — `external-agent-read`
  is added to the `GateClassification` enum at
  `kernos/kernel/tool_descriptor.py:69-81` (currently four values).
  `kernos/kernel/gate.py` extends to route the new classification
  to the same authority gate as other read-class operations PLUS
  the budget/timeout pre-checks specific to consultation.

## Acceptance criteria

1. **AC1** — `Harness` protocol defined with `consult()` and
   optional `build()`. Four implementations: `claude_code`,
   `codex`, `gemini`, `aider`. Registry discovers + reports health
   per harness.
2. **AC2** — `consult` agent tool returns the harness's response
   for live-installed CLIs (claude_code, codex, gemini on this
   system). Returns structured error when CLI is missing
   (aider on this system) — no crash, clear surface.
3. **AC3** — Session continuity per harness: a `session_id`
   threaded across two `consult` calls produces a coherent reply
   (the second call sees the first's context). Test pin: live
   for claude_code (the most-tested CLI for sessions); structural
   for codex (asserts the same Kernos session_id is passed to
   `codex exec --thread <id>` on first call and `codex exec resume <id>`
   on the second; the captured native_session_ref is unchanged
   across calls, confirming continuity); structural for gemini
   (asserts conversation-history file exists + is replayed).
4. **AC4** — Workspace-dir resolution honors per-call override →
   instance config → repo-root detection → cwd. Allowlist
   enforcement rejects out-of-allowlist paths when configured.
5. **AC5** — Dispatch-gate classification `external-agent-read`
   blocks the `consult` tool when the calling member lacks
   authority; permits when present. Test pin: existing gate test
   pattern.
6. **AC6** — Reentrancy guard blocks `consult` from blocked
   contexts (trigger eval, CRB dispatch, WLP execution,
   compaction, recovery sweep); permits from allowed contexts
   (conversational, drafter). Depth guard rejects
   consult-within-consult past configured limit (default 2).
7. **AC7** — `consultation_log` table populated with full audit
   row per call. Truncation flag set when response exceeds
   configured cap. Schema enforces harness CHECK constraint.
8. **AC8** — `code_exec` tool gains optional `backend` param;
   default `KERNOS_BUILDER` env var preserved. Test pin: existing
   `code_exec` tests pass unchanged; new test for explicit
   `backend="native"` and `backend="aider"` (latter skipped if
   aider not installed).
9. **AC9** — `kernos/kernel/builders/` continues exporting the
   exact name set callers depend on:
   `BuilderBackend`, `BuildResult`, `VALID_BUILDERS`,
   `BUILDER_TIER`, `UnknownBuilderError`, `get_builder`. Test
   pin: structural import test that imports each name and
   verifies it's a re-export from `kernos/kernel/external_agents/`
   (not a divergent shadow definition).
10. **AC10** — Live test sweep: real subprocess invocations to
    `claude --print` and `codex exec` succeed end-to-end with a
    test prompt; response captured; consultation_log populated.
    Gemini live test executes if `gemini` is installed; skipped
    otherwise with marker.
11. **AC11** — Agent template integration: primary agent's
    operating-principles template includes a section on external
    agent consultation (when to use, how to invoke, costs, gates).
12. **AC12** — Capability description for `external_agent_consultation`
    service includes a clear when-to-use / when-not-to-use rubric
    that the primary agent surfaces during decision-making.
13. **AC13** — No regression on shipped substrate. Existing
    `code_exec` / `KERNOS_BUILDER` flows untouched. Builder tests
    pass unchanged.
14. **AC14** — Documentation: a new doc file at
    `docs/EXTERNAL-AGENTS.md` describes the primitive, the four
    harnesses, the consult tool, the reentrancy policy, and the
    audit surface. Linked from `docs/TECHNICAL-ARCHITECTURE.md`.
15. **AC15** — Codex native-session capture/resume verified live:
    first call to `codex exec --thread <id>` records a
    `native_session_ref` in `consultation_log`; second call with
    the same Kernos `session_id` resumes via Codex's session
    machinery and the native_session_ref is unchanged.
16. **AC16** — Failed and timeout consultations populate
    `consultation_log` with the correct status. Failed
    consultations (subprocess exits non-zero) write
    `status="failed"` with non-zero `exit_status` and `error`
    text. Timed-out consultations (subprocess exceeds
    `timeout_seconds`) write `status="timed_out"` and raise
    `ConsultationTimeout`. Both rows are queryable post-failure
    by the same `consultation_log` API.
17. **AC17** — Concurrent-async isolation: two tasks running
    through different calling paths via `asyncio.gather` see
    independent ContextVar state; allowlisted-path task succeeds
    while blocked-path task raises; neither leaks context to
    the other.
18. **AC18** — Aider in consult mode raises `HarnessUnavailable`
    with a clear message; aider in build mode (when installed)
    routes through the existing aider harness via the facade.
    On this system Aider is NOT installed: the build-mode test
    asserts `HarnessUnavailable` with "binary not on PATH".
19. **AC19** — Session-id sanitization: agent-supplied raw
    session_id with path-injection characters
    (e.g. `"../../etc/passwd"`) hashes to a hex string that's safe
    as a filesystem-path component; the on-disk path stays inside
    the consultations directory; raw id preserved in
    `metadata_json` for triage.

## Tests

### Unit

* Harness protocol round-trips (mock subprocess return).
* Registry discovery returns expected health per harness.
* Workspace-dir resolution + allowlist enforcement.
* Reentrancy sentinel: enter/exit in nested contexts; depth limit.
* `consultation_log` mutators: round-trip; truncation flag set
  correctly when response exceeds cap; schema CHECK rejection.

### Integration (live-CLI tests with skip markers)

* `claude` end-to-end: spawn `claude --print "Q"`, capture
  response, store in consultation_log, verify timing + structured
  return shape.
* `codex` end-to-end: same shape via `codex exec`.
* `gemini` end-to-end if installed.
* `aider` test marked-skip with explicit reason (binary not on
  PATH); structural test verifies error surface for missing CLI.

### Reentrancy + gate

* Conversational turn → consult succeeds.
* Trigger evaluation → consult raises ReentrancyBlocked.
* CRB approval flow → consult raises ReentrancyBlocked.
* Recursive consult-within-consult past depth limit raises
  DepthExceeded.
* Member without `consult` authority → AuthorityDenied at gate.

### No-regression

* Existing `code_exec` tests pass unchanged with default `native`
  backend.
* `KERNOS_BUILDER=aider` env var path still resolves Aider
  builder via facade.
* Full repo test suite green.

## Error class hierarchy

```python
# kernos/kernel/external_agents/errors.py

class ExternalAgentError(Exception):
    """Base for external_agents module."""

class HarnessUnavailable(ExternalAgentError):
    """CLI binary not on PATH or auth missing."""

class ConsultationTimeout(ExternalAgentError):
    """Subprocess exceeded timeout_seconds; killed."""

class ConsultationFailed(ExternalAgentError):
    """Subprocess exited non-zero; stderr captured."""

class WorkspaceNotAllowed(ExternalAgentError):
    """Resolved workspace_dir not in configured allowlist."""

class ReentrancyBlocked(ExternalAgentError):
    """Consultation attempted from a blocked calling context."""

class DepthExceeded(ExternalAgentError):
    """Consult-within-consult exceeded configured depth limit."""

class HarnessRegistrationError(ExternalAgentError):
    """Registry couldn't construct a harness (bad options)."""
```

## Commit strategy

Six commits. C1 establishes shared substrate before harnesses (same
posture as WORKFLOW-TRIGGERS C1-first constraint).

* **C1 — Shared substrate + Harness protocol + registry skeleton.**
  New `kernos/kernel/external_agents/` module. `Harness` protocol,
  `HarnessRegistry`, `subprocess_substrate.py` shared
  spawn/capture/scope plumbing, `consultation_log` table + mutators,
  `errors.py`, reentrancy sentinel. NO harness implementations
  yet. NO agent tool yet. Tests: protocol round-trip, registry
  discovery shell, log mutators, reentrancy sentinel.
* **C2 — Per-harness implementations: claude_code + codex + gemini.**
  Subprocess shape + session-id mapping + workspace-dir resolution
  + harness_options validation per harness. Live integration tests
  for each (with skip markers when CLI not installed). Codex
  `--last` deliberately not used; explicit Kernos-supplied id
  passed to `codex exec --thread` / `codex exec resume`.
* **C3 — Aider harness + builders/ compatibility facade.**
  Port the existing `kernos/kernel/builders/aider.py` logic into
  `external_agents/harnesses/aider.py` (build mode only;
  consultation not implemented for aider since its CLI is task-
  shaped). `builders/` becomes facade re-exporting the new types.
  Existing `code_exec` + `KERNOS_BUILDER` env var unchanged. Test
  pin: structural import test + existing builder tests pass.
* **C4 — Reentrancy guard + agent-facing `consult` tool + service
  binding.** ContextVar-based reentrancy guard; path-sentinels
  added at each calling-context entry (handler, compaction,
  drafter, CRB, WLP, recovery). Tool descriptor; service
  registration; dispatch-gate classification `external-agent-read`
  added to `tool_descriptor.GateClassification` enum and
  `gate.py` routing. `consult` tool calls registry → harness, but
  ONLY after the reentrancy guard accepts. Reentrancy enforcement
  ships in the same commit as the tool surface so the tool is
  never live without its guard. Tests: tool dispatch round-trip;
  gate denial without authority; gate accept with authority;
  every blocked context raises ReentrancyBlocked; every allowed
  context succeeds; depth guard pin; concurrent-task ContextVar
  isolation.
* **C5 — Per-call backend choice for `code_exec`.** `code_exec`
  extended with optional `backend` param routing through the new
  registry (default to `KERNOS_BUILDER` env var preserved). Tests:
  existing `code_exec` flows pass unchanged; explicit `backend="native"`
  and `backend="aider"` (the latter asserts `HarnessUnavailable`
  on this system) both honored.
* **C6 — Documentation + agent template integration + live test
  sweep + no-regression.** `docs/EXTERNAL-AGENTS.md` written.
  `kernos/kernel/template.py` extended with consultation
  awareness in operating principles. Capability description
  includes when-to-use rubric. Live test sweep against installed
  CLIs (claude + codex + gemini) executed in CI / local; aider
  skipped. Full repo test suite green.

Codex review confer:
* Mid-batch after C2 (subprocess shape + session model locked).
* Final after C6.
* Confirmation pass on the fold (per CC contract).

## Kick-back triggers

Implementer escalates to architect when:

1. **Claude Code's `--session-id` doesn't behave as expected.** v1
   assumes `claude --print --session-id <uuid>` resumes prior
   context. If the CLI rejects unknown ids or doesn't actually
   thread, the session model needs revision before C2 ships.
2. **Codex session-id approach incompatible with `codex exec`.**
   v1 assumes `codex exec --thread <kernos_id>` accepts an
   opaque caller-supplied id and that subsequent
   `codex exec resume <kernos_id>` resumes that thread. If Codex
   requires a CLI-issued id format or rejects unknown ids on
   first call, the session model needs rework. v1 falls back to
   "no session continuity for codex" if necessary — structural
   support stays; the resume path becomes a no-op until a
   session-id approach lands.
3. **Gemini session model unclear.** Gemini CLI may not have a
   standard session-id; v1 might need to rebuild context as part
   of the prompt on each call. Surface before C2 if so.
4. **Reentrancy guard breaks an existing flow.** If a shipped
   pipeline (e.g., compaction) actually NEEDS to consult — surface
   for architect call on whether to allowlist that path.
5. **Schema migration on `code_exec`'s service binding can't
   accept new authority cleanly.** Surface before C4.
6. **Aider's existing implementation can't be moved without
   breaking its sandbox-scope wrapping.** The sitecustomize-based
   scope mechanism is delicate; if relocating the file breaks it,
   the move needs a different shape.
7. **Codex CLI session-model surprises.** v1 assumes
   `codex exec --thread <id>` and `codex exec resume <id>` work
   for opaque Kernos-supplied ids. If Codex requires a specific
   id format (UUIDv4, etc.) or rejects unknown ids on first call,
   the session model needs revision before C2 closes. v1 falls
   back to "no session continuity for codex" if necessary —
   structural support stays; only the resume path becomes a
   no-op.
8. **Native session-id capture failure.** v1 captures Codex /
   Gemini / etc. native session refs from CLI stdout/stderr for
   `consultation_log.native_session_ref`. If a CLI doesn't expose
   its native ref in output (parser regex returns no match), the
   field stays NULL and we log a warning. Hard kick-back only if
   the CLI rejects requests without a session ref — in which case
   v1 needs a different harness model.
9. **`GateClassification` enum extension breaks shipped tools.**
   v1 adds `external-agent-read` to the enum; if the existing
   four-value enum is used in serialized form anywhere (catalog
   cards, persisted descriptors), the addition can't be additive
   and a migration is needed before C4.

## Out of scope

* MCP-server transport for harnesses with MCP server modes.
* ACP transport.
* `KERNOS-MCP-SERVER-V1` (Kernos exposing its own tools as MCP).
* Async / streaming consultation.
* Token-budget enforcement beyond per-call timeout.
* User-facing consultation UI.
* Cross-instance consultation sharing.

## Documentation requirements

The primary agent must understand consultation as a first-class
capability. Three documentation surfaces:

* **`docs/EXTERNAL-AGENTS.md`** — operator-facing reference.
  Lists harnesses, what each is good at, costs, gates, audit
  surface. Linked from `docs/TECHNICAL-ARCHITECTURE.md`.
* **Capability description** for the `external_agent_consultation`
  service — agent-facing one-liner that surfaces in the catalog
  card during decision-making. Includes when-to-use rubric.
* **Template integration** in `kernos/kernel/template.py` — adds
  a brief section on consultation under operating principles, so
  the agent knows the tool exists and when to reach for it
  without re-reading the catalog every turn.

When-to-use rubric (drafted; refined during C6):

| Use consultation for | Don't use consultation for |
|---|---|
| Code review / second opinion on a non-trivial change | Simple code lookups (use repo search) |
| Architectural sanity check before a big spec | Routine bug fixes (just fix it) |
| "Have I missed an edge case?" double-check | User-facing answers (Kernos answers directly) |
| Exploratory design space mapping | Tasks that need Kernos's persistent memory |
| Cross-checking a Codex / CC implementation | Lookup-style queries |

## References

* Framing context: founder direction 2026-04-30 ("build it into
  Kernos"; harness pattern; CC + Codex + Gemini + Aider; matches
  the consultation-out direction discussed in this conversation).
* Substrate composed against:
  * `kernos/kernel/builders/` (existing module — refactored into
    `external_agents/`; preserved as compatibility facade)
  * `kernos/kernel/code_exec.py` (extended for per-call backend
    in C5)
  * `kernos/kernel/cohorts/_substrate/action_log.py` (Drafter v2
    pattern — informs but does not unify with consultation_log)
  * `kernos/kernel/gate.py` (extended for new
    `external-agent-read` classification in C4)
  * `kernos/kernel/template.py` (extended for agent
    operating-principles awareness in C6)
* Shipped specs: `MODEL-AND-STATUS-V1.md`,
  `WORKFLOW-TRIGGERS-CONSOLIDATION-V1.md` (in flight at Kit's
  inbox).

## Codex deliberation outcomes (D1–D7)

* **D1 AGREE + refinement** — refactor builders/ into
  external_agents/; preserve builders/ as compatibility facade.
  Folded.
* **D2 AGREE + refinement** — single tool with `harness_options`
  dict for harness-specific knobs validated by registry. Folded.
* **D3 REVISE** — Kernos-owned opaque session_id mapped internally
  to each CLI's native mechanism. Audit row records both. No
  unscoped `codex resume --last`. Folded.
* **D4 REVISE** — workspace_dir defaults from per-instance config
  → repo root → cwd. Optional allowlist. Per-call canonicalize.
  Folded.
* **D5 REVISE** — dispatch-gate `external-agent-read` (not plain
  `read`) reflecting context-disclosure + paid-subprocess
  semantics. Folded.
* **D6 AGREE + additions** — dedicated `consultation_log` table
  with `context`, `metadata_json`, `workspace_dir`,
  `timeout_seconds`, `truncated` columns. Folded.
* **D7 NEW** — reentrancy/context policy: blocked from trigger
  eval, CRB dispatch, WLP execution, compaction, recovery; allowed
  from conversational + drafter; depth limit configurable. Folded
  as a load-bearing v1 constraint.
