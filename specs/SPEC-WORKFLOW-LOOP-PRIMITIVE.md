# ## Design Review verdict (post-the design review re-review v4 → REVISE NARROWLY → v5 below)

**REVISE NARROWLY → ALL THREE v5 EDITS FOLDED.** the design review confirmed v3→v4 fixes are materially folded and substrate is clean. Three remaining edits folded surgically; one was load-bearing (gate semantics inversion), two were tighter scoping:

1. **Approval gate semantics inverted/ambiguous — fixed.** v4 said `gate_ref` paused the workflow *before* executing the action, but the design review example put `gate_ref` on the action that *sends* the approval request — which deadlocked because the approval request never got dispatched. Corrected v5 semantics: action with `gate_ref`**executes first** (sending the approval request), THEN the engine records `paused_for_approval` and waits for the matching approval event. The action's purpose is to *initiate* the approval; the gate's purpose is to *wait for the response*. Design Review-workflow Shape 4 example updated to reflect.
2. **`auto_proceed_with_default`constrained.** Cannot bypass real human approval for irreversible world-effect actions. Registration fails loudly with field-level error if `auto_proceed_with_default` is declared on a gate referenced by an irreversible world-effect action descriptor. Reversible internal actions (`mark_state`, `append_to_ledger`) may use any timeout behavior.
3. **English compiler wording cleaned.** v1 English compiler outputs the canonical predicate AST (or expression-string DSL form, which the parser then compiles to the canonical AST). Wording aligned with the predicate-syntax canonicalization from v3→v4.

⠀
v5 awaits the design review re-review. the design review's v4 bottom-line was "After that, I'd approve for CC." Three v5 edits land cleanly.

---

## Design Review verdict (v6 → REVISE NARROWLY → v7)

**SAFETY PIN MOVED TO POST-GATE CONTINUATION.** the design review confirmed v6 had the right shape but the safe-deny was attached to the wrong side of the gate semantics. With the corrected execution order (action executes → pause → wait → resume), the action carrying `gate_ref` has already executed by the time the gate pauses. The protected risk is the **post-gate continuation** — the next action(s) that timeout would allow without real human approval, NOT the gate-carrying action itself.

**v7 corrections:**

1. **§2 ApprovalGate descriptor:** safe-deny constraint reframed as "MUST NOT permit timeout-driven continuation into any subsequent action that is irreversible / world-effect." Validator inspects action descriptors AFTER the gated action up to next gate (or workflow end); any irreversible action in that downstream slice fails registration loudly.
2. **AC #41b:** rewritten as "safe-deny on irreversible post-gate continuation." Pin: registration walks the post-gate slice, not just the gate-carrying action.
3. **Live test scenario #3:** rewritten to test the actual bypass risk — reversible approval-request action with gate_ref + auto_proceed gate, followed by irreversible downstream action. Registration must fail with field-level error naming gate AND offending downstream action.

⠀
v7 awaits the design review re-review. the design review's v6 bottom-line: "Move safe-deny to the post-gate continuation semantics and add the downstream-action test; after that, this should be approval-ready." Both done.

---

## Design Review verdict (v5 → REVISE NARROWLY → v6)

**SAFE-DENY EDIT FOLDED.** the design review confirmed gate execution order, approval-gate tests, and compiler wording are clean. Remaining mismatch was on `auto_proceed_with_default`: header described safety constraint but body only required `default_value` to exist. v6 moves the stronger safe-deny / registration-failure rule into the load-bearing schema + AC.

**Safe-deny constraint:** a gate with `bound_behavior_on_timeout: "auto_proceed_with_default"` MUST NOT be referenced by any action descriptor whose action is **irreversible / world-effect**. Action library verbs are classified by reversibility at registration time; tool registry exposes per-tool `reversibility` attribute (default `irreversible` for unclassified tools per conservative-by-default principle). Registration walks action sequence; irreversible action with `auto_proceed_with_default` gate fails loudly. Live test scenario added pinning the safe-deny behavior.

AC #41 expanded to three sub-pins (a) default_value required, (b) safe-deny on irreversible actions, (c) tool registry reversibility classification at primitive ship time. Live test count ~12 → ~13.

v6 awaits the design review re-review.

---

## Design Review verdict (v4 → STILL REVISE NARROWLY → v5)

**ALL FOUR BODY-CONSISTENCY EDITS FOLDED.** v4 header described the gate fix correctly; body retained the deadlocking pre-execution pause semantics. v5 aligns body to header.

1. **Gate semantics corrected end-to-end.** Action executes first → engine records `paused_for_approval` AFTER action completes → waits for matching approval event → resumes. Both §2 (Workflow registry `gate_ref`description) and Shape 4 explanation in §Conceptual examples are corrected. The deadlock pattern (pause-before-execution would mean the approval-requesting action never fires) is gone.
2. **`auto_proceed_with_default`constraint** moved into descriptor schema, validation, and AC. ApprovalGate now has a `default_value` field; gate with `bound_behavior_on_timeout: "auto_proceed_with_default"` MUST declare non-null `default_value`; registration fails loudly otherwise. New AC #41.
3. **English compiler wording** in §6 updated to "translates English to canonical predicate AST." No residual ambiguity about emit shape.
4. **End-to-end approval-gate live test scenarios added** (happy path + timeout path). Live test count ~10 → ~12.

⠀
v5 awaits the design review re-review.

---

## Design Review verdict (post-the design review re-review v3 → REVISE NARROWLY → v4)

**REVISE NARROWLY → ALL SIX EDITS FOLDED in v4.** the design review confirmed v3's five seams are materially fixed and portable descriptors are the right addition. Six narrow tightenings folded: (1) stale "safety context = instance/member/space only" language removed; (2) redaction overclaim removed; (3) predicate syntax canonicalized to structured JSON AST with expression-string DSL accepted at registration; (4) approval gates first-class via top-level `approval_gates`; (5) `register_workflow(file_path)` atomicity pinned; (6) instance-specific-field allowlist defined.

v4 was supposed to be CC-ready but the design review's v4 review caught one load-bearing semantic flaw (gate execution inversion) plus two narrow constraints; v5 corrects.

---

## Design Review verdict (post-the design review review v1 → REVISE SPECIFICALLY → v2)

**REVISE SPECIFICALLY → ALL NINE EDITS FOLDED.** the design review's catches were substrate-correct in the strongest sense: v1 was about to invent a third event substrate when `kernos/kernel/event_stream.py` is shipped (per [SPEC — EVENT-STREAM-TO-SQLITE](https://www.notion.so/SPEC-EVENT-STREAM-TO-SQLITE-34bffafef4db81798016dc9bdb5146fe?pvs=21)) with the exact correlation/instance/member/space schema needed. Same lesson as IWL: verify against shipped reality before drafting; don't draft against what would be nice in theory.

**Key correctives in v2:**

