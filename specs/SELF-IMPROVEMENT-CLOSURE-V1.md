# SELF-IMPROVEMENT-CLOSURE-V1

## Plain-English overview

This section is for human readers and for Kernos itself, doing a
perspective check before implementation begins. The spec body
that follows is the technical contract; this is what the feature
actually does in language a person can hold.

### The lie this closes

Today, when Kernos notices the same kind of friction recurring
— the same shape of failure showing up turn after turn — a
workflow fires. It asks Claude Code to look into it. Claude
Code investigates, files a response, and Kernos marks the
friction "resolved."

But that word — *resolved* — has been doing more work than it
should. All Kernos actually knows is that Claude Code came back
with an answer. Whether the substrate genuinely behaves
differently now, whether the failure can still recur, whether
the fix even landed — none of that is verified. The pattern's
lifecycle says "resolved" because the conversation finished,
not because the world changed.

This spec closes that gap. It draws a clean line between
*we asked someone to fix it* and *the fix actually holds.* From
this spec forward, a friction pattern only transitions to
resolved when the substrate itself confirms the underlying rule
it was violating is now honored.

### The new primitives, plainly

**An invariant** is a rule Kernos believes the substrate should
always honor. Not a wish, not a goal — a contract. "If the
catalog says a tool is available, the dispatcher must actually
be able to call it." That's an invariant. It's a sentence the
substrate either honors or violates, with no third option.

**A friction pattern** is what the substrate observed: a symptom
that recurred. Friction patterns are observational — they
describe what went wrong. Invariants are normative — they
describe what should never go wrong.

**A link** between the two says: this pattern is a symptom of
this invariant being violated. One invariant can produce many
symptoms; one symptom can implicate multiple invariants. The
link table records which symptoms point at which contracts.

**A closure attempt** is the durable record of what Kernos tried
when a pattern recurred enough to fire the workflow. It records
who got asked (Claude Code today; other routes later), what
probe Kernos will use to verify the fix, and — most
importantly — the outcome of that probe when it runs.

**A probe** is a small, read-only piece of code that asks the
substrate: "is the invariant honored right now?" For the seed
invariant — Tool Availability Honesty — the probe walks the
catalog, asks the dispatch gate to classify each tool, and
verifies each registered tool has an actual handler that can
call it. If every catalog entry is reachable and classifiable,
the probe passes. If even one isn't, the probe fails with a
specific list of which tools diverge.

### The new flow

When a friction pattern recurs enough times to trip the
threshold, the workflow now does this:

It looks up which invariants the pattern is linked to. If none
— because the operator hasn't yet authored the normative rule
the symptom violates — the workflow falls back to today's
behavior unchanged. Existing patterns keep working. Migration
is incremental.

If there is a linked invariant, the workflow records a closure
attempt: a row in a SQLite table saying "we're trying to fix
this; here's the invariant we're verifying against; here's the
probe we'll use." Then it asks Claude Code to investigate, the
same way it does today. Claude Code does its work, files its
response, the workflow reads it.

Then — and this is the new part — the workflow runs the probe.
The probe is bounded, read-only, and looks at the actual
substrate state. If the probe passes, the pattern transitions
to resolved, the closure attempt is marked succeeded, and the
world matches the workflow's claim. If the probe fails, the
pattern stays right where it was. A `closure.probe_failed`
event drops into the substrate event stream with full evidence
— which tool diverged, what the probe found, what evidence to
look at. The pattern doesn't lie about being fixed when it
isn't.

### What the operator sees differently

You'll see closure attempts in a new table. Each one is a small
story: which pattern recurred, which invariant it was supposed
to violate, what route Kernos took to fix it, what the probe
found, whether it passed or failed. The audit trail is no
longer "CC said this" — it's "CC said this, and here's whether
the substrate confirms it."

When a fix doesn't hold, you'll see a `closure.probe_failed`
event with specific evidence. Not "something went wrong" —
actually "the tool catalog still has X registered but the
dispatcher can't call it; that's the same divergence as
before." The operator can read the evidence and decide whether
to retry, escalate, or take the conversation back into their
own hands.

For the seed scenario specifically: the moment this spec lands,
Kernos can deterministically verify whether its own tool
catalog matches its own dispatcher. If you ever again see the
situation where Kernos claims a tool exists but can't actually
call it, that's now a first-class catchable invariant violation
rather than a vague feeling that something seems off.

### What this opens up

The Tool Availability Honesty invariant is the first instance,
not the whole point. The point is the *shape*. Every future
invariant — about merged-message preservation, about
preference detection, about whatever Kernos discovers it
should never silently violate — follows the same pattern.
Author the rule. Link the symptom. Define the probe. Let the
workflow verify rather than assume.

The substrate becomes self-checking. Bugs don't quietly
resolve into "we asked someone about it." They resolve into
"the substrate now demonstrably honors the rule that bug
violated." The autonomy loop grows teeth without growing
complexity — the same workflow runs; the difference is what
*resolved* now means.

The architectural philosophy underneath is small but
load-bearing: **fixes don't close until verified.** Everything
else — the tables, the tools, the probe handlers, the workflow
YAML — is plumbing to make that one sentence operationally
true.

---

The remainder of this document is the technical spec the
implementation builds against. The plumbing details below
implement the shape described above.

---

**Date:** 2026-05-24 (v4 after Codex round-4 fold)
**Status:** Draft for review
**Scope:** Introduces `Invariant` as a first-class primitive, links
  it to `FrictionPattern` via a many-to-many table, adds
  `ClosureAttempt` with a stored deterministic probe, and modifies
  `specs/workflows/self_improvement.workflow.yaml` to use an
  explicit `branch` action so a pattern is marked `resolved` only
  after probe verification — not on receipt of CC's investigation
  response. Seeds the spec on the **Tool Availability Honesty
  Invariant** with a deterministic catalog-vs-dispatch parity
  probe against existing substrate APIs.
