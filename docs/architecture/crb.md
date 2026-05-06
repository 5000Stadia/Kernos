# CRB — Conversational Routine Builder

The user-facing surface where conversations become installed routines. CRB authors the proposal text, owns the approval-flow state machine, and emits the approval events that the substrate's workflow registry consumes for real registration.

CRB is **a service module the principal cohort uses** — NOT a cohort itself. It has no independent cursor, no per-turn budget, no passive observation loop. It runs reactively when the principal cohort invokes its authoring or approval methods.

## Where it sits

```
user conversation
        │
        ▼
principal cohort  ◄──────── decides WHEN
        │                   to surface a proposal
        ▼
CRB service       ◄──────── decides WHAT to say
        │
        ├─ compiler        (draft → descriptor)
        ├─ proposal author (LLM-driven user-facing wording)
        └─ approval flow   (state machine)
                │
                ▼
         event_stream emits
              │
              ▼
        STS / WorkflowRegistry
        (atomic registration)
```

The principal cohort decides when surfacing makes sense. CRB decides what wording to use, owns the approval flow's state, and emits the right events.

## Module layout

CRB lives in `kernos/kernel/crb/`:

| Submodule | Purpose |
|---|---|
| `compiler/` | Pure descriptor translation: `draft_to_descriptor_candidate(draft)` plus cheap shape assertions. Replaces Drafter v1's compiler_helper_stub. |
| `proposal/` | `InstallProposal` types + durable `InstallProposalStore` (SQLite, composite uniqueness on `(instance_id, correlation_id)`) + `CRBProposalAuthor` (LLM-driven user-facing wording with `MAX_TEMPERATURE=0.3`). |
| `approval/` | `CRBApprovalFlow` state machine + the six duplicate/late C11 cases inline in `handle_response` + `recover_pending_registrations` engine-startup sweep. |
| `events.py` | Five typed emit methods + the `"crb"` source_module emitter adapter. |
| `principal_integration/` | Subscription and receipt-ack wiring back to the principal cohort. |
| `bringup_adapters.py` | Production adapters (`ReasoningLLMAdapter`, `DraftRegistryReadAdapter`, `SubstrateToolsSTSAdapter`) bridging substrate dependencies to CRB ports. |
| `errors.py` | Typed error hierarchy. |

## Approval flow state machine

A proposal moves through one of these terminal states:

| State | Meaning |
|---|---|
| `proposed` | Initial. Proposal authored and surfaced; awaiting user response. |
| `approved_pending_registration` | User approved; STS registration in flight. Crash-safe — recoverable on restart via `recover_pending_registrations`. |
| `approved_registered` | Registration succeeded; the workflow is live. |
| `modify_requested` | User wants changes; control returns to Drafter for re-shaping. |
| `declined` | User declined. |

The state machine is enforced at the `transition_state` boundary in `InstallProposalStore`. Illegal transitions raise `InvalidStateTransition`. Concurrent transitions (e.g., user approving while a recovery sweep tries to retry registration) are caught by `StaleStateError` — the UPDATE is conditional on the prior state via composite WHERE.

## Restricted ports

`CRBApprovalFlow` takes its dependencies as typed Protocols, not the full substrate facades:

| Port | What it allows | Why narrowed |
|---|---|---|
| `DraftReadPort` | `get_draft(instance_id, draft_id)` only | Read-only — CRB never writes drafts; the registry's write surface is structurally absent from CRB. |
| `STSRegistrationPort` | `register_workflow(...)` + `find_workflow_by_approval_event_id(...)` only | No `dry_run` parameter — CRB is always production registration, never dry-run. |
| `CRBEventPort` | Five typed emit methods | Raw `EventEmitter.emit` is structurally absent from the surface; no way to spoof source_module via payload. |

Tests inject deterministic stubs against these Protocols; production wires concrete adapters from `crb/bringup_adapters.py` over the substrate's full facades.

## Event shapes

Five named event types emit through the event_stream substrate via the registered `"crb"` source_module:

| Event | When |
|---|---|
| `routine.proposed` | Proposal authored and surfaced; awaiting response |
| `routine.approved` | User approved a fresh proposal |
| `routine.modification.approved` | User approved a modification of an existing routine |
| `routine.declined` | User declined |
| `crb.feedback.modify_request` | User requested changes; Drafter picks up the feedback for re-shape |

The `"crb"` source_module is registered exactly once at substrate bring-up via the EmitterRegistry singleton. The event envelope's `source_module` is set by the substrate from the registered emitter's identity, not from caller payload — this is the trust boundary that makes STS approval source authority structurally enforceable. STS's approval-binding gate reads source authority from `event.envelope.source_module`, never from the payload.

## Author temperature pin

`CRBProposalAuthor` refuses an LLM client whose `temperature > MAX_TEMPERATURE = 0.3` at construction. The bring-up `ReasoningLLMAdapter` pins temperature at 0.2; the cheap chain in production is configured for low-temperature stateless completions. This is a conservatism pin — proposal text the user reads and approves should be predictable, not creative.

## Crash-safe registration handoff

Approval moves the proposal to `approved_pending_registration` BEFORE STS registration runs. If the process crashes between approval and registration, the row is durable and recoverable: on engine startup, `recover_pending_registrations` walks pending rows across all instances and retries STS registration. Composite uniqueness on `(instance_id, correlation_id)` plus the state-machine WHERE conditions prevent double-registration on retry.

Codex mid-batch fix REAL #1 (folded during C5b development) closed a stale-snapshot race: the UPDATE for `transition_state` is conditional on the prior state via composite WHERE; a `rowcount = 0` means another path got there first and the caller must re-read.

## Anti-fragmentation invariant

CRB consumes shared context surfaces (event_stream, WDP DraftRegistry, STS query surfaces, principal cohort context). It does NOT build a parallel context model. Future specs that compose against CRB (Workshop layer, voice/expression module, audit log surface, routine library) consume CRB's existing facades without converting CRB into a cohort.

Reviewers should reject changes that introduce CRB-private context state, parallel friction-detection logic, shadow registries, a cursor, a budget, or a passive observation loop into CRB. CRB stays a service module.

## Bring-up

`bring_up_substrate` constructs the five-component CRB bundle:

- `InstallProposalStore` — sqlite-backed proposal state machine
- `CRBProposalAuthor` — LLM-driven user-facing wording
- `CRBApprovalFlow` — state machine over the restricted ports
- `CRBEventEmitter` — typed adapter over the registered `"crb"` source_module
- The three port adapters that bridge substrate dependencies (DraftRegistry, SubstrateTools, ReasoningService)

Tear-down stops `install_proposal_store` cleanly (closes the aiosqlite connection); `CRBApprovalFlow` has no stop method (it's stateless beyond what's in the store). The EmitterRegistry's `get_or_register` shape makes the `"crb"` registration idempotent across re-bring-up.

See [`workflow-loops.md`](workflow-loops.md) for how CRB composes with WLP / STS / WDP at the substrate level.
