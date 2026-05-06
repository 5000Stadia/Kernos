# Architecture Overview

> Three layers, one principal agent, judgment cohorts surrounding it, six-phase turn pipeline. The rest of the architecture documentation fans out from here.

## The three layers

Kernos separates its runtime into three layers. Each has one job; the boundaries between them are enforced by imports (the handler never imports from adapters; adapters never import from the handler — they share only `NormalizedMessage`).

```
┌─────────────────────────────────────────────────────────────┐
│ Adapters                                                    │
│   Discord · SMS/Twilio · Telegram · HTTP · (pluggable)      │
│   Job: normalize inbound platform messages into             │
│   NormalizedMessage; deliver outbound responses.            │
├─────────────────────────────────────────────────────────────┤
│ Handler (kernos/messages/handler.py)                        │
│   Job: the six-phase turn pipeline. Receives a              │
│   NormalizedMessage, produces a response string. Manages    │
│   conversation history, event bookends, persistence.        │
├─────────────────────────────────────────────────────────────┤
│ Kernel (kernos/kernel/)                                     │
│   Event Stream, State Store, Reasoning Service, Dispatch    │
│   Gate, Router, Messenger, Fact Harvester, Compaction,      │
│   Friction Observer, Capability Registry, Plan Execution.   │
│   Job: state, reasoning, cohorts, primitives.               │
└─────────────────────────────────────────────────────────────┘
```

The handler is the turn engine. The kernel is everything the turn engine calls into. The adapters are the pluggable I/O surface. Adding a platform (Discord, SMS, Telegram) is an adapter implementation; it doesn't touch the handler or the kernel.

## The six-phase turn pipeline

Every turn — reactive (user message), proactive (whisper, awareness), or self-directed (plan step) — runs the same six phases:

```
1. PROVISION    Resolve instance, member, platform; ensure per-member
                state (spaces, profiles, permissions) exists.
2. ROUTE        The router cohort reads the message and picks the focus
                space. Every downstream phase is scoped to that space.
3. ASSEMBLE     Build the Cognitive UI zones (RULES, ACTIONS, NOW, STATE,
                RESULTS, PROCEDURES, MEMORY). Load the scoped tool catalog.
4. REASON       Call the principal agent with the assembled context. Run
                the tool-use loop: each tool call passes through the
                dispatch gate; every cross-member message passes through
                the Messenger.
5. CONSEQUENCE  Apply the effects of the turn: outbound delivery,
                receipts, tool-call persistence.
6. PERSIST      Update conversation logs; emit events; run boundary-
                triggered cohorts (fact harvest, compaction) if the
                boundary was crossed; run friction observer post-turn.
```

The same pipeline runs regardless of the origin. A self-directed plan step is a synthetic inbound message from the kernel itself (`sender="self_directed"`); it goes through the same six phases, including the gate and the Messenger, as any user-originated turn.

See [Pipeline reference](pipeline-reference.md) for a per-phase breakdown with inputs, outputs, and invariants.

## The principal and the cohorts

One LLM handles the conversation. **Judgment cohorts** — bounded specialist LLM workers with narrow prompts and narrow input scopes — run around it. Six core judgment cohorts mediate the principal's turn directly:

| Cohort | Where it runs | What it does |
|---|---|---|
| Router | Phase 2, before ASSEMBLE | Picks the focus space; tags the turn |
| Gate | Phase 4, per tool call | Returns APPROVE / CONFIRM / CONFLICT / CLARIFY |
| Messenger | Phase 4 or 5, per cross-member exchange | Judges whether the response serves the disclosing member's welfare |
| Fact harvester | Phase 6, at compaction boundaries | Extracts durable facts with single-call reconciliation |
| Compaction | Phase 6, at compaction boundaries | Rewrites Living State; appends archive block |
| Friction observer | Phase 6, post-turn | Detects patterns where the system works against the user |

These six are the cohorts the [cohort-and-judgment.md](cohort-and-judgment.md) page covers in depth.

Additional cohorts run on different cycles outside the per-turn-judgment pipeline — they live in `kernos/kernel/cohorts/` and are invoked when their substrate fires:

- **Gardener** — bounded canvas-shape authority (initial-pattern selection, continuous-evolution heuristics, reshape proposals). See [`gardener.md`](gardener.md) and [`cohort-gardener.md`](cohort-gardener.md).
- **Memory cohort** — per-turn memory-shaping; see [`cohort-memory.md`](cohort-memory.md).
- **Covenant cohort** — covenant evaluation as a structured surface; see [`cohort-covenant.md`](cohort-covenant.md).
- **Cataloging cohort** — async cheap-tier worker that produces catalog rows for the [reference primitive](reference-primitive.md). See [`cohort-cataloging.md`](cohort-cataloging.md).
- **Drafter cohort** — conversational shaper of [workflow drafts](workflow-drafts.md).

Cohorts (in either sense) never appear in the principal agent's context. Their outputs are consumed by the kernel, not injected into the next prompt. See [`cohort-and-judgment.md`](cohort-and-judgment.md) for the discipline and [`primitives-reference.md`](primitives-reference.md) for each cohort's entry point.

## Load-bearing architectural choices

Each gets its own page:

### [Cohort architecture →](cohort-and-judgment.md)

One principal agent surrounded by bounded specialist LLM workers. Judgment work on LLMs; state work in Python. The principal keeps its full attention on the conversation; cohorts handle routing, gating, fact extraction, disclosure judgment, and friction observation without ever appearing in the agent's context.

