# REQUEST-APPROVAL-ACTION-V1

**Date:** 2026-05-28 (v2 — Codex r1 fold)
**Status:** Draft for round-2 Codex review
**Origin:** DEFERRED #94 from DURABLE-APPROVAL-RECEIPTS-V1 batch
**Scope:** Workflow-engine action verb that wraps the existing
  `approval_receipts.request_approval()` surface and folds onto
  the engine's existing `gate_ref` + `approval_gates` mechanism.
  Workflow YAML declares `action_type: request_approval` with a
  `gate_ref`; engine pauses; resumes on
  `approval.decision_recorded` event; decision surfaces through
  a durable storage contract for downstream refs.
**Estimated size:** ~250 LOC source + ~300 LOC tests.

## What v2 changes from v1 (Codex r1 fold)

v1 of this spec assumed a new action class would create its
own gate semantics. Codex r1 verdict YELLOW — six blocking
contract mismatches with the shipped engine. v2 folds onto
existing primitives:

1. **Event name fix.** `approval.approved` /
   `approval.rejected` do not exist. Receipts emit
   `approval.decision_recorded` with
   `payload.decision in {"approved", "rejected", "expired"}`.
2. **Race-proof resume rule.** `_on_post_flush_for_gates()`
   does NOT buffer events before `_await_gate()` installs a
   waiter. Fix: on `_await_gate()` entry for a receipt-backed
   gate, query the receipt row by `(workflow_execution_id,
   gate_nonce)` first. If terminal, synthesize the gate
   payload and advance. Only if pending, install the waiter.
3. **Descriptor shape.** Use the existing `gate_ref` +
   `approval_gates` block. Don't invent a new gate concept.
   Use current YAML vocabulary: `action_type`, `parameters`,
   `{...}` refs, `branch_on_true`, `branch_on_false`.
4. **`gate_nonce` injection.** Don't extend `CohortContext`
   (frozen + built once before the action loop). The engine
   already exposes `{workflow.gate_nonce}` as a ref pattern;
   the action's `parameters` resolves it. No context surgery.
5. **Storage contract.** Extend the persisted step-output
   envelope with an optional `approval_outcome` field, written
   atomically in the same transaction that clears the gate
   nonce and advances the cursor. Ref-resolver loads after
   restart.
6. **Validation surfaces.** Add `request_approval` to
   `KNOWN_ACTION_TYPES`, `is_irreversible` (world-effect, safe-
   deny on timeout), `ACTION_OPERATION_CLASS_BY_VERB`, and the
   production `ActionLibrary` bring-up.

Plus SHOULDs folded:
- `decision` first-class field in `approval_outcome`.
- Workflow gate release calls `consume_approval()` on
  `single_use=True` receipts (lean per Codex).
- AC18 uses `RefResolutionError` (parameter context) or
  no-match (predicate context); not a literal sentinel.
- `binding_payload` must be JSON-serializable mapping.
- `operator_actor_id` stays explicit.
- TTL extension deferred.
- The approval predicate uses `approval_id` for clarity;
  engine binding still requires `workflow_execution_id` +
  `gate_nonce`.

## Why this spec exists

DURABLE-APPROVAL-RECEIPTS-V1 shipped the receipts substrate
(commit `96f4582`). The IMPROVEMENT-LOOP-WORKFLOW-V1
orchestrator uses receipts as a Python primitive (calls
`request_approval()` directly).

The `action_library.py` ships eight verbs today
(`notify_user`, `write_canvas`, `route_to_agent`, `call_tool`,
`post_to_service`, `mark_state`, `append_to_ledger`, `branch`).
Receipt requests were intentionally deferred because they
combine action execution with a workflow PAUSE — not just a
side-effect.

This spec adds a ninth verb (`request_approval`) that wraps
the receipt creation AND folds onto the engine's already-
shipped gate mechanism. No new gate concept; the verb
co-operates with `gate_ref` + `approval_gates`.

## v2 scope (what ships)

- One new action class: `RequestApprovalAction` (follows the
  `Action` protocol).
- A receipt-backed gate-resume rule added to
  `_await_gate()`: on entry, query the receipt by
  `(workflow_execution_id, gate_nonce)` and short-circuit if
  terminal. Eliminates the lost-event window.
- Storage contract extension: `workflow_step_outputs`
  envelope gains an optional `approval_outcome` field,
  populated atomically with gate clear + cursor advance.
