# Cross-Space Requests

The agent's primitive for mutating state in another context space without
entering it. The origin agent submits a typed request; the kernel evaluates
the target space's covenants and dispatches the mutation under target's
rules; origin gets a typed receipt. **Agent thinks; kernel enforces.**

The primitive lives at `kernos/kernel/cross_space/`. The full spec is at
`specs/CROSS-SPACE-REQUESTS-V1.md` (mirrored from Notion).

## When to use which shape

The agent picks among five action shapes depending on what the right move is:

| Shape | When |
|---|---|
| **Active space** | Default — respond in the user's current space. |
| **Cross-domain query** | Read a fact from another space to answer the current one (`query_mode` on the router). |
| **Cross-space request** *(this primitive)* | Write a bounded, typed mutation to another space — knowledge entry, covenant proposal, plan/workflow draft. |
| **External tool call** | Q&A against Claude Code / Codex / Gemini (`consult`); delegated CLI work (`code_exec(backend=...)`). |
| **Relational message** | Cross-member communication via the existing Messenger / relational-dispatch path. |

Cross-space requests are the right shape when the agent recognizes the
correct *action* belongs in another space and is one of the four whitelisted
kinds. Use sparingly.

## Action kind whitelist (v1)

| `action_kind` | What it does | Status produced |
|---|---|---|
| `write_knowledge` | Append a knowledge entry to target with cross-space provenance | `completed` |
| `propose_covenant` | Create a *covenant proposal* (always proposed-not-applied) in target | `proposed` |
| `create_plan_draft` | Create a plan in target with `status='draft'` (activation requires same-turn user confirmation) | `needs_confirmation` |
| `create_workflow_draft` | Create a workflow descriptor in target with `status='draft'` | `completed` |

Anything else is rejected at the substrate. **Deletes**, **external side
effects**, **arbitrary tool calls**, **cross-member targets**, and
**recursive cross-space requests** are all explicitly out of scope for v1.

## Tool surface

```yaml
request_space_action:
  target_space_id: str
  action_kind: write_knowledge | propose_covenant | create_plan_draft | create_workflow_draft
  work_order: dict     # structured per action_kind
  request_id: str      # optional; auto-generated; idempotency key
  safety_class: str    # optional; default "default"
```

Returns a `CrossSpaceReceipt` JSON with: `status`, `target_space_id`,
`timestamp`, `created_refs`, `target_audit_ref`, `provenance`,
`user_visible_summary`, `refusal_reason`.

