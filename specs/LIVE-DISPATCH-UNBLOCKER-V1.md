# LIVE-DISPATCH-UNBLOCKER-V1

**Date:** 2026-05-22
**Status:** Draft for review (sub-spec #2 of `TOOL-MAKING-ARC-V1`)
**Scope:** Bring `DispatchGate.evaluate()` onto the live tool-
  call dispatch path (`LiveExecutor` + `LiveIntegrationDispatcher`),
  add catalog metadata storage + lookup so the gate can consult
  per-tool classification, add Kernos's "scoped amortization"
  layer so repeated authorized calls don't burn cognitive load
  on the user, and emit structured **binding-failure diagnostic
  receipts** when a tool call references something the substrate
  can't resolve.
**Estimated size:** ~400 LOC source + ~250 LOC tests.

## Why this spec exists

Per `TOOL-MAKING-ARC-V1` D1: the live dispatch path
(`kernos/kernel/integration/live_wiring.py:31`) acknowledges
the full policy gate as "Batch 3 follow-up at both seams." Today
every live tool call routes through `classify_tool_effect` only —
the gate's model-driven `evaluate()` (which weighs covenants, recent
conversation, loss-cost, mode policy) does NOT fire. Agent-authored
tools (workspace) AND third-party MCP tools currently get less
gate scrutiny per-call than the architecture intends.

Per Kernos's architect input (2026-05-22, see
[[kernos-dispatch-gate-design-input]]):
- **Scoped amortization, not skip.** Evaluate always runs; the
  user-visible CONFIRM collapses when the substrate has a stable
  binding (same actor + tool hash + effect + scope + recent
  successful receipts).
- **Agent-authored = same scrutiny as stock.** Self-authorship
  is NOT a trust signal.
- **Failed bindings = first-class diagnostic receipts.** Not
  vibes. The diff between "tool not registered" vs. "registered
  but evicted" vs. "blocked by gate classification" vs. "renderer
  produced invalid action" matters.

## Design principles (load-bearing)

Two non-negotiables that shape every choice below:

1. **Layered design per [[agent-facing-natural-simplicity]].**
   Substrate layer keeps full sophistication — structured
   dataclasses, gate decisions, binding signatures, hashes,
   amortization-cache state. That's where operators inspect +
   the friction observer reads + audit lives. Agent-facing
   layer is natural English: prose responses, sentence-shaped
   error messages, prose covenants when relevant. The agent
   never sees status enums or JSON payloads from these
   facilities. Operator sees both; agent sees the sentence.
   The middle ground IS the design — don't collapse to a side.

2. **Scoped amortization per [[kernos-dispatch-gate-design-input]].**
   The gate ALWAYS runs evaluate on the live path. What
   amortizes is the user-visible interaction cost, not the
   safety check. When the substrate has a stable binding,
   CONFIRM collapses into "still inside the authorized
   envelope; proceed." Repeated authorized actions stop
   feeling like the system forgot. Gate evaluation is still
   happening — the agent just doesn't surface needless
   confirmations to the user.

## Current state

- `LiveExecutor.execute()` at `live_wiring.py:244-304`:
  classifies via `gate.classify_tool_effect()`, refuses on
  unknown, otherwise calls `execute_tool` directly. No
  `gate.evaluate()` invocation.
- `LiveIntegrationDispatcher.dispatch()` at `live_wiring.py:312+`:
  same pattern. Both seams skip evaluate.
- `DispatchGate.evaluate()` (`gate.py:354+`) is fully
  implemented — covenant query, model call, mode policy,
  approval token bypass, permission override, reactive
  soft-write bypass. Used by workflow/test paths only.
- `ToolCatalog` (`kernos/kernel/tool_catalog.py`) stores
  `name`, `description`, `source` on each entry; richer
  metadata (gate_classification, service_id, registration_hash)
  is set on workspace entries by `register_tool` but isn't a
  read API. Kernel + MCP entries don't carry it.
- Failed-binding diagnosis today: a bare error string from the
  dispatcher. Kernos's symptoms ("tool not found" for
  page_write; "tool 'request_space_action' not classified by
  the dispatch gate") have no structured surface.

## Design

### Phase A — Wire `gate.evaluate()` into the live path

Replace the shallow `classify_tool_effect`-only gate at
`LiveExecutor.execute()` with the full `gate.evaluate()`:

