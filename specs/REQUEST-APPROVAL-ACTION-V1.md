# REQUEST-APPROVAL-ACTION-V1

**Date:** 2026-05-28 (v3 — Codex r2 fold)
**Status:** Draft for round-3 Codex review (expect GREEN per
  multi-round convergence pattern)
**Origin:** DEFERRED #94 from DURABLE-APPROVAL-RECEIPTS-V1 batch
**Scope:** Workflow-engine action verb that wraps the existing
  `approval_receipts.request_approval()` surface and folds onto
  the engine's existing `gate_ref` + `approval_gates` mechanism.
  Workflow YAML declares `action_type: request_approval` with a
  `gate_ref`; engine pauses; resumes on
  `approval.decision_recorded` event; decision surfaces through
  a durable storage contract for downstream refs.
**Estimated size:** ~250 LOC source + ~300 LOC tests.

## What v3 changes from v2 (Codex r2 fold)

Codex r2 verdict was YELLOW with 4 BLOCKING ordering bugs that
would have stranded executions. v3 fixes the ordering and
cleans up descriptor / operation-class / migration wording:

1. **`_await_gate()` install-waiter-before-query.** v2 queried
   the receipt before installing the waiter — that leaves a
   window where `approval.decision_recorded` arrives between
   "found no terminal" and "waiter installed" and is silently
   ignored by `_on_post_flush_for_gates()` (which returns
   early when `_gate_waiters` is empty). v3 installs the
   waiter FIRST, then queries; only waits if no terminal
   receipt exists. Optionally double-queries for robustness.

2. **Consume after advance.** v2 called `consume_approval()`
   before `_clear_gate_nonce_and_advance()`. If the process
   crashes between consume and advance, the receipt is
   `consumed` but the workflow still has `gate_nonce` and
   no `approval_outcome` — and `consumed` is not in the
   terminal set, so the receipt-short-circuit can't recover
   it. v3 persists `approval_outcome` + clears nonce +
   advances cursor FIRST, then calls `consume_approval()`
   best-effort. Telemetry survives this reorder.

3. **Adapter binding at bring-up.** v2 wired
   `RequestApprovalAction(request_approval_fn=approval_receipts.request_approval)`
   directly — but `request_approval()` requires `data_dir`
   and accepts `event_stream`. First execution would fail at
   runtime. v3 explicitly shows the
   `functools.partial(approval_receipts.request_approval,
   data_dir=..., event_stream=...)` adapter pattern.

4. **AC23 reframed.** `KNOWN_ACTION_TYPES` only validates
   action-type membership, not per-action params. v3 makes
   missing/invalid `request_approval` params an execute-time
   failure (returns `ActionResult` with `error="missing_param:..."`
   etc.) — registration only validates known action type +
   gate descriptor shape.

Plus SHOULDs folded:
- Descriptor examples use `action_sequence` (not `steps`)
  and the real `BranchAction` shape (`parameters.condition`
  as native bool + `branch_on_true`/`branch_on_false`).
- Operation class is `register` (not `world_effect`, which
  is not in `RISK_LEVEL_BY_OPERATION_CLASS`). Matches
  `route_to_agent` precedent.
- Migration wording: "optional `approval_outcome` field in
  the JSON envelope" (not "optional column" — the envelope
  is stored as JSON in the existing `workflow_step_outputs`
  table; no schema migration).
- `find_terminal_by_binding` terminal set includes
  `consumed` (terminal-approved for defensive recovery);
  deterministic `ORDER BY decided_at DESC LIMIT 1` with
  multi-match telemetry.
- Telemetry naming dotted: `workflow.gate_approval_consume_skipped`.

## What v2 changed from v1 (Codex r1 fold)

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

**The race-proof resume rule (v3 — install-waiter-FIRST,
then query):** Codex r2 BLOCKING #1 fold. v2's query-before-
install order had a window where `approval.decision_recorded`
could flush between "found no terminal" and "waiter installed"
and be silently dropped by `_on_post_flush_for_gates()` (which
returns early when `_gate_waiters` is empty). v3 installs the
waiter FIRST, then queries; the helper can still short-circuit
already-recorded decisions, and any event that flushes
during/after install will trigger the installed waiter
normally.