1. **Extend the shipped `event_stream.py`substrate; do NOT create a new event bus.** Trigger registry and workflow engine register against the existing emit/read API. Event-type namespace extends the existing dotted convention (`workflow.*`, `bridge.inbox.*`, `time.*`).
2. **Workflow execution is queued/background**, never inline with emit. Queue-and-pop pattern; emit returns immediately per shipped contract; workflow engine drains a separate workflow-execution queue between turns.
3. **Event schema aligned with live shape:** `instance_id`, `member_id` (NOT tenant_id), `space_id`, `correlation_id`. v2 corrects every reference.
4. **Webhook path corrected.** `kernos/server.py` is a file, not a directory. Webhook receiver lives at `kernos/kernel/webhooks/receiver.py` with `server.py` registering the route.
5. **`route_to_agent`targets a Bridge/AgentInbox interface**, not Notion directly. v1 implementation MAY have a Notion-backed concrete; the verb's contract is interface-shaped so the local-folder migration is a concrete swap, not a verb rewrite.
6. **Out-of-turn covenant/safety seam defined.** Workflows running between turns construct a full CohortContext-equivalent synthetic safety context (matching the shipped cohort interface's required shape — see §3 for the full shape) that the existing covenant cohort surface consumes. No new covenant surface; reuse via context construction.
7. **Action library verbs are NOT all action-loop instances.** Only verbs whose intent-satisfaction is meaningfully verifiable wrap action-loop. Trivial deterministic verbs (`mark_state`, `append_to_ledger`) are direct effects with structural assertions, not action-loops. Per ACTION-LOOP-PRIMITIVE Anti-Goal: don't force LLM-judged verifiers on deterministic ops.
8. **Design Review-workflow predicate vocabulary added:** actor (who emitted the event), source (which subsystem), correlation_id (chain to a specific turn or workflow execution), idempotency_key (suppress duplicate firings).
9. **Persistence + restart-resume pinned.** Triggers and workflows persist in SQLite alongside events. Workflow executions in flight at shutdown record state to a `workflow_executions` table; on restart, the engine resumes from recorded state. Test pins restart-resume.

⠀
v2 awaits the design review re-review.

---

## Framing

**This spec adds the trigger surface, workflow descriptor format, action library, and execution engine to Kernos by composing on existing substrates.** It is NOT a new architectural primitive from scratch. Three existing substrates compose to produce workflow loops:

1. [ACTION-LOOP-PRIMITIVE](https://www.notion.so/ACTION-LOOP-PRIMITIVE-Spec-for-Review-34affafef4db81eab74fd08bd164e0f7?pvs=21) — the canonical five-step loop pattern (Receive Intent → Gather Context → Take Action → Verify → Decide), shipped 2026-04-22 with the design review SHIP AS-IS. Workflow loops are action-loop instances.
2. [EVENT-STREAM-TO-SQLITE](https://www.notion.so/SPEC-EVENT-STREAM-TO-SQLITE-34bffafef4db81798016dc9bdb5146fe?pvs=21) — durable per-instance event stream backed by SQLite at `kernos/kernel/event_stream.py`. Existing schema: `event_id`, `instance_id`, `member_id`, `space_id`, `timestamp`, `event_type` (dotted), `payload`, `correlation_id`. Six subsystems already emit. Workflow loops register against this substrate; do NOT create a parallel one.
3. The integration arc (cohorts, integration, enactment, presence) — the substrate workflows execute on top of, with covenant gating preserved through context construction.

⠀
A workflow loop is **an ACTION-LOOP-PRIMITIVE instance triggered by a registered event from the event stream**, executed asynchronously between turns, with output appended to a per-workflow ledger.

**Why now:** Owner has surfaced a concrete real workflow loop need (the design review workflow — the multi-step coordination cascade for spec design through implementation). Per the parked notes, the right conditions are met: integration arc complete, real friction signal present, multiple workflow loops queued.

**Why this spec is the right next step:** building the design review workflow as bespoke pipeline produces one-off coordination logic. Building the **primitive first and proving it by composition** produces (a) reusable substrate, (b) the design review workflow as clean composition, (c) confidence in the primitive through real exercise. The design review workflow's spec follows this one and validates the primitive.

## Goal

After this spec ships:

1. **Trigger registry exists** — registers against the shipped `event_stream.py` substrate. Maps `(event_type, predicate)` → workflow_id. Predicates evaluated deterministically against event payload.
2. **Workflow registry exists** — stores named workflows. Each has descriptor (event filter + action sequence + bounds + verifier), persisted in SQLite alongside events.
3. **Workflow execution engine exists** — drains a queued workflow-execution queue between turns; runs each as an ACTION-LOOP-PRIMITIVE instance; emits trace events through `event_stream.emit`.
4. **Action library exists** — bounded set of verbs (notify_user, write_canvas, route_to_agent, call_tool, post_to_service, mark_state, append_to_ledger). Verbs that wrap world-effect surfaces are action-loop instances with intent-satisfaction verification; trivial deterministic verbs are direct effects with structural assertions.
5. **Workflow ledger exists** — per-workflow append-only markdown log of executions. Brief synopses; no big code blocks; human-readable.
6. **English-to-structured trigger compiler exists** (v1, basic) — cheap LLM call at registration translates English description to structured form.
7. **Webhook receiver exists** at `kernos/kernel/webhooks/receiver.py` — accepts external events, validates, emits as `external.webhook` events to the shipped event stream. Route registered through `kernos/server.py`.
8. **Bridge/AgentInbox interface exists** — abstract interface that `route_to_agent` targets. v1 concrete: Notion-backed implementation that writes to existing inbox databases. Migration to local-folder concrete is a swap.
9. **Out-of-turn safety seam defined** — workflows running between turns construct a full CohortContext-equivalent synthetic context (the shipped cohort interface's required shape; see §3 in scope-in for the full shape) that existing covenant cohort surface consumes.
10. **Persistence + restart-resume.** Triggers, workflows, in-flight executions all persist in SQLite. Engine resumes in-flight executions on restart.
11. **No regression** on shipped invariants: ACTION-LOOP-PRIMITIVE preserved, all 11 PDI invariants holding, IWL composition + contract pins green, event stream contract preserved.
12. **Soak observable** through the existing event stream — workflow events compose with existing emitters.

⠀
## Scope-in

### 1. Trigger registry (extends event_stream substrate)

**New module:** `kernos/kernel/workflows/trigger_registry.py`.

* `Trigger` dataclass: `trigger_id` (UUIDv4), `workflow_id`, `event_type` (dotted, matches event_stream namespace), `predicate` (structured JSON), `description` (human-readable; possibly the original English form), `actor_filter`(which member_id may emit; nullable for any), `correlation_filter` (optional correlation_id chain), `idempotency_key_template` (for duplicate suppression), `owner`, `created_at`, `version`, `status`(active/paused/retired).
* **Registry subscribes to event_stream emissions, NOT to a parallel event bus.** Mechanism: `event_stream.emit`is extended with an in-process post-write hook that the trigger registry attaches to. Hook fires after the SQLite batch flush succeeds (so triggers only fire on durable events). NOT inline with emit — emit returns immediately per shipped contract; trigger evaluation happens on the writer task's flush callback.
* **Failure isolation (the design review edit, narrow review):** the post-flush hook is wrapped so any exception raised by trigger evaluation or workflow-execution enqueue is logged and contained — it MUST NOT propagate back into event_stream's flush path or block subsequent event writes. Durable event persistence stays independent of workflow code health. Pin: a trigger-evaluation exception during a flush leaves the event durably persisted and subsequent flushes proceeding normally.
* **Predicate format** — structured JSON AST is the **canonical machine shape** stored and evaluated. Operators may also write predicates in an **accepted expression-string DSL** (e.g. `event.payload.field == "value"`); the registration pipeline compiles the expression-string DSL to the canonical AST at registration time. Compiled AST is what the trigger registry stores and evaluates; expression-string source is preserved alongside as `predicate_source` for human reading. This means the YAML/JSON/Markdown loaders can accept either form: structured AST blocks (preferred for machine-authored / shared workflows) or expression-string predicates (preferred for human-authored). Examples in this spec's §Conceptual examples + portable workflow format use the expression-string form for readability; the loader compiles them to the AST at registration. Operators:
  * Equality: `payload.field == value`
  * Contains: `payload.field contains substring`
  * Present-or-absent: `payload.field exists` / `not exists`
  * Value-in-set: `payload.field in [...]`
  * Time-window: `event.timestamp within [start, end]`
  * **Actor:** `event.member_id == X` (who emitted)
  * **Source:** `event.event_type starts_with "subsystem.*"` (which subsystem)
  * **Correlation:** `event.correlation_id == X` (chain to specific turn/workflow)
  * **Idempotency:** `idempotency_key(event)` already-fired → suppress
  * Composable via AND/OR/NOT
  * Deterministic; no LLM calls at evaluation time.
* **Persistence** — new SQLite table `triggers` (alongside the shipped `events` table). Schema includes all Trigger fields plus indexes on `(instance_id, status, event_type)` for fast lookup.
* **Multi-tenancy** — keyed to `instance_id` from day one. All trigger lookups filter by current instance_id. Existing shipped multi-tenancy invariant preserved.
### 2. Workflow registry

**New module:** `kernos/kernel/workflows/workflow_registry.py`.

* `Workflow` dataclass: `workflow_id` (UUIDv4), `name`, `description`, `owner`, `version`, `action_sequence` (ordered list of action descriptors), `approval_gates` (top-level field; ordered list of named ApprovalGate descriptors — see below), `bounds` (per ACTION-LOOP-PRIMITIVE: iteration count / wall time / cost / composite — declared explicitly), `verifier` (descriptor naming intent-satisfaction check; deterministic / LLM-judged / human-in-the-loop), metadata, `created_at`, `status`.
* Action sequence: ordered list. Each descriptor: `action_type` (verb name), `parameters` (typed), `per_action_expectation` (used by enactment-style divergence reasoning if action is action-loop-shaped), `continuation_rules` (on failure: abort / continue / retry up to N), `gate_ref` (optional; references a named ApprovalGate from the workflow's `approval_gates` list — if present, the action **executes first** and then the engine pauses AFTER the action completes, waiting for the gate's approval event before proceeding to the next action).
* **`ApprovalGate`descriptor** (the design review edit, narrow review v3 → v4): each gate has `gate_name` (referenced by action descriptors), `pause_reason` (human-readable), `approval_event_type` (event the engine waits for), `approval_event_predicate` (which approval event resumes — typically actor + correlation matching), `timeout_seconds` (how long to wait before the gate's bound rule fires), `bound_behavior_on_timeout`(`abort_workflow` / `escalate_to_owner` / `auto_proceed_with_default`), `default_value` (required if and only if `bound_behavior_on_timeout` is `auto_proceed_with_default`; the value the engine substitutes for the missing approval). Engine knows about approval gates by name from the descriptor at registration time, not implicitly from action-descriptor flags. Gates referenced by action descriptors but not declared in `approval_gates` fail registration loudly. Gates with `auto_proceed_with_default` timeout behavior but no `default_value` fail registration loudly (the design review edit, v4 → v5). **Safe-deny constraint on `auto_proceed_with_default`** (the design review edit, v6 → v7): a gate whose `bound_behavior_on_timeout` is `auto_proceed_with_default` MUST NOT permit timeout-driven continuation into any subsequent action that is **irreversible / world-effect**. Action library verbs are classified by reversibility at registration time: `notify_user`, `write_canvas` (when `append_or_replace == "replace"` on canvases without versioning), `route_to_agent` (deliveries to external destinations), `call_tool` for tools the tool registry marks as irreversible, and `post_to_service` for any external service are classified **irreversible**. `mark_state` (versioned, supersede-via-new-version), `append_to_ledger` (append-only), `write_canvas` (when `append_or_replace == "append"`), and `call_tool` for tools marked reversible are classified **reversible**. Registration walks the action sequence; for every gate with `auto_proceed_with_default`, the validator inspects all action descriptors that come AFTER the gated action up to either workflow end or another gate boundary; any irreversible action in that downstream slice fails registration loudly with field-level error identifying the gate, the gated action, and the offending downstream action. The intent: timeout cannot bypass real human approval for downstream actions that cannot be undone. Operators wanting auto-proceed timeout behavior must either (a) ensure all downstream actions until the next gate (or workflow end) are reversible, or (b) choose a different `bound_behavior_on_timeout`(`abort_workflow` or `escalate_to_owner`).
* Verifier: workflow's outer verifier. Per ACTION-LOOP-PRIMITIVE §Verify, MUST check intent-satisfaction not action-dispatched.
* **Persistence** — new SQLite table `workflows` alongside `triggers`. Multi-tenancy keyed to instance_id.
### 3. Workflow execution engine (background)

**New module:** `kernos/kernel/workflows/execution_engine.py`.

* **NOT inline with event emission.** When a trigger fires (post-flush hook in trigger registry), it enqueues a `WorkflowExecution` record to an in-process workflow-execution queue. Engine runs as a background task draining this queue.
* Engine task wakeup model: cooperative yield to event loop; checks queue every iteration; processes one execution at a time per instance to preserve safety ordering.
* For each dequeued execution: instantiates an ACTION-LOOP-PRIMITIVE shape (intent = trigger event payload; gather = read context relevant to workflow; action = run action sequence through action library; verify = workflow's declared verifier; decide = complete or retry within bounds), threads request context (synthetic safety context constructed from trigger event), runs action sequence, applies bounds, emits trace events, terminates cleanly.
* **Out-of-turn safety seam (the design review edit, narrow review):** synthetic safety context constructed at execution start MUST be a full **CohortContext-equivalent** matching the shipped cohort interface's required shape: `instance_id`, `member_id`, `user_message` (synthetic placeholder for trigger-event-shape), `conversation_thread` (tuple), `active_spaces` (tuple of `ContextSpaceRef`), `turn_id` (synthetic workflow execution turn_id), `produced_at`(timestamp). Workflows build this from the trigger event payload plus resolved space references plus a synthetic workflow turn_id. Covenant cohort surface consumed via the existing cohort interface using this constructed context. Workflows do NOT bypass covenant evaluation. **Kick-back trigger added below for active-space resolution failure.** Do not overclaim restriction-class metadata; the constructed context carries only what's actually built into it.
* **Audit:** every workflow execution emits `workflow.execution_started` and `workflow.execution_terminated`events to the shipped event stream, with a freshly minted `correlation_id` for the execution. Per-step audit through action library verbs that wrap existing surfaces (their existing audit emissions thread through the workflow's correlation_id).
* **Persistence + restart-resume** — `workflow_executions` SQLite table records each in-flight execution: state (queued / running / completed / aborted), action_index_completed, intermediate state, last_heartbeat. On restart, engine reads `running` state executions; either resumes from `action_index_completed` (if action sequence is resume-safe per workflow descriptor) or aborts with a `workflow.execution_terminated` event flagged `aborted_by_restart`. Workflow descriptor declares resume-safety per action; default is NOT resume-safe (conservative).
### 4. Action library

**New module:** `kernos/kernel/workflows/action_library.py`.

Bounded set of verbs. Each wraps an existing Kernos surface; no verb invents new world-effect machinery.

**World-effect verbs (action-loop instances with intent-satisfaction verifiers):**

* `notify_user(channel, message, urgency)` — wraps existing presence/adapter surface. Verifier: message delivered to channel and persisted in conversation log. Covenant-gated.
* `write_canvas(canvas_id, content, append_or_replace)` — wraps existing canvas write surface. Verifier: canvas state reflects intended content (read-after-write check). Covenant-gated.
* `route_to_agent(agent_id, payload)` — writes payload to `Bridge/AgentInbox` interface. Verifier: payload visible at the inbox surface's read API.
* `call_tool(tool_id, args)` — wraps existing dispatch primitive. Verifier: per the existing tool's declared verifier (this verb does NOT redefine tool verification; it adopts the tool's). Covenant-gated.
* `post_to_service(service_id, payload)` — wraps workshop primitive. Verifier: per the service's declared verifier. Covenant-gated.
**Direct-effect verbs (NOT action-loop instances; structural assertions only):**

* `mark_state(key, value, scope)` — internal state mutation, scoped to instance/member/space/workflow. Assertion: post-mutation state read returns the new value. No destructive deletions per standing principle (mutations are versioned). NOT an action-loop because intent-satisfaction reduces to "the value is now X" which is the structural assertion itself.
* `append_to_ledger(workflow_id, entry)` — appends a synopsis entry to the workflow's ledger. Assertion: ledger file's last entry matches the appended record. NOT an action-loop for the same reason.
**Per ACTION-LOOP-PRIMITIVE Anti-Goal:** "Do not use the Action Loop as an excuse to add LLM calls to deterministic operations." `mark_state` and `append_to_ledger` get structural pins; everything else goes through the canonical loop.

**Bounded set in v1.** New verbs require a separate spec extending the library. Prevents drift; preserves covenant gating; keeps action surface auditable.

### 5. Workflow ledger

**Per-workflow append-only markdown log, instance-scoped (the design review edit, narrow review).**

* One markdown file per workflow at `data/{instance_id}/workflows/{workflow_id}/ledger.md` (or equivalent safe-name form for filesystem paths). **Path is instance-keyed**, matching the multi-tenancy invariant that all triggers, workflows, executions, ledgers carry. Cross-instance ledger reads MUST NOT cross instance_id boundaries.
* Format: ordered entries. Schema: `timestamp`, `execution_id`, `step_index`, `agent_or_action`, `synopsis` (1-3 sentences), `result_summary`, `kickback_if_any`, `references` (links to artifacts produced this step).
* Used by owner for workflow observation. Used by any agent reviewing along the way.
* Append-only. No destructive deletions per standing principle.
* Ledger is the read surface; the synopsis entries are the lightweight observability narrative owner reads at a glance.
* **Cross-instance isolation pinned.** Structural test verifies ledger read/write paths reject any path operation that would resolve outside the calling instance's `data/{instance_id}/workflows/` subtree.
### 6. English-to-structured trigger compiler

**New module:** `kernos/kernel/workflows/trigger_compiler.py`.

* At trigger registration, operator may write the trigger in English: "when CC posts a batch report."
* One-time cheap LLM call translates English to canonical predicate AST (the structured JSON AST form stored and evaluated by the trigger registry).
* Structured form is what's stored and evaluated; English preserved as `description` field.
* v1: basic translation. Recognizes a small set of event types and predicate patterns. Operator can edit structured form directly if compilation is wrong.
* Future spec WORKFLOW-LOOPS-ENGLISH-V2 expands recognition surface.
### 7. Webhook receiver

**New module:** `kernos/kernel/webhooks/receiver.py`. **Route registration in `kernos/server.py`.** (kernos/[server.py](http://server.py/) is a file; new directory `kernos/server/` would shadow it.)

* HTTP endpoint accepting external webhook events.
* Per-source authentication (HMAC signature verification, bearer token).
* Validates payload against registered webhook schemas.
* Translates validated webhook into `event_stream.emit(...)` call with `event_type="external.webhook"` and a payload that includes the source identifier and validated body.
* v1: HTTP POST with JSON. Future expansion in separate spec.
### 8. Bridge/AgentInbox interface

**New module:** `kernos/kernel/workflows/agent_inbox.py`.

Abstract interface that `route_to_agent` action library verb targets:

```
class AgentInbox(Protocol):
    async def post(self, agent_id: str, payload: dict) -> InboxPostResult: ...
    async def read(self, agent_id: str, since: datetime | None = None) -> list[InboxItem]: ...
```

v1 concrete: `NotionAgentInbox` — wraps existing Notion bridge inbox databases. Read/post via existing Notion tool surface. Used during the developing-Kernos period.

Future concrete: `LocalFolderAgentInbox` — reads/writes to a local folder structure on the server. Migration is a concrete swap, no verb rewrite.

**Provider configuration containment (the design review edit, narrow review).** `route_to_agent` depends on a **configured `AgentInbox`provider**, not on a hardcoded default. The primitive ships `NotionAgentInbox` as available concrete during the working period, but installations choose whether to bind it. If no `AgentInbox` provider is configured, `route_to_agent` fails loudly with a clear `agent_inbox_unavailable` error; the rest of the primitive still works (other verbs unaffected). This prevents "Notion default" from quietly becoming a primitive dependency. Pin: structural test verifies `route_to_agent` raises explicit unavailable-error when no provider is configured, rather than silently falling back to Notion.

**Notion-independence pin:** the `route_to_agent` verb's contract MUST go through the `AgentInbox` Protocol; it MUST NOT reference Notion directly. Structural test scans for direct `notion.so` / Notion-tool references in action library code.

### 9. Composition with covenant + cohorts + integration

* **Covenant-gated actions** through synthetic safety context constructed at workflow execution start. Action library verbs that affect the world (notify_user, write_canvas, route_to_agent, call_tool, post_to_service) consult covenant cohort using the constructed context.
* **Cohorts may observe workflow state** during turns by querying workflow_registry / workflow_executions tables. Surface defined here as read-only; cohort consumption integration in follow-on specs.
* **Integration redaction discipline preserved.** Workflows preserve redaction by running world-effect verbs through covenant-gated context and by preventing restricted workflow data from entering user-visible payloads unless explicitly allowed. The synthetic CohortContext-equivalent provides what's actually built into it; the spec does not claim identical restriction-class metadata to the in-turn integration unless that metadata is part of the constructed context.
* **Enactment composition.** Workflow's outer action-loop and per-verb action-loops compose with enactment's plan-with-expectation structure for any verb shaped that way.
## Scope-out

* **The design review workflow itself.** Separate spec composing on top.
* **Plumber email pipeline.** Separate workflow composition.
* **Gardener-proposes-workflows.** Separate spec after primitive proves out.
* **WORKFLOW-LOOPS-ENGLISH-V2.** Expansion of compilation surface; separate spec.
* **Cross-workflow composition.** Workflows triggering other workflows is implicitly possible via routed events; explicit workflow-of-workflows orchestration is out of scope.
* **LocalFolderAgentInbox.** Out of scope here. Lands when workflow migration spec ships.
* **Multi-instance Claude Code/Codex spawning.** Out of scope. Design Review workflow spec addresses spawning runnable instances; from this primitive's view, those are agents `route_to_agent` can target.
* **Gardener-narrow vs broad.** No new gardener responsibilities introduced by this spec.
## Acceptance criteria

1. Trigger registry exists at `kernos/kernel/workflows/trigger_registry.py` and registers via post-flush hook on the shipped `event_stream.py` substrate. **NO new event substrate created.** **Hook is failure-isolated:** any exception raised during trigger evaluation or enqueue is logged and contained; durable event persistence is independent of workflow code health (the design review edit).

2. Trigger predicates include: equality, contains, present/absent, value-in-set, time-window, **actor (member_id)**, **source (event_type starts_with)**, **correlation_id**, **idempotency_key**.

3. Trigger predicates evaluated deterministically; no LLM calls at evaluation.

4. **Predicate evaluation is on the post-flush hook, NOT inline with emit.** Pin: `event_stream.emit` latency unchanged within noise.

5. Triggers persist in SQLite `triggers` table; restart-resumable.

6. Workflow registry exists at `kernos/kernel/workflows/workflow_registry.py`.

7. Workflow descriptor includes bounds per ACTION-LOOP-PRIMITIVE (declared explicitly, not unbounded). Pin: workflow without declared bounds fails registration loudly.

8. Workflow descriptor includes verifier per ACTION-LOOP-PRIMITIVE (intent-satisfaction). Pin: workflow without declared verifier fails registration loudly.

9. Workflows persist in SQLite `workflows` table.

10. Workflow execution engine exists at `kernos/kernel/workflows/execution_engine.py`.

11. **Workflow executions run BACKGROUND, not inline with emit or with user turns.** Pin: turn-loop latency unchanged when workflows are firing concurrently.

12. Workflow executions persist in SQLite `workflow_executions` table; engine resumes in-flight executions on restart per workflow descriptor's declared resume-safety; otherwise aborts cleanly with `aborted_by_restart` event.

13. Workflow execution emits `workflow.execution_started` and `workflow.execution_terminated` via shipped `event_stream.emit`.

14. Workflow execution events carry a freshly minted `correlation_id`; per-step actions thread that correlation_id through their existing audit emissions.

15. **Out-of-turn safety seam:** workflows construct a full **CohortContext-equivalent** synthetic context with the shipped cohort interface's required shape (`instance_id`, `member_id`, `user_message` synthetic placeholder, `conversation_thread` tuple, `active_spaces` tuple of `ContextSpaceRef`, synthetic workflow `turn_id`, `produced_at`); covenant cohort consumed via existing surface using this context. Pin: workflow attempting world-effect verb without fully-constructed context fails loudly. Kick-back if active-space resolution fails (the design review edit).

16. Action library exists at `kernos/kernel/workflows/action_library.py` with seven verbs.

17. Each verb wraps an existing Kernos surface; no new world-effect machinery.

18. **Verbs split: world-effect verbs are action-loop instances with intent-satisfaction verifiers; trivial deterministic verbs (`mark_state`, `append_to_ledger`) are direct-effect with structural assertions only.** Per ACTION-LOOP-PRIMITIVE Anti-Goal.

19. Bridge/AgentInbox interface exists at `kernos/kernel/workflows/agent_inbox.py` with `Protocol` definition.

20. v1 concrete `NotionAgentInbox` ships and is **available** for binding (the design review edit). It is **NOT a hardcoded default**: `route_to_agent` depends on a configured `AgentInbox` provider; if no provider configured, `route_to_agent` fails loudly with `agent_inbox_unavailable`. Pin: structural test verifies `route_to_agent` raises unavailable-error when no provider is configured, rather than silently selecting Notion.

21. **`route_to_agent`does NOT reference Notion directly.** Goes through `AgentInbox` interface. Structural test pins absence of direct Notion references in action library code.

22. Workflow ledger exists per workflow at `data/{instance_id}/workflows/{workflow_id}/ledger.md` (instance-scoped path, the design review edit); markdown-formatted; append-only; brief synopses; no big code blocks. Cross-instance isolation pinned: structural test verifies ledger read/write paths cannot resolve outside the calling instance's subtree.

23. English compiler exists at `kernos/kernel/workflows/trigger_compiler.py`; one-time call at registration.

24. Webhook receiver exists at `kernos/kernel/webhooks/receiver.py`. **NOT at `kernos/server/webhook_receiver.py` (kernos/[server.py](http://server.py/)is a file, not a directory).** Route registered through `kernos/server.py`.

25. Webhook events emit as `external.webhook` to shipped event_stream, parameterized by source.

26. **Event schema usage aligned with shipped contract:** `instance_id`, `member_id` (NOT tenant_id), `space_id`, `correlation_id`, dotted `event_type`. Pin: structural test scans new code for `tenant_id` references that should be `instance_id`.

27. **No regression on PDI's 11 structural invariants.** Verified by existing pin tests.

28. **No regression on IWL's composition + contract pins.** Verified by existing pin tests.

29. **No regression on ACTION-LOOP-PRIMITIVE pattern compliance** for the action-loop-shaped verbs.

30. **No regression on event_stream contract.** Pin: `emit` latency unchanged; existing six instrumented subsystems still emit cleanly; existing read APIs unchanged.

31. **Multi-tenancy invariant preserved.** All triggers, workflows, executions, ledgers keyed to `instance_id` from day one.

32. **No destructive deletions.** Append-only or versioned-supersede.

33. **Covenant gating preserved.** World-effect verbs consult covenant via synthetic safety context.

34. **Structural redaction preserved.** Restricted-information workflows apply integration-layer discipline.

35. **Bounds enforcement structural.** Bound-exceeding workflow terminates cleanly with "unable to complete" outcome.

36. **Notion-independence pin (per DOCS-INTRODUCTION-INTEGRATION-ARC precedent).** Structural test scans new code for `notion.so` / `notion.com` / `www.notion.` tokens; only `NotionAgentInbox` concrete implementation is allowed to reference Notion (and that's contained behind the `AgentInbox` Protocol).

37. **Portable descriptor format ships.** `.workflow.yaml`, `.workflow.json`, and `.workflow.md` (with YAML frontmatter) all parse to the same internal `Workflow` dataclass via three loaders. Schema validation at parse time fails loudly on invalid descriptors with field-level error messages.

38. **`register_workflow(file_path)`entry point exists** (CLI, API, or callable) and persists both `Workflow` and `Trigger` records **atomically via a single transaction**. Any failure (parse error, schema validation, predicate compilation, gate-reference validation, persistence) leaves NO partial state in SQLite. Pin: structural test injects failures at each phase and verifies no residual Workflow or Trigger rows.

39. **Sharing constraint enforced at parse time using explicit allowlist** (the design review edit). Allowlist of instance-specific field paths includes: `member_id`, `instance_id`, `space_id`, `canvas_id`, `agent_id` / AgentInbox provider bindings, `channel_id`, `service_id` / credential references. Descriptor referencing any allowlisted field path without parameterization (e.g. `{installer.member_id}`) OR `instance_local: true` flag fails registration loudly with field-level error identifying the offending path.

40. **Approval gate semantics** (the design review edit, v4 → v5): action with `gate_ref` executes first; engine records `paused_for_approval` AFTER action completes; engine waits for matching approval event; resumes from next action. Pin: structural test verifies action fires before pause is recorded.

41. **`auto_proceed_with_default`validation** (the design review edits, v4 → v6):

42. a. **Default value required.** Gate with `bound_behavior_on_timeout: "auto_proceed_with_default"` MUST declare non-null `default_value`. Pin: registration of gate with that timeout behavior but missing `default_value`fails loudly with field-level error.

43. b. **Safe-deny on irreversible post-gate continuation** (the design review edit, v6 → v7): a gate with `auto_proceed_with_default` MUST NOT permit timeout-driven continuation into any subsequent irreversible / world-effect action. The action carrying `gate_ref` has already executed by the time the gate pauses, so the protected risk is the **post-gate continuation** — the next action(s) the timeout would allow without real human approval. Action library verbs are classified by reversibility at registration time. Irreversible: `notify_user`, `write_canvas` with `append_or_replace == "replace"` on non-versioned canvases, `route_to_agent` to external destinations, `post_to_service`, and `call_tool` for tools the tool registry marks irreversible. Reversible: `mark_state` (versioned), `append_to_ledger` (append-only), `write_canvas` with `append_or_replace == "append"`, and `call_tool` for tools marked reversible. Pin: registration walks the action sequence; for every gate with `auto_proceed_with_default`, the validator inspects all action descriptors that come AFTER the gated action up to either workflow end or the next approval gate; any irreversible action in that downstream slice fails registration loudly with field-level error identifying the gate, the gated action, and the offending downstream action.

44. c. **Tool registry reversibility classification.** Each tool in the tool registry exposes a `reversibility` attribute (`reversible` / `irreversible`). Existing tools default to `irreversible` if unclassified at the time this primitive ships (conservative-by-default per standing principle). Tool authors may opt in to `reversible` classification when the tool's effects are fully undoable. Pin: structural test verifies all currently-shipped tools have a reversibility classification at primitive ship time.

⠀
## Live test

`data/diagnostics/live-tests/WORKFLOW-LOOP-PRIMITIVE-live-test.md`

Approximately 13 scenarios.

### Substrate composition

1. **Trigger fires on event_stream emission, not inline.** Register a trigger; emit a matching event via `event_stream.emit`; verify (a) emit returns immediately within noise of pre-spec latency, (b) trigger fires after the SQLite flush, (c) workflow execution is enqueued not run inline.
2. **Predicate evaluation deterministic.** Various predicate shapes including new actor/source/correlation/idempotency operators. Events that should and shouldn't match. Deterministic outcomes; no LLM calls.
3. **Idempotency suppresses duplicates.** Same event payload emitted twice with idempotency_key matching; trigger fires once.

⠀
### Background execution

1. **Workflows run background, not inline.** Engineered: trigger fires during a user turn. Verify turn-loop latency is unchanged from baseline; workflow execution begins after turn returns; workflow events appear in event stream after turn correlation_id is closed.
2. **Restart-resume.** Mid-execution workflow with resume-safe action sequence. Simulate restart. Verify engine resumes from `action_index_completed`; ledger shows resume entry. Then: workflow with non-resume-safe sequence; restart; verify clean abort with `aborted_by_restart`.

⠀
### Composition verification

1. **Workflow as action-loop instance.** Trigger workflow with simple action sequence. Verify execution traces match ACTION-LOOP-PRIMITIVE shape.
2. **World-effect verb is action-loop instance.** Within a workflow execution, verify each world-effect verb's action-loop trace is present (intent-satisfaction verification).
3. **Direct-effect verb is structural assertion only.** Verify `mark_state` and `append_to_ledger` produce structural-pin traces, NOT action-loop traces. Verifier flavor is "deterministic-assertion" not "intent-satisfaction".

⠀
### Composition with existing primitives

1. **Out-of-turn covenant gating.** Workflow with `notify_user` of restricted material. Verify (a) synthetic safety context constructed at execution start, (b) covenant cohort consulted via context, (c) restricted material does not surface.
2. **Verifier requirement structural.** Attempt to register workflow without verifier. Verify loud failure. Same with workflow missing bounds.

⠀
### Approval gate end-to-end (the design review edit, v4 → v5)

1. **Approval gate happy path.** Register a workflow with an approval gate referenced from an action. Fire the trigger. Verify (a) the approval-requesting action **executes first** (target inbox shows the approval request artifact), (b) engine then marks the execution `paused_for_approval` with the correct `gate_name` recorded, (c) execution sits paused until an approval event matching the gate's predicate is emitted, (d) on matching event arrival, execution resumes from the next action in the sequence and runs to completion.
2. **Approval gate timeout path.** Register a workflow with an approval gate whose `timeout_seconds` is short and `bound_behavior_on_timeout` is set. Fire the trigger; let the action execute and the engine pause; do NOT fire an approval event. Verify the engine fires the declared timeout behavior (`abort_workflow` / `escalate_to_owner` / `auto_proceed_with_default` per gate's declaration) when the timeout elapses. For `auto_proceed_with_default`, verify the gate's `default_value` is substituted for the missing approval and execution proceeds.
3. **Safe-deny on irreversible post-gate continuation with auto_proceed_with_default** (the design review edit, v6 → v7). Attempt to register a workflow with an approval gate using `bound_behavior_on_timeout: "auto_proceed_with_default"` whose `gate_ref` is on a **reversible** approval-request action (e.g. `route_to_agent`to design review inbox), followed by an **irreversible downstream action** (e.g. `post_to_service` to external destination, `route_to_agent` to external destination, irreversible `call_tool`). Verify registration fails loudly with field-level error identifying the gate AND the offending downstream action. Verify registration succeeds when downstream actions until the next gate (or workflow end) are all reversible.

⠀
## the design review review focus (v2)

The v1 substrate-correctness questions are largely resolved by the v2 corrections. New focus areas:

1. **Post-flush hook on event_stream.** v2 adds an in-process post-write hook to event_stream so trigger registry attaches without parallel substrate. Right shape, or is there a cleaner integration that doesn't modify event_stream's surface? Existing `event_stream.py` was scoped tightly; adding a hook expands its responsibility.
2. **Synthetic safety context construction.** Workflows running between turns build a full CohortContext-equivalent (per §3 scope-in). Sufficient to preserve in-turn redaction discipline, or are there context fields the integration layer constructs that the synthetic version misses?
3. **Resume-safety declaration per workflow.** Workflow descriptor declares whether action sequence is resume-safe; default NOT-resume-safe (conservative). Right default? Or should it be per-action declaration so a partially-resume-safe workflow is possible?
4. **AgentInbox Protocol shape.** `post(agent_id, payload)` and `read(agent_id, since)`. Sufficient for v1 design review workflow needs, or are there interface methods the design review workflow's eventual handoff patterns will need that this Protocol doesn't expose?
5. **Webhook security scope.** v1 HMAC + bearer token. Defensive enough for the foreseen webhook sources, or insufficient for a class we're likely to hit?
6. **Direct-effect verb scope.** `mark_state` and `append_to_ledger` are NOT action-loops. Right call, or does any future verb need to be added to the direct-effect list (and how does it earn its way there structurally)?
7. **English compiler scope.** v1 basic. Right starting point with V2 follow-on, or insufficient for design review workflow's English triggers?
8. **Concurrency model.** Per-instance serialization in v1. Sufficient, or does design review workflow throughput require richer concurrency from the start?

⠀
## Conceptual examples + portable workflow format

This section grounds the abstract surface with concrete example workflows of varying shapes, plus the portable descriptor format that makes workflows authorable / shareable / installable as artifacts (analogous to how other agent ecosystems install skills).

### Workflow shape varieties

Workflows fall into a few canonical shapes. Each shape uses the same descriptor; the difference is what triggers them and how their action sequences compose.

**Shape 1: Wait-until-X then Y.** One trigger, simple action sequence. Most common shape.

Example: "When CC posts a batch report, append a synopsis line to the design review's ledger and notify owner."

```
workflow_id: "cc-batch-arrival-notice"
name: "CC batch report arrival notice"
description: "When a CC batch report lands in the design review inbox, log it and ping owner."
owner: "owner"
version: "1.0"
trigger:
  event_type: "bridge.inbox.updated"
  predicate:
    AND:
      - event.payload.inbox_name == "design review"
      - event.payload.source_agent == "cc"
      - event.payload.artifact_type == "batch_report"
bounds:
  iteration_count: 1
  wall_time_seconds: 30
verifier:
  flavor: "deterministic"
  check: "ledger_entry_appended AND notification_delivered"
action_sequence:
  - action_type: "append_to_ledger"
    parameters:
      workflow_id: "cc-batch-arrival-notice"
      entry:
        synopsis: "CC posted batch report at {event.timestamp} for spec {event.payload.spec_slug}"
  - action_type: "notify_user"
    parameters:
      channel: "primary"
      message: "CC batch report ready for {event.payload.spec_slug}"
      urgency: "normal"
    continuation_rules:
      on_failure: "abort"
```

**Shape 2: Periodic.** Time-tick trigger, bounded action sequence. Cadence work.

Example: "Every 5 minutes, check the integration arc roadmap for stale items and surface anything older than 14 days that's still marked active."

```
workflow_id: "integration-arc-staleness-sweep"
name: "Integration arc staleness sweep"
description: "Periodic check for active roadmap items that haven't moved."
owner: "owner"
version: "1.0"
trigger:
  event_type: "time.tick"
  predicate:
    AND:
      - event.payload.cadence == "5min"
      - event.payload.tick_label == "roadmap_sweep"
bounds:
  iteration_count: 1
  wall_time_seconds: 60
verifier:
  flavor: "deterministic"
  check: "sweep_completed_within_bound"
action_sequence:
  - action_type: "call_tool"
    parameters:
      tool_id: "read_doc"
      args:
        path: "roadmap/integration-arc.md"
  - action_type: "mark_state"
    parameters:
      key: "last_roadmap_sweep_at"
      value: "{now}"
      scope: "workflow"
  - action_type: "notify_user"
    parameters:
      channel: "ambient"
      message: "{N} stale items detected"
      urgency: "low"
    continuation_rules:
      on_failure: "continue"
      condition: "stale_items_count > 0"
```

**Shape 3: Conditional cascade with branching.** Trigger plus action sequence with continuation rules that adapt to results.

Example: "When an email arrives in the owner's inbox, classify it. If urgent and from a known sender, draft a response. If not urgent, log to the inbox-triage canvas. If from unknown sender, just log."

```
workflow_id: "owner-email-triage"
name: "Owner email triage"
description: "Classify inbound email and route appropriately."
owner: "owner"
version: "1.0"
trigger:
  event_type: "external.webhook"
  predicate:
    AND:
      - event.payload.source == "gmail"
      - event.payload.recipient == "owner@kernos.dev"
bounds:
  iteration_count: 5
  wall_time_seconds: 120
verifier:
  flavor: "deterministic"
  check: "email_routed_to_outcome"
action_sequence:
  - action_type: "call_tool"
    parameters:
      tool_id: "classify_email"
      args:
        email: "{event.payload.email}"
    per_action_expectation:
      structured: { contains_field: "urgency" }
  - action_type: "call_tool"
    parameters:
      tool_id: "check_known_sender"
      args:
        sender: "{event.payload.email.sender}"
  - action_type: "call_tool"
    parameters:
      tool_id: "draft_email_response"
      args:
        email: "{event.payload.email}"
    continuation_rules:
      condition: "step_1.urgency == 'high' AND step_2.is_known == true"
      on_skip: "continue"
  - action_type: "write_canvas"
    parameters:
      canvas_id: "inbox-triage"
      content: "{event.payload.email.summary}"
      append_or_replace: "append"
    continuation_rules:
      condition: "step_3 was skipped"
```

**Shape 4: Multi-stage with human-approval gates.** The design review workflow shape. Long-running; pauses at gates; resumes when owner responds.

Example (design review workflow, abbreviated; note `approval_gates` is a top-level descriptor field, and action descriptors reference gates by name via `gate_ref`):

```
workflow_id: "design review-workflow"
name: "Design Review workflow"
description: "Coordinate spec design through implementation across multiple agents."
owner: "owner"
version: "1.0"
trigger:
  event_type: "bridge.inbox.updated"
  predicate:
    AND:
      - event.payload.inbox_name == "design review"
      - event.payload.message_type == "new_concept"
bounds:
  iteration_count: 50
  wall_time_seconds: 86400
verifier:
  flavor: "human-in-the-loop"
  check: "founder_approves_completion"
approval_gates:
  - gate_name: "spec_approval"
    pause_reason: "Design Review must approve spec before code agent proceeds"
    approval_event_type: "bridge.inbox.updated"
    approval_event_predicate:
      AND:
        - event.payload.inbox_name == "design review"
        - event.payload.operation == "approve_spec"
        - event.correlation_id == workflow.execution_id
    timeout_seconds: 86400
    bound_behavior_on_timeout: "escalate_to_owner"
  - gate_name: "final_push_approval"
    pause_reason: "Design Review must give final push approval before deploy"
    approval_event_type: "bridge.inbox.updated"
    approval_event_predicate:
      AND:
        - event.payload.inbox_name == "design review"
        - event.payload.operation == "final_approval_and_push"
        - event.correlation_id == workflow.execution_id
    timeout_seconds: 86400
    bound_behavior_on_timeout: "escalate_to_owner"
action_sequence:
  - action_type: "append_to_ledger"
    parameters:
      entry:
        synopsis: "New concept received from owner"
  - action_type: "route_to_agent"
    parameters:
      agent_id: "spec-agent"
      payload:
        concept: "{trigger.payload.concept}"
        ledger_ref: "{workflow_ledger_path}"
  - action_type: "route_to_agent"
    parameters:
      agent_id: "design review"
      payload:
        operation: "approve_spec"
        spec_ref: "{step_2.spec_artifact_ref}"
    gate_ref: "spec_approval"
  - action_type: "route_to_agent"
    parameters:
      agent_id: "code-agent"
      payload:
        spec: "{step_2.spec_artifact_ref}"
        approval_ref: "{step_3.approval_ref}"
  - action_type: "route_to_agent"
    parameters:
      agent_id: "design review"
      payload:
        operation: "final_approval_and_push"
        batch_report_ref: "{step_4.batch_report_ref}"
    gate_ref: "final_push_approval"
```

The `gate_ref` field on an action descriptor causes the engine to **execute the action first** (e.g. `route_to_agent` posts the approval request to the target agent's inbox), then mark the workflow execution `paused_for_approval` (record state to `workflow_executions` SQLite table, record `gate_name`), then wait for an event matching the gate's `approval_event_type` and `approval_event_predicate` before proceeding to the next action. This avoids the deadlock that would arise if the engine paused before the action fired — since the action itself produces the approval request, no approval event would ever arrive. Resume mechanics use the same restart-resume path the spec defines for crash-recovery. If `timeout_seconds` elapses before the matching event fires, the gate's `bound_behavior_on_timeout` rule applies.

### Portable workflow descriptor format

Workflow descriptors are authored as **YAML or JSON files** that operators or other agents can author, version, share, and install — the same shape as skill installation in other agent ecosystems. The descriptor format is the same whether the workflow is written by hand, generated by Kernos's English-to-structured compiler, or downloaded from an external source.

**File extension:** `.workflow.yaml` (preferred) or `.workflow.json`. Markdown-with-embedded-YAML-frontmatter (`.workflow.md`) is also accepted for human-readable workflows that include narrative context above the descriptor; the YAML frontmatter is parsed as the structured form, and the markdown body becomes the `description` field.

**Schema:** the YAML/JSON shape matches the `Workflow` dataclass exactly. Any field accepted in the dataclass is acceptable in the descriptor file. The compiler validates the file against the dataclass schema at registration time; invalid descriptors fail loudly with field-level error messages.

**Installation flow:**

1. **Operator writes or downloads** a `.workflow.yaml` (or equivalent) descriptor file.
2. **Operator registers it** via a `register_workflow(file_path)` call (CLI, API, or future UI). Registration parses the file, validates against the `Workflow` schema, parses the trigger predicate (compiling expression-string DSL to canonical AST if needed), validates that all `gate_ref` references in action descriptors resolve to gates declared in the workflow's `approval_gates` list, and **persists both `Workflow` and `Trigger`records atomically to SQLite via a single transaction**. Any failure during registration (parse error, schema validation failure, predicate compilation failure, gate-reference validation failure, persistence failure on either Workflow or Trigger record) leaves NO partial state. Pin: structural test verifies that injected failures at each registration phase produce no residual Workflow or Trigger rows in SQLite.
3. **Workflow becomes active** — trigger registry now matches incoming events against the registered predicate; matches enqueue executions.

⠀
**Authorability surface:** future spec WORKFLOW-LIBRARY-AND-MARKETPLACE may define a registry of community-contributed workflows operators browse and install. Out of scope for this spec; the descriptor format is designed so that registry can ship later without changes to the workflow primitive.

**Sharing constraint:** workflows referencing instance-specific values MUST be either (a) parameterized so the installer fills in instance-specific values at registration, or (b) marked as `instance_local: true` and not shareable. Compiler enforces this distinction at file parse time using an **explicit allowlist of field paths** considered instance-specific (the design review edit, narrow review v3 → v4):

* `member_id` — any reference in trigger predicates, action parameters, or descriptor fields
* `instance_id` — any reference
* `space_id` — any reference
* `canvas_id` — any reference in action parameters (e.g. `write_canvas` target)
* `agent_id` and AgentInbox provider bindings — references that name a specific provider configuration
* `channel_id` — channel references in `notify_user` action parameters
* `service_id` and credential references — in `post_to_service` action parameters
Compiler walks the descriptor AST, matches each field path against the allowlist, and any match that is neither parameterized (e.g. `{installer.member_id}`) nor protected by `instance_local: true` flag fails registration loudly with field-level error message identifying the offending field path.

**Markdown variant example** (`.workflow.md`):

```
---
workflow_id: "morning-briefing"
name: "Morning briefing"
version: "1.0"
owner: "owner"
trigger:
  event_type: "time.tick"
  predicate:
    AND:
      - event.payload.cadence == "daily"
      - event.payload.local_time == "08:00"
bounds:
  iteration_count: 1
  wall_time_seconds: 30
verifier:
  flavor: "deterministic"
  check: "briefing_delivered"
action_sequence:
  - action_type: "notify_user"
    parameters:
      channel: "primary"
      message: "Good morning. {synthesized_overnight_summary}"
      urgency: "low"
---

# Morning briefing

Fires daily at 8am local time. Synthesizes overnight events into a brief summary and delivers it through the primary channel.

Intent: ambient awareness of what changed overnight without demanding interaction. Stays quiet on days when nothing meaningful happened.
```

### Why this matters for CC implementation

* The descriptor format is **the file shape CC ships parser code for** in C2 (workflow registry). Three loaders: YAML, JSON, Markdown-with-frontmatter. Schema validation against the `Workflow` dataclass.
* The `register_workflow(file_path)` entry point is the **operator-facing surface** for installing workflows.
* The design review workflow's spec (next spec, ARCHITECT-WORKFLOW) ships with its descriptor as a `.workflow.md` file that gets installed during design review-workflow setup. That descriptor file is the proof composition.
* Future workflows (plumber email pipeline, status sweeps, gardener-proposed loops) ship as additional `.workflow.yaml` files installed the same way. No bespoke pipeline code per workflow.
## Composition with prior specs

* **ACTION-LOOP-PRIMITIVE:** EXTENDED. Workflow loops are action-loop instances triggered by events. World-effect verbs are action-loop instances. Direct-effect verbs are explicitly NOT action-loop instances per Anti-Goal. Pattern preserved.
* **EVENT-STREAM-TO-SQLITE:** EXTENDED. Trigger registry registers via post-flush hook. New event types extend dotted namespace. Schema fields used as shipped (`instance_id`, `member_id`, `space_id`, `correlation_id`). NO parallel substrate.
* **PDI:** Unchanged. All 11 structural invariants preserved.
* **IWL:** Unchanged. Composition + contract pins preserved.
* **DOCS-INTRODUCTION-INTEGRATION-ARC:** Notion-independence commitment extends here. `route_to_agent` goes through `AgentInbox` Protocol; only `NotionAgentInbox` concrete may reference Notion.
* **Cohort architecture:** Synthetic safety context construction reuses cohort interface. No new cohort surface.
* **Covenant cohort:** Action library verbs affecting world consult covenant via synthetic context. Standing safety discipline preserved.
* **Existing tool dispatch primitive:** REUSED via `call_tool` verb. Single source of truth.
* **Existing canvas write surface:** REUSED via `write_canvas` verb.
* **Workshop primitive:** REUSED via `post_to_service` verb.
* **Existing audit + event stream:** EXTENDED with workflow.* events composing with existing audit families. event_[stream.py](http://stream.py/) surface itself extended with post-flush hook.
## Commit strategy

7 commits (foundational substrate; one more than v1 because the post-flush hook on event_stream warrants its own commit):

1. **C1: Event_stream post-flush hook + extension to support trigger registry attach.** Smallest possible expansion of shipped event_stream surface. Tests pin emit-latency unchanged, hook fires after SQLite flush, multi-tenancy preserved.
2. **C2: Trigger registry + structured predicate evaluation + persistence.** Tests pin predicate shapes (including actor/source/correlation/idempotency), deterministic evaluation, multi-tenancy keying, restart-resumable triggers.
3. **C3: Workflow registry + Workflow dataclass + persistence + portable descriptor parser (YAML/JSON/Markdown loaders) + `register_workflow(file_path)`entry point.** Tests pin Workflow shape, verifier-required and bounds-required structural invariants, multi-tenancy, descriptor-format parsing across all three loaders, sharing-constraint enforcement at parse time.
4. **C4: Action library + verb split (world-effect action-loops + direct-effect direct-assertions) + Bridge/AgentInbox Protocol + NotionAgentInbox concrete.** Tests pin ACTION-LOOP-PRIMITIVE compliance for world-effect verbs, structural assertions for direct-effect, existing-surface-wrapping (no new world-effect machinery), covenant gating via synthetic context, structural redaction, Notion-independence pin (route_to_agent through Protocol only).
5. **C5: Workflow execution engine (background, queued) + ledger + audit composition + restart-resume + persistence.** Tests pin out-of-turn execution, ledger append-only, audit emissions through event_stream, restart-resume per workflow descriptor, no-regression composition with PDI/IWL invariants.
6. **C6: English compiler + webhook receiver (correct path: kernos/kernel/webhooks/[receiver.py](http://receiver.py/)).** Webhook security pins (HMAC + bearer); route registration in [server.py](http://server.py/).
7. **C7: Integration tests + live test scenarios + Notion-independence pin (whole spec) + architecture doc.** End-to-end scenarios; structural Notion-leakage pin scanning all new modules; architecture doc covering substrate composition.

⠀
Local only. Hold for push approval. the design review review precedes ship.

## Kick-back triggers

* **event_stream post-flush hook reveals coupling.** If extending event_stream surface to support hook attachment requires deeper restructuring than expected, surface; spec amends with revised attachment strategy.
* **Action library verb wrapping reveals coupling.** If wrapping existing surface (canvas write, tool dispatch, workshop primitive) reveals tighter coupling than recon suggests, surface.
* **Synthetic safety context insufficient.** If covenant cohort fails when consumed via constructed context (because in-turn integration constructs context fields the synthetic version doesn't), surface; spec amends with extended context construction OR re-shapes verb to skip covenant for v1 (with explicit deferred-covenant note that's not allowed for shipping).
* **Active-space resolution fails (the design review edit).** Synthetic CohortContext requires resolved `active_spaces` tuple. If trigger event payload doesn't carry sufficient information to resolve the workflow's relevant active spaces (e.g., space_id field absent or stale), surface; either define a default-space resolution rule (workflow descriptor declares its space context explicitly at registration) or kick the workflow's covenant evaluation to a deferred path.
* **Post-flush hook failure isolation breaks (the design review edit).** If extending event_stream's flush path to support a failure-isolated hook reveals that exceptions can still propagate or that the writer task aborts on hook failure, surface; spec amends with stronger isolation strategy (e.g., separate worker task drains a hook-trigger queue independently).
* **Restart-resume granularity insufficient.** If workflows' resume-safety needs per-action declaration rather than per-workflow, surface; spec amends.
* **AgentInbox Protocol surface insufficient.** Surface; extend.
* **Webhook security insufficient.** Surface; spec amends.
* **PDI / IWL / event_stream invariant regresses.** Surface immediately; do not weaken; spec amends with strategy preserving invariant.
* **ACTION-LOOP-PRIMITIVE compliance breaks** for some world-effect verb (intent-satisfaction not meaningfully checkable). Surface; either redesign verb or move to direct-effect list with explicit justification.
## What this unblocks

* **ARCHITECT-WORKFLOW** (next spec). The named workflow that proves the primitive: owner → design review (Kernos) → spec agent (with Codex/the design review assistant) → design review approval → code agent (CC, with Codex assistant) → batch report → design review final → push approval. Composes entirely on top of this primitive.
* **Plumber email pipeline** and similar event-driven sequences.
* **Status sweep workflows** (weekly review of integration arc roadmap; canvas freshness checks; soak signal accumulation).
* **Time-bounded event canvas heuristics** (Pattern 05; served by workflow loops with `time.relative` triggers).
* **Gardener-proposes-workflows.**
* **Cross-agent routing primitives** (design review/the design review/CC inbox routing automated rather than owner-relayed).
* **External integration workflows** (Slack DM arrives → extract action items; calendar event created → schedule prep nudge).
* **LocalFolderAgentInbox migration** (workflow migration spec; concrete swap on AgentInbox Protocol).
Once this primitive ships and the design review workflow proves it, every future workflow loop is a clean composition rather than bespoke pipeline.

## the design review re-review — v2

Verdict: REVISE NARROWLY. This is now the right architecture: it composes on shipped event_stream instead of inventing an event bus, moves execution off the emitting turn, uses live instance/member/space/correlation schema, fixes webhook placement, introduces AgentInbox, splits world-effect vs direct-effect verbs, and pins persistence/restart. The rewrite resolves the v1 substrate problems.

## Narrow edits before CC

* Ledger path must be instance-keyed. Scope says data/workflows/{workflow_id}/ledger.md, but AC says all triggers/workflows/executions/ledgers are keyed to instance_id. Make the path instance-scoped, e.g. data/{instance_id}/workflows/{workflow_id}/ledger.md or equivalent safe-name form, and pin cross-instance isolation for ledger reads/writes.
* Synthetic safety context needs the full CohortContext shape, not just instance_id + member_id + space_id. The shipped cohort interface requires member_id, user_message, conversation_thread tuple, active_spaces as ContextSpaceRef tuple, turn_id, instance_id, produced_at. Revise the seam to say workflows build a real CohortContext-equivalent from trigger payload + resolved space refs + synthetic workflow turn_id. Add a kick-back if active space resolution fails. Do not overclaim “same restriction-class metadata” unless that metadata is actually part of the constructed context.
* Post-flush hook must be failure-isolated. Add a pin that trigger-hook exceptions are logged and do not poison event_stream flush/write progress. The hook can enqueue trigger evaluation, but it should not make durable event persistence dependent on workflow code health.
* NotionAgentInbox default needs one sentence of containment. The primitive can ship a NotionAgentInbox concrete for the working period, but route_to_agent should depend on a configured AgentInbox provider. If no provider is configured, route_to_agent fails unavailable loudly; the primitive itself still works. This avoids “Notion default” quietly becoming a primitive dependency.
* Remove or soften “Direction approved by the design review / v2 safe-for-CC body” in the page header. That is only true after this re-review. Keep the correction summary, but don’t pre-state the review result inside the spec.
## What is now clean

* Event substrate: correct to extend event_stream + EventType dotted namespace; no third bus.
* Execution posture: queued/background is the right answer for no turn-latency regression.
* Predicate vocabulary: actor/source/correlation/idempotency now covers design review workflow needs.
* Verb split: world-effect verbs as action-loop instances; deterministic internal verbs as structural assertions. Good anti-ceremony call.
* Restart-resume: conservative default not-resume-safe is right for v1.
## Bottom line

This no longer needs specific redesign. Tighten the five seams above and it is safe to hand to CC.

## the design review re-review — v3

Verdict: REVISE NARROWLY. The five v2 seams are materially fixed, and the portable descriptor direction is right. This is close enough that I would not redesign the primitive. But v3 introduced/retained a few stale or underspecified surfaces that CC could implement inconsistently if handed off as-is.

## Narrow edits before CC

* Remove stale v2 language that still says safety context is only “instance_id + member_id + space_id.” It appears in the v2 correction summary / Goal / the design review review focus. The load-bearing body now says full CohortContext-equivalent; keep only that. Historical notes are fine, but not if they look like current scope.
* Fix the remaining redaction overclaim in Composition with covenant/cohorts/integration: “same restriction-class metadata the in-turn integration would have.” We explicitly narrowed this. Replace with: workflows preserve redaction by running world-effect verbs through covenant-gated context and by preventing restricted workflow data from entering user-visible payloads unless explicitly allowed; do not claim identical metadata unless implemented.
* Portable descriptor predicates need one canonical machine shape. The examples use expression strings like event.payload.foo == "bar", while the spec says structured JSON predicate expressions. Either define that expression-string DSL as accepted syntax and compile it to the structured predicate AST at registration, or rewrite examples to the actual structured AST. Without this, three loaders can parse files but still disagree on predicate semantics.
* Approval gates need to be first-class in the descriptor schema, not buried only inside route_to_agent parameters. If needs_human_approval pauses workflow execution and resumes on approval event, define the action descriptor field/state transition explicitly: pause reason, approval event type/predicate, timeout/bounds behavior, and resume correlation. Otherwise design review-workflow will be the first proof and immediately require schema surgery.
* register_workflow(file_path) should persist Workflow + Trigger atomically AND leave no partial records on validation/compile failure. Add rollback/transaction language and a pin for invalid descriptor → neither workflow nor trigger exists.
* Sharing constraint needs an allowlist of what counts as instance-specific. At minimum: member_id, space_id, canvas_id, agent_id/provider binding, channel IDs, service IDs/credentials. Otherwise parse-time enforcement becomes vibes-based and hard to test.
## What is approved in shape

* Event_stream extension + failure-isolated post-flush hook is acceptable as the v1 attachment strategy.
* Background workflow execution, per-instance serialization, restart-resume conservative default, and instance-scoped ledgers are correct.
* AgentInbox provider containment is now clean: NotionAgentInbox available, not hardcoded dependency.
* Portable workflow descriptors are the right owner-direction addition. Making design review-workflow ship as a .workflow.md proof composition is exactly the right test of the primitive.
## Bottom line

This is no longer “revise specifically.” It is a tight narrow pass: clean the stale contradictions, formalize descriptor predicate/approval semantics, and add transaction + shareability pins. Then it is safe for CC.

## the design review re-review — v4

Verdict: REVISE NARROWLY. The six v3→v4 edits are materially folded, and the primitive is now substrate-correct. One load-bearing semantic issue remains in approval gates before this is safe for CC.

## Required narrow fix

* Approval gate semantics are currently inverted / ambiguous. The spec says gate_ref causes the engine to consult the named gate before executing the action. But the design review-workflow example places gate_ref on the route_to_agent action that asks design review for approval. If the engine pauses before executing that action, the approval request is never sent and the workflow deadlocks. Fix by making approval gates explicit as either: (a) request action executes, then workflow pauses awaiting the gate event before continuing; or (b) gate_ref belongs on the protected downstream action, while the prior action sends the approval request. I recommend (a): action with gate_ref performs the request/notification, records paused_for_approval with gate_name + correlation, then resumes when approval_event_predicate matches. Pin this with an end-to-end approval-gate test.
* Constrain bound_behavior_on_timeout for approval gates. auto_proceed_with_default is only safe if the descriptor declares an explicit safe default outcome and the protected downstream action is not an irreversible/world-effect action requiring human approval. Otherwise timeout behavior must abort_workflow or escalate_to_owner. Add this as a validation rule or a loud kick-back.
## Minor wording cleanup

* In the English compiler section, replace “event_type + predicate expression” with “event_type + canonical predicate AST” (or “expression compiled to AST”) so it matches the predicate section. This is wording, not architecture.
## What is now approved in shape

* No third event substrate: event_stream + failure-isolated post-flush hook is the right attachment point.
* Background execution, instance-scoped ledgers, full CohortContext-equivalent safety context, AgentInbox provider containment, atomic register_workflow, and shareability checks are all clean enough for implementation.
* Portable descriptors are approved in direction; allowing readable expression DSL compiled to canonical AST is the right compromise for authorability + deterministic runtime.
## Bottom line

This is very close. Fix approval-gate execution order / timeout safety and the wording mismatch, then I would mark WORKFLOW-LOOP-PRIMITIVE approved for CC.

## the design review re-review — v5

Verdict: STILL REVISE NARROWLY. The header says the three v5 edits landed, but the load-bearing body still contradicts the approval-gate correction. This is close, but not CC-safe until the body is internally consistent.

## Blocking narrow fixes

* Approval gate semantics are still stale in the Workflow registry section and the Shape 4 explanation. The action descriptor line still says gate_ref “pauses at this gate before execution,” and the example explanation still says the engine consults the gate before executing the action. That is exactly the deadlock we were fixing. Replace the body with the v5 semantics: execute the gate_ref action first, then record paused_for_approval with gate_name + correlation, then wait for approval_event_predicate before continuing to the next action.
* auto_proceed_with_default constraint is only stated in the header summary. It needs to be in the ApprovalGate descriptor / validation rules / acceptance criteria. Pin: registration fails if auto_proceed_with_default is used for a gate referenced by an irreversible world-effect action unless an explicit safe default outcome is declared and no human-required irreversible effect can proceed without approval.
* English compiler wording is still stale in Scope-in §6: “event_type + predicate expression.” Change to “event_type + canonical predicate AST” or “expression-string DSL compiled to canonical predicate AST.”
* Add the promised end-to-end approval-gate live/structural test to the live test section: route_to_agent approval request executes, workflow state becomes paused_for_approval, matching approval event resumes, downstream code-agent action executes; timeout path aborts/escalates and never silently proceeds on irreversible world-effect approval.
## What is otherwise clean

* The concept, event_stream substrate, background execution, full CohortContext-equivalent safety seam, AgentInbox containment, portable descriptor format, atomic registration, and shareability enforcement remain approved in shape.
## Bottom line

This is a body-consistency pass, not a redesign. Fix these stale lines and add the validation/test pins; then I expect to approve for CC.

## the design review re-review — v5 current

Verdict: REVISE NARROWLY — one remaining body/AC constraint mismatch. Gate execution order and English compiler wording are now clean, and the approval-gate live tests were added. This is not a redesign; it is one safety constraint that must move from header prose into the load-bearing schema/validation/AC.

### Blocking narrow fix

* auto_proceed_with_default is still weaker in the load-bearing body than in the header. The top summary says it cannot bypass real human approval for irreversible world-effect actions, but the ApprovalGate descriptor and AC only require non-null default_value. That would allow an unsafe default to silently stand in for missing approval.
* Fold the stronger rule into ApprovalGate validation and acceptance criteria: registration must fail if auto_proceed_with_default is declared on a gate whose missing approval could allow a human-required irreversible/world-effect downstream action to proceed, unless the default is explicitly safe-deny / non-proceeding. Do not rely on header prose for this.
### Now clean

* Approval gate execution order is corrected in the Workflow registry body and Shape 4 explanation: gate_ref action executes first, then the engine records paused_for_approval, then it waits for the matching approval event before continuing.
* The live test section now includes happy-path and timeout-path approval-gate coverage, including action-before-pause and resume semantics.
* English compiler wording now targets canonical predicate AST; predicate DSL canonicalization remains coherent.
### Bottom line

Fix the auto-proceed safety rule in the actual descriptor/validation/AC surface and I would approve this for CC. Everything else I asked for appears folded.

## the design review re-review — v6

Verdict: REVISE NARROWLY — the safe-deny rule is now load-bearing, but it is attached to the wrong side of the gate semantics. This is one semantic alignment fix, not a redesign.

### Blocking narrow fix

* v6 validates the action descriptor that carries gate_ref: if that action is irreversible and its gate auto_proceeds, registration fails. But the v5/v6 gate semantics say the gate_ref action executes first, then the workflow pauses, then approval/timeout controls whether the workflow proceeds to the next action. That means the protected risk is the post-gate continuation, not only the action that carries gate_ref.
* As written, a reversible approval-request action could carry gate_ref with auto_proceed_with_default, then timeout could proceed into an irreversible post_to_service / external route_to_agent / irreversible call_tool as the next action. That is exactly the bypass we were trying to forbid, and the current v6 schema/test would not catch it.
* Fix: registration must reject auto_proceed_with_default when the timeout would allow continuation into any human-required irreversible/world-effect action in the protected continuation path. Minimal v1 rule: inspect the immediate next executable action after the gate pause, respecting simple condition/skip rules conservatively; broader safe rule: until next approval gate or workflow terminal, any possible irreversible action makes auto_proceed invalid. Keep the existing gate_ref-action irreversibility check if useful, but it is not sufficient.
* Add one test case: reversible approval-request action with gate_ref + auto_proceed gate, followed by irreversible post_to_service / external route_to_agent / irreversible call_tool. Registration must fail with field-level error naming the gate and protected downstream action.
### What is otherwise clean

* The stronger rule is now in schema/validation/AC instead of just the header.
* Tool reversibility classification defaulting unclassified tools to irreversible is the right conservative posture.
* Gate execution order, approval-gate happy/timeout tests, and canonical predicate AST wording remain clean.
### Bottom line

I cannot mark APPROVED-for-CC yet because the safety pin currently protects the wrong action. Move safe-deny to the post-gate continuation semantics and add the downstream-action test; after that, this should be approval-ready.

## the design review re-review — v7

Verdict: APPROVED FOR CC.

The v6 blocker is resolved. Safe-deny is now attached to the post-gate continuation, which matches the corrected gate semantics: the gate_ref action executes, the workflow pauses, and approval/timeout controls whether the next action(s) may run.

### Verified clean

* ApprovalGate schema now forbids auto_proceed_with_default when timeout-driven continuation could enter a subsequent irreversible/world-effect action before the next gate boundary or workflow end.
* Validation walks the downstream slice after the gated action and fails loudly with field-level error identifying the gate, gated action, and offending downstream action.
* AC #41b now pins safe-deny on irreversible post-gate continuation, not the already-executed gate-carrying action.
* Live test scenario #3 now covers the actual bypass risk: reversible approval request + auto-proceed gate followed by irreversible downstream action must fail registration; reversible downstream slices may register.
* Prior clean areas remain clean: event_stream substrate, background execution, CohortContext-equivalent safety seam, AgentInbox containment, portable descriptors, atomic registration, approval-gate happy/timeout paths, tool reversibility default-irreversible posture, and canonical predicate AST wording.
### Implementation note

Treat older review blocks on this page as historical trail. CC should implement against the v7 body/AC/live-test surface, especially the post-gate continuation validation semantics in §2 and AC #41.

### Bottom line

WORKFLOW-LOOP-PRIMITIVE is CC-ready.