**Estimated size:** ~450 LOC source (kernel tools, table migrations,
  workflow YAML) + ~350 LOC tests.

## Why this spec exists

Kernos already has a partially-operating self-improvement loop:
`FrictionObserver` writes signals → `friction_patterns.py`
classifies them → `FrictionPatternFrequencyEmitter` (which now
handles both reactivation AND active-pattern threshold crossings)
emits `friction.pattern_frequency_threshold_exceeded` → the
`self_improvement` workflow asks Claude Code via
`ask_coding_session_for_workflow`, awaits the response, and marks
the pattern resolved.

The loop has one architectural lie: it treats CC's investigation
response as proof the system is fixed. The workflow unconditionally
calls `transition_friction_pattern_lifecycle(new_state=resolved)`
on `step.ask_cc` completion, without verifying that the live
substrate no longer exhibits the friction the pattern names.

This spec closes that gap with a stored deterministic probe that
must pass before the pattern transitions to `resolved`, and
formalizes the **invariant** the pattern was violating so the
probe has a normative target.

## Audit findings (what exists, what's missing)

**Already operational:**

- `FrictionObserver` (`kernos/kernel/friction.py`) — post-turn
  signal detection (`TOOL_REQUEST_FOR_SURFACED_TOOL`,
  `MERGED_MESSAGES_DROPPED`, `TOOL_AVAILABLE_BUT_NOT_USED`,
  `EMPTY_RESPONSE`, `PROVIDER_ERROR_REPEATED`).
- `friction_patterns.py` — SQLite-backed pattern catalog with
  stable IDs, lifecycle states, `active_epoch` field
  (activation-episode counter), auto-classifier, recurrence
  tracking.
- `FrictionPatternFrequencyEmitter` — handles both resolved-pattern
  reactivation and active-pattern threshold crossings.
- `self_improvement.workflow.yaml` — workflow triggered by the
  frequency event.
- Workflow action library — already supports `notify_user`,
  `write_canvas`, `route_to_agent`, `call_tool`, `post_to_service`,
  `mark_state`, `append_to_ledger`, `branch`. The new operations
  this spec defines ride on existing `call_tool` and `branch`
  verbs — no new action types.
- `BranchAction` (`kernos/kernel/workflows/action_library.py:678`) —
  takes a strict-bool `condition`, routes to
  `branch_on_true`/`branch_on_false` step IDs.
- `ToolCatalog.get_all()` / `get_names()`
  (`kernos/kernel/tool_catalog.py`) — substrate's current
  registration state.
- `DispatchGate.classify_tool_effect()`
  (`kernos/kernel/gate.py`) — substrate's current classification
  surface.
- `ReasoningService` kernel tool dispatch table
  (`kernos/kernel/reasoning.py:_KERNEL_TOOLS` + the
  `if tool_name in self._KERNEL_TOOLS:` branch in `execute_tool`).
- `improve_kernos` orchestrator and related primitives.
- `TOOL_ALIAS_REPAIRED` event corpus accumulating (shipped 567fc12).

**Missing — what this spec adds:**

1. First-class `Invariant` primitive (no example fields in v1).
2. `friction_pattern_invariant` many-to-many link table.
3. `ClosureAttempt` row with stored probe + outcome.
4. Three new kernel tools (`record_closure_attempt`,
   `run_closure_probe`, `lookup_pattern_invariants`) registered
   via the normal tool-catalog surface and invoked via the
   existing `call_tool` action verb.
5. Workflow YAML modifications using `branch` for the fallback
   path (zero linked invariants).
6. Explicit substrate semantics for "what happens after a failed
   probe" — durable `closure.probe_failed` event, ClosureAttempt
   row preserved, pattern stays in current lifecycle state.

## Design principles (load-bearing)

- **Don't conflate response with resolution.** CC `completed`
  means investigation completed, not fix operationally verified.
- **Invariants are normative; patterns are observational.** Many-
  to-many link table, not 1:1 embedding.
- **v1 closure probes are read-only.** Hard-rejected via allowlist.
  No receipt-bypass loophole — mutating probes require a future
  registration/authorization spec.
- **Routing is persisted data, not classification intelligence.**
- **Probe definitions are versioned with the invariant** via
  `probe_payload_version`.
- **Outcome vocabulary is ADDITIVE in v1.** Legacy
  `investigation_outcome` field preserved unchanged for existing
  audit consumers; new `closure_outcome` field added to
  `extra_payload`.
- **Workflow does not add new approval gates** — the new
  operations are synchronous kernel tools invoked via existing
  `call_tool`. Single-worker engine's 86400s CC-gate timeout
  remains the worst-case blocking window for this spec.
- Per [[agent-facing-natural-simplicity]] — agent-facing surfaces
  (workflow status, probe outcomes) are natural prose; operator-
  facing surfaces (SQLite tables, event payloads) carry full
  structured state.

## New primitives

### Invariant table

```sql
CREATE TABLE invariant (
    instance_id TEXT NOT NULL,
    invariant_id TEXT NOT NULL,
    statement TEXT NOT NULL,
    owner TEXT NOT NULL,             -- "architect" | "operator" | "kernos"
    status TEXT NOT NULL DEFAULT 'active',  -- "active" | "deprecated"
    created_at TEXT NOT NULL,
    last_edited TEXT NOT NULL,
    PRIMARY KEY (instance_id, invariant_id)
);
```

Immutable `invariant_id`; mutable `statement` (with a
`last_edited` bump). No example fields in v1.

### friction_pattern_invariant link table

```sql
CREATE TABLE friction_pattern_invariant (
    instance_id TEXT NOT NULL,
    pattern_id TEXT NOT NULL,
    invariant_id TEXT NOT NULL,
    relation TEXT NOT NULL DEFAULT 'violates',
    created_at TEXT NOT NULL,
    PRIMARY KEY (instance_id, pattern_id, invariant_id, relation),
    FOREIGN KEY (instance_id, pattern_id)
        REFERENCES friction_pattern(instance_id, pattern_id),
    FOREIGN KEY (instance_id, invariant_id)
        REFERENCES invariant(instance_id, invariant_id)
);
```

