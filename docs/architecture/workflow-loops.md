# Workflow Loops

> An event-triggered substrate for action sequences that run between turns. Triggers fire on the system's event stream; bounded action verbs compose against existing Kernos surfaces; approval gates pause execution and bind structurally to a specific paused execution; portable descriptors install workflows like skills.

> **Note (post-WTC v1, 2026-05-04):** WORKFLOW-TRIGGERS-CONSOLIDATION v1 (C1-C7) consolidated three pre-existing trigger paths (legacy `TriggerRegistry` post-flush hook, scheduler heartbeat, ad-hoc Pattern 05 helpers) into a unified `TriggerEvaluationRuntime`. The legacy `trigger_registry.start(attach_post_flush_hook=True)` path is **retired in production** — production calls `attach_post_flush_hook=False` and routes events through `InternalEventAdapter` → `TriggerEvaluationRuntime` → `ExecutionEngine.execute_workflow`. The flow described below is conceptually correct; the production wiring goes through the trigger runtime substrate. See [`trigger-runtime.md`](trigger-runtime.md) for the as-built substrate, including the three-part predicate model, `FireOutbox` CAS state machine, fail-closed `fire_id` idempotency, and missed-window semantics. STS atomic registration (`register_workflow` registers workflow + triggers together) is the entry point CRB and `manage_schedule` use today.

## The problem

Kernos's reactive turn loop handles the case where the user asks and the agent answers. But many useful patterns aren't reactive. *"When CC posts a batch report, append a synopsis to the design review's ledger and notify me."* *"Every morning at 8am, summarize overnight events and deliver a briefing."* *"When an email matching this pattern arrives, classify it and route the urgent ones for response."* *"Coordinate spec drafting through implementation across multiple agents with owner approval at the design and push gates."*

These all share a shape — an event triggers a sequence, the sequence runs verifiable steps, some steps need human approval, the whole thing produces an auditable trail — but inventing each one as a one-off pipeline produces N pieces of bespoke coordination logic. The workflow-loop primitive is the substrate every such pattern composes against.

## Shape of the primitive

A workflow is a long-lived registered entity. An execution is a transient instance of running through it. Within an execution, action verbs run sequentially in a single context. Between agents (via `route_to_agent`), the workflow handoff is payload-only; any receiving agent processes it in whatever context its own inbox/runtime provides. Across executions, continuity lives in the workflow's ledger and any state the workflow saved.

```
event_stream.emit(...)
  │
  ▼
post-flush hook (failure-isolated)
  │
  ▼
trigger_registry.match(event)
  │
  ▼ (on match)
workflow_executions queue
  │
  ▼ (background drain)
execution_engine
  ├─ construct synthetic CohortContext-equivalent
  ├─ for each action descriptor in sequence:
  │    ├─ if gate_ref: mint gate_nonce → expose to payload context
  │    ├─ run action via action_library
  │    ├─ if gate_ref + action succeeded: persist paused_for_approval
  │    │    └─ wait for matching approval event (descriptor predicate AND nonce/execution_id)
  │    └─ append synopsis to per-workflow ledger
  └─ workflow.execution_terminated event emitted
```

The substrate composes on two existing primitives — [ACTION-LOOP-PRIMITIVE](../design-frames/action-loop.md) gives every workflow execution its five-step shape (Receive Intent → Gather Context → Take Action → Verify → Decide), and [event stream](event-stream.md) gives durable, correlation-threaded event delivery.

## Trigger registry

`kernos/kernel/workflows/trigger_registry.py` registers `(event_type, predicate, workflow_id)` mappings. When `event_stream.emit` flushes a batch to SQLite, a post-flush hook walks active triggers; matching triggers enqueue workflow executions.

The hook is failure-isolated: a trigger-evaluation exception is logged and contained. Durable event persistence stays independent of workflow code health — a buggy workflow can never poison the event stream.

Predicates are a structured JSON AST evaluated deterministically against event payloads. Operators include equality, contains, present-or-absent, value-in-set, time-window, plus three that matter for workflow scoping:

- **Actor** — `event.member_id == X`. Who emitted the event.
- **Source** — `event.event_type starts_with "subsystem.*"`. Which subsystem.
- **Correlation** — `event.correlation_id == X`. Chain to a specific turn or workflow execution.