### [Context spaces →](context-spaces.md)

Multiple parallel memory threads per member, each with its own ledger, facts, and promoted tool set. Invisible to the user and the agent — a single continuous conversation routes transparently across specialist domains. The router picks the focus; the agent receives the turn pre-scoped.

### [Memory: Ledger and Facts →](memory.md)

Two stores, two jobs. Ledger holds the conversational arc, compressed at boundaries rather than summarized turn-by-turn. Facts holds structured knowledge, reconciled in a single LLM call against the existing store rather than extracted per-turn and deduplicated after the fact.

### [Multi-member disclosure layering →](disclosure-and-messenger.md)

One hatched agent per member. A permission matrix declares relationships; a Messenger cohort sits above the matrix and judges whether the response as written serves the disclosing member's welfare. The `send_relational_message` tool is excluded from the gate because the Messenger takes over there unconditionally.

### [Infrastructure-level safety →](safety-and-gate.md)

Every tool call passes through a dispatch gate that classifies effect (read / soft_write / hard_write), evaluates the initiator context (reactive / proactive), and consults user-declared covenants. Verdicts are APPROVE, CONFIRM, CONFLICT, or CLARIFY — each with its own downstream behavior. Safety as behavioral shaping, not access control.

### [Cognitive UI grammar →](cognitive-ui.md)

The system prompt as a typed document with named zones — RULES, ACTIONS, NOW, STATE, RESULTS, PROCEDURES, MEMORY — cacheable prefix, provenance tags on knowledge fragments. The runtime refreshes zones selectively without rebuilding the prompt.

### [Canvases →](canvas.md)

Scoped directories of markdown pages — personal / specific-members / team — that accumulate structured content across turns and members. Page types (note / decision / log) are advisory; state transitions fire routes and can consult the operator; canvas creation dispatches `canvas_offer` envelopes to declared members. Section markers layer HTML-comment metadata under H2 headings for navigable reads of large pages.

### [The Gardener →](gardener.md)

Bounded canvas-shape authority. Picks the initial pattern at `canvas_create` time by consulting the Workflow Patterns library; runs continuous-evolution heuristics on page events; surfaces reshape proposals with confidence-floor + 24-hour coalescing discipline so members see proposals at most once or twice a week per canvas. **Every Gardener action except retrieval is fire-and-forget** — the primary agent never waits on Gardener work in the turn path.

### [Reference primitive →](reference-primitive.md)

The reach mechanism agents use to find canonical Kernos documentation + agent-stored project-deep reference material. Catalog over `docs/` (instance-scoped) + `data/references/<space_id>/` (domain-owned). Three surfacing paths replace single-tool-call gating: explicit retrieval via `request_reference`, mechanical auto-induction by signal-matching, hatching-time substrate awareness baked into the bootstrap.

### [Trigger runtime + workflow loops →](trigger-runtime.md)

Unified time + event trigger substrate (WORKFLOW-TRIGGERS-CONSOLIDATION v1). Three-part predicate model (event_selector + temporal_relation + dispatch_policy), `FireOutbox` CAS state machine, fail-closed `fire_id` idempotency, atomic STS registration. The substrate the [workflow-loop primitive](workflow-loops.md) and [CRB](crb.md) compose against.

## Hard invariants

These are enforced at the code-architecture level. Violating any of them is a build failure regardless of what a feature spec says.

| Invariant | Location | Enforcement |
|---|---|---|
| Adapter/handler isolation | Import graph | Handler never imports adapters; adapters never import handler; shared surface is `NormalizedMessage` only |
| `instance_id` from day one | Every state-writing code path | No code ever assumes a single user; all state is keyed to `instance_id` |
| MCP for capabilities | All capability integrations | Tools and data go through MCP; no direct API integrations that bypass the capability abstraction |
| Graceful errors | Every failure mode | Every failure produces a friendly user-facing response; never a silent crash, never a raw exception |
| Event emission is best-effort | `kernos/kernel/events.py` | Every `emit()` is wrapped in try/except; event-logging failures never break the user's message flow |
| State Store is the query surface | Runtime reads | Runtime lookups go to the State Store, not the Event Stream |
| Shadow archive on removal | State mutations | No method permanently deletes data; "removal" sets `active: false` |
| Per-member first-class | All agent-facing code paths | Every member has their own profile, ledger, facts, relationships; the Soul dataclass is deprecated for identity |

## Reference documents

- **[Pipeline reference](pipeline-reference.md)** — per-phase inputs, outputs, code entries, and invariants for the six-phase turn
- **[Primitives reference](primitives-reference.md)** — the kernel primitives (RM dispatcher, fact harvest, compaction, awareness loop, gate, plan execution) with code pointers
- **Workflow Patterns** — see `/docs/workflow-patterns/` for the Gardener's judgment library (18 domain patterns plus a meta-contract; seeded into the Workflow Patterns canvas on first boot)

## Related depth

For the design thinking behind specific choices, see `/docs/reference/`:

- `kernel-architecture-outline.md` — the Phase 1B design document that shaped the kernel
- `design-ledger-vs-facts.md` — the dual-memory design rationale
- `design-build-the-missing-handle.md` — the workspace-discipline design frame
- `design-tool-execution-mediation.md` — the safety-and-tool design frame
- `architecture-notebook.md` — the reasoning behind the primitives (historical, still informative)
- `blueprint-original-vision.md` — the original owner vision (historical)