- Ref-resolver: `{step.<step_id>.approval_outcome.<field>}`
  reads the new envelope field.
- Validation: `request_approval` added to
  `KNOWN_ACTION_TYPES`, `ACTION_OPERATION_CLASS_BY_VERB`, and
  `is_irreversible` (returns True — world-effect that
  persists an operator-visible request and triggers downstream
  authorization).
- ActionLibrary bring-up registration: wired in production
  factory at `kernos/setup/bring_up_substrate.py`-class file.

## Out of scope (deferred)

- Multi-approver receipts (v2).
- Auto re-issue on TTL expiry (operator re-requests).
- Per-action approval policies beyond covenant_gate.
- TTL extension surface (re-issuance is acceptable for v1).
- Structured `binding_payload` schemas (free-form for v1;
  JSON-serializable mapping validation only).
- Engine-derived `operator_actor_id` default (explicit-only).

## Architecture

### Action class shape

```python
class RequestApprovalAction:
    """Workflow action verb that creates an approval receipt
    bound to the current workflow execution's pending gate
    nonce. The workflow PAUSES via the existing gate_ref
    mechanism — this verb does NOT itself create a gate.

    Receipt resume happens via the engine's race-proof
    receipt-backed gate rule (see _await_gate fold below).
    """

    action_type = "request_approval"

    def __init__(
        self,
        request_approval_fn: Callable[..., Awaitable[str]],
        *,
        covenant_gate: CovenantGate | None = None,
    ) -> None:
        self._request_approval = request_approval_fn
        self._covenant_gate = covenant_gate

    async def execute(
        self, context: Any, params: dict,
    ) -> ActionResult:
        if not await _resolve_covenant(
            self._covenant_gate, context,
            self.action_type, params,
        ):
            return ActionResult(
                success=False, error="covenant_denied",
            )

        # binding_payload validation (Codex r1 SHOULD #7):
        # must be a JSON-serializable mapping. Empty default.
        binding_payload = params.get("binding_payload") or {}
        if not isinstance(binding_payload, dict):
            return ActionResult(
                success=False,
                error="invalid_binding_payload:not_a_mapping",
            )
        try:
            json.dumps(binding_payload)
        except (TypeError, ValueError) as exc:
            return ActionResult(
                success=False,
                error=f"invalid_binding_payload:{exc}",
            )

        # workflow_execution_id + gate_nonce are NOT on
        # CohortContext (Codex r1 BLOCKING #4). They come
        # through resolved params via {workflow.execution_id}
        # + {workflow.gate_nonce} ref patterns the engine
        # already exposes (see execution_engine.py:458).
        workflow_execution_id = params.get(
            "_workflow_execution_id",
        )
        gate_nonce = params.get("_gate_nonce")
        if not (workflow_execution_id and gate_nonce):
            return ActionResult(
                success=False,
                error="missing_workflow_binding",
            )

        try:
            approval_id = await self._request_approval(
                instance_id=getattr(context, "instance_id", ""),
                kind=params["kind"],
                requested_for_actor=params.get(
                    "requested_for_actor",
                    getattr(context, "member_id", ""),
                ),
                operator_actor_id=params["operator_actor_id"],
                request_summary=params["request_summary"],
                binding_payload=binding_payload,
                workflow_execution_id=workflow_execution_id,
                gate_nonce=gate_nonce,
                ttl_seconds=params.get("ttl_seconds", 86400),
                single_use=params.get("single_use", True),
            )
        except KeyError as exc:
            return ActionResult(
                success=False,
                error=f"missing_param:{exc.args[0]}",
            )
        except Exception as exc:
            return ActionResult(
                success=False,
                error=f"approval_request_failed:{exc}",
            )
        return ActionResult(
            success=True,
            value={"approval_id": approval_id},
            receipt={
                "approval_id": approval_id,
                "requested_at": _now(),
            },
        )

    async def verify(
        self, context: Any, params: dict, result: ActionResult,
    ) -> bool:
        return result.success and bool(
            result.value and result.value.get("approval_id"),
        )
```

### Engine fold

**No new gate concept.** The verb works with the existing
`gate_ref` + `approval_gates` machinery. The author declares
both in the descriptor; the engine mints `pending_gate_nonce`
before the gated step executes (existing flow); the verb
reads the nonce through the resolved params; the verb
creates the receipt with that nonce bound; engine emits
`workflow.execution_paused_at_gate` (existing); engine waits
for `approval.decision_recorded` (existing event for the
approval-receipts substrate).