```python
async def _await_gate(self, ...):
    # 1) Install the waiter + map entries FIRST so any
    #    post-flush event arriving from this moment on
    #    will be caught by the existing handler.
    self._install_gate_waiter(
        execution_id=workflow_execution_id,
        gate_nonce=gate_nonce,
        event_type=approval_gate.approval_event_type,
        predicate=approval_gate.approval_event_predicate,
    )

    # 2) Emit paused_at_gate (existing telemetry).
    await self._emit_paused_at_gate(...)

    # 3) NEW (REQUEST-APPROVAL-ACTION-V1): for approval-event
    #    gates, query the receipt row. If terminal, synthesize
    #    payload + short-circuit advance. If pending, wait.
    if approval_gate.approval_event_type == "approval.decision_recorded":
        try:
            receipt = await approval_receipts.find_terminal_by_binding(
                data_dir=self._data_dir,
                workflow_execution_id=workflow_execution_id,
                gate_nonce=gate_nonce,
            )
        except Exception as exc:
            # Lookup failure: emit telemetry, fall through to
            # the wait path. Don't strand the gate on a
            # transient DB issue.
            await self._emit(
                "workflow.gate_receipt_lookup_failed",
                {"gate_nonce": gate_nonce, "error": str(exc)},
            )
            receipt = None
        if receipt is not None:
            # The synthesized payload mirrors the real
            # approval.decision_recorded event payload shape
            # from approval_receipts.py (Codex r2 fold).
            synthesized_payload = {
                "execution_id": workflow_execution_id,
                "gate_nonce": gate_nonce,
                "approval_id": receipt["approval_id"],
                "decision": receipt["decision"],         # approved | rejected | expired | consumed
                "kind": receipt["kind"],
                "operator_actor_id": receipt["operator_actor_id"],
                "decided_at": receipt["decided_at"],
                "reason": receipt.get("rejection_reason") or "",
            }
            await self._emit(
                "workflow.gate_receipt_short_circuited",
                {
                    "approval_id": receipt["approval_id"],
                    "decision": receipt["decision"],
                    "source": "receipt_short_circuit_after_install",
                },
            )
            await self._resume_with_gate_payload(synthesized_payload)
            return

    # 4) Wait for the post-flush handler to wake us via the
    #    waiter we installed in step 1.
    await self._gate_waiter_event.wait()
    # ...existing resume path...
```

The new `approval_receipts.find_terminal_by_binding(...)`
helper (added by this spec):

```python
async def find_terminal_by_binding(
    *, data_dir, workflow_execution_id, gate_nonce,
) -> dict | None:
    """Return the most recent terminal receipt matching
    (workflow_execution_id, gate_nonce), or None if pending.

    Terminal set: ``approved``, ``rejected``, ``expired``,
    ``consumed`` (Codex r2 SHOULD #8 fold — defensive
    recovery if v3's consume-after-advance crashes between
    advance and consume).

    Deterministic selection (Codex r2 SHOULD #8):

        SELECT * FROM approval_receipts
        WHERE workflow_execution_id = ? AND gate_nonce = ?
          AND decision IN ('approved','rejected','expired','consumed')
        ORDER BY decided_at DESC
        LIMIT 1

    Emits `workflow.gate_receipt_multi_terminal` telemetry
    when more than one terminal row matches (should never
    happen because gate_nonce is freshly minted per gated
    step; emit so we hear it if invariants break).
    """
```

**Single-use consumption — AFTER advance (v3 BLOCKING #2
fold):** The engine persists `approval_outcome` + clears
`gate_nonce` + advances the cursor in one transaction FIRST,
then calls `approval_receipts.consume_approval(approval_id)`
best-effort. If the process crashes between advance and
consume, the receipt stays in `approved` state; restart-resume
sees the workflow has already advanced (no `gate_nonce`) so
the receipt-short-circuit doesn't re-fire. A background sweep
(separate concern) can consume orphan-approved receipts.

`consume_approval` failure (already-consumed, DB error)
does NOT block anything — the cursor has already advanced.
Telemetry event `workflow.gate_approval_consume_skipped`
records the no-op for observability (renamed from v2's
`approval_consume_skipped` per Codex r2 SHOULD #9 — dotted
namespace consistency).

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

