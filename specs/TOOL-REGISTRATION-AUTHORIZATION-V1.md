# TOOL-REGISTRATION-AUTHORIZATION-V1

**Date:** 2026-05-22
**Status:** Draft for review (sub-spec #1 of `TOOL-MAKING-ARC-V1`)
**Scope:** Register-time approval gate for `hard_write` and
  `external_agent_read` tool registrations. Durable
  pending-registration store. Consumes `DURABLE-APPROVAL-RECEIPTS-V1`
  as the operator-approval substrate. No dispatch-time change
  (that's `LIVE-DISPATCH-UNBLOCKER-V1`).
**Estimated size:** ~200 LOC source + ~150 LOC tests.

## Why this spec exists

Per `TOOL-MAKING-ARC-V1` D3 + Codex r2#4: the live dispatch
path doesn't run the full policy gate today
(`live_wiring.py:31` confirms full evaluate is acknowledged-
future). Until D1 (`LIVE-DISPATCH-UNBLOCKER-V1`) ships and
restores dispatch-time policy enforcement, register-time is the
only gate that can stop an agent from registering a `hard_write`
tool and immediately invoking it without operator awareness.

Defense in depth: once D1 lands, register-time + dispatch-time
both gate `hard_write`. Register-time confirms the operator
agreed to expose the tool at all; dispatch-time evaluates
whether *this particular call* should fire.

Bonus: this is the first real consumer of the receipts
substrate (`DURABLE-APPROVAL-RECEIPTS-V1`), validating its
shape against an actual integration before the autonomous-loop
arc lands its own consumer.

## Current state

- `WorkspaceManager.register_tool()` in
  `kernos/kernel/workspace.py:376` validates the descriptor
  + implementation, computes a registration hash, and
  *unconditionally* creates the `CatalogEntry` + adds to
  manifest. No approval gate.
- `parse_tool_descriptor()` already extracts the
  `gate_classification` field (default `unknown`) — the
  substrate already knows whether a descriptor declares
  `hard_write`.
- `approval_receipts` module ships (`96f4582`) with
  `ensure_schema`, `issue`, `approve`, `reject`, `boot_reconcile`,
  `expire_pass`. Schema includes
  `workflow_execution_id` + `gate_nonce` columns (sized for the
  autonomous-loop use case) — we can use them or leave them
  NULL for this consumer.
- `/approve` + `/reject` slash commands wire to
  `approval_receipts.approve()` + `reject()` for the two-step
  CONFIRM contract.

## Design

### Classification → approval requirement

v1 simplifies the design spec's 6-row table down to **3 rules**
per `[[v1-operational-verification-scope-discipline]]`:

| Classification         | Approval at register-time                     |
| ---------------------- | --------------------------------------------- |
| `read` / `soft_write`  | auto-approve (current behavior preserved)     |
| `hard_write`           | owner confirm-once via receipt                |
| `external_agent_read`  | owner confirm-once via receipt                |

The design spec's cross-space distinction for `soft_write` is
deferred: no current caller distinguishes own-space vs.
cross-space at registration time, and adding the distinction
without a use case to pin the contract risks the
PREFLIGHT-pattern mistake. Future sub-spec can add the
distinction when cross-space tool authoring emerges as a
demand.

Classification source: the descriptor's `gate_classification`
field, parsed by `parse_tool_descriptor()`. Descriptors that
omit the field default to `unknown` — those are treated as
**auto-approve** for v1 (the existing behavior). Tightening
unknown → require approval is the cleaner posture but would
also break every existing agent-authored tool flow today.
Note this as a known follow-up; tighten after the catalog
classification audit in `LIVE-DISPATCH-UNBLOCKER-V1` lands.

### Pending-registration store

Receipts substrate already provides durable storage with
issue/approve/reject lifecycle + boot reconcile + expiry. The
new shape: when `register_tool` hits the gate, it issues a
receipt whose payload carries the tool registration descriptor
+ implementation hash. On approval, a callback creates the
`CatalogEntry`.

No new SQL table needed. The receipts table's `request_payload`
JSON column carries:
```json
{
  "kind": "tool_registration",
  "instance_id": "...",
  "space_id": "...",
  "descriptor_file": "my_tool.tool.json",
  "name": "fetch_weather",
  "description": "...",
  "gate_classification": "external_agent_read",
  "registration_hash": "...",
  "force": false
}
```

The receipts `purpose` field gets a stable string like
`"tool_registration"` so `/approve` can identify the kind +
dispatch to the activation callback. Existing receipts that
don't carry a `purpose` continue working — the dispatch is
defensive: missing/unknown purpose falls back to the existing
no-op (receipts already track approved state; this is just
"what to do on approval").

### `register_tool` flow

```
1. Validate descriptor + impl (existing).
2. Compute registration_hash (existing).
3. Classify via parse_tool_descriptor → extended_descriptor.gate_classification.
4. If classification requires approval AND no auto-approve mode:
   a. Look up any existing pending receipt for (instance_id,
      registration_hash). If approved → proceed to step 6.
      If still pending → return "tool pending approval,
      request_id=X". If rejected → return rejection reason.
   b. Otherwise issue a new receipt with purpose=
      "tool_registration" + the payload above.
   c. Return synchronously: "Tool registration pending owner
      approval. Request ID: {request_id}. The owner will see
      a notification; the tool surfaces on next assemble after
      approval."
   d. Emit TENANT_TOOL_REGISTRATION_PENDING event.
5. If classification is auto-approve: proceed with existing flow.
6. Activate (existing flow): catalog.register + manifest update +
   TOOL_REGISTER log.
```

### Approval callback dispatch

`approval_receipts.approve()` already returns `(ok, message)`. We
extend the slash-command path (`/approve <id> CONFIRM`) to:
1. Call `approval_receipts.approve()` — atomic CAS.
2. If `ok=True`, read the approved receipt's `request_payload`.
3. If `purpose == "tool_registration"`: invoke the activation
   callback (a new helper that takes the payload + completes
   the catalog.register flow).
4. If `purpose` unknown / missing: existing behavior (no
   downstream action).

The callback is failure-isolated: if activation fails, the
receipt stays approved (idempotency) and a friction report
fires. Re-approve is a no-op since the receipt is already in
terminal state; operator must intervene manually or the agent
must re-issue.

### Activation failure semantics

Two paths to consider:
- **Activation succeeds**: catalog.register + manifest update +
  TOOL_REGISTER log + (new) TOOL_REGISTRATION_APPROVED event.
- **Activation fails after approval** (e.g., descriptor file
  was deleted between issue + approve): friction report,
  surface to operator with clear "approved but activation
  failed — descriptor missing" message. Receipt stays approved
  (terminal); agent's next register_tool call sees no pending
  + no catalog entry + can retry from scratch.

### Race handling

- **Agent invokes a pending tool by name**: the catalog
  doesn't have it yet (we deferred register), so dispatch
  returns the standard "tool not found" error. No special
  case needed.
- **Agent calls register_tool again with same hash, while
  pending**: returns "tool pending approval, request_id=X"
  with the original request_id. Idempotent on hash.
- **Agent calls register_tool with a different descriptor**
  for an already-pending hash: pending entry stays; new call
  creates its OWN pending entry. The two are independent.
- **Operator approves while agent is mid-register_tool**: the
  agent's call sees the approved state on its next pending
  lookup; proceeds to activation. No double-activation
  (CatalogEntry already exists → existing "name conflict"
  return path catches it).

### Notification surface

For v1: the operator sees pending registrations via:
1. The synchronous reply message the agent's request emitted
   (operator is usually in the same thread).
2. `/approve` lists pending receipts (existing surface).
3. A new INFO log: `TOOL_REGISTRATION_PENDING request_id=X
   tool_name=Y classification=hard_write`.

No new Discord DM or scheduled notification flow in v1 — that
overlaps with the broader operator-notification arc that's
not in scope here. If the agent wants to nudge the operator
they can use the existing `send_to_channel` tool.

### Idempotency on descriptor hash

A second register_tool call with the same hash within the
pending window returns the existing pending receipt's
request_id rather than issuing a new one. This protects
against retry loops + provides a stable handle the agent can
poll on. Hash mismatch (descriptor or impl edited) → new
pending entry, original stays open until expiry or explicit
rejection.

## Acceptance criteria

| AC | Description |
|---|---|
| AC1 | `register_tool` with classification=`read` proceeds without approval (existing behavior preserved). |
| AC2 | `register_tool` with classification=`soft_write` proceeds without approval. |
| AC3 | `register_tool` with classification=`hard_write` issues a receipt + returns request_id + does NOT create CatalogEntry. |
| AC4 | `register_tool` with classification=`external_agent_read` issues a receipt + returns request_id. |
| AC5 | Receipt payload contains `purpose="tool_registration"` + descriptor + registration_hash. |
| AC6 | `/approve <id> CONFIRM` on a tool_registration receipt: marks approved AND creates CatalogEntry AND adds to manifest. |
| AC7 | `/reject <id> <reason>` on a tool_registration receipt: marks rejected; agent's next register_tool with same hash returns the rejection reason. |
| AC8 | `register_tool` called twice with same descriptor hash while pending: returns the same request_id (idempotent). |
| AC9 | After approval, calling `register_tool` again with same hash: returns the existing CatalogEntry (no double-register). |
| AC10 | Invoking a pending (unregistered) tool by name returns the standard "tool not found" error (no surface leak). |
| AC11 | Activation failure (descriptor deleted post-approval) surfaces via friction report + clear operator message; receipt stays approved. |
| AC12 | `TOOL_REGISTRATION_PENDING` event fires when a registration enters pending state. |
| AC13 | `TOOL_REGISTRATION_APPROVED` event fires on successful activation. |
| AC14 | Pending receipts survive restart (already covered by receipts substrate; verify the registration-payload round-trips). |
| AC15 | Pending receipts expire per receipts substrate's TTL (already covered; verify expiry doesn't crash on missing payload fields). |
| AC16 | Classification=`unknown` defaults to auto-approve (documented as known follow-up; pin the v1 behavior). |
| AC17 | No regressions on existing workspace + tool-registration tests. |

## Soak gate

1. **Automated**: ACs pin gate + lifecycle + idempotency + race.
2. **Operator soak**:
   - Agent calls register_tool with a hard_write descriptor.
   - Operator sees the pending message + request_id.
   - `/approve <id> CONFIRM` → operator sees activation
     success message; agent sees tool in next turn's catalog.
   - Verify `TOOL_REGISTRATION_APPROVED` event fired.
3. **Reject soak**:
   - Same setup; operator `/reject <id> not appropriate`.
   - Agent's retry returns the rejection reason.
   - No CatalogEntry created.

## Out of scope

- Dispatch-time policy enforcement → `LIVE-DISPATCH-UNBLOCKER-V1`.
- Cross-space distinction for `soft_write` → future spec when
  caller emerges.
- Tightening `unknown` classification default → future spec
  after LIVE-DISPATCH-UNBLOCKER-V1 lands the audit.
- Operator notification UX (Discord DM, scheduled nudge) →
  out of scope; operator polls `/approve` list today.
- Per-call dispatch gate → `LIVE-DISPATCH-UNBLOCKER-V1`.

## Risks

- **Risk:** Activation failure after approval leaves the
  receipt approved + the catalog unchanged. Agent retries see
  "approved but no entry" — confusing.
  - **Mitigation:** Friction report + clear operator-facing
    message. Agent's retry hits "no existing pending + no
    catalog entry" → falls through to re-register (which
    re-issues a fresh receipt). Documented in spec.