```python
async def execute(self, inputs):
    classification = self._gate.classify_tool_effect(
        inputs.tool_id, None, inputs.arguments,
    )
    if not classification or classification == "unknown":
        return _binding_failure_result(
            tool_id=inputs.tool_id,
            reason="unclassified",
            diagnostic=self._build_binding_diagnostic(
                inputs.tool_id, "unclassified",
            ),
        )

    # NEW: full policy gate
    gate_result = await self._gate.evaluate(
        tool_name=inputs.tool_id,
        tool_input=dict(inputs.arguments or {}),
        effect=classification,
        agent_reasoning=inputs.agent_reasoning or "",
        instance_id=inputs.instance_id,
        active_space_id=inputs.active_space_id or "",
        is_reactive=inputs.is_reactive,
        approval_token_id=inputs.approval_token_id or "",
        messages=inputs.recent_messages or [],
        user_message=inputs.user_message or "",
    )
    if not gate_result.allowed:
        return _gate_blocked_result(
            tool_id=inputs.tool_id, gate_result=gate_result,
        )
    # ... proceed to execute_tool
```

`ToolExecutionInputs` gains the fields the gate needs
(`agent_reasoning`, `is_reactive`, `approval_token_id`,
`recent_messages`, `user_message`). Callers in the live path
already have these — wire them through.

Same change to `LiveIntegrationDispatcher.dispatch()`. Both
seams now run the full gate.

### Phase B — Scoped amortization layer (Kernos input)

The amortization layer sits **inside** `DispatchGate.evaluate()`
as a new Step 3.5 (after permission override + reactive-soft_write
bypass, before model call). It consults a per-instance cache of
**authorization receipts** keyed on a **binding signature**:

```python
binding_signature = (
    instance_id,
    actor_member_id,
    tool_name,
    tool_hash,             # registration_hash for workspace, name for kernel
    effect_classification, # read / soft_write / hard_write
    scope_key,             # active_space_id or "global"
)
```

If a recent successful evaluation exists for this binding
(within TTL, default 60min, env-configurable via
`KERNOS_GATE_AMORTIZATION_TTL_SEC`), gate returns
`allowed=True, reason="amortized", method="amortization"` without
invoking the model. The CONFIRM-surface burden collapses while
the safety check remains.

The cache is in-memory on `DispatchGate` (no DB write per call
— call-rate could be high). Invalidated by:
- TTL expiry
- Covenant rule mutation (any add/update of a rule affecting
  the tool's effect class → cache wipe for that effect class)
- Mode policy swap (`set_mode_policy` clears the whole cache)
- Approval token issued for a different binding (no impact)

When the model is consulted (cache miss / non-cacheable
effect): on `allowed=True` outcome, write a cache entry with
TTL. On `allowed=False`, no cache write (don't amortize denial;
operator may reverse it next turn).

**Non-amortizable classifications**: `hard_write` never
amortizes — every hard-write call gets full evaluation per
its semantics (the operator's confirmation IS the per-call
event). `external_agent_read` amortizes after first approval
within the TTL (network egress to the same target stays
authorized for the window).

**Cache size**: bounded LRU at 256 entries per instance.
Eviction by LRU when full. Kernos's "stable binding" intent
holds within this window; rarely-used tools naturally drop out.

### Phase C — Failed-binding diagnostics: layered surface

New module: `kernos/kernel/dispatch_diagnostics.py`.

Per [[agent-facing-natural-simplicity]] — two layers, distinct
audiences:

**Substrate layer (operator-facing)**: structured diagnostic
captured per failure. This is what the operator inspects, what
the friction observer + event stream consume, what /dump can
show. Sophistication intact — Kernos's diagnostic shape preserved
here:

```python
@dataclass(frozen=True)
class BindingFailureDiagnostic:
    tool_id: str
    status: Literal[
        "not_registered", "registered_but_inactive",
        "registered_but_evicted", "blocked_by_gate_classification",
        "blocked_by_service_disable", "blocked_by_covenant",
        "renderer_produced_invalid_action",
    ]
    expected_source: str
    gate_class: str
    last_registration_hash: str
    reason_omitted: str

def build_binding_diagnostic(
    *, tool_id: str, gate, catalog, registry,
) -> BindingFailureDiagnostic: ...
```

Emits `tool.binding_failure` event with this payload. Operator
sees the structured form via `/dump` + friction reports + the
event stream.

**Agent-facing layer (natural prose)**: the dispatcher's
returned error is a short English sentence the agent can read
+ relay or work around naturally. The substrate composes the
sentence from the diagnostic so the agent never touches the
structured form. Concrete shapes per failure mode:

- `not_registered` → *"That tool isn't registered here."*
- `registered_but_evicted` → *"That tool exists but wasn't loaded this turn. Restate what you need and I can try again."*
- `blocked_by_gate_classification` → *"I can't act on that — it isn't classified for safe dispatch yet."*
- `blocked_by_service_disable` → *"That capability is connected to a service that's currently disabled. Ask the operator to enable it if needed."*
- `blocked_by_covenant` → *"A standing rule prevents that action: {rule_text}."* (covenant text inlined naturally, not as a status code)
- `renderer_produced_invalid_action` → *"I called something that doesn't exist as a tool — let me try a different approach."*

The agent never sees `status="registered_but_evicted"`; it sees
the sentence. The operator never loses the structured
attribution — `/dump` + friction reports carry the full
diagnostic. Both audiences get what they need at the right
level of richness.

**Why this matters:** Kernos's symptoms (page_write missing,
request_space_action unknown) need substrate-level structure
for diagnosis AND a natural agent-facing response so the
agent stays elegant in conversation. The layered design serves
both without forcing the agent to translate JSON in its
output.