The agent surfaces `user_visible_summary` to the user verbatim or
paraphrases in its voice. The receipt itself is logged to origin's
conversation log as part of the principal turn (it's a tool result).

## What appears where

| State location | What lands |
|---|---|
| **Target persistent state** | The actual mutation, with provenance fields populated (origin_space_id, source_turn_id, request_id, initiating_member_id, action_kind). |
| **Target event stream** | A `cross_space.action` event with the request capsule + receipt. Surfaces in target's awareness preamble on next entry. |
| **Target conversation log** | **No append.** Cross-space requests are not turns. |
| **Target compaction / harvest** | Untouched. |
| **Origin conversation log** | The `request_space_action` call + JSON receipt as part of the principal turn. |
| **Origin compaction** | Harvests cross-space reasoning normally — it happened in origin's turn. |
| **Audit log** | Distinct `event_type='cross_space.action'` entries. Cross-space mutations are higher-trust by class; same-space short-circuits do *not* audit (it's a normal local action). |

## Same-space short-circuit

When `target_space_id == origin_space_id`, the dispatch detects this
**before** acquiring any lock and routes to the existing local tool path
inline within the origin's already-held turn lock. No new lock is taken; no
audit entry is written (it's a normal local action). Receipt is shaped
consistently with cross-space so the agent can use `request_space_action`
uniformly without worrying about whether the target is local.

## Per-space lock + bounded timeout

Each (instance, space) pair has an `asyncio.Lock` shared between the turn
processor and the cross-space dispatch engine. The turn processor holds
the lock around the turn body; cross-space requests targeting that space
acquire the same lock with a bounded timeout (default 30s). On timeout,
the receipt comes back with `status='failed'` and
`refusal_reason='timeout_waiting_for_target'` — no mutation is attempted.

The lock scope is **mutation + event/audit emission as one ordered unit**:
the receipt only returns to origin after the lock has been released. This
guarantees origin sees a consistent view of target state when it reads the
receipt.

## Reentrancy

`_CROSS_SPACE_POLICY` is a separate policy table from the consult tool's
`_POLICY`:

| Calling context | Cross-space depth budget |
|---|---|
| `CONVERSATIONAL` | 1 |
| `WLP_EXECUTION` | 1 (future plan-step integration) |
| `DRAFTER` / `CRB_DISPATCH` / `TRIGGER_EVAL` / `COMPACTION` / `RECOVERY_SWEEP` / `UNKNOWN` | blocked |

Recursion (a cross-space request running inside a target dispatch trying
to issue another cross-space request) is structurally rejected.

## Target covenants

The kernel evaluates target-space covenants **before** any mutation. The
evaluator wraps the `DispatchGate`'s LLM-based covenant check, scoped to
`target_space_id`. Decision tokens:

- `approved` → execute the mutation; audit; return `completed`
- `covenant_conflict` → return `refused` with the conflicting rule's text in `refusal_reason`
- `needs_confirmation` → return `needs_confirmation`; target user re-evaluates

**`propose_covenant` bypasses target covenant evaluation by design** —
proposals are always proposed-not-applied; the proposal entity itself
records the request capsule and is auditable. Live covenant gating doesn't
apply to a write that doesn't go live.

## Target re-entry awareness

When the agent (or user) next enters a target space that has received
cross-space mutations within the last 24h, the assemble phase prefixes a
short `[CROSS_SPACE_INBOUND]` block to the agent's situation context. The
block names origin space, initiating member, request_id, status, and
created refs for up to 5 most recent events. The agent reads this and can
answer "why is this here?" using only target-local provenance + audit —
origin's conversation never enters target.

## Idempotency

`request_id` is the idempotency key. The dispatch engine looks up prior
`cross_space.action` events in target's event stream by request_id; a hit
returns the original receipt with no second mutation. Origin can safely
re-issue the same request on transient failures.

## Out of scope for v1

- Cross-instance requests (KERNOS-MESH; deferred indefinitely).
- Generated text in target's voice — receipts and structured artifacts only.
- `start_plan` (plans have triggers and scheduler hooks; reversibility unclear).
- `append_canvas_note` (canvas ownership semantics not yet stable).
- Plan-step `target_space` field on `manage_plan` (separate follow-on spec).
- Multi-member targets in a single space (concept doesn't exist in v1; each space has at most one owner).
- Async / long-running requests beyond same-turn-completion semantics.

## Composition with future arcs

The envelope and receipt shapes are stable so future consumers compose
without renegotiating the contract:

- **Workflow runtime** routing primitive
- **Domain actor inboxes** (if domains become first-class actors)
- **Pattern Observer patches** generating `propose_covenant` /
  `create_workflow_draft` requests
- **INTENT-TO-ROUTINE-COMPILER** producing workflow drafts via
  `create_workflow_draft`
- **Plan-step integration** — plan steps with `target_space` issue requests
- **Integration-layer briefings** — substrate-originated cross-space context

## Operator queries

```sql
-- Recent cross-space activity (any target):
SELECT timestamp, payload->>'origin_space_id', payload->>'target_space_id',
       payload->>'action_kind', payload->'receipt'->>'status'
FROM events
WHERE type = 'cross_space.action'
ORDER BY timestamp DESC LIMIT 20;

-- All requests targeting a particular space:
SELECT * FROM events
WHERE type = 'cross_space.action'
  AND payload->>'target_space_id' = '<space_id>';

-- Refused / failed only:
SELECT * FROM events
WHERE type = 'cross_space.action'
  AND payload->'receipt'->>'status' IN ('refused', 'failed');
```

## References

- Spec: `specs/CROSS-SPACE-REQUESTS-V1.md`
- Substrate: `kernos/kernel/cross_space/`
- Tool: `kernos/kernel/cross_space/tool.py` (schema + service)
- Tests: `tests/test_cross_space_*.py`
