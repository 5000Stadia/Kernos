# REQUEST-APPROVAL-ACTION-V1

**Date:** 2026-05-28
**Status:** Draft for architect + Codex spec review
**Origin:** DEFERRED #94 from DURABLE-APPROVAL-RECEIPTS-V1 batch
**Scope:** Workflow-engine action verb that wraps the existing
  `approval_receipts.request_approval()` surface and integrates
  with the engine's existing approval-gate concept. Workflow
  YAML can declare `action: request_approval` as a step;
  workflow execution pauses at the gate; resumes on
  `approval.approved` event; surfaces the decision into
  workflow state for downstream steps to reference.
**Estimated size:** ~200 LOC source + ~250 LOC tests.

## Why this spec exists

DURABLE-APPROVAL-RECEIPTS-V1 shipped the receipts surface
(commit `96f4582`): receipts created, expired, approved,
rejected, consumed. The IMPROVEMENT-LOOP-WORKFLOW-V1
orchestrator uses receipts as a Python primitive (calls
`request_approval()` directly).

The Action-Library batch (`action_library.py`) shipped six
world-effect verbs (`notify_user`, `write_canvas`,
`route_to_agent`, `call_tool`, `post_to_service`,
`mark_state`) plus internal-state verbs. Receipt requests
were intentionally deferred — they have a different shape:
the action doesn't just emit a side-effect, it PAUSES the
workflow until an external signal (approval/rejection) lands.

This spec closes that gap: `request_approval` becomes a
first-class workflow action that workflow YAML can declare,
and the engine's existing gate mechanism handles the pause/
resume cycle.

## v1 scope (the minimum that ships)

- One new action class: `RequestApprovalAction` (follows
  the `Action` protocol in `action_library.py`).
- One new gate-class: `approval_receipt_gate` (extends the
  engine's existing gate concept; replaces the
  Python-orchestrator's hand-wired receipt creation +
  continuation hook).
- Engine surface change: `CohortContext` gains an
  `approval_outcome` field (set by the gate-resume handler
  when an approval lands, before the workflow advances).
- Engine surface change: ref-resolver supports
  `${step.<step_id>.approval_outcome.*}` patterns to let
  downstream steps reference the approval decision.

## Out of scope (deferred)

- Multi-approver receipts (current receipts are
  single-operator; multi-approver is a v2 question).