### Phase D — Catalog metadata reads

`ToolCatalog.get_metadata(name)` returns a dict of the
inline-presence fields Kernos called critical:
```python
{
    "name": "page_write",
    "source": "stock",
    "gate_classification": "soft_write",
    "registration_status": "active",
    "registration_hash": "abc123",  # if available
}
```

The gate consults this for the per-tool gate_classification
when present (more authoritative than `classify_tool_effect`'s
hardcoded branches for kernel-tool overrides). Catalog entries
that don't carry classification fall back to
`classify_tool_effect`'s existing logic.

**Inline catalog presence**: the existing tool catalog text
that the agent sees (built by `build_catalog_text` /
`build_tool_directory`) stays unchanged in v1 — Kernos called
out "presence shouldn't feel like reading a build log." The
deeper inspection surface lands in
`TOOL-INTROSPECTION-V1` (next sub-spec).

## Acceptance criteria

### Phase A: live path runs evaluate

| AC | Description |
|---|---|
| AC1 | `LiveExecutor.execute()` calls `gate.evaluate()` for every classified tool call (not just `classify_tool_effect`). |
| AC2 | `LiveIntegrationDispatcher.dispatch()` calls `gate.evaluate()` on the same flow. |
| AC3 | When `evaluate()` returns `allowed=False` (CONFIRM/CONFLICT/CLARIFY), live dispatch refuses + surfaces the gate's reason. |
| AC4 | When `evaluate()` returns `allowed=True`, live dispatch proceeds to `execute_tool`. |
| AC5 | `ToolExecutionInputs` carries the new fields needed by evaluate (`agent_reasoning`, `is_reactive`, `approval_token_id`, `recent_messages`, `user_message`). |

### Phase B: amortization layer

| AC | Description |
|---|---|
| AC6 | First call for a (actor, tool_hash, effect, scope) binding hits the model; second call within TTL returns `allowed=True, reason="amortized"` without a model call. |
| AC7 | Cache entry expires after `KERNOS_GATE_AMORTIZATION_TTL_SEC` (default 3600s). |
| AC8 | `hard_write` calls never amortize — every call hits the model regardless of cache state. |
| AC9 | `external_agent_read` amortizes within TTL after first approval. |
| AC10 | Covenant rule mutation clears the cache for the affected effect class. |
| AC11 | `set_mode_policy` swap clears the whole cache. |
| AC12 | Cache is bounded LRU at 256 entries per instance; eviction works under pressure. |

### Phase C: failed-binding diagnostic

| AC | Description |
|---|---|
| AC13 | Unclassified tool dispatch returns a `BindingFailureDiagnostic` with `status="blocked_by_gate_classification"`. |
| AC14 | Tool name not in catalog returns diagnostic with `status="not_registered"`. |
| AC15 | Tool blocked by service-disable returns diagnostic with `status="blocked_by_service_disable"`. |
| AC16 | Diagnostic includes a source-aware `suggestion` field. |
| AC17 | `tool.binding_failure` event fires on every binding failure with the diagnostic payload. |

### Phase D: catalog metadata

| AC | Description |
|---|---|
| AC18 | `ToolCatalog.get_metadata(name)` returns the documented field shape. |
| AC19 | When a catalog entry carries `gate_classification`, the gate uses that classification (overrides hardcoded branches). |
| AC20 | Catalog entries without classification fall back to `classify_tool_effect`'s existing logic. |
| AC21 | No regression on existing dispatch / gate / surfacing tests. |

## Soak gate

1. **Automated**: ACs pin every phase. Particularly important:
   the amortization layer's cache-invalidation paths
   (covenant mutation, mode swap, TTL).
