# CODING-SESSION-BRIDGE-V1 — Implementation Review Document

**Status:** IMPLEMENTED — code shipped on branch `coding-session-bridge-v1`,
commit `6a20f58`. Awaiting Codex post-implementation review +
architect's close ratification.

**Architect-ratified spec source:** Notion `35cffafef4db8152b3dad07092eaf142`
(renamed from CODING-CONSULT-V1; IA review folded; event-emission
revision folded with CC's three substrate observations).

**Roadmap context:** Spec 2 of the five-spec autonomy-loop arc
(Notion `35cffafef4db81c0b855cb0984dcd8df`). Spec 1
(FRICTION-PATTERN-STABLE-IDS-V1) shipped to `main` as `452dbee`.

**Composes with:** RESPONSE-FIDELITY-V1 (every consultation produces
ActionStateRecord); EXTERNAL-AGENT-CONSULTATION-V1 (distinct
operational shape; see disambiguation below); future workflow
primitive (the response-received event becomes a workflow gate
release signal).

## Scope of this spec

Two new kernel tools that let Kernos talk to **already-running** coding
sessions (Claude Code, Codex) via a file-based bridge directory. Not to
be confused with the existing `consult` tool, which spawns a *fresh* CLI
subprocess per call.

| Surface | Classification | Returns |
|---|---|---|
| `ask_coding_session(target, question, context)` | `soft_write` | `(summary, ActionStateRecord)` — `attempted` state, `request_id` in `receipt_refs` |
| `read_coding_session_response(request_id)` | `read` | `(summary, ActionStateRecord)` — `completed` / `attempted` / `failed` |

Use case: when Kernos is confused about substrate behavior or wants to
verify a claim about current code, write a request to the bridge; the
operator (or v2 watcher) relays it to the already-running coding
session; the coding tool investigates and writes its response back;
Kernos reads.

## Disambiguation: relation to existing `consult` tool

Kernos already ships a `consult` tool under EXTERNAL-AGENT-CONSULTATION-V1
that spawns a fresh CLI subprocess per call (claude_code, codex, gemini,
aider harnesses), blocks until the CLI returns, and audits via
`ConsultationLog`.

**This spec ships a different operational shape:**

- **Existing `consult`** — spawn subprocess; block; one-shot answer from
  a fresh harness; bounded by subprocess lifetime.
- **New `ask_coding_session`** — write request to bridge directory that
  an *already-running* CC/Codex session reads; operator pastes into
  session; response writes back to bridge; Kernos reads. Asynchronous
  by design.

Both tools coexist. Agents pick by use case: spawn-fresh-subprocess →
`consult`; ask-running-session → `ask_coding_session`.

## Bridge directory layout

Per instance:

```
data/<safe(instance_id)>/coding_session_bridge/
├── requests/
│   └── {request_id}.json
└── responses/
    ├── {request_id}.json
    └── {request_id}.emitted    # sentinel; presence = event already fired
```

Request files and response files use atomic tempfile + `os.rename` so
partial writes never surface to the relayer or the polling Kernos
agent. The sentinel file uses the same atomicity for idempotent event
emission.

## Message formats

### Request body (`requests/{request_id}.json`)

```javascript
{
  "request_id": "<uuid hex>",
  "timestamp": "<iso8601>",
  "target": "claude_code" | "codex",
  "originating_kernos_instance": "<instance-id>",
  "originating_space": "<space-id>",
  "originating_member_id": "<member-id>",
  "question": "<prose>",
  "context": {
    "suspected_paths": [...],
    "related_conversation": "...",
    "prior_decisions": [...]
  }
}
```

`originating_member_id` carries multi-member routing readiness now to
avoid schema migration later (per CC's IA observation 3). Single-member
v1 deployments treat it as harmless metadata.

### Response body (`responses/{request_id}.json`)

```javascript
{
  "request_id": "<uuid same as request>",
  "timestamp": "<iso8601>",
  "target": "<which tool answered>",
  "findings": "<prose>",
  "source_references": [
    {"path": "<file>", "line_range": "<start-end>", "relevance": "<prose>"}
  ],
  "caveats": "<prose>",
  "investigation_outcome": "completed" | "partial" | "unable_to_investigate"
}
```

## ActionStateRecord shape

Both tools follow the `note_this` pattern from RESPONSE-FIDELITY-V1
Batch 1.2: handler returns `tuple[str, ActionStateRecord]`; the
caller (`reasoning.execute_tool`) appends the record to
`self._turn_action_records`. The integration runner folds them into
the briefing's AuditTrace at finalize time.

- `ask_coding_session` → `operation_class="mutate"`,
  `execution_state="attempted"`, `receipt_refs=(request_id,)`,
  `surface="coding_session_bridge"`.
- `read_coding_session_response` → `operation_class="read"`,
  `execution_state` is `completed` / `attempted` / `failed`,
  `receipt_refs=(request_id,)` whenever the request exists.

`pending` is NOT used (it's not in `ACTION_EXECUTION_STATES` per
IA Finding B); the in-flight read returns `attempted` consistent
with the ask record's initial state.

## Event emission contract

When `read_coding_session_response` detects a fresh response,
`_emit_response_received_once` fires one event:

- `event_type` = `coding_consult.response_received`
- `correlation_id` = `request_id` (literally; no prefix or
  transformation, so workflow `ApprovalGate` predicates filtering on
  `payload.request_id` work without unwrapping — per CC's IA
  observation 2)
- `payload` carries `request_id`, `originating_kernos_instance`,
  `originating_member_id`, `target`, `investigation_outcome`

Idempotency: a sentinel file `responses/{request_id}.emitted` is
written via `os.rename` atomicity after the first emission. Subsequent
reads see the sentinel and skip re-emission (per CC's IA observation 1).

Emission path: prefers the optional `emit_event` callable passed by
the caller; falls back to module-level `event_stream.emit` so the
event never silently disappears even when no callable is injected
(mirrors the FRICTION-PATTERN-STABLE-IDS-V1 lifecycle-event fallback
pattern).

## Path scope discipline (tool-internal validation)

No substrate-level filesystem gate primitive exists yet (per IA
Finding C). Path scope is enforced at the tool boundary:

- `instance_id` sanitized via `_safe_name` (existing helper that
  blocks path traversal; comes from `kernos.utils`).
- `request_id` validated via `_safe_request_id`:
  - Regex `^[A-Za-z0-9_\-]+$`
  - Explicit `..` guard (defense in depth)
  - Rejects on empty input
- No other caller-supplied value reaches the path construction.

A future spec can promote this to a substrate primitive (per-tool path
allowlist enforced at the gate) if multiple tools need it.

## Timeout

`KERNOS_FRICTION_CODING_SESSION_BRIDGE_TIMEOUT_SECONDS` env var (default
3600 = 1 hour). `read_coding_session_response` reads the request's
`timestamp` field and compares to `now`; past the threshold returns
`failed` with a timeout reason.

The request file itself is **not** cleaned up on timeout. v1 leaves it
on disk for audit; a follow-up GC pass can be added if disk pressure
shows up.

## Wiring

- `kernos/kernel/reasoning.py:_KERNEL_TOOLS` — both tool names added.
- `kernos/kernel/reasoning.py:_KERNEL_TOOL_PATHS` — both
  `frozenset({"confirmed"})`.
- `kernos/kernel/reasoning.py:execute_tool` — elif dispatch added; record
  appended to `self._turn_action_records` (same shape as `note_this`).
- `kernos/kernel/kernel_tool_registry.py` — imports + adds both
  schemas to the surfacer's list so agents see them in the catalog.

## What ships in code

- NEW: `kernos/kernel/coding_session_bridge.py` (~450 LOC) — schemas,
  handlers, idempotent event emission, path-scope helpers.
- MODIFIED: `kernos/kernel/reasoning.py` — dispatch wiring.
- MODIFIED: `kernos/kernel/kernel_tool_registry.py` — surfacer
  registration.
- NEW: `tests/test_coding_session_bridge.py` (20 tests).

## What does NOT ship

- Stale-request GC (timed-out request files persist for audit).
- Watcher / awareness whisper (polling is the v1 pattern;
  event-stream watcher and awareness whisper are v2 migration paths
  documented in the spec but not built).
- Workflow integration (`ApprovalGate` consumption of the
  response-received event lands when the self-improvement workflow
  definition ships in Spec 4).
- Substrate-level path scoping primitive (deferred to its own spec
  if/when multiple tools need it).

## Test categories (20 tests, all green)

**Round-trip (3):** ask writes request file + returns attempted; read
returns completed with findings; ActionStateRecord chain preserves
request_id (provenance).

**Async-state (5):** read before response yields `attempted`, not
`pending`; polling has no side effects (no response file, no
sentinel); polling then response yields `completed`; unknown
request_id yields `failed`; timeout (env-tunable) yields `failed`.

**Tool-implementation scope (5):** invalid target rejected; empty
question rejected; six path-traversal variants rejected at the tool
boundary; `_safe_request_id` accepts valid shapes only; bridge
directory is per-instance with cross-instance isolation verified.

**Event emission (3):** emitted once on first response read with full
payload shape; sentinel prevents re-emit on repeated reads;
`event_stream` fallback uses `correlation_id = request_id` literally.

**Tool schemas (3):** required-fields sanity, enum sanity, name sanity.

## Architectural pushback prompts for Codex

Four points worth Codex's substrate-side perspective:

1. **No stale-request GC.** Timed-out request files persist forever on
   disk. Acceptable for v1 (audit-friendly; cheap), but worth flagging
   if disk pressure becomes a concern.

2. **Emit-callable injection vs `event_stream` fallback duality.**
   `read_coding_session_response` defaults the `emit_event` callable to
   `None` and falls back to module-level emit. The runtime dispatch path
   doesn't construct/inject a callable; production wiring relies on the
   fallback. Either shape is reasonable; flagging the design choice for
   consistency with the FRICTION-PATTERN-STABLE-IDS-V1 pattern.

3. **Sentinel content choice.** The sentinel marker is content-bearing
   (the current `utc_now()` is written into it for debugging). Could be
   zero-byte. Either shape is fine; flagging.

4. **Race between concurrent reads.** Two coroutines reading the same
   `request_id` simultaneously could both see "no sentinel" and both
   emit before either's atomic rename lands. Worst case: event fires
   twice. Acceptable for v1 since downstream consumers should be
   idempotent anyway. Flagging if any consumer can't tolerate dupes.

## Seven verification points (for Codex)

Match implementation against architect-ratified spec:

1. **Tool schemas** — target enum is `claude_code`/`codex`; required
   fields are `target`+`question` for ask, `request_id` for read;
   descriptions disambiguate from existing `consult`.

2. **Bridge directory** — per-instance, requests + responses
   subdirectories; all writes are atomic tempfile + `os.rename`.

3. **ActionStateRecord shape** — ask is mutate-class with state
   `attempted` and `request_id` in `receipt_refs`; read is read-class
   with state `completed` / `attempted` / `failed` matching the spec
   table.

4. **Event emission** — fires exactly once per response arrival via
   sentinel-marker; payload carries the five required fields;
   `correlation_id` equals `request_id` literally; falls back to
   `event_stream.emit` when no callable is supplied.

5. **Path scope** — instance_id sanitized via `_safe_name`; request_id
   validated via `_safe_request_id` (regex + `..` guard) before any
   filesystem operation; six unsafe variants pinned by tests.

6. **Timeout** — env-tunable via
   `KERNOS_CODING_SESSION_BRIDGE_TIMEOUT_SECONDS` (default 3600);
   compared from the request's `timestamp` field.

7. **Dispatch** — both names in `_KERNEL_TOOLS` and `_KERNEL_TOOL_PATHS`
   as confirmed-only; elif branch in `execute_tool` appends the record
   to `self._turn_action_records`; the kernel-tool-registry imports
   and surfaces both schemas.

## Sequence

1. ✅ Architect-ratified spec at Notion `35cffafef4db8152b3dad07092eaf142`.
2. ✅ CC implements per spec on branch `coding-session-bridge-v1`.
3. 🟡 **Codex post-implementation review** — pending.
4. CC folds any findings.
5. Architect ratifies on close.
6. CC merges branch to `main`.

## Linked artifacts

- Architect-ratified spec: Notion `35cffafef4db8152b3dad07092eaf142`
- IA substrate review (folded): Notion `35cffafef4db8192b4c3fb53ecea4853`
- Event-emission revision (folded): Notion `35cffafef4db8101bebffe32e8b43e74`
- Five-spec roadmap: Notion `35cffafef4db81c0b855cb0984dcd8df`
- CC implementation ping: Notion `35dffafef4db819796c8e25d18e277da`
- Spec 1 close ratification (preceding spec on main): Notion
  `35dffafef4db818c982af6d7e69c5948`
- Live commit: `6a20f58` on branch `coding-session-bridge-v1` from `452dbee` on `main`