Many-to-many. `relation` v1 only uses `violates`; future values may
include `informs`, `compounds_with`.

### ClosureAttempt table

```sql
CREATE TABLE closure_attempt (
    instance_id TEXT NOT NULL,
    closure_id TEXT NOT NULL,
    pattern_id TEXT NOT NULL,
    invariant_id TEXT NOT NULL,
    active_epoch INTEGER NOT NULL,
    route TEXT NOT NULL,                -- one of ROUTE_CLASSES
    route_payload_json TEXT NOT NULL,   -- JSON; route-specific (e.g. CC request_id)
    probe_kind TEXT NOT NULL,           -- one of PROBE_KINDS
    probe_payload_json TEXT NOT NULL,   -- JSON; serializable probe definition
    probe_payload_version INTEGER NOT NULL,
    outcome TEXT NOT NULL DEFAULT 'pending',  -- "pending" | "passed" | "failed" | "aborted"
    outcome_evidence_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY (instance_id, closure_id),
    FOREIGN KEY (instance_id, pattern_id)
        REFERENCES friction_pattern(instance_id, pattern_id),
    FOREIGN KEY (instance_id, invariant_id)
        REFERENCES invariant(instance_id, invariant_id)
);

-- One pending closure per (pattern, invariant, episode).
CREATE UNIQUE INDEX closure_attempt_pending_unique
    ON closure_attempt(instance_id, pattern_id, invariant_id, active_epoch)
    WHERE outcome = 'pending';
```

Note: the uniqueness key is **(instance_id, pattern_id,
invariant_id, active_epoch)**. `active_epoch` is the pattern's
activation-episode counter (already a column on `friction_pattern`).
If a pattern links to multiple invariants, one ClosureAttempt per
invariant per episode.

**ROUTE_CLASSES (v1 enumerated, hand-authored per pattern):**

- `code_change_via_cc` — ask_coding_session_for_workflow (current default)
- `covenant_update` — author or amend a covenant
- `prompt_change` — update template / system prompt
- `tool_surface_fix` — modify catalog / surfacing / gate
- `codex_review_only` — adversarial review, no implementation
- `human_only` — escalate to operator; no automated action

v1 ships `code_change_via_cc` only as the implemented route. The
other route names are enumerated in the spec as the persistence
vocabulary (so a ClosureAttempt can record what route it took
when a future spec implements those handlers), but the workflow
YAML in v1 does NOT branch on route — every closure-path attempt
records `route="code_change_via_cc"` at
`record_closure_attempt`. Adding YAML branch logic + handlers
for the other routes is follow-up spec work
(`SELF-IMPROVEMENT-CLOSURE-ROUTES-V2` candidate).

**PROBE_KINDS (v1 enumerated):**

- `deterministic_introspection` — read-only enumeration over
  substrate state, returns pass/fail with structured evidence.

`event_absence_window` and `manual_operator_confirmation` are
EXPLICITLY DEFERRED out of v1:

- `manual_operator_confirmation` requires either a new approval
  gate (which violates the no-new-gates principle for this spec)
  or a non-blocking poll pattern. Both are follow-up work.
- `event_absence_window` requires a "relevant opportunity"
  counter the substrate doesn't currently track per pattern.

## New kernel tools (NOT new workflow action verbs)

Per Codex round-2 High 2: `record_closure_attempt` and
`run_closure_probe` are KERNEL TOOLS, registered in the
`ReasoningService._KERNEL_TOOLS` dispatch table and invoked from
the workflow YAML via the existing `call_tool` action verb. They
classify in `DispatchGate.classify_tool_effect()` like any other
kernel tool — NOT in `workflow_action_classification.py`.

### Workflow dispatch adapter wiring (Codex round-3 High 3)

`ReasoningService._KERNEL_TOOLS` registration alone is NOT enough.
The workflow's `call_tool` action goes through the autonomy-tool
adapter at `kernos/setup/bring_up_substrate.py:1149`, which only
routes a fixed allowlist (`autonomy_tool_ids`). This spec extends
that allowlist with the three new closure tools and adds direct
handlers for each:

```python
# In bring_up_substrate.py _call_tool_adapter
autonomy_tool_ids = frozenset({
    # existing
    "transition_friction_pattern_lifecycle",
    "record_friction_pattern_recurrence",
    "emit_autonomy_loop_event",
    "ask_coding_session_for_workflow",
    "read_coding_session_response_for_workflow",
    # SELF-IMPROVEMENT-CLOSURE-V1
    "lookup_pattern_invariants",
    "record_closure_attempt",
    "run_closure_probe",
})
```

Direct handlers live in a new module
`kernos/kernel/workflows/closure_tools.py` with the same calling
convention as the existing autonomy handlers
(`instance_id, member_id, args` keyword args).

### `record_closure_attempt`

```python
async def record_closure_attempt(
    *,
    instance_id: str,
    pattern_id: str,
    invariant_id: str,
    active_epoch: int,
    route: str,
    route_payload: dict,
    probe_kind: str,
    probe_payload: dict,
    probe_payload_version: int,
) -> dict:
    """Insert a ClosureAttempt row with outcome='pending'.

    Idempotent on (instance_id, pattern_id, invariant_id,
    active_epoch): if a pending row already exists for this key,
    return that row's closure_id rather than insert. This makes
    workflow retry safe (Codex finding #6: idempotent retry
    semantics).

    PATTERN-INVARIANT LINK VALIDATION (Codex round-3 medium):
    rejects with InvariantNotLinkedToPattern if no row exists in
    friction_pattern_invariant for (instance_id, pattern_id,
    invariant_id). The link table is the single source of truth
    for the pattern↔invariant relationship; record_closure_attempt
    will not create closures for unlinked pairs.

    Hard-rejects probe_kind not in READ_ONLY_PROBE_KINDS allowlist;
    no payload-based bypass.

    Returns: {"closure_id": str, "newly_created": bool}.
    """
```