- **Risk:** Operator approves the wrong receipt by ID typo
  (no exact-phrase confirm for tool registrations).
  - **Mitigation:** The receipts substrate already requires
    `/approve <id> CONFIRM` (two-step). The two-step is the
    typo guard. If the operator types the wrong ID in step 1,
    they'd see a preview of the wrong tool and re-issue with
    the correct ID. Acceptable.

- **Risk:** Hash collision between two distinct descriptors
  with the same hash (theoretical).
  - **Mitigation:** SHA-256 collision space is astronomical;
    not a practical concern. Documented.

## Dependencies

- `DURABLE-APPROVAL-RECEIPTS-V1` (commit `96f4582`) — landed.
  Provides issue/approve/reject + payload storage + expiry.
- `WORKSHOP-EXTERNAL-SERVICE-PRIMITIVE` — landed.
  Provides `parse_tool_descriptor` + classification extraction.

## Migration

- **Schema**: no new tables. Reuses `approval_receipts`.
- **Existing workspace tools**: pre-existing CatalogEntries
  are unaffected. The gate only triggers on `register_tool`
  calls (new registrations).
- **Receipts in flight**: receipts created before this spec
  shipped have NULL `purpose` — they continue working through
  the existing `/approve` path with no activation callback,
  which is exactly the current behavior. No back-fill needed.