**The race-proof resume rule (BLOCKING #2 fold):** Add a
receipt-state check at the top of `_await_gate()` for gates
whose `approval_event_type == "approval.decision_recorded"`:

```python
async def _await_gate(self, ...):
    # NEW (REQUEST-APPROVAL-ACTION-V1): if this gate's event
    # type is the approval-receipt event, check the receipt
    # row first. The post-flush hook does NOT buffer events
    # that arrive between persist-nonce and install-waiter.
    if approval_gate.approval_event_type == "approval.decision_recorded":
        receipt = await approval_receipts.find_terminal_by_binding(
            data_dir=self._data_dir,
            workflow_execution_id=workflow_execution_id,
            gate_nonce=gate_nonce,
        )
        if receipt is not None:
            synthesized_payload = {
                "approval_id": receipt["approval_id"],
                "decision": receipt["decision"],          # approved | rejected | expired
                "decided_at": receipt["decided_at"],
                "decided_by_actor": receipt["decided_by_actor"],
                "rejection_reason": receipt.get("rejection_reason"),
                "workflow_execution_id": workflow_execution_id,
                "gate_nonce": gate_nonce,
            }
            await self._resume_with_gate_payload(
                synthesized_payload, source="receipt_short_circuit",
            )
            return

    # Existing path: emit paused_at_gate, install waiter,
    # wait for future approval.decision_recorded events.
    # ...
```

The new `approval_receipts.find_terminal_by_binding(...)`
helper (added by this spec):

```python
async def find_terminal_by_binding(
    *, data_dir, workflow_execution_id, gate_nonce,
) -> dict | None:
    """Return the most recent terminal receipt matching
    (workflow_execution_id, gate_nonce), or None if pending.
    Terminal = decision in {approved, rejected, expired}.
    Mirrors find_recent_terminal_by_binding_field() but uses
    the workflow binding rather than an arbitrary field."""
```

**Single-use consumption (SHOULD #2 fold):** When the gate
resumes from a `decision=approved` receipt (either via post-
flush event or the short-circuit), the engine calls
`approval_receipts.consume_approval(approval_id)` before
advancing the cursor. The consume call is idempotent against
already-consumed receipts (mirrors the existing helpers'
shapes). This satisfies both the gate-nonce single-step
guarantee AND the receipt-level single-use contract.

### Storage contract (BLOCKING #5 fold)

`workflow_step_outputs` currently stores envelopes with
`success`, `value`, `error`, `receipt`. Add an optional
`approval_outcome` field at the same level, populated by the
engine on gate resume:

```python
@dataclass
class StepOutputEnvelope:
    success: bool
    value: Any = None
    error: str | None = None
    receipt: dict = field(default_factory=dict)
    approval_outcome: dict | None = None  # NEW
```

`approval_outcome` shape (SHOULD #1 fold — adds `decision`):

```python
{
    "approved": bool,
    "decision": "approved" | "rejected" | "expired",
    "approval_id": str,
    "decided_at": str,            # ISO timestamp
    "decided_by_actor": str,
    "rejection_reason": str | None,  # populated when approved=False
}
```

**Atomic write:** The engine's existing
`_clear_gate_nonce_and_advance` helper extends to also
persist the `approval_outcome` into the requesting step's
envelope. The persist + clear-nonce + advance-cursor happen
in a single transaction. On restart, the envelope round-trips
through the existing step-output loader.

### Ref-resolver

`{step.<step_id>.approval_outcome.<field>}` is the public ref
pattern. Resolves via the existing step-output loader; if the
envelope's `approval_outcome` field is `None` or the inner
field is missing:
- In **parameter context**: raises `RefResolutionError`
  (matches existing behavior).
- In **predicate context**: returns no-match (matches existing
  behavior).

Per Codex r1 SHOULD #3: there is NO literal `<missing_ref>`
sentinel.

### Descriptor shape (BLOCKING #3 fold)

Example workflow YAML using existing vocabulary:

```yaml
steps:
  - id: request_op_approval
    action_type: request_approval
    parameters:
      kind: git_commit_authorization
      operator_actor_id: "{context.operator_actor_id}"
      request_summary: "Commit ready: {step.draft_spec.value.spec_summary}"
      binding_payload:
        expected_parent_sha: "{step.snapshot.value.head_sha}"
      ttl_seconds: 86400
      single_use: true
      _workflow_execution_id: "{workflow.execution_id}"
      _gate_nonce: "{workflow.gate_nonce}"
    gate_ref: await_op_approval

approval_gates:
  - gate_name: await_op_approval
    approval_event_type: approval.decision_recorded
    approval_event_predicate:
      op: eq
      path: payload.approval_id
      value: "{step.request_op_approval.value.approval_id}"
    timeout_seconds: 86400
    bound_behavior_on_timeout: abort_workflow
```

Downstream branching uses existing `branch_on_true` /
`branch_on_false`:

```yaml
  - id: branch_on_approval
    action_type: branch
    parameters:
      predicate:
        op: eq
        path: "{step.request_op_approval.approval_outcome.decision}"
        value: approved
    branch_on_true: do_commit
    branch_on_false: surface_rejection
```

**Engine-level binding sanity (SHOULD #5 fold):** the
`approval_event_predicate` SHOULD match on `approval_id` for
clarity. The engine's `_on_post_flush_for_gates()` continues
to require `workflow_execution_id` + `gate_nonce` for binding
— the predicate is an additional clarity surface, not the
binding contract.

### Validation surfaces (BLOCKING #6 fold)

Updates needed (each gets an AC):

1. **`action_classification.KNOWN_ACTION_TYPES`** — add
   `"request_approval"` to the world-effect verb set.
2. **`action_classification.is_irreversible`** — returns
   `True` for `"request_approval"` (per Codex: world-effecting
   send-ish, irreversible for safe-deny-on-timeout purposes).
3. **`action_sink.ACTION_OPERATION_CLASS_BY_VERB`** — map
   `"request_approval"` to the appropriate operation class
   (likely `world_effect` consistent with `notify_user` /
   `route_to_agent`).
4. **Workflow registry validation** — descriptor parsing
   already routes unknown action types through
   `KNOWN_ACTION_TYPES` check. Once added, no extra parse
   logic needed.
5. **Bring-up registration** — wire
   `RequestApprovalAction(request_approval_fn=approval_receipts.request_approval, covenant_gate=...)`
   into the production `ActionLibrary` factory at
   `bring_up_substrate.py`.

## Acceptance criteria

### Action verb (V2)

| AC | Description |
|---|---|
| AC1 | `RequestApprovalAction` follows the `Action` protocol with `action_type="request_approval"`. |
| AC2 | `execute()` returns success + `{"approval_id": str}` value when the wrapped `request_approval_fn` succeeds. |
| AC3 | `execute()` returns `error="missing_param:<name>"` when required param missing (kind, operator_actor_id, request_summary). |
| AC4 | `execute()` returns `error="approval_request_failed:<msg>"` when the wrapped function raises. |
| AC5 | `execute()` returns `error="covenant_denied"` when the covenant gate denies. |
| AC6 | `verify()` returns True iff `result.success` AND `result.value.approval_id` is non-empty. |
| AC7 | Defaults: `ttl_seconds=86400`, `single_use=True`, `requested_for_actor=context.member_id`. |
| AC7a | `binding_payload` must be a `dict` (JSON-serializable mapping); non-dict → `error="invalid_binding_payload:not_a_mapping"`. |
| AC7b | `binding_payload` containing non-JSON-serializable values → `error="invalid_binding_payload:<typeerror>"`. |
| AC7c | Missing `_workflow_execution_id` or `_gate_nonce` in resolved params → `error="missing_workflow_binding"`. |

### Engine integration (V2)

| AC | Description |
|---|---|
| AC8 | Engine mints `pending_gate_nonce` before the gated `request_approval` step's action runs (existing flow). |
| AC9 | `{workflow.execution_id}` + `{workflow.gate_nonce}` refs resolve to the current execution row's id + minted nonce (existing surface — validate the action verb actually receives them). |
| AC10 | Engine emits `workflow.execution_paused_at_gate` on the gated step (existing behavior). |
| AC11 | **Race-proof resume rule**: `_await_gate()` for an approval-event gate first queries `find_terminal_by_binding(workflow_execution_id, gate_nonce)`. If terminal: synthesize gate payload and short-circuit advance. If pending: install waiter as before. |
| AC11a | New helper `approval_receipts.find_terminal_by_binding(...)` returns the row dict for terminal (approved/rejected/expired) decisions matching `(workflow_execution_id, gate_nonce)`, else None. |
| AC11b | Receipt-short-circuit emits engine telemetry event `workflow.gate_receipt_short_circuited` with `{approval_id, decision, source}` so soak can verify the path fires when expected. |
| AC12 | On `approval.decision_recorded` event matching the persisted `gate_nonce`: engine populates `approval_outcome` per the storage contract (atomic with clear-nonce + advance-cursor) and resumes. |
| AC13 | `approval_outcome.decision` field carries the receipt's decision verbatim (`approved` / `rejected` / `expired`). `approved` is a boolean convenience. |
| AC14 | Single-use receipts (`single_use=True`): engine calls `consume_approval(approval_id)` before advancing the cursor on `decision=approved`. Idempotent against already-consumed. |
| AC14a | `consume_approval` failure (e.g., receipt already consumed by another path) does NOT block gate advance — the gate's single-step guarantee is independent. Telemetry event `approval_consume_skipped` records the no-op. |
| AC15 | Existing gate timeout behaviors (`abort_workflow`, `auto_proceed_with_default`) apply unchanged. |

### Storage + ref-resolver (V2)

| AC | Description |
|---|---|
| AC16 | `workflow_step_outputs` envelope schema extends with optional `approval_outcome` (default `None`). Existing tests pass without modification. |
| AC17 | Engine writes `approval_outcome` into the requesting step's envelope atomically with `_clear_gate_nonce_and_advance`. Atomic = single transaction. |
| AC18 | Ref `{step.<id>.approval_outcome.decision}` in **parameter** context: resolves to the decision string; if envelope `approval_outcome` is None, raises `RefResolutionError`. |
| AC18a | Same ref in **predicate** context: resolves on match; if envelope `approval_outcome` is None, returns no-match (per existing resolver behavior). |
| AC19 | After Kernos restart, a workflow that previously resumed through a request_approval step still resolves `${step.<id>.approval_outcome.*}` refs on subsequent steps — the envelope round-trips through the loader. |

### Validation + classification (V2)

| AC | Description |
|---|---|
| AC20 | `action_classification.KNOWN_ACTION_TYPES` contains `"request_approval"`. |
| AC21 | `action_classification.is_irreversible("request_approval", ...)` returns `True`. |
| AC22 | `action_sink.ACTION_OPERATION_CLASS_BY_VERB["request_approval"]` is mapped to the appropriate operation class (world-effect tier consistent with `notify_user` / `route_to_agent`). |
| AC23 | Workflow descriptor with unknown params for `request_approval` fails registration via the existing `KNOWN_ACTION_TYPES`-gated validator (no per-action parameter-required validation in v1 — Codex SHOULD #4 noted, deferred). |
| AC24 | Production `ActionLibrary` bring-up registers `RequestApprovalAction` with the receipt surface + covenant gate wired. |

### Descriptor (V2)

| AC | Description |
|---|---|
| AC25 | Workflow YAML using `action_type: request_approval` + `gate_ref` + an `approval_gates` entry with `approval_event_type: approval.decision_recorded` validates at registration. |
| AC26 | The recommended descriptor pattern (predicate matches on `approval_id` for clarity) is documented in `docs/workflow-actions.md` (or equivalent) with the example from this spec. |

### Restart-resume

| AC | Description |
|---|---|
| AC27 | Bring-up restart-resume re-enters `_await_gate()` for executions with `gate_nonce`. The race-proof rule fires, picking up terminal receipts that landed before restart. |

### Integration with IMPROVEMENT-LOOP-WORKFLOW-V1

| AC | Description |
|---|---|
| AC28 | The orchestrator's Python-orchestrator path keeps working unchanged. |
| AC29 | A parallel YAML-orchestrator descriptor variant can be authored using `request_approval` and exercises the same receipt schema. |

## Soak gate

1. **Automated**: ACs above via test fixtures.
   - **Receipt short-circuit test**: persist a terminal
     receipt row with a known `(execution_id, nonce)`; enter
     `_await_gate()`; assert short-circuit fires + emits
     `workflow.gate_receipt_short_circuited`.
   - **Approval-flow test**: approve via existing `/approve`
     surface; assert `approval.decision_recorded` event;
     assert envelope `approval_outcome.decision=approved` +
     `consume_approval` called.
   - **Rejection-flow test**: reject; assert envelope shape
     and predicate branches correctly.
   - **Expiry-flow test**: TTL=60s; wait; assert
     `decision=expired` + envelope shape; no
     `consume_approval` call.
2. **Operator soak**: author a minimal YAML workflow that
   pauses at `request_approval`; approve via `/approve`;
   verify downstream step fires with the expected refs.
3. **Race soak**: inject the race window (mock-time the
   receipt write to land before the engine installs the
   waiter); verify the short-circuit fires and the workflow
   does not get stuck.

## Migration

Additive. No schema migration for receipts (existing rows
serve fine). `workflow_step_outputs` envelope adds an
optional column; existing rows have `approval_outcome=NULL`.
Existing tests pass unchanged.

The race-proof rule lands as an additive top-of-`_await_gate()`
check that only fires for `approval_event_type ==
"approval.decision_recorded"` gates. Pre-existing gate flows
(non-approval) are unaffected.

`improvement_loop_workflow.py` keeps the Python-orchestrator
path; new YAML-orchestrator paths can use `request_approval`.

## Risks

- **Risk:** The receipt short-circuit reads from
  `approval_receipts` mid-`_await_gate()`. If the read fails
  (DB locked, etc.), behavior must fall through to the
  existing wait path rather than aborting the gate.
  - **Mitigation:** Wrap the lookup in try/except; on
    exception log + fall through. Telemetry event
    `workflow.gate_receipt_lookup_failed` so the soak can
    detect lookup health.

- **Risk:** Storage extension of `workflow_step_outputs`
  envelope could collide with concurrent spec work touching
  the same surface.
  - **Mitigation:** Check the envelope dataclass at impl
    time; add the field at the end with default `None`.
    Codex round-2 review verifies no collision.

- **Risk:** `consume_approval` failure pathway. The engine
  must advance even if consume returns "already consumed"
  (idempotency) — otherwise a botched-but-recoverable receipt
  state could pin the workflow.
  - **Mitigation:** AC14a covers this. `approval_consume_skipped`
    telemetry event provides operator-visible signal without
    blocking advance.

- **Risk:** The descriptor predicate's `{step.X.value.approval_id}`
  ref must resolve at the time the gate event arrives — which
  is AFTER the step has succeeded but BEFORE the gate
  resumes. The resolver loads from durable step outputs;
  this works iff `_persist_gate_nonce_only` also persists
  the action's success envelope first.
  - **Mitigation:** Sequence in the engine's existing flow
    is: execute action, append success + persist gate_nonce
    (single transaction), then `_await_gate()`. So
    `{step.X.value.approval_id}` is already durable when the
    predicate evaluates. Confirmed in v1 by Codex's
    architecture summary.

## Dependencies

All shipped:
- DURABLE-APPROVAL-RECEIPTS-V1 (`96f4582`) — receipts +
  `approval.decision_recorded` event
- WLP-GATE-SCOPING (C1) — gate_nonce persistence + match
- WORKFLOW-ACTION-LIBRARY — Action Protocol + ActionResult
- IMPROVEMENT-LOOP-WORKFLOW-V1 — Python-orchestrator that
  proves the receipt+gate combo
- Engine's `gate_ref` + `approval_gates` descriptor surface
  (existing per descriptor_parser.py:63, 318, 333, etc.)
- Engine ref-resolver patterns `{workflow.execution_id}` +
  `{workflow.gate_nonce}` (existing per
  execution_engine.py:458)

New helper added by this spec (~30 LOC):
- `approval_receipts.find_terminal_by_binding(...)` — mirrors
  existing `find_recent_terminal_by_binding_field` shape but
  binds on `(workflow_execution_id, gate_nonce)`.

## Open architect questions

(All four Q's from v1 resolved by Codex r1 SHOULD list:)

1. ~~**Binding-payload shape**~~ ✅ Free-form mapping with
   JSON-serializable validation; structured per-kind schemas
   are v2.
2. ~~**Expiry treatment**~~ ✅ Add `decision` first-class
   field; expiry remains `approved=False` with
   `decision="expired"`; predicates can branch cleanly.
3. ~~**TTL extension**~~ ✅ Deferred; re-issuance is
   operationally acceptable.
4. ~~**Default `operator_actor_id`**~~ ✅ Explicit-only.

New for v2:
5. **Telemetry naming.** This spec introduces three new
   engine events: `workflow.gate_receipt_short_circuited`,
   `workflow.gate_receipt_lookup_failed`,
   `approval_consume_skipped`. Are these the right shapes
   + are they consistent with existing engine telemetry
   verbs? Codex round-2 to validate naming.