Gate classification: `soft_write` (bounded-scope insert; idempotent
on the uniqueness key).

### `run_closure_probe`

```python
async def run_closure_probe(
    *,
    instance_id: str,
    closure_id: str,
) -> dict:
    """Execute the stored probe for a ClosureAttempt.

    IDEMPOTENT REPLAY: if the ClosureAttempt row's outcome is
    NOT 'pending', return the stored outcome + evidence WITHOUT
    re-running the probe, re-transitioning the pattern, or re-
    emitting closure.probe_failed. This makes engine-retry safe
    (Codex round-3 medium-high: idempotent retry semantics).

    On outcome='pending':
      Reads probe_kind + probe_payload from the row; dispatches
      to the appropriate probe handler. v1 dispatch table:
        deterministic_introspection → _run_deterministic_probe

      On pass: updates outcome='passed', completed_at, evidence;
          transitions friction_pattern to 'resolved'.
      On fail: updates outcome='failed', completed_at, evidence;
          pattern stays in current lifecycle state;
          emits closure.probe_failed event with full evidence.

    Hard-rejects if probe_kind not in READ_ONLY_PROBE_KINDS.

    Returns: {"outcome": str, "evidence": dict, "replayed": bool}.
    """
```

Gate classification: `soft_write` (Codex round-4 fix). The
PROBE HANDLER (`_run_deterministic_probe`) is read-only — pure
in-memory enumeration against catalog/gate/dispatch. But the
WRAPPER TOOL `run_closure_probe` performs bounded SQLite writes
(updates `closure_attempt` outcome + evidence; may transition
the friction pattern lifecycle to `resolved`) and emits the
`closure.probe_failed` substrate event on failure. Those are
soft writes, not reads.

`READ_ONLY_PROBE_KINDS` constrains the HANDLER side (the
substrate inspection itself must not mutate); the WRAPPER's
recording-and-lifecycle work is the soft-write portion the gate
classifies on.

### `lookup_pattern_invariants`

```python
async def lookup_pattern_invariants(
    *,
    instance_id: str,
    pattern_id: str,
) -> dict:
    """Return the primary invariant linked to this pattern plus
    a native-bool has_invariants for workflow branching.

    Returns:
      {
        "has_invariants": bool,            # for {step.X.value.has_invariants}
        "primary_invariant_id": str | "",  # first by deterministic ordering
                                           # (invariant_id ASC); empty string
                                           # when has_invariants is False.
        "all_invariant_ids": [str],        # full list (informational only);
                                           # workflow refs scalar field only.
      }
    """
```

The ref resolver (`kernos/kernel/workflows/refs.py:267`) walks
dict keys only — no list indexing. So the workflow YAML
references `{step.lookup_invariants.value.primary_invariant_id}`,
NOT `{step.lookup_invariants.value.invariant_ids.0}`.
`all_invariant_ids` is included for operator audit / future
multi-invariant routing but is NOT referenced by v1 workflow
steps.

Gate classification: `read`.

## Modified workflow

Changes to `specs/workflows/self_improvement.workflow.yaml`. The
spec uses explicit `branch` (Codex round-2 High 1) for the
zero-invariant fallback. The legacy path lives in
`terminal_branches.legacy_fallback` (Codex round-3 High 1) so the
closure path's main-sequence steps DO NOT fall through into the
legacy steps after `emit_outcome_closure`.

**New action_sequence + terminal_branches** (replaces lines
72-136 of current YAML):