A separate **idempotency** mechanism lives on the trigger record itself — `idempotency_key_template` renders against the matched event, the engine records the rendered key on first fire, and a duplicate render of the same key suppresses subsequent matches. It's not a predicate operator; predicates evaluate matches, idempotency suppresses re-fire on already-handled keys.

Operators may also write predicates as expression-string DSL (`event.payload.field == "value"`); the registration pipeline compiles them to canonical AST. The compiled AST is what's stored and evaluated; the expression source is preserved as `predicate_source` for human reading.

Triggers persist in SQLite alongside the events table. All trigger lookups filter by `instance_id` — the multi-tenancy invariant that runs through every Kernos primitive.

## Workflow registry and descriptor

`kernos/kernel/workflows/workflow_registry.py` stores named workflows. Each `Workflow` has:

| field | notes |
|---|---|
| `workflow_id` | UUIDv4 |
| `instance_id` | multi-tenancy key — the install this workflow lives in |
| `name`, `description`, `owner`, `version` | human-readable metadata |
| `action_sequence` | ordered list of action descriptors |
| `approval_gates` | top-level list of named `ApprovalGate` descriptors |
| `bounds` | iteration count / wall time / cost / composite — declared explicitly |
| `verifier` | descriptor naming the intent-satisfaction check |
| `trigger` | embedded `TriggerDescriptor` (event_type + predicate + optional filters) — registered atomically alongside the Workflow row |
| `metadata` | free-form dict for descriptor-author notes |
| `instance_local` | when `true`, opts out of the sharing-constraint check |
| `created_at`, `status` | lifecycle |

Each action descriptor names an action verb, parameters, optional per-action expectation, continuation rules (`on_failure`: abort / continue / retry up to N), an optional `gate_ref` referencing a named approval gate, and a `resume_safe` flag (default `false`) controlling restart-resume behavior.

A workflow without declared bounds fails registration loudly. A workflow without a declared verifier fails registration loudly. These are structural invariants, not advisory — the primitive refuses to register a workflow that can run unbounded or claim success without checking. (Of the bounds, only `wall_time_seconds` is enforced at runtime today; `iteration_count` and `cost_usd` are registration-required metadata that future runtime tracking will honor.)

### Portable descriptor format

Workflows author as files: `.workflow.yaml` (preferred), `.workflow.json`, or `.workflow.md` (Markdown with YAML frontmatter for human-readable workflows). The file shape matches the `Workflow` dataclass. `register_workflow_from_file(file_path)` parses, validates against the schema, compiles the trigger predicate, validates that all `gate_ref` references resolve to declared gates, and persists `Workflow` + `Trigger` records **atomically via a single SQLite transaction**. Any failure at any phase (parse, schema validation, predicate compilation, gate-reference validation, persistence) leaves no partial state.

Predicates ship in two forms: canonical AST (a JSON object with the documented operator vocabulary) and an expression-string DSL (e.g. `event.payload.kind == "report"`). The descriptor parser auto-detects which form a string predicate is and compiles DSL to AST through `trigger_compiler.compile_dsl`. English-form predicates are an opt-in surface: an operator who wires an `english_compiler` callable into `compile_predicate_source` can author triggers in plain English, but `register_workflow_from_file` does NOT invoke an LLM by default — registration stays deterministic and offline-safe.

> **`english_compiler` is intentionally unbound in production.** Register-time English-form predicate compilation requires an LLM call, which violates the "registration stays deterministic and offline-safe" invariant. The parameter is reserved on `compile_predicate_source` so an operator-opt-in path exists (bind explicitly when needed); without an explicit bind, English-form predicates raise `PredicateValidationError` rather than silently invoking a compiler. The wiring is preserved by design — not by accident — so the path is available when the constraint changes.

Workflows referencing instance-specific values must either parameterize them (`{installer.member_id}`) or mark themselves `instance_local: true`. The registration compiler enforces this against an explicit allowlist: `member_id`, `instance_id`, `space_id`, `canvas_id`, `agent_id` / AgentInbox provider bindings, `channel_id`, `service_id` / credential references. Unparameterized matches without the local-only flag fail registration loudly with field-level error messages identifying the offending path.

The format is what makes workflows authorable / shareable / installable like skills. Operators write them by hand, write them in DSL form for the registration pipeline to compile, or — once an English compiler is wired up — generate them from English descriptions.

A minimal `.workflow.yaml` looks like this:

```yaml
workflow_id: cc-batch-arrival-notice
instance_id: "{installer.instance_id}"   # filled in at registration time
name: CC batch report arrival notice
owner: owner
version: 1.0
trigger:
  event_type: external.webhook
  predicate:
    AND:
      - event.payload.source_id == "architect_inbox"
      - event.payload.body.source_agent == "cc"
      - event.payload.body.artifact_type == "batch_report"
bounds:
  iteration_count: 1
  wall_time_seconds: 30
verifier:
  flavor: deterministic
  check: ledger_entry_appended AND notification_delivered
action_sequence:
  - action_type: append_to_ledger
    parameters:
      entry:
        synopsis: "CC posted batch report for {event.payload.spec_slug}"
  - action_type: notify_user
    parameters:
      channel: primary
      message: "CC batch report ready for {event.payload.spec_slug}"
      urgency: normal
```

The descriptor declares its trigger, bounds, verifier, and action sequence; runtime values like `event.payload.spec_slug` interpolate when the workflow fires.

## Action library

Seven bounded verbs split into two classes by ACTION-LOOP-PRIMITIVE compliance:

**World-effect verbs** are action-loop instances with intent-satisfaction verifiers. Each wraps an existing Kernos surface; no verb invents new world-effect machinery.

| verb | wraps | verifier |
|---|---|---|
| `notify_user(channel, message, urgency)` | presence/adapter surface | wrapped delivery returned a truthy receipt; conversation-log persistence is delivery-side, not re-checked here |
| `write_canvas(canvas_id, content, append_or_replace)` | canvas write surface | read-after-write: replace → exact match; append → content visible in current state |
| `route_to_agent(agent_id, payload)` | AgentInbox Protocol | payload's `persisted_id` is visible at inbox read API |
| `call_tool(tool_id, args)` | tool dispatch primitive | per the tool's declared verifier when one is wired in via `tool_verifier_fn`; otherwise falls back to the dispatch's success bit |
| `post_to_service(service_id, payload)` | workshop primitive | per the service's declared verifier when wired via `service_verifier_fn`; otherwise success-bit fallback |

**Direct-effect verbs** are NOT action-loop instances. Per ACTION-LOOP-PRIMITIVE's anti-goal ("do not use the action loop as an excuse to add LLM calls to deterministic operations"), these have structural assertions only:

| verb | assertion |
|---|---|
| `mark_state(key, value, scope)` | post-mutation state read returns the new value |
| `append_to_ledger(workflow_id, entry)` | ledger file's last entry matches the appended record |

The bounded set is a v1 commitment. New verbs require a separate spec extending the library, which keeps covenant gating coherent and the action surface auditable.

## Execution engine

`kernos/kernel/workflows/execution_engine.py` runs as a background task draining the workflow-execution queue. It is **not** inline with `event_stream.emit` — emit returns immediately per the shipped contract; trigger evaluation happens on the writer task's flush callback; workflow execution happens on the engine's own loop.

For each dequeued execution:

1. **Construct safety context.** Build a synthetic `CohortContext`-equivalent matching the shipped cohort interface's required shape — `instance_id`, `member_id`, synthetic placeholder `user_message`, `conversation_thread` tuple, `active_spaces` tuple of `ContextSpaceRef`, synthetic workflow `turn_id`, `produced_at`. Covenant cohort consults this context the same way it consults a turn's real context. Workflows do not bypass covenant evaluation; they reuse the cohort interface via constructed context.

2. **Run the action sequence.** Each verb executes via the action library, threaded with the workflow's correlation_id so per-step audit emissions stay correlated.

3. **Apply bounds.** A workflow that would exceed its declared bound terminates cleanly with an "unable to complete" outcome, not silent over-run.

4. **Emit trace events.** Every execution emits `workflow.execution_started` and `workflow.execution_terminated` to the shipped event stream. Approval-gate pauses emit `workflow.execution_paused_at_gate` carrying the nonce.

### Persistence and restart-resume

Every in-flight execution records to `workflow_executions` SQLite table: state (queued / running / completed / aborted), `action_index_completed`, intermediate state, `gate_nonce` if paused, `last_heartbeat`. On engine restart, executions in `running` state either resume from `action_index_completed` (if the workflow descriptor declares the action sequence resume-safe) or abort cleanly with `aborted_by_restart`. Default is conservative: not resume-safe unless explicitly declared.

