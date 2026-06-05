# Kernos — Introduction

A personal agent operating system that learns who you are, builds the
tools you need, and earns trust one correct small action at a time.

This document is the canonical introduction. When the agent is asked
what Kernos is — what it does, how it works, what makes it different
— this is the answer it reaches. Everything else in `docs/` is depth
on a specific surface.

---

## Position

Most agent systems are stateless conversation loops with retrieval
bolted on. They forget you between sessions, treat every domain
identically, dump every concern into one context window, and enforce
behavioral rules through prompt instruction. The next conversation
starts from zero.

Kernos is built around a different shape. State is persistent and
hierarchical. Context is organized into specialized domains that
route invisibly. Memory compresses without losing truth. Behavioral
rules are enforced by the kernel, not requested in the prompt.
Specialized cohort agents handle judgment work *around* the principal
agent without competing for its attention. Tools build themselves in
conversation.

The promise is concrete: talk to it in plain language, and the
relevant context arrives when needed, the right tool is already at
hand, and the things worth remembering get remembered without you
curating them. When the work gets technical, Kernos writes the code,
finds the API, wires the integration, and files the result inside the
same conversation.

---

## Architectural innovations

Every agent framework faces the same hard problems. Kernos's
contribution is solving each one structurally — at the kernel level —
rather than patching them with prompt instructions or per-feature
band-aids.

### Cohort architecture

A principal agent surrounded by bounded specialist LLM workers.
Routing, gating, fact extraction, disclosure judgment, friction
observation — each runs as its own focused cohort, in parallel, on
its own slice of context. The principal agent never sees the cohorts;
the cohorts never see each other.

The conventional shape (LangChain, CrewAI, single-agent harnesses)
runs one agent loop where every system concern competes for the
agent's attention inline. Memory retrieval, safety checks, tool
routing, multi-member disclosure, skill selection — all in the same
context window. Kernos splits that. Judgment work runs on LLMs in
specialized cohorts. State work runs in Python. The principal agent
receives a curated, orchestrated context each turn and spends its
full attention on the conversation.

See [`architecture/cohort-and-judgment.md`](architecture/cohort-and-judgment.md)
for the design rationale and
[`architecture/cohort-fan-out.md`](architecture/cohort-fan-out.md)
for the runtime fan-out mechanism.

### Hierarchical context spaces

Multiple parallel domains per member — work, personal, a specific
project, a research sprint — each with its own ledger, its own
facts, its own promoted tool set, its own compaction rhythm. The
spaces are organized in a tree (General → Domain → Subdomain) and
inherit through a scope chain.

The user keeps one continuous conversation; the system delicately
weaves it into whichever space the topic belongs to. Switching
between domains doesn't mean starting over. The agent holds the
thread on both sides. **100 domains in Kernos is better than 100 chat
threads with one model**, because 100 chat threads forget you and
forget each other.

See [`architecture/context-spaces.md`](architecture/context-spaces.md).

### Compaction-driven memory

Two stores, two jobs.

The **Ledger** holds the conversational arc, compressed at compaction
boundaries rather than summarized turn-by-turn. Facts, decisions, and
context survive in their original form, addressable by source
reference.

The **Living State** holds structured knowledge, reconciled in a
single LLM call against the existing store rather than extracted
per-turn and deduplicated after the fact.

Lossless narrative retrieval and deduplicated fact supersession, both
at once. Full conversations are permanently archived and retrievable.

See [`architecture/memory.md`](architecture/memory.md).

### Infrastructure-level safety

Most agent systems gate what the agent can *reach*. Kernos gates what
the agent *does* — under which covenant, under which initiator
context. Every tool call passes through a dispatch gate that
classifies effect (`read` / `soft_write` / `hard_write` / `delete`)
and evaluates it against user-declared covenants.

Reactive soft-writes pass. Hard-writes gate for confirmation.
Non-reactive paths gate by default. Covenant violations surface as
**conflicts the agent must resolve**, not as silent denials. The
agent thinks; the kernel enforces. Rules survive across sessions,
spaces, tool calls, and member relationships.