```yaml
action_sequence:
  - id: record_recurrence
    action_type: call_tool
    parameters:
      tool_id: record_friction_pattern_recurrence
      args:
        pattern_id: '{idea_payload.pattern_id}'
        classified_by: auto-signal-type
    continuation_rules:
      on_failure: abort

  # NEW: look up invariants linked to this pattern.
  - id: lookup_invariants
    action_type: call_tool
    parameters:
      tool_id: lookup_pattern_invariants
      args:
        pattern_id: '{idea_payload.pattern_id}'
    continuation_rules:
      on_failure: abort

  # NEW: branch on has_invariants. True → closure path (stays in
  # main sequence). False → terminal legacy_fallback branch
  # (cannot fall through back into main).
  - id: branch_on_invariants
    action_type: branch
    parameters:
      condition: '{step.lookup_invariants.value.has_invariants}'
      branch_on_true: record_closure_attempt
      branch_on_false: 'terminal:legacy_fallback:legacy_ask_cc'
    continuation_rules:
      on_failure: abort

  # ─── CLOSURE PATH (main sequence continues) ───────────────────
  - id: record_closure_attempt
    action_type: call_tool
    parameters:
      tool_id: record_closure_attempt
      args:
        pattern_id: '{idea_payload.pattern_id}'
        invariant_id: '{step.lookup_invariants.value.primary_invariant_id}'
        active_epoch: '{idea_payload.active_epoch}'
        route: code_change_via_cc
        route_payload: {}
        probe_kind: deterministic_introspection
        probe_payload: {}    # resolved from pattern_id → probe map at tool time
        probe_payload_version: 1
    continuation_rules:
      on_failure: abort

  - id: ask_cc_closure
    action_type: call_tool
    parameters:
      tool_id: ask_coding_session_for_workflow
      args:
        target: claude_code
        question: 'Address recurring friction pattern: {idea_payload.pattern_id} (invariant: {step.lookup_invariants.value.primary_invariant_id})'
        context:
          pattern_id: '{idea_payload.pattern_id}'
          invariant_id: '{step.lookup_invariants.value.primary_invariant_id}'
          closure_id: '{step.record_closure_attempt.value.closure_id}'
          _workflow_execution_id: '{workflow.execution_id}'
          _workflow_gate_nonce: '{workflow.gate_nonce}'
    continuation_rules:
      on_failure: abort
    gate_ref: await_cc_response_closure

  - id: read_response_closure
    action_type: call_tool
    parameters:
      tool_id: read_coding_session_response_for_workflow
      args:
        request_id: '{step.ask_cc_closure.value.request_id}'
    continuation_rules:
      on_failure: abort

  - id: run_closure_probe
    action_type: call_tool
    parameters:
      tool_id: run_closure_probe
      args:
        closure_id: '{step.record_closure_attempt.value.closure_id}'
    continuation_rules:
      on_failure: abort

  - id: emit_outcome_closure
    action_type: call_tool
    parameters:
      tool_id: emit_autonomy_loop_event
      args:
        workflow_id: self_improvement
        outcome: '{step.read_response_closure.value.investigation_outcome}'  # LEGACY preserved
        addresses_friction_patterns:
          - '{idea_payload.pattern_id}'
        extra_payload:
          closure_outcome: '{step.run_closure_probe.value.outcome}'  # NEW additive
          closure_id: '{step.record_closure_attempt.value.closure_id}'
          invariant_id: '{step.lookup_invariants.value.primary_invariant_id}'
    continuation_rules:
      on_failure: abort

# ─── TERMINAL BRANCH: legacy_fallback (Codex round-3 High 1) ────
# Reached only from branch_on_invariants on False. Cannot fall
# through into main action_sequence after completion.
terminal_branches:
  legacy_fallback:
    - id: legacy_ask_cc
      action_type: call_tool
      parameters:
        tool_id: ask_coding_session_for_workflow
        args:
          target: claude_code
          question: 'Address recurring friction pattern: {idea_payload.pattern_id}'
          context:
            pattern_id: '{idea_payload.pattern_id}'
            _workflow_execution_id: '{workflow.execution_id}'
            _workflow_gate_nonce: '{workflow.gate_nonce}'
      continuation_rules:
        on_failure: abort
      gate_ref: await_cc_response_legacy

    - id: legacy_read_response
      action_type: call_tool
      parameters:
        tool_id: read_coding_session_response_for_workflow
        args:
          request_id: '{step.legacy_ask_cc.value.request_id}'
      continuation_rules:
        on_failure: abort

    - id: legacy_mark_resolved
      action_type: call_tool
      parameters:
        tool_id: transition_friction_pattern_lifecycle
        args:
          pattern_id: '{idea_payload.pattern_id}'
          new_state: resolved
          resolved_by_spec: self_improvement
      continuation_rules:
        on_failure: abort

    - id: legacy_emit_outcome
      action_type: call_tool
      parameters:
        tool_id: emit_autonomy_loop_event
        args:
          workflow_id: self_improvement
          outcome: '{step.legacy_read_response.value.investigation_outcome}'
          addresses_friction_patterns:
            - '{idea_payload.pattern_id}'
          extra_payload:
            closure_outcome: no_invariant_fallback
      continuation_rules:
        on_failure: abort

# ─── APPROVAL GATES (Codex round-3 direct answer #6) ────────────
# Both paths need their own gate declarations. Each is a separate
# pause-point with its own approval_event_predicate binding.
approval_gates:
  - gate_name: await_cc_response_closure
    pause_reason: awaiting coding session response (closure path)
    approval_event_type: coding_consult.response_received
    approval_event_predicate:
      op: eq
      path: payload.request_id
      value: '{step.ask_cc_closure.value.request_id}'
    timeout_seconds: 86400
    bound_behavior_on_timeout: abort_workflow

  - gate_name: await_cc_response_legacy
    pause_reason: awaiting coding session response (legacy path)
    approval_event_type: coding_consult.response_received
    approval_event_predicate:
      op: eq
      path: payload.request_id
      value: '{step.legacy_ask_cc.value.request_id}'
    timeout_seconds: 86400
    bound_behavior_on_timeout: abort_workflow
```

Closure path executes through `emit_outcome_closure` and
terminates naturally at the end of main `action_sequence`. Legacy
fallback path runs entirely inside the `terminal_branches`
declaration; execution cannot return to main sequence after
entering a terminal branch.

## Seed invariant: Tool Availability Honesty

```python
Invariant(
    invariant_id="tool-availability-honesty",
    statement=(
        "If the substrate's tool catalog registers a tool, that "
        "tool must be classifiable by the dispatch gate AND "
        "dispatchable through the kernel-tool execution path (or "
        "the MCP execution path for MCP-sourced tools). Tools "
        "present in the catalog but unclassifiable / unreachable "
        "represent a silent capability-claim vs callability "
        "divergence."
    ),
    owner="architect",
    status="active",
)
```

**Linked friction pattern:** authored as part of v1 with signal
type `CAPABILITY_CATALOG_DISPATCH_DIVERGENCE` (new).

**v1 detection mode: probe-only, no automatic FrictionObserver
detector** (Codex round-4 fix). v1 ships the invariant + linked
pattern + probe machinery, but does NOT add a corresponding
FrictionObserver detector that auto-emits
`CAPABILITY_CATALOG_DISPATCH_DIVERGENCE` signals during turn
trace. Reasons:

- Adding the detector requires deciding what trace shape
  reliably surfaces "model requested a tool that's catalog-
  registered but unreachable" — and whether the detector should
  fire post-turn (after a failed dispatch) or via a periodic
  substrate-state audit. Both modes are plausible; v1 defers
  the choice until the closure machinery is shipped and
  operator evidence informs the right detector shape.
- The closure machinery itself doesn't require the detector
  to validate. Operator can manually:
  1. Insert a `friction_pattern_invariant` link row connecting
     the seed pattern to the invariant.
  2. Insert a `ClosureAttempt` row manually via a pattern-
     catalog tool (or directly via SQLite for v1 smoke).
  3. Invoke `run_closure_probe` against the closure_id.
  4. Observe probe pass/fail against the live substrate.

  That manual flow exercises every load-bearing piece of v1
  WITHOUT requiring an auto-detector.