- Auto re-issue on TTL expiry (operator manually re-requests
  in v1; matches IMPROVEMENT-LOOP-WORKFLOW-V1 Risk #3).
- Per-action approval policies (e.g., "always require
  approval for this workflow regardless of operator
  defaults") — covered by existing covenant gate system.

## Architecture

### Action class shape

```python
class RequestApprovalAction:
    """Workflow action verb that creates an approval receipt
    and produces a gate the engine pauses on. Workflow
    resumes on approval.approved or approval.rejected event;
    decision surfaces as ``approval_outcome`` in step state.

    Wraps the existing approval_receipts.request_approval()
    function — does NOT duplicate the receipt schema, TTL,
    or single-use semantics.
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
                binding_payload=params.get("binding_payload") or {},
                workflow_execution_id=context.execution_id,
                gate_nonce=context.gate_nonce,
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
        # Verification at execute-time: receipt was persisted.
        # The actual approval outcome is verified at gate
        # resume time (downstream concern).
        return result.success and bool(
            result.value and result.value.get("approval_id"),
        )
```

### Engine integration

**`request_approval` is a gating action.** When the engine
processes a step with `action: request_approval`:

1. Generates `gate_nonce` (UUID) BEFORE calling execute().
2. Persists `gate_nonce` on the execution row (via existing
   `_append_and_persist_gate_nonce` helper).
3. Calls `action.execute(context, params)` — the action's
   binding_payload is wired to the persisted `gate_nonce` +
   `execution_id` via context.
4. On success: emits `workflow.execution_paused_at_gate`
   (existing event); marks execution `paused_at_gate`;
   stops processing this execution.
5. Engine's existing `_on_post_flush_for_gates` handler
   matches incoming `approval.approved` or
   `approval.rejected` events against persisted
   `gate_nonce`. On match:
   - Clears gate_nonce (via existing
     `_clear_gate_nonce_and_advance`)
   - Sets `step_outputs[<step_id>].approval_outcome` to
     `{"approved": bool, "approval_id": str,
       "decided_at": iso, "decided_by_actor": str,
       "rejection_reason": str | None}`
   - Resumes execution from next step.

### CohortContext extension

The Action's `execute()` reads `context.execution_id` and
`context.gate_nonce` (currently not exposed on
`CohortContext`). Two minimal additions:

```python
@dataclass
class CohortContext:
    instance_id: str
    member_id: str
    space_id: str
    # NEW (this spec):
    execution_id: str = ""   # workflow execution row id
    gate_nonce: str = ""     # currently-pending gate's nonce
```

These are populated by the engine when it constructs the
CohortContext for an action call inside a workflow execution.
Both default to `""` so non-workflow callers
(`ActionLibrary.execute(action_type, ctx, params)` from
direct caller paths) keep working unchanged.

### Ref-resolver extension

Workflow YAML supports refs like
`${step.<step_id>.value.<field>}` today. The approval action's
decision lives in a sibling field for clarity:

```yaml
steps:
  - id: request_op_approval
    action: request_approval
    params:
      kind: git_commit_authorization
      operator_actor_id: ${context.operator_actor_id}
      request_summary: "Commit ready for approval: ${step.draft_spec.value.spec_summary}"
      binding_payload:
        expected_parent_sha: ${step.snapshot.value.head_sha}

  - id: gate_on_approval
    action: branch
    params:
      condition: ${step.request_op_approval.approval_outcome.approved}
      true_branch: do_commit
      false_branch: surface_rejection
```

`${step.X.approval_outcome.*}` resolves to the gate-resume
fields populated by the engine, distinct from
`${step.X.value.*}` which holds the action's execute-time
return value.

## Acceptance criteria

### Action verb

| AC | Description |
|---|---|
| AC1 | `RequestApprovalAction` registered in `ActionLibrary` with `action_type="request_approval"`. |
| AC2 | `execute()` returns success + `{"approval_id": str}` value when the wrapped `request_approval_fn` succeeds. |
| AC3 | `execute()` returns `error="missing_param:<name>"` when required param missing (kind, operator_actor_id, request_summary). |
| AC4 | `execute()` returns `error="approval_request_failed:<msg>"` when the wrapped function raises. |
| AC5 | `execute()` returns `error="covenant_denied"` when the covenant gate denies. |
| AC6 | `verify()` returns True iff `result.success` AND `result.value` contains a non-empty `approval_id`. |
| AC7 | Defaults: `ttl_seconds=86400`, `single_use=True`, `requested_for_actor=context.member_id`. |

### Engine integration

| AC | Description |
|---|---|
| AC8 | When step `action: request_approval` runs: engine generates `gate_nonce` BEFORE `execute()` and persists it on the execution row. |
| AC9 | Action's `execute()` reads `gate_nonce` + `execution_id` from CohortContext and passes both to `request_approval_fn`. |
| AC10 | On successful execute: engine emits `workflow.execution_paused_at_gate` with payload `{gate_nonce, approval_id, step_id}`. |
| AC11 | On `approval.approved` event matching the persisted `gate_nonce`: engine populates `step_outputs[<step_id>].approval_outcome = {approved: True, approval_id, decided_at, decided_by_actor, rejection_reason: None}` and resumes. |
| AC12 | On `approval.rejected` event matching the persisted `gate_nonce`: engine populates `approval_outcome = {approved: False, ..., rejection_reason: <reason>}` and resumes. |
| AC13 | On `approval.expired` event matching the persisted `gate_nonce`: engine populates `approval_outcome = {approved: False, ..., rejection_reason: "expired"}` and resumes (workflow can branch on the expired outcome). |
| AC14 | Existing gate behavior (`abort_workflow`, `auto_proceed_with_default`) applies if the workflow descriptor declares it on the request_approval step. |

### CohortContext + ref-resolver

| AC | Description |
|---|---|
| AC15 | `CohortContext` gains `execution_id: str=""` and `gate_nonce: str=""` fields; non-workflow callers see defaults. |
| AC16 | Engine populates both fields when constructing the context for actions inside a workflow execution. |
| AC17 | Ref-resolver resolves `${step.<id>.approval_outcome.<field>}` to the gate-resume fields. |
| AC18 | Ref to non-existent approval_outcome field returns the existing `<missing_ref>` sentinel (per existing resolver behavior). |

### Workflow-level

| AC | Description |
|---|---|
| AC19 | Workflow YAML descriptor declaring `action: request_approval` validates at registration. |
| AC20 | Required params (kind, operator_actor_id, request_summary) enforced at registration; missing params fail descriptor validation. |
| AC21 | Workflow with paused-at-gate execution survives Kernos restart: bring-up bring-up scans pending gates and re-attaches the gate handler. |

### Integration with IMPROVEMENT-LOOP-WORKFLOW-V1

| AC | Description |
|---|---|
| AC22 | The orchestrator's Python-orchestrator path (current production) keeps working unchanged. |
| AC23 | A parallel YAML-orchestrator descriptor variant can be authored using `request_approval` action verb and exercises the same receipt schema. |

## Soak gate

1. **Automated**: ACs above via test fixtures. Approval-flow
   tests use a fake event stream + monkeypatched
   `request_approval_fn` to assert the gate handshake.
2. **Operator soak**: author a minimal YAML workflow that
   uses `request_approval` (e.g., "notify operator → request
   approval → write canvas"). Trigger; observe pause; approve
   via existing `/approve <id>` surface; observe canvas
   write fires.
3. **Failure soak**: trigger the same workflow; reject the
   approval; observe `approval_outcome.approved=False`;
   verify the branch step routes to the rejection path.
4. **Expiry soak**: trigger the workflow with `ttl_seconds=60`;
   wait for expiry; verify `approval_outcome.rejection_reason
   ="expired"` and the workflow handles it cleanly.

## Migration

Additive. No schema change. The engine's gate concept already
exists (per execution_engine.py lines 26-54); this spec only
adds a new gate-creating action. The `_on_post_flush_for_gates`
handler at `execution_engine.py:1688` already does
gate_nonce-based matching; this spec wires the action into
that handler's known gate types.

CohortContext extension is two new fields with `""` defaults —
backward-compatible per `[[schema-extension-defaults]]`.

The `improvement_loop_workflow.py` Python orchestrator keeps
using `request_approval()` directly (no migration). The YAML-
orchestrator path becomes available; any future workflow that
needs operator gating can use it.

## Risks

- **Risk:** Gate handshake race — between persisting
  `gate_nonce` and the action's `execute()` calling
  `request_approval` with that nonce, an approval event for
  the matched nonce could (theoretically) land before the
  receipt row exists.
  - **Mitigation:** Order matters in the engine. The fix is
    to (1) persist gate_nonce first, (2) execute the action
    which writes the receipt with that nonce, (3) ONLY THEN
    register the gate handler. The handler being late-bound
    means in-flight approval events for our nonce buffer
    until the action returns. Codex should verify this
    sequencing in review.

- **Risk:** CohortContext extension may collide with other
  in-flight specs adding their own fields.
  - **Mitigation:** Check the latest CohortContext shape at
    impl time (grep for `class CohortContext`). Add the two
    fields at the end of the dataclass with defaults.

- **Risk:** Ref-resolver extension for `approval_outcome`
  field could overlap with workflows that already use
  `value.approval_outcome` shape.
  - **Mitigation:** New field is at the step level, NOT
    inside value. Resolver's existing `${step.<id>.<field>}`
    pattern naturally accommodates this.

- **Risk:** Existing tests for `_on_post_flush_for_gates`
  may need updates if the new action type isn't currently
  exercised by any fixture.
  - **Mitigation:** Add a fixture-level test for the new
    gate type; existing fixtures should be unaffected.

## Dependencies

All shipped:
- DURABLE-APPROVAL-RECEIPTS-V1 (`96f4582`) — receipt schema +
  request_approval() + approve/reject/expire surfaces
- WLP-GATE-SCOPING (C1) — gate_nonce persistence + match
- WORKFLOW-ACTION-LIBRARY (action_library.py) — Action
  Protocol + ActionResult shape + covenant_gate pattern
- IMPROVEMENT-LOOP-WORKFLOW-V1 (`ed91b76`-class commit) —
  Python orchestrator that proves the receipt+gate flow
  works end-to-end and motivates the YAML-orchestrator
  migration path

## Open architect questions

1. **Should `request_approval` action expose the binding-
   payload as a structured object in YAML, or a free-form
   dict?** Free-form maximizes flexibility but loses
   validation; structured constrains the use cases the
   action supports. Spec v1 leans free-form because the
   binding_payload IS already free-form in receipts.

2. **`approval_outcome.rejection_reason="expired"` as a
   first-class outcome.** Is treating expiry as a rejection
   with a special reason the right shape, or should there
   be a third path (`approval_outcome.timed_out=True`)? v1
   leans on the reason field because branching on the same
   `approved=False` keeps YAML logic simpler.

3. **TTL extension.** If an operator says "I'll approve
   tomorrow" — should the action verb support a way to
   extend TTL without re-issuing? Current receipt schema
   doesn't have TTL extension; would need a new receipt
   primitive.

4. **Default `operator_actor_id` resolution.** Current spec
   makes the workflow author specify it explicitly. Should
   the engine derive it from context (e.g., "the instance's
   owner member") when omitted? Trade-off: explicit-only
   prevents footguns; engine-derived removes boilerplate.