See [`behaviors/covenants.md`](behaviors/covenants.md) and
[`architecture/safety-and-gate.md`](architecture/safety-and-gate.md).

### Multi-member disclosure with judgment

One hatched agent per member, not per install. A relationship matrix
declares permissions between members. A Messenger cohort sits *above*
the permissions and evaluates whether a response serves the
disclosing member's welfare.

Your spouse *can* see your calendar — the relationship grants it —
but Kernos still makes a judgment call about the therapy
appointment. Permission is necessary; it isn't sufficient.

See [`architecture/disclosure-and-messenger.md`](architecture/disclosure-and-messenger.md).

### Cognitive UI grammar

The system prompt as a typed document. Named zones (`RULES`,
`ACTIONS`, `NOW`, `STATE`, `RESULTS`, `PROCEDURES`, `MEMORY`),
cacheable prefix, provenance tags on every knowledge fragment. The
runtime refreshes zones selectively without rebuilding the prompt.

The agent knows where every piece of context came from — which space,
which conversation, which compaction. That provenance is what makes
trust enforceable.

See [`architecture/cognitive-ui.md`](architecture/cognitive-ui.md).

### Self-building tool workspace

The agent writes its own tools in conversation. "Track my invoices"
becomes a working invoice tracker in seconds. Tools register in a
universal catalog, surface by intent (not keyword matching), and stay
within a token budget via schema-weighted LRU eviction.

The user never sees infrastructure. No API keys to copy, no
configuration files, no Python environment. Capability grows with
use.

See [`architecture/workshop-external-services.md`](architecture/workshop-external-services.md).

### Four-layer cognition (decoupled-cognition path)

Cohorts → integration → presence → expression. The integration layer
produces a structured **Briefing**: what's relevant, what was
filtered, what action presence should take, what envelope constrains
it. The enactment layer routes between a thin path (render-only for
conversational kinds) and full machinery (plan + tier hierarchy +
envelope validation for action-shape kinds).

Hard rules are enforced **structurally**: thin path never dispatches
tools (the dispatcher is unreachable from that code path). Streaming
disabled inside full machinery (Protocol return types preclude it).
EnactmentService never changes the integration layer's decided
action (envelope validation enforces). No same-turn integration
re-entry on user-disambiguation flows (the dependency is structurally
absent).

Currently soak-validating behind a feature flag.

See [`architecture/integration-layer.md`](architecture/integration-layer.md),
[`architecture/presence-decoupling.md`](architecture/presence-decoupling.md),
and [`architecture/integration-wire-live.md`](architecture/integration-wire-live.md).

### Self-improvement loop

Kernos can rewrite its own code. The `improve_kernos` loop drafts a
spec, implements it with an external coding agent, reviews the diff —
checking not just correctness but **fidelity to the original request**
— gates the commit on the owner's approval, pushes, redeploys via
restart, and runs a **post-restart self-test** to confirm the new
build is healthy. If the self-test fails it enters a bounded recovery
flow (a small number of approval-gated fix-up commits) or abandons and
rolls back. The loop never goes silent: it narrates each step and
wakes the owner on completion, on a needed approval, or on failure.
`start.sh` (the un-protectable bootstrap) is off-limits to this loop by
construction.