The follow-up sub-spec `CAPABILITY-CATALOG-DISPATCH-DETECTOR-V1`
adds the FrictionObserver detector. v1 acceptance criteria
explicitly do NOT require auto-detection. (Distinct from
`TOOL_REQUEST_FOR_SURFACED_TOOL` which catches "model requested
a non-surfaced tool" — that's a surfacing issue, not a
catalog/dispatch parity issue.)

**Probe definition (using public substrate APIs):**

Codex round-3 medium: the probe MUST NOT reach into private
`ReasoningService._KERNEL_TOOLS`. Codex round-4 medium: the
helper MUST NOT equate `_KERNEL_TOOLS` membership with
dispatchability, because `execute_tool` returns
`"Kernel tool '<name>' not handled."` for any name in
`_KERNEL_TOOLS` without a real handler branch — exactly the
catalog-vs-dispatch divergence the invariant catches.

This spec introduces a SEPARATE explicit registry that names
ONLY the tools with confirmed handler branches:

```python
class ReasoningService:
    # SELF-IMPROVEMENT-CLOSURE-V1: explicit dispatchability
    # registry. Every name in this set MUST have a concrete
    # branch in execute_tool that does NOT return the
    # "Kernel tool '<name>' not handled." sentinel. Adding a
    # name here without a handler breaks AC17's test.
    _DISPATCHABLE_KERNEL_TOOLS: frozenset[str] = frozenset({
        # ... enumerated explicitly; subset of _KERNEL_TOOLS
        # restricted to names with verified handlers
    })

    def get_dispatchable_kernel_tools(self) -> set[str]:
        """Return the set of tool names with confirmed dispatch
        paths through execute_tool. Public surface for substrate
        parity probes (SELF-IMPROVEMENT-CLOSURE-V1).

        Contract: every returned name has a concrete handler
        branch in execute_tool that does NOT return the
        "Kernel tool '<name>' not handled." sentinel. The
        returned set is a subset of _KERNEL_TOOLS by
        construction; names in _KERNEL_TOOLS but NOT in
        _DISPATCHABLE_KERNEL_TOOLS represent registration drift
        and are exactly what the Tool Availability Honesty
        probe detects.
        """
        return set(self._DISPATCHABLE_KERNEL_TOOLS)
```

v1 implementation seeds `_DISPATCHABLE_KERNEL_TOOLS` by walking
the actual `if/elif` chain in `execute_tool` and enumerating
every name that has a real handler. AC17 enforces the contract
via a test that for every name in
`get_dispatchable_kernel_tools()`, calling
`execute_tool(name, {}, mock_request)` does NOT return the
sentinel string.

Probe definition:

```python
{
    "kind": "deterministic_introspection",
    "payload": {
        "description": (
            "Enumerate all entries in ToolCatalog.get_all(). For "
            "each, verify (a) DispatchGate.classify_tool_effect() "
            "returns a value other than 'unknown' AND (b) the "
            "tool is reachable in the dispatch path: kernel-tool "
            "source must be in "
            "ReasoningService.get_dispatchable_kernel_tools(), "
            "MCP-source must have catalog source equal to 'mcp' or "
            "starting with 'mcp:'. Any catalog entry failing "
            "either check is a divergence."
        ),
        "data_sources": [
            "kernos.kernel.tool_catalog.ToolCatalog.get_all",
            "kernos.kernel.gate.DispatchGate.classify_tool_effect",
            "kernos.kernel.reasoning.ReasoningService.get_dispatchable_kernel_tools",
        ],
        "pass_condition": (
            "for every CatalogEntry e: "
            "classify_tool_effect(e.name) != 'unknown' AND "
            "(e.name in get_dispatchable_kernel_tools() "
            "OR e.source == 'mcp' "
            "OR e.source.startswith('mcp:'))"
        ),
    },
    "version": 1,
}
```

Notes on substrate-state vs turn-state:
- Probe runs entirely against substrate state at probe execution
  time (post-turn, post-workflow-trigger).
- No "current turn" context required.
- MCP source check uses `e.source == "mcp"` (current registration
  string per `kernos/messages/handler.py:1079`) OR
  `startswith("mcp:")` (future-compatible with colon-prefix
  variants).
- Probe is bounded: no network calls, no subprocess invocations,
  no SQLite writes. Pure in-memory enumeration. Bounded timeout
  enforced at the kernel-tool level (default 5s; configurable
  via `KERNOS_CLOSURE_PROBE_TIMEOUT_SECONDS`).

## What happens after a failed probe (Codex finding #6 subtle case)

When `run_closure_probe` returns `outcome="failed"`:

1. `ClosureAttempt` row is updated: `outcome='failed'`,
   `completed_at` set, `outcome_evidence` populated.
2. Friction pattern stays in current state (`active` or
   `reactivated`). NOT transitioned to `resolved`.
3. `active_epoch` is NOT bumped — the failed closure is not a
   re-activation; it's a failed remediation within the same
   episode.
4. `closure.probe_failed` event emitted with payload:
   `{pattern_id, invariant_id, closure_id, active_epoch,
   evidence}`. This event is durable substrate-tier signal.
5. **NO automatic re-trigger within the same episode** (Codex
   round-3 medium correction). Active-pattern occurrences do NOT
   bump `active_epoch`; the frequency emitter's dedup keyed on
   `active_epoch` means subsequent occurrences within the same
   episode will NOT re-fire the workflow. Another closure
   attempt requires EITHER:
   - explicit operator action (e.g., manual ClosureAttempt
     insert via a pattern-catalog tool), OR
   - a lifecycle transition that creates a new activation
     episode (e.g., transition to `resolved` and a subsequent
     reactivation via `record_recurrence`).
6. v1 does NOT add automated retry. The `closure.probe_failed`
   event is durable signal that operator-facing tooling or a
   future spec can react to.

Auto-retry, escalation routing, and operator-facing closure-failure
summaries are explicitly follow-up spec work
(`SELF-IMPROVEMENT-CLOSURE-RETRY-V1`, candidate).

