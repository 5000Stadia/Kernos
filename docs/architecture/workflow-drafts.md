# Workflow Drafts (WDP)

The Workflow Draft Primitive (WDP). Persistent, conversational workflow drafts — the durable coordination object that lives between the moment a user describes a routine and the moment it gets installed.

WDP is what makes routine-creation conversational rather than form-shaped. The user can shape a draft over multiple turns, switch contexts and come back, get clarifying questions answered, and end up with a real, validated workflow descriptor — without losing the in-flight state at any point.

## Where WDP sits

```
user conversation
    │ "let's set up a thing where..."
    ▼
Drafter (the cohort that shapes drafts conversationally)
    │
    ▼
DraftRegistry (this primitive)  ◄──── persistent, lifecycle-tracked
    │
    │  draft.create / draft.update / draft.mark_committed
    ▼
CRB compiler  ◄──── pure: draft → descriptor candidate
    │
    ▼
CRBProposalAuthor → CRBApprovalFlow → STS register_workflow
    │                                      (atomic w/ triggers)
    ▼
WorkflowRegistry + TriggerEvaluationRuntime
```

WDP is the durable middle. Drafter shapes; CRB authors + approves; STS registers. Without WDP the conversation has nowhere to land between turns.

## What a draft carries

A `WorkflowDraft` row carries identity + lifecycle + provenance + a free-form partial spec:

| Field | Purpose |
|---|---|
| `draft_id` | Stable identifier within the instance |
| `instance_id` | Composite PK with `draft_id` |
| `version` | Optimistic concurrency counter |
| `status` | Lifecycle state — `drafting` / `pending_approval` / `committed` / `abandoned` |
| `home_space_id` | Mutable "most-relevant-to" pointer; supports re-homing as the draft's topic clarifies |
| `source_thread_id` | The conversation thread the draft originated in |
| `created_at`, `last_touched_at` | Provenance timestamps |
| `intent_summary` | Human-readable description of what the draft is for |
| `resolution_notes` | Reasons / decisions captured during the draft's lifecycle |
| `aliases` | Alternate names users have given the draft |
| `partial_spec_json` | The in-progress spec — fields filled, fields still pending |

## State machine

```
   create_draft
        │
        ▼
   ┌─drafting──┐
   │            │  update_draft (n times)
   │            │  re-home / re-name / re-shape
   │            │
   │  mark_committed                        abandon_draft
   ▼                                            ▼
committed                                   abandoned
   │
   │  via CRB approval flow + STS register
   ▼
(workflow lives in WorkflowRegistry)
```

Mutations are optimistic-concurrency-controlled via `expected_version`; concurrent updates raise `DraftConcurrentModification` and the caller must re-read.

## Composite primary key

`(instance_id, draft_id)` per DAR's pattern. Two instances may both have a `draft-001` without collision. Cross-instance lookups return `None` / empty. This is the consistent multi-instance keying the rest of the substrate uses.

## Substrate-neutral by invariant

WDP MUST NOT depend directly on Canvas, tools, domains, agents, or any specific future subsystem. The design-review invariant is explicit: drafts expose stable identity (`draft_id`), lifecycle state, provenance, mutable home pointer, human-readable notes, and the five `draft.*` event types. Other surfaces project drafts into their own world by **reference**, not by composition.

Why: if WDP knew about every surface that might want to render or enrich drafts, the primitive would balloon and break under each new surface added. Instead WDP is a small, durable coordination object; Canvas can render pending routines, tools can validate capabilities, domains can provide briefs — without coupling back into WDP.

Reviewers of any WDP follow-on spec should reject changes that introduce direct dependencies on adjacent subsystems.

## Event shapes

Five named event types emit through the event_stream substrate via an injected emitter:

| Event | When |
|---|---|
| `draft.created` | `create_draft` lands |
| `draft.updated` | `update_draft` lands (any field change) |
| `draft.committed` | `mark_committed` flips the row |
| `draft.abandoned` | `abandon_draft` flips the row (or sweep reaps it) |
| `draft.live_sweep` | Background sweep ran; reports any abandoned rows reaped |

Event emission is optional at the registry level — an injected callable; `None` means no-op (test fixtures). Production wires the emitter to `event_stream.emit` at engine bring-up.

## Where it lives

`kernos/kernel/drafts/registry.py`. Schema stored on `instance.db` via the same aiosqlite pattern as the other instance-scoped tables.

## Composes with

- **Drafter cohort** — the conversational shaper. Reads + mutates drafts via the registry's keyword-only API.
- **CRB compiler** (`kernos/kernel/crb/compiler/`) — pure translation `draft → descriptor candidate`. Reads `partial_spec_json` and produces a workflow descriptor.
- **CRB approval flow** — takes a `DraftReadPort` adapter over the registry (`get_draft` only — read-only narrowed surface).
- **STS `register_workflow`** — final atomic step that registers the workflow + its triggers from the descriptor; on success, the draft is `committed`.
- **Live sweep** — `cleanup_abandoned_older_than` runs periodically to reap stale `drafting` rows whose `last_touched_at` exceeds a threshold.