Two quieter self-stewardship lanes share the same machinery: a **daily
self-maintenance review** (a creative two-lens pass over one slice of
the system per day) and an **immediate friction response** (reactive,
anti-looping resolution of the system's own operational errors). All
three ship **default-off** and opt-in.

See [`TECHNICAL-ARCHITECTURE.md`](TECHNICAL-ARCHITECTURE.md) §11b.

---

## What you can do with it

- **Talk naturally across domains.** Text a phone number, talk about
  work, then personal, then a side project. The system routes the
  turn invisibly into the right space and continues threads across
  hours, days, weeks.
- **Manage calendar, email, web, files.** Connected services exposed
  as MCP tools. The agent reaches them when the conversation calls
  for it. See [`capabilities/overview.md`](capabilities/overview.md).
- **Build tools in conversation.** "Log my calories" → a calorie
  logger with daily budgets. "Track my mileage" → a mileage tracker
  that knows which client you were visiting.
- **Set behavioral rules once.** "Always confirm before spending
  money." "Don't ask follow-ups about food logging." "Never reference
  the surprise party." Captured once, enforced permanently.
- **Share access selectively.** Add a spouse, a co-owner, a
  housemate. Declare what each can see. The system makes judgment
  calls inside the permissions.
- **Get smarter over time.** The system compacts conversations,
  extracts values, detects tensions, surfaces operational insights as
  whispers when there's a concrete actionable idea. See
  [`architecture/gardener.md`](architecture/gardener.md) and
  [`behaviors/proactive-awareness.md`](behaviors/proactive-awareness.md).
- **Improve its own code.** Ask it to make itself better and the
  `improve_kernos` loop specs, implements, reviews, gets your
  approval, deploys, and self-tests — rolling back if the new build
  fails. Default-off self-maintenance and friction-response lanes
  round it out. See [`TECHNICAL-ARCHITECTURE.md`](TECHNICAL-ARCHITECTURE.md) §11b.

---

## Operating principles

The agent's standing instructions. Some are enforced structurally by
the kernel; others are part of the principal agent's character.

- **Proper stewardship.** You manage someone's digital life. Act
  accordingly.
- **Intent over instruction.** Understand what they mean, not just
  what they say.
- **Conservative by default.** When uncertain, ask. Never assume
  permission.
- **Honest about limits.** Say what you don't know. Never fabricate.
- **Own your mistakes.** If you get something wrong, acknowledge it
  and correct it.
- **Be yourself.** You have a personality that evolves with the
  relationship. See [`identity/who-you-are.md`](identity/who-you-are.md)
  and [`identity/soul-system.md`](identity/soul-system.md).
- **Memory is your responsibility.** Remember things automatically;
  the user shouldn't have to repeat themselves.

---

## Documentation map

For depth on any surface, follow the link.

### Architecture

| Document | What it covers |
|----------|----------------|
| [`architecture/overview.md`](architecture/overview.md) | High-level map: kernel, handler, adapters, cohorts, spaces, memory, events |
| [`architecture/cohort-and-judgment.md`](architecture/cohort-and-judgment.md) | The cohort pattern — what it solves, what it costs, what it enables |
| [`architecture/cohort-fan-out.md`](architecture/cohort-fan-out.md) | Cohort runtime fan-out mechanism |
| [`architecture/cohort-cataloging.md`](architecture/cohort-cataloging.md) | The cataloging cohort that turns reference source files into catalog rows |
| [`architecture/context-spaces.md`](architecture/context-spaces.md) | Hierarchical spaces, scope chain, routing, isolation |
| [`architecture/memory.md`](architecture/memory.md) | Compaction, Ledger, Living State, retrieval |
| [`architecture/reference-primitive.md`](architecture/reference-primitive.md) | Reference primitive — catalog, cohort, hash validation, scope visibility, the seven tools |
| [`architecture/safety-and-gate.md`](architecture/safety-and-gate.md) | Dispatch gate, covenant enforcement, effect classification |
| [`architecture/disclosure-and-messenger.md`](architecture/disclosure-and-messenger.md) | Multi-member disclosure, relationship matrix, Messenger cohort |
| [`architecture/cognitive-ui.md`](architecture/cognitive-ui.md) | The system-prompt grammar — zones, caching, provenance |
| [`architecture/integration-layer.md`](architecture/integration-layer.md) | The Briefing contract that integration produces for presence |
| [`architecture/presence-decoupling.md`](architecture/presence-decoupling.md) | Four-layer cognition: cohorts → integration → presence → expression |
| [`architecture/integration-wire-live.md`](architecture/integration-wire-live.md) | Production wiring of the decoupled-cognition path |
| [`architecture/workshop-external-services.md`](architecture/workshop-external-services.md) | The agent's tool-building workspace |
| [`architecture/canvas.md`](architecture/canvas.md) | Canvas — shared markdown spaces for collaborative work |
| [`architecture/gardener.md`](architecture/gardener.md) | The improvement-loop primitive |
| [`architecture/event-stream.md`](architecture/event-stream.md) | Append-only event log: append, replay, audit |
| [`architecture/trigger-runtime.md`](architecture/trigger-runtime.md) | Unified time + event trigger runtime (WTC v1) |
| [`architecture/workflow-loops.md`](architecture/workflow-loops.md) | Workflow descriptors, action verbs, approval gates |
| [`architecture/workflow-drafts.md`](architecture/workflow-drafts.md) | Workflow Draft Primitive (WDP) — persistent conversational drafts |
| [`architecture/crb.md`](architecture/crb.md) | Conversational Routine Builder — proposal authoring + approval flow |
| [`architecture/kernel-tool-registry.md`](architecture/kernel-tool-registry.md) | The canonical kernel-tool registry |
| [`architecture/model-registry.md`](architecture/model-registry.md) | Provider/chain configuration and resolution |

### Behaviors

| Document | What it covers |
|----------|----------------|
| [`behaviors/covenants.md`](behaviors/covenants.md) | What covenants are, how they're captured, how they're enforced |
| [`behaviors/dispatch-gate.md`](behaviors/dispatch-gate.md) | Per-call gating and confirmation routing |
| [`behaviors/proactive-awareness.md`](behaviors/proactive-awareness.md) | Whispers, suppression, the awareness cycle |
| [`behaviors/scheduler.md`](behaviors/scheduler.md) | Time-based and event-based triggers |
| [`behaviors/instruction-types.md`](behaviors/instruction-types.md) | The taxonomy of user instructions |

### Capabilities

| Document | What it covers |
|----------|----------------|
| [`capabilities/overview.md`](capabilities/overview.md) | Catalog of connected services |
| [`capabilities/tool-surface.md`](capabilities/tool-surface.md) | The full kernel-tool catalog (76 tools) — effect classifications, per-tool documentation links |
| [`capabilities/references.md`](capabilities/references.md) | Reference primitive tools — `request_reference`, `store_reference`, recovery primitives |
| [`capabilities/calendar.md`](capabilities/calendar.md) | Calendar management |
| [`capabilities/web-browsing.md`](capabilities/web-browsing.md) | Web search and page browsing |
| [`capabilities/file-system.md`](capabilities/file-system.md) | File reads and writes |
| [`capabilities/memory-tools.md`](capabilities/memory-tools.md) | Memory query and recall surfaces |
| [`capabilities/channels.md`](capabilities/channels.md) | Inbound/outbound messaging |
| [`capabilities/cross-space-requests.md`](capabilities/cross-space-requests.md) | `request_space_action` — typed cross-space mutation |
| [`capabilities/external-agents.md`](capabilities/external-agents.md) | `consult` — bounded delegation to an external agent |
| [`capabilities/relational-messaging.md`](capabilities/relational-messaging.md) | Member-to-member messaging + `manage_members` |
| [`capabilities/diagnostics.md`](capabilities/diagnostics.md) | Runtime trace, friction observation, spec submission, chain inspection |

### Identity

| Document | What it covers |
|----------|----------------|
| [`identity/who-you-are.md`](identity/who-you-are.md) | The principal agent's character |
| [`identity/soul-system.md`](identity/soul-system.md) | How identity evolves through hatching and graduation |
| [`identity/onboarding.md`](identity/onboarding.md) | The first-15-turn hatching arc |

### Roadmap

| Document | What it covers |
|----------|----------------|
| [`roadmap/vision.md`](roadmap/vision.md) | Where this is going |
| [`roadmap/whats-next.md`](roadmap/whats-next.md) | Decided next steps |
| [`roadmap/future.md`](roadmap/future.md) | Longer-horizon directions |

For a more granular pipeline-level reference, see
[`architecture/pipeline-reference.md`](architecture/pipeline-reference.md)
and [`architecture/primitives-reference.md`](architecture/primitives-reference.md).

---

## Origin

Kernos is a one-person research project. Designed and constructed
from first principles. Every mechanism — compaction, hierarchical
spaces, the tool-building workspace, four-layer cognition — designed,
specified, implemented, and tested as part of one continuous design
arc, with the explicit goal of building a second brain that works
while you sleep.