## Six seams (Codex consultation flagged these — addressed)

1. **Per-pattern concurrency.** Unique partial index on
   `closure_attempt(instance_id, pattern_id, invariant_id,
   active_epoch) WHERE outcome='pending'`. Second pending insert
   raises `IntegrityError`; `record_closure_attempt` tool catches
   this and returns the existing row's `closure_id` (idempotent
   retry semantics per Codex finding #6).

2. **Lifecycle ordering.** Pattern stays in current state until
   `run_closure_probe` returns `passed`. Mid-state lives in
   `ClosureAttempt.outcome='pending'`, not in pattern lifecycle.
   `transition_friction_pattern_lifecycle(new_state=resolved)` is
   called from inside `run_closure_probe` on probe pass, not from
   a separate workflow step.

3. **Routing idempotency.** `ClosureAttempt.route` written once at
   `record_closure_attempt`. Workflow retry hits the idempotent
   path — same closure_id returned, no re-classification.

4. **Gate classification.** New kernel tools classify in
   `DispatchGate.classify_tool_effect()`:
   - `record_closure_attempt` → `soft_write`
   - `run_closure_probe` → `soft_write` (handler is read-only;
     wrapper records outcome + may transition pattern lifecycle
     + may emit `closure.probe_failed`)
   - `lookup_pattern_invariants` → `read`

5. **Probe side effects.** Hard allowlist in
   `READ_ONLY_PROBE_KINDS` constant. Probe kinds not in allowlist
   raise `ProbeKindNotAllowed`. NO receipt-bypass loophole.
   Adding mutating probe kinds requires a future spec
   (probe-kind registration/authorization surface).

6. **Outcome semantics.** Legacy `outcome` field on
   `autonomy_loop_event` preserved unchanged. New `closure_outcome`
   field added to `extra_payload` (additive; existing audit
   consumers unaffected). Vocabulary: `passed`, `failed`,
   `no_invariant_fallback`.

## Acceptance criteria

**AC1 — Invariant table exists, instance-scoped.** Insert with
`(instance_id="i1", invariant_id="tool-availability-honesty",
statement=..., owner="architect", status="active")` succeeds. PK
violation on duplicate `(instance_id, invariant_id)`.

**AC2 — Link table exists, composite FK enforced.** Inserting a
link row with `(instance_id, pattern_id, invariant_id)` where
either the pattern or invariant doesn't exist for that
`instance_id` raises FK violation.

**AC3 — ClosureAttempt insertion + uniqueness on pending.**
Inserting a ClosureAttempt with `outcome='pending'` succeeds.
Second insert with same `(instance_id, pattern_id, invariant_id,
active_epoch)` AND `outcome='pending'` raises IntegrityError
(partial unique index). After first attempt resolves to `failed`,
a second pending insert succeeds (NOT blocked by the partial
index).

**AC4 — `record_closure_attempt` idempotent on retry.** Calling
the tool twice with same `(instance_id, pattern_id, invariant_id,
active_epoch)` returns the same `closure_id` with
`newly_created=True` on first call, `newly_created=False` on
second.

**AC5 — Workflow `lookup_invariants` + `branch_on_invariants`
routes correctly.** Pattern with linked invariant routes to
`record_closure_attempt`. Pattern with zero linked invariants
routes to `legacy_ask_cc`.

**AC6 — Workflow does NOT mark resolved before probe passes.**
For a pattern with linked invariant + a probe returning `failed`:
pattern stays in current state; `closure.probe_failed` event
emitted; `ClosureAttempt.outcome='failed'`. For a probe returning
`passed`: pattern transitions to `resolved`;
`ClosureAttempt.outcome='passed'`.

**AC7 — Seed probe runs against current substrate.** The Tool
Availability Honesty probe enumerates `ToolCatalog.get_all()`,
checks each against `classify_tool_effect()` and the
kernel-tool/MCP dispatch reachability. On a substrate where every
catalog entry is classifiable and reachable, probe returns
`passed`. On a fixture where a catalog entry is added without
gate classification, probe returns `failed` with evidence naming
the divergent tool.

**AC8 — Probe-kind allowlist hard-rejects.** Calling
`run_closure_probe` with a `closure_id` whose stored `probe_kind`
is NOT in `READ_ONLY_PROBE_KINDS` raises `ProbeKindNotAllowed`,
even if extra fields like `_approval_receipt` are present in
`route_payload`. No bypass.

**AC9 — Gate classification correct.**
`classify_tool_effect("record_closure_attempt") == "soft_write"`;
`classify_tool_effect("run_closure_probe") == "soft_write"`;
`classify_tool_effect("lookup_pattern_invariants") == "read"`.

**AC10 — Outcome vocabulary additive, not replacing.** Post-spec
`autonomy_loop_event` payload retains the legacy `outcome` field
with CC's investigation outcome string unchanged. Adds
`extra_payload.closure_outcome` with one of
`{"passed", "failed", "no_invariant_fallback"}`. Adds
`extra_payload.closure_id`, `extra_payload.invariant_id` on
closure-path runs; absent on legacy-path runs.

**AC11 — Failed probe emits durable event.** On probe fail,
`closure.probe_failed` event appears in the event stream with
payload `{pattern_id, invariant_id, closure_id, active_epoch,
evidence}`. Pattern's `active_epoch` is NOT bumped.

**AC12 — Concurrency: engine architecture unchanged.** The new
steps add NO new approval gates. Worst-case blocking window for
queued workflow executions equals the existing CC-gate timeout
(86400s) PLUS the synchronous closure-tool durations. Probes are
bounded: no network, no subprocess, no SQLite writes, default 5s
timeout (configurable via `KERNOS_CLOSURE_PROBE_TIMEOUT_SECONDS`).
"Unaffected" applies to architecture, not to the absolute
worst-case execution time.