**Atomic write order (Codex r2 BLOCKING #2):**
The engine's existing `_clear_gate_nonce_and_advance` helper
extends to ALSO persist `approval_outcome` into the requesting
step's envelope. The persist + clear-nonce + advance-cursor
happen in a single transaction. ONLY AFTER that transaction
commits successfully does the engine call
`consume_approval(approval_id)` best-effort. This ordering
ensures a crash between advance and consume leaves the
receipt in `approved` state (still discoverable by future
recovery sweeps), not `consumed`-with-no-outcome-record.

On restart, the envelope round-trips through the existing
step-output loader. If `approval_outcome` was persisted but
`consume_approval` never ran (crash window), the receipt is
orphan-approved; restart-resume sees the workflow has already
advanced (no `gate_nonce`), so the receipt-short-circuit
doesn't re-fire — but the receipt is not consumed. v1 logs
the orphan at WARNING; a background reaper (separate spec)
can sweep them.

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

### Descriptor shape (Codex r2 SHOULD #5 fold — current grammar)

Example workflow descriptor using the current grammar
(`action_sequence` root key; `BranchAction` takes
`parameters.condition` as a native bool resolved via the ref
resolver's sole-reference shortcut, plus `branch_on_true` /
`branch_on_false`):

```yaml
action_sequence:
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

  - id: branch_on_approval
    action_type: branch
    parameters:
      condition: "{step.request_op_approval.approval_outcome.approved}"
      branch_on_true: do_commit
      branch_on_false: surface_rejection

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

Notes:
- `parameters.condition` resolves to a native bool via the
  resolver's sole-reference shortcut (see `BranchAction`
  docstring at `action_library.py:678`). `approval_outcome.
  approved` is the boolean convenience for this purpose;
  branches that need the three-state distinction
  (`approved` vs `rejected` vs `expired`) reference
  `approval_outcome.decision` and compare strings.
- The engine resolves `{workflow.execution_id}` +
  `{workflow.gate_nonce}` at action-execute time. The action
  reads them from `parameters["_workflow_execution_id"]` and
  `parameters["_gate_nonce"]`.

**Engine-level binding sanity (Codex r1 SHOULD #5):** the
`approval_event_predicate` SHOULD match on `approval_id` for
clarity. The engine's `_on_post_flush_for_gates()` continues
to require `workflow_execution_id` + `gate_nonce` for binding
— the predicate is an additional clarity surface, not the
binding contract.

### Validation surfaces

Updates needed (each gets an AC):

1. **`action_classification.KNOWN_ACTION_TYPES`** — add
   `"request_approval"` to the world-effect verb set.
2. **`action_classification.is_irreversible`** — returns
   `True` for `"request_approval"` (world-effecting send-
   ish; irreversible for safe-deny-on-timeout purposes).
3. **`action_sink.ACTION_OPERATION_CLASS_BY_VERB`** — map
   `"request_approval"` to **`"register"`** (Codex r2
   SHOULD #6 fold). This matches `route_to_agent`'s
   precedent: the verb creates a durable receipt + emits an
   operator-facing request through that receipt path.
   `RISK_LEVEL_BY_OPERATION_CLASS["register"] = "medium"`
   applies. `"world_effect"` is NOT a valid class in the
   shipped vocabulary; do not introduce it here.
4. **Workflow registry validation** — descriptor parsing
   already routes unknown action types through
   `KNOWN_ACTION_TYPES` check. Once added, no extra parse
   logic needed. (Per-action parameter validation is
   deferred to v2 per Codex r2 Q4; v3 validates required
   `request_approval` params at execute time — see AC23
   below.)
5. **Bring-up registration — adapter pattern (Codex r2
   BLOCKING #3 fold).** `approval_receipts.request_approval`
   needs `data_dir` and accepts `event_stream`; both must be
   bound before the receipt function is handed to the action.
   The bring-up factory wires an adapter:

```python
import functools
from kernos.kernel import approval_receipts

request_approval_adapter = functools.partial(
    approval_receipts.request_approval,
    data_dir=self._data_dir,
    event_stream=self._event_stream,
)
action_library.register(RequestApprovalAction(
    request_approval_fn=request_approval_adapter,
    covenant_gate=self._covenant_gate_factory("request_approval"),
))
```

Without this adapter, the first workflow execution of
`request_approval` fails at runtime with a missing-kwarg
error.

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
| AC11 | **Race-proof resume rule (Codex r2 BLOCKING #1 fold)**: `_await_gate()` for an approval-event gate FIRST installs the waiter/maps + emits `paused_at_gate`, THEN queries `find_terminal_by_binding(workflow_execution_id, gate_nonce)`. If terminal: synthesize gate payload + short-circuit advance. If no terminal: wait on the already-installed waiter for future `approval.decision_recorded` events. Install-first ordering closes the lost-decision window where a decision flushes between query-time and waiter-install. |
| AC11a | New helper `approval_receipts.find_terminal_by_binding(...)` returns the row dict for terminal decisions matching `(workflow_execution_id, gate_nonce)`, else None. Terminal set: `approved`, `rejected`, `expired`, `consumed` (Codex r2 SHOULD #8 — defensive recovery if v3's consume-after-advance crashes mid-step). Deterministic selection: `ORDER BY decided_at DESC LIMIT 1`. Multi-match emits `workflow.gate_receipt_multi_terminal` telemetry. |
| AC11b | Receipt-short-circuit emits engine telemetry event `workflow.gate_receipt_short_circuited` with `{approval_id, decision, source}` so soak can verify the path fires when expected. |
| AC11c | Synthesized gate payload mirrors the real `approval.decision_recorded` event payload shape: `{execution_id, gate_nonce, approval_id, decision, kind, operator_actor_id, decided_at, reason}`. Verified against `approval_receipts.py:384`. |
| AC11d | `find_terminal_by_binding` lookup failure (DB error, etc.) emits `workflow.gate_receipt_lookup_failed` telemetry; gate falls through to the wait path on the installed waiter — does NOT abort the gate. |
| AC12 | On `approval.decision_recorded` event matching the persisted `gate_nonce`: engine populates `approval_outcome` per the storage contract (atomic with clear-nonce + advance-cursor) and resumes. |
| AC13 | `approval_outcome.decision` field carries the receipt's decision verbatim (`approved` / `rejected` / `expired`). `approved` is a boolean convenience. |
| AC14 | Single-use receipts (`single_use=True`): engine calls `consume_approval(approval_id)` AFTER `_clear_gate_nonce_and_advance` (Codex r2 BLOCKING #2 fold). Reorder rationale: a crash between consume and advance would convert a recoverable approved receipt into an unrecoverable consumed one. Consume-after-advance is safe — even if consume crashes, the cursor has already moved. |
| AC14a | `consume_approval` failure (already-consumed, DB error) does NOT block anything — cursor already advanced. Telemetry event `workflow.gate_approval_consume_skipped` records the no-op (dotted naming per Codex r2 SHOULD #9). |
| AC14b | Orphan-approved receipts (cursor advanced, consume failed) are picked up by a background sweep — separate concern, not in v1 scope. v1 logs the receipt-id at WARNING for operator visibility. |
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
| AC22 | `action_sink.ACTION_OPERATION_CLASS_BY_VERB["request_approval"] == "register"` (Codex r2 SHOULD #6 — `register` matches `route_to_agent` precedent; `world_effect` is NOT a valid class). Corresponding `RISK_LEVEL_BY_OPERATION_CLASS["register"] = "medium"` applies unchanged. |
| AC23 | (Codex r2 BLOCKING #4 fold) Registration validates only: known action type (via `KNOWN_ACTION_TYPES`), valid operation-class registry entry, valid irreversibility classification, and valid `approval_gates` descriptor shape. Per-action param validation is deferred (Codex r2 Q4 — acceptable for v1). Missing or invalid `request_approval` params fail at EXECUTE time via `ActionResult(success=False, error="missing_param:..." / "invalid_binding_payload:..." / "missing_workflow_binding")` — see AC3, AC7a/b/c. |
| AC24 | Production `ActionLibrary` bring-up wires `RequestApprovalAction` with the receipt function bound via `functools.partial(approval_receipts.request_approval, data_dir=..., event_stream=...)` adapter (Codex r2 BLOCKING #3 fold). Without the adapter, the first execution fails with a missing-kwarg error. Bring-up test asserts the registered action's `request_approval_fn` is callable with only the per-call kwargs (no `data_dir`/`event_stream` required). |

### Descriptor (V2)

| AC | Description |
|---|---|
| AC25 | Workflow descriptor at the `action_sequence` root key (Codex r2 SHOULD #5 — current grammar) using `action_type: request_approval` + `gate_ref` + an `approval_gates` entry with `approval_event_type: approval.decision_recorded` validates at registration. Downstream `branch` action uses `parameters.condition` (native bool via sole-ref shortcut) + `branch_on_true`/`branch_on_false` per `BranchAction` contract. |
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
serve fine). `workflow_step_outputs` is stored as a JSON
envelope in the existing table; this spec adds an OPTIONAL
`approval_outcome` FIELD inside that envelope (Codex r2
SHOULD #7 — not a new column). Existing rows lack the
field; the loader treats absence as `None`. Ref resolution
treats missing `approval_outcome` as `None` / no-match per
existing conventions.

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

- **Risk:** Crash window between advance and consume leaves
  an orphan-approved receipt.
  - **Mitigation:** v3 explicitly accepts this trade. The
    receipt stays `approved`; restart-resume sees the cursor
    has advanced (no `gate_nonce`), so the receipt-short-
    circuit does not re-fire. Operator gets a WARNING log
    with the orphan approval_id. A background reaper for
    these is a separate spec (out of scope).
- **Risk:** `consume_approval` failure during the
  best-effort post-advance call. The engine has already
  advanced, so a consume failure cannot strand the workflow.
  - **Mitigation:** AC14a covers this.
    `workflow.gate_approval_consume_skipped` telemetry
    provides operator-visible signal.

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