Single-worker serialization in v1 — one asyncio queue, one worker task, sequential dispatch. For the developing-Kernos period (single instance) this is equivalent to per-instance ordering; per-instance worker partitioning ships as a follow-on once fan-out and human-paused workflows coexist meaningfully so a paused execution doesn't block unrelated work.

## Approval gates

Some workflow steps need human approval before downstream work proceeds — a spec being approved before code agent runs against it, a final push approval before deploy, a household member confirming a non-trivial calendar change. Approval gates are first-class in the descriptor schema.

An `ApprovalGate` descriptor has a name (referenced by action descriptors via `gate_ref`), a pause reason, the event type and predicate that count as the approval response, a timeout window, and a timeout-behavior rule (`abort_workflow` / `escalate_to_owner` / `auto_proceed_with_default`).

### Gate execution order

The action carrying `gate_ref` is usually a `route_to_agent` posting an approval request to the target agent's inbox. So the engine **executes the gated action first**, then pauses, then waits — not the reverse. The reverse would be a deadlock: pause-before-execution means the approval-requesting action never fires, so no approval event is ever produced, so the workflow waits forever.

The engine mints a fresh `gate_nonce` (UUIDv4) **before** invoking the gated action and exposes it (along with `execution_id`) to the action's payload construction context. The `route_to_agent` action carrying `gate_ref` includes both fields in its `approval_request` block when posting to the AgentInbox. After the action completes successfully, the engine persists the same nonce with `paused_for_approval` substate. If the action fails or aborts, the unused nonce is discarded and no pause is entered.

### Match logic

To wake a paused execution, an incoming event must satisfy **both**:

1. The gate's descriptor-level predicate (event_type, actor matching, etc.) — author-controlled.
2. Matching `execution_id` AND matching `gate_nonce` carried in the event payload — engine-enforced.

Either failing means no wake. The author cannot weaken nonce enforcement by writing a broad descriptor predicate, because nonce verification happens outside the predicate evaluation. This separation is what makes the gate binding structurally safe across multiple paused executions.

### Safe-deny on irreversible post-gate continuation

`auto_proceed_with_default` is the timeout behavior that substitutes a declared default outcome if no approval arrives. It must not allow timeout to silently bypass real human approval for downstream actions that cannot be undone. The constraint is structural: a gate with `auto_proceed_with_default` MUST NOT permit timeout-driven continuation into any subsequent action that is irreversible / world-effect.

Action library verbs are classified at registration time. `notify_user`, `write_canvas` with `append_or_replace == "replace"`, `route_to_agent`, `post_to_service`, and `call_tool` are classified **irreversible** (v1 treats every tool dispatch as irreversible by default — per-tool reversibility metadata lands when the tool registry exposes it). `mark_state` (versioned), `append_to_ledger` (append-only), and `write_canvas` with `append_or_replace == "append"` are **reversible**.

Registration walks the action sequence; for every gate with `auto_proceed_with_default`, the validator inspects all action descriptors AFTER the gated action up to the next gate or workflow end. Any irreversible action in that downstream slice fails registration loudly with field-level error identifying the gate, the gated action, and the offending downstream action.

The protected risk is the post-gate continuation, not the gate-carrying action itself (which already executed before pause). Operators wanting auto-proceed timeout behavior must either ensure all downstream actions until the next gate are reversible, or choose a different timeout behavior (`abort_workflow` or `escalate_to_owner`).

## AgentInbox Protocol

`route_to_agent` targets an `AgentInbox` Protocol, not Notion or any other concrete handoff substrate directly:

```python
class AgentInbox(Protocol):
    async def post(
        self, agent_id: str, payload: dict, *, instance_id: str = "",
    ) -> InboxPostResult: ...

    async def read(
        self,
        agent_id: str,
        *,
        since: datetime | None = None,
        instance_id: str = "",
    ) -> list[InboxItem]: ...
```

The `instance_id` keyword carries the multi-tenancy key through to the concrete provider so cross-instance reads cannot resolve into another instance's inbox state. Concrete implementations are required to honor the keying — same invariant that runs through every other Kernos primitive.

The Protocol depends on a configured provider — the primitive ships a `NotionAgentInbox` concrete during the working period, but installations choose whether to bind it. If no `AgentInbox` provider is configured, `route_to_agent` raises `AgentInboxUnavailable` (the engine surfaces this on the step-failed event with the class name in the error string); the rest of the primitive still works (other verbs unaffected). This containment prevents the available concrete from quietly becoming a primitive dependency.