**AC13 — record_closure_attempt rejects unlinked pattern-
invariant pairs.** Calling `record_closure_attempt` with a
`(pattern_id, invariant_id)` pair that has NO row in
`friction_pattern_invariant` raises
`InvariantNotLinkedToPattern`. The link table is the single
source of truth.

**AC14 — run_closure_probe idempotent on replay.** Calling
`run_closure_probe` with a `closure_id` whose `outcome` is NOT
'pending' returns the stored outcome + evidence with
`replayed=True`; does NOT re-execute the probe handler, NOT
re-transition the pattern, NOT re-emit `closure.probe_failed`.

**AC15 — Branch + terminal_branches topology validates.** The
modified workflow YAML parses without error; descriptor parser
accepts `terminal_branches.legacy_fallback`; branch validator
accepts `branch_on_false: terminal:legacy_fallback:legacy_ask_cc`
target syntax; cycle detector confirms no cycles.

**AC16 — Approval gates declared.** Workflow YAML's
`approval_gates` section declares both `await_cc_response_closure`
and `await_cc_response_legacy` with distinct predicates bound to
their respective `ask_cc_closure.value.request_id` /
`legacy_ask_cc.value.request_id` step refs.

**AC17 — Public dispatch-helper has strict contract.**
`ReasoningService.get_dispatchable_kernel_tools()` returns a
subset of `_KERNEL_TOOLS` (the explicit
`_DISPATCHABLE_KERNEL_TOOLS` registry). Test enforces: for every
name in the returned set, `execute_tool(name, {}, mock_request)`
does NOT return the `"Kernel tool '<name>' not handled."`
sentinel string. Probe data sources reference the public
helper, not the private `_KERNEL_TOOLS` attribute. Adding a name
to `_DISPATCHABLE_KERNEL_TOOLS` without a corresponding handler
branch breaks the test (forcing the dev to add the handler or
remove the registry entry).

## Out of scope (deferred)

- **Automatic positive_examples / negative_examples capture.**
- **LLM-based route classification.** Hand-authored per
  `pattern_id` in v1.
- **Procedure regeneration / canvas update on resolve.**
- **Project-level autonomy.**
- **`event_absence_window` probe kind.**
- **`manual_operator_confirmation` probe kind.** Requires
  non-blocking command/event contract; follow-up spec.
- **Mutating probe kinds.** Requires probe-kind registration
  /authorization surface; follow-up spec.
- **Auto-retry on failed closure.** `closure.probe_failed` event
  is durable signal; auto-retry is `SELF-IMPROVEMENT-CLOSURE-
  RETRY-V1` candidate.
- **Route handlers beyond `code_change_via_cc`.** The other
  ROUTE_CLASSES enum values are persisted but their workflow
  handlers are follow-up work.

## Test plan (scoped per [[feedback-test-scope-proposal]])

New tests:

- `tests/test_invariant_registry.py` — Invariant CRUD,
  friction_pattern_invariant link table mechanics, composite FK
  enforcement (AC1-AC2).
- `tests/test_closure_attempt.py` — ClosureAttempt CRUD,
  partial-unique-index behavior, idempotent `record_closure_attempt`
  tool semantics, probe-kind allowlist (AC3-AC4, AC8).
- `tests/test_self_improvement_closure_workflow.py` — workflow
  branch routing, closure vs legacy paths, probe pass/fail
  pattern-lifecycle effects (AC5-AC6, AC10-AC11).
- `tests/test_tool_availability_honesty_probe.py` — seed probe
  pass/fail against fixtures using real ToolCatalog +
  DispatchGate (AC7).
- `tests/test_dispatch_gate_closure_tools.py` — gate
  classifications for new kernel tools (AC9).

Regression touch on adjacent suites:
`tests/test_self_improvement_workflow.py` (existing — must still
pass for the legacy fallback path), `tests/test_friction*.py`
(existing — unaffected by this spec but touched by FK additions).

## Resolved pre-spec decisions (Codex round-3 direct answer #5)

**Seed pattern authoring is in-spec.** The
`CAPABILITY_CATALOG_DISPATCH_DIVERGENCE` friction pattern is
authored as part of this spec (AC7 depends on it). Pattern
definition added to v1 implementation deliverables:

```python
FrictionPattern(
    pattern_id="capability-catalog-dispatch-divergence",
    display_name="Capability claim vs dispatch divergence",
    description=(
        "A tool registered in ToolCatalog is either not "
        "classifiable by DispatchGate or not reachable through "
        "either the kernel-tool dispatch path or an MCP source. "
        "Silent capability-vs-callability divergence."
    ),
    signal_type_keys=["CAPABILITY_CATALOG_DISPATCH_DIVERGENCE"],
    lifecycle_state="active",
    # ... other catalog fields per friction_patterns.py schema
)
```

Plus the friction_pattern_invariant link row:

```python
{
    "pattern_id": "capability-catalog-dispatch-divergence",
    "invariant_id": "tool-availability-honesty",
    "relation": "violates",
}
```

These seed rows land via an idempotent migration helper invoked
during substrate bring-up.

## Open questions for architect ratification

1. **Legacy fallback retirement timeline.** This spec preserves
   no-invariant-fallback so unmigrated patterns keep working.
   Retirement could be: at a coverage threshold (X% of patterns
   have linked invariants), explicit architect call, or never
   (fallback is the long-term escape hatch for patterns we don't
   want to formalize).
2. **Route handler implementation cadence.** v1 implements only
   `code_change_via_cc`. Should the other ROUTE_CLASSES handlers
   land as follow-up sub-specs (one per route), or as a single
   "routes-V2" spec?

---

**Routing this spec:** drafted under founder authorization to
consult Codex. Round-1 ratified framing; round-2 found 8
substantive issues (folded); round-3 found 8 more (folded);
round-4 found 3 substantive fixes + 3 doc cleanups (folded into
this v4). Codex's round-4 verdict: "After those folds, I'd call
it green for founder ratification." Awaits founder ratification
before implementation per [[push-approval-semantics]].
