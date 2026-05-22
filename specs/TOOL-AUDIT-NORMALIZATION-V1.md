# TOOL-AUDIT-NORMALIZATION-V1

**Date:** 2026-05-22
**Status:** Draft for review (sub-spec #3 of `TOOL-MAKING-ARC-V1`)
**Scope:** Replace the current dual-audit emission pattern with
  exactly one canonical `ToolInvocationAuditEntry` per dispatch
  attempt, constructed at the dispatch entry point (the live
  integration dispatcher) and populated from catalog metadata
  via `LIVE-DISPATCH-UNBLOCKER-V1`'s `get_metadata()`. Downstream
  paths reference the canonical entry's id rather than emitting
  their own audit. Event-stream `tool.called` / `tool.result`
  events stay unchanged.
**Estimated size:** ~150 LOC source + ~120 LOC tests.

## Why this spec exists

Per `TOOL-MAKING-ARC-V1` D5 + Codex r1#6 / r2#3: audit-log
entries and event-stream events are DIFFERENT things, and the
audit-log invariant today is broken:

- **Service-bound dispatch** (workspace tools with `service_id`)
  emits a rich `ToolInvocationAuditEntry` via `_execute_service_bound_tool`.
- **`LiveIntegrationDispatcher`** emits its OWN simpler audit
  shape via `_emit_audit` (`live_wiring.py:442` for failed,
  `live_wiring.py:467` for succeeded) — `{type, instance_id,
  tool_id, classification, escalated}`.
- Service-bound calls flowing through both paths produce TWO
  audit entries describing the same dispatch.
- Live-path-only calls (non-service-bound: kernel tools, MCP,
  unbound workspace tools) produce an audit entry MISSING
  audit_category + operation context — the catalog metadata
  never reached the live-path audit constructor.

The architectural target: one canonical, catalog-metadata-aware
audit entry per dispatch.

## Design principles (load-bearing)

Per [[agent-facing-natural-simplicity]]: audit is an
**operator-facing** facility. The agent does NOT consume audit
entries. The canonical audit entry stays fully structured
(dataclass, hashes, normalized categories) because operators,
compliance reviews, and per-tool cost aggregators need it. No
agent-facing prose layer is required for audit — agents do not
interact with audit at all.

This is "layered design without an agent layer" — the right
choice for facilities that are purely substrate-internal.

## Current state

- `ToolInvocationAuditEntry` (`kernos/kernel/tool_audit.py:111`)
  is the canonical shape. Carries: `type, timestamp, instance_id,
  member_id, space_id, tool_name, operation, service_id,
  authority, audit_category, normalized_category, payload_digest,
  success, error`. Already rich enough for the v1 invariant.
- `build_audit_entry()` constructor exists.
- `LiveIntegrationDispatcher._emit_audit` writes a
  legacy-shaped dict — both at `tool_call_failed` and
  `tool_call_succeeded` exits.
- Service-bound dispatch (`_execute_service_bound_tool` in
  `workspace.py`) builds + emits its own `ToolInvocationAuditEntry`.
- `tool.called` / `tool.result` events emit from `_emit_event`
  on the same dispatcher — these stay unchanged.

## Design

### One audit entry per dispatch — constructed at the entry point

The dispatch entry point is `LiveIntegrationDispatcher.dispatch()`
(and `LiveExecutor.execute()` for the principal-model loop). At
both seams:

1. **Construct the canonical entry up front**: at the start of
   dispatch, build a partial `ToolInvocationAuditEntry` with
   `success=False, error=""` (to be set on completion). Generate
   an `entry_id` (UUID hex).
2. **Populate from catalog metadata** via
   `catalog.get_metadata(tool_name)` (the API
   `LIVE-DISPATCH-UNBLOCKER-V1` Phase D ships): `service_id`,
   `audit_category`, `normalized_category`, `operation` (when the
   tool declares per-operation classifications).
3. **Pass `entry_id` through the dispatch chain**: extend
   `ToolExecutionInputs` (or the dispatcher's positional
   `inputs` shape) with `audit_entry_id: str` — downstream
   handlers consult it to know "an entry already exists for this
   dispatch; don't construct a second one."
4. **On completion (success or failure)**: finalize the
   canonical entry with `success`, `error`, then write to the
   audit store. Single emit.

### Downstream paths suppress their own audit

`_execute_service_bound_tool` (and any other path that
currently constructs its own `ToolInvocationAuditEntry`) checks
the inputs / context for `audit_entry_id`. If present, that
path:
- Skips its `build_audit_entry()` + `audit_store.log()` calls.
- Returns enough context for the caller to finalize the
  canonical entry (success/error/payload — the inner path
  already knows these).

Pattern: each downstream path returns a small structured
`ServiceDispatchOutcome` instead of writing the audit itself.
The outer canonical-entry owner reads the outcome + writes the
single audit.

### Events stay unchanged

`tool.called` and `tool.result` events keep their current shape
+ emission cadence — once per dispatch. UI surfaces, trace
consumers, and cost aggregators that depend on them continue
working. The events carry the `audit_entry_id` so a consumer
that wants the full audit detail can join.

### Legacy `_emit_audit` retirement

`LiveIntegrationDispatcher._emit_audit` (and the
`tool_call_failed` / `tool_call_succeeded` legacy shapes it
wrote) are removed once the canonical-entry path is wired.
Operators depending on the legacy shape (none known) get the
canonical shape, which is a strict superset.

If a legacy consumer is discovered post-flip, the canonical
entry can be projected back to the legacy shape via a small
shim — no audit data is lost in the migration.

### Audit storage

The existing audit-log writer (`audit_store.log()`) is
unchanged — it already accepts a `ToolInvocationAuditEntry`
shape. Just one entry per dispatch instead of two.

## Acceptance criteria

| AC | Description |
|---|---|
| AC1 | A successful service-bound dispatch produces exactly ONE audit entry (not two). |
| AC2 | A successful non-service-bound live-path dispatch (kernel tool) produces exactly ONE audit entry. |
| AC3 | A failed dispatch produces exactly ONE audit entry with `success=False` + `error` populated. |
| AC4 | The canonical audit entry carries `audit_category` populated from catalog metadata (via `get_metadata()`). |
| AC5 | The canonical audit entry carries `normalized_category` populated. |
| AC6 | The canonical audit entry carries `service_id` for service-bound tools, empty string for kernel/internal tools. |
| AC7 | The canonical audit entry carries `operation` when the tool declares per-operation classifications. |
| AC8 | The canonical audit entry carries `payload_digest` (SHA-256 hex of input). |
| AC9 | `tool.called` event still fires once per dispatch (shape preserved). |
| AC10 | `tool.result` event still fires once per dispatch (shape preserved). |
| AC11 | `tool.called` + `tool.result` events include `audit_entry_id` for join queries. |
| AC12 | Legacy `tool_call_failed` / `tool_call_succeeded` audit-log shapes are NO LONGER emitted. |
| AC13 | `_execute_service_bound_tool` (and any other downstream audit emitter) detects `audit_entry_id` in context and skips its own audit emission. |
| AC14 | `ToolExecutionInputs` / dispatcher positional `inputs` shape carries `audit_entry_id`. |
| AC15 | No regressions on `tests/test_workspace_service_dispatch.py` audit assertions. |
| AC16 | No regressions on `tests/test_live_wiring*.py` event-emission assertions. |

## Soak gate

1. **Automated**: ACs pin one-entry-per-dispatch + event
   preservation + downstream-path suppression.
2. **Operator soak**: run a turn that invokes 4 distinct tools
   (1 kernel read, 1 kernel soft_write, 1 workspace
   service-bound, 1 MCP). Verify:
   - Audit log: exactly 4 entries (one per dispatch).
   - Each entry carries the catalog-derived metadata
     (audit_category, normalized_category, service_id where
     applicable, operation where applicable).
   - Event stream: 4 `tool.called` + 4 `tool.result` (8 total)
     — unchanged from today's count.
3. **Failure soak**: trigger a dispatch failure. Verify single
   audit entry with `success=False` + matching `tool.called`
   + no `tool.result` (or `tool.result` with error flag,
   matching today's shape).

## Out of scope

- Audit log schema change → no, the existing
  `ToolInvocationAuditEntry` shape is preserved as-is. Spec
  only changes WHO emits and WHEN.
- Audit retention policy → unchanged (existing audit_store
  behavior).
- Per-member audit visibility → unchanged.
- Cost aggregation re-implementation → existing aggregator
  uses event stream (`tool.result`); not impacted.

## Risks

- **Risk:** A downstream path that constructs its own audit
  is missed in the audit-suppression pass, producing duplicate
  entries.
  - **Mitigation:** Grep + audit at impl time for every call
    site of `build_audit_entry` outside the dispatch entry
    point. Each non-entry-point site must accept
    `audit_entry_id` and short-circuit when present.
    Convert the legacy emit_audit dict path to error-on-call
    (assertion) once wired, so any forgotten site fails loud
    in tests rather than silently double-emitting.

- **Risk:** `ToolExecutionInputs` field addition ripples
  through more callers than expected.
  - **Mitigation:** Default `audit_entry_id=""` preserves
    every existing caller. Only the two dispatch entry points
    (LiveExecutor + LiveIntegrationDispatcher) populate it.
    Tests that construct `ToolExecutionInputs` for unit
    coverage continue working unchanged.

- **Risk:** Operators depending on `tool_call_failed` /
  `tool_call_succeeded` audit-log shapes break.
  - **Mitigation:** No known consumers today. If discovered
    post-flip, project canonical entry → legacy shape via
    shim. Documented in spec migration section.

## Dependencies

- `LIVE-DISPATCH-UNBLOCKER-V1` — provides `catalog.get_metadata()`
  used to populate the canonical entry's `audit_category` /
  `normalized_category` / `service_id` / `operation` fields.
  Must ship first.
- `WORKSHOP-EXTERNAL-SERVICE-PRIMITIVE` (landed) — provides the
  service-bound dispatch path that today emits its own audit;
  this spec rewires it to suppress when canonical entry exists.

## Migration

- **Schema**: no schema change. `ToolInvocationAuditEntry` is
  unchanged.
- **Audit log**: pre-spec log entries (legacy shape OR rich
  shape) continue working — readers handle both. Post-spec, only
  the rich canonical shape is written.
- **Event stream**: no change. `tool.called` + `tool.result`
  continue with their current shape, plus a new `audit_entry_id`
  field (additive — existing consumers ignore unknown fields).