The `route_to_agent` verb's contract MUST go through the Protocol; it MUST NOT reference Notion directly. A structural test scans new code for `notion.so` / `notion.com` tokens; only the `NotionAgentInbox` concrete may reference them.

Approval-shape payloads carry a documented `approval_request` block:

```yaml
approval_request:
  execution_id: <uuid>
  gate_nonce: <uuid>
  gate_name: <str>
  pause_reason: <str>
  response_event_type: <str>
  response_predicate: <structured AST>
```

Receiving agents read this block to know what response to compose. The response event must carry `execution_id` and `gate_nonce` in its payload. The schema is documented contract; the engine's match logic enforces correctness.

## Workflow ledger

Every workflow has an append-only markdown ledger at `data/{instance_id}/workflows/{workflow_id}/ledger.md`. The path is instance-scoped — the multi-tenancy invariant carries through to the filesystem layout. Cross-instance ledger reads cannot resolve outside the calling instance's subtree.

Each entry is a brief synopsis (1-3 sentences) with structured fields: timestamp, execution_id, step_index, agent_or_action, synopsis, result_summary, kickback (if any), references to artifacts produced this step. No big code blocks. Append-only — entries supersede via new entries, never overwrite.

The ledger is the human-readable observability surface. A owner watching a workflow run reads the ledger at a glance and sees the trail: what fired, when, what each step produced, what's still in flight, what was kicked back. Any agent reviewing along the way reads the same ledger.

## Composition with covenant + cohorts + integration

The synthetic `CohortContext`-equivalent the engine constructs at execution start is what makes workflows compose with the rest of Kernos's safety machinery. World-effect verbs (`notify_user`, `write_canvas`, `route_to_agent`, `call_tool`, `post_to_service`) consult the covenant cohort using this constructed context. Standing safety discipline preserved: workflows do not have a separate covenant surface, do not bypass covenant evaluation, do not opt out of redaction.

Workflows preserve redaction by running world-effect verbs through covenant-gated context and by preventing restricted workflow data from entering user-visible payloads unless explicitly allowed. The synthetic context provides what's actually built into it; the primitive does not claim identical restriction-class metadata to the in-turn integration unless that metadata is part of the constructed context.

Cohorts may observe workflow state during turns by querying `workflow_registry` and `workflow_executions`. The surface is read-only from the cohort side; consumption integration (e.g., a status cohort surfacing in-flight workflow context to the principal) ships via follow-on specs.

## Source modules

| module | role |
|---|---|
| `kernos/kernel/workflows/predicates.py` | Predicate AST schema + deterministic evaluator (no I/O, no LLM) |
| `kernos/kernel/workflows/trigger_compiler.py` | Expression-string DSL → canonical AST compiler; optional English compiler hook |
| `kernos/kernel/workflows/trigger_registry.py` | Trigger persistence, post-flush hook attachment, match evaluation |
| `kernos/kernel/workflows/workflow_registry.py` | Workflow persistence, descriptor validation, atomic Workflow + Trigger registration |
| `kernos/kernel/workflows/descriptor_parser.py` | YAML / JSON / Markdown-frontmatter loaders + sharing-constraint enforcement |
| `kernos/kernel/workflows/action_classification.py` | Per-verb reversibility classification powering safe-deny |
| `kernos/kernel/workflows/action_library.py` | Seven bounded verbs |
| `kernos/kernel/workflows/agent_inbox.py` | AgentInbox Protocol + InMemory / Notion concretes |
| `kernos/kernel/workflows/execution_engine.py` | Background queue drain, action-sequence run, gate pause/resume, gate_nonce binding, restart-resume |
| `kernos/kernel/workflows/ledger.py` | Append-only per-workflow markdown ledger with cross-instance path isolation |
| `kernos/kernel/webhooks/receiver.py` | External webhook ingestion → `external.webhook` events |

## See also

- [Event stream](event-stream.md) — the substrate workflow loops register against
- [Action loop primitive](../design-frames/action-loop.md) — the canonical pattern workflow executions instantiate
- [Cohort and judgment](cohort-and-judgment.md) — the cohort interface workflows consume via synthetic context
- [Infrastructure-level safety](safety-and-gate.md) — the dispatch gate and covenant surface every world-effect verb passes through