2. **Operator soak**: per `[[cognition-migration-soak-gate]]` —
   this IS a cognition-path migration (the gate model now fires
   on every live tool call). Required before flipping
   production-default:
   - Run a canvas-test conversation: agent calls `canvas_create`
     (hard_write, never amortizes — gate fires), then 3
     consecutive `page_write` calls (soft_write, first hits gate,
     second + third hit amortization cache).
   - Verify: gate event log shows 1 fresh evaluate + 2
     `method="amortization"` decisions.
   - Verify: user-visible interaction count matches what Kernos
     described — "the system is always checking, but only
     interrupts when the check found something the user would
     actually care about."
3. **Failed-binding soak**: hand-trigger each binding failure
   mode (rename a tool mid-session, disable a service mid-session,
   issue an unclassified tool call). Verify diagnostic shape +
   event emission.

## Out of scope

- **TOOL-INTROSPECTION-V1** ships the agent-facing
  inspection surface (`inspect_state` extensions, "why was X
  surfaced" queries). This spec ships the substrate metadata;
  introspection lands next.
- **TOOL-AUDIT-NORMALIZATION-V1** ships the canonical
  `ToolInvocationAuditEntry` per dispatch + dedupes audit
  entries. Depends on this spec's metadata-storage but separate
  shippable.
- **Per-member amortization scope** — v1 keys on (instance,
  actor, tool, effect, scope). Per-member overrides reserved
  until member-relationship work needs them.
- **Persistent amortization cache** — v1 is in-memory.
  Restart re-evaluates the first call per binding. Acceptable
  per Kernos's "stable binding" framing (warm-up cost is
  bounded).

## Risks

- **Risk:** Bringing evaluate onto the live path slows every
  tool call by one cheap LLM call (~200-500ms typical).
  - **Mitigation:** Amortization (Phase B) collapses the
    repeated-call cost. First call per binding pays the model;
    subsequent calls within TTL are cache hits (microseconds).
    The CONFIRM-surface burden Kernos called out is the more
    important UX cost; latency is secondary.

- **Risk:** Amortization cache invalidation could miss a
  covenant-mutation case (e.g., rule with empty capability
  that affects all effect classes).
  - **Mitigation:** Conservative: any covenant mutation clears
    the WHOLE cache, not just the matched effect class.
    Cheap (re-warm is one model call per binding) and avoids
    correctness drift.

- **Risk:** The agent sees gate refusals it didn't see before
  (CONFIRM that used to be skipped). Could feel like a
  regression in agent agency.
  - **Mitigation:** Posture mode (POSTURE-EVALUATION-MODES-V1)
    governs the bias. `permissive` (the default) returns
    `proceed` on ambiguous model responses — most repeat-action
    cases land here. Plus amortization handles the
    repeat-call concern. Operators who want pre-V1 behavior
    can set `KERNOS_GATE_MODE=balanced` (only on `confirm` the
    model EXPLICITLY says confirm, otherwise proceed).

- **Risk:** Binding-failure diagnostic adds output bloat to
  every dispatch error.
  - **Mitigation:** Diagnostic only fires on FAILURES, not
    happy-path. The verbose shape lands where the diagnosis
    matters (agent already had to recover); happy-path is
    untouched.

## Dependencies

- `POSTURE-EVALUATION-MODES-V1` (commit `4e3458b`) — landed.
  Provides the mode policy this spec's evaluate consults.
- `TOOL-REGISTRATION-AUTHORIZATION-V1` (commit `27f0352`) —
  landed. The catalog metadata read consumes the
  `registration_hash` + classification this spec uses for
  amortization-binding signatures.
- `DURABLE-APPROVAL-RECEIPTS-V1` (commit `96f4582`) — landed.
  Already integrated; amortization layer is separate from
  receipts (in-memory cache vs. durable approval).
- Per Kernos's input ([[kernos-dispatch-gate-design-input]]):
  load-bearing for the spec shape.

## Migration

- **Schema**: no new tables. Catalog metadata is in-process
  (already on workspace entries; extended to kernel + MCP via
  the `get_metadata` API).
- **Live path**: behavior change is significant — every live
  tool call now runs evaluate. Mitigated by:
  - Posture mode default = `permissive` (ambiguous → proceed)
  - Amortization cache (repeat calls free)
  - Existing approval token + permission override fast paths
    short-circuit before model call (already in evaluate)
- **Operator visibility**: gate INFO log lines + new
  `method="amortization"` / `method="model_check"` distinction
  let operators audit gate behavior pre/post-flip.

## Phased rollout

Recommend shipping phases A + B + C together; D's read API
helper is small and lands with the same change. The whole
spec is one batch. Codex review post-impl is warranted given
the dispatch-path surface change.

If Codex round 1 raises a substantive concern about one phase,
split: ship Phase A solo (full evaluate on live, no
amortization) + immediate follow-up `LIVE-DISPATCH-AMORTIZATION-V1`.
Same for diagnostic if it grows in scope.
