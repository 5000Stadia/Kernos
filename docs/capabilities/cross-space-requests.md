# Capability: Cross-Space Requests

CROSS_SPACE_REQUESTS-V1 (shipped 2026-05-01). The `request_space_action` tool — a bounded primitive for typed, gate-mediated mutations into another space.

This page is a capability summary. The full architectural detail lives at [`../CROSS-SPACE-REQUESTS.md`](../CROSS-SPACE-REQUESTS.md).

## What it does

When you (an agent in space A) need to mutate state in space B — write a knowledge entry, propose a covenant, create a plan draft, create a workflow draft — `request_space_action` is the bounded primitive. It composes through the target space's covenants and gate; it doesn't bypass them.

The four allowed action kinds:

| Kind | What it does |
|---|---|
| `write_knowledge` | Add a knowledge entry to the target space's memory ledger |
| `propose_covenant` | Propose a covenant in the target space (target-side approval still required) |
| `create_plan_draft` | Create a plan draft in the target space |
| `create_workflow_draft` | Create a workflow draft in the target space |

Non-listed action kinds are rejected at the dispatch boundary.

## How it composes

Cross-space dispatch:

1. The originating agent (in space A) calls `request_space_action(target_space_id, action_kind, action_payload, request_id)`.
2. Idempotency check via `request_id` — repeated requests with the same id are recognized as duplicates and don't double-execute.
3. The target space's lock acquires (bounded timeout). Concurrent cross-space requests serialize against the target.
4. The target's covenants evaluate against the request — the target space owns "yes / no" on whether the action is allowed.
5. The target replays a re-entry awareness preamble describing what's about to happen — the target's agent gets a chance to surface friction.
6. Action executes (or is declined).
7. A typed receipt returns to the originator.

## When to use it

Use `request_space_action` when:

- You're in space A and the action belongs to space B's substrate (e.g., recording a fact about B's project; covenants that bind B's agent).
- You need the target space's covenants to gate the action (you're not bypassing them).
- The four action kinds cover what you want to do.

Don't use it for:

- Reading from another space (that's a different shape — query mode, not action).
- Mutations within your own space (use the regular tools).
- General-purpose RPC into another space (the surface is intentionally narrow; if you find yourself wanting more action kinds, that's a spec request).

## Per-space lock + bounded timeout

Each target space has a per-space asyncio lock. `request_space_action` acquires the lock with a bounded timeout; if the target is busy, the request returns a typed "target busy" error rather than blocking indefinitely.

## Idempotency via request_id

Pass a stable `request_id` per logical request. The cross-space dispatcher recognizes duplicates: if a request with the same `(originator_space, target_space, request_id)` has already executed, the existing receipt returns instead of re-executing. This makes retry-on-network-blip safe.

## Effect classification

`request_space_action` itself is `soft_write` from the originating space's perspective; the target side runs whatever effect classification the target action kind carries.
