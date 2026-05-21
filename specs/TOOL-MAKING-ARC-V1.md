# TOOL-MAKING-ARC-V1 — Design spec

**Date:** 2026-05-21 (revised post-Codex rounds 1 + 2)
**Status:** Draft for review (design spec, not implementation)
**Scope:** The end-to-end arc for Kernos to make, register, classify,
  dispatch, audit, and revoke tools — both pre-built stock integrations
  AND tools the agent authors at runtime. This spec is the
  **architecture contract**; implementation sub-specs flow from it.
**Estimated size:** 0 LOC. This is a design.

## Why this spec exists

The Kernos vision includes "Kernos can make its own tools." Today the
substrate has most of the necessary pieces — agent-callable
`register_tool`, batch-loader `register_stock_tools`, descriptor schema
with classification fields, catalog, dispatch path, service registry,
audit log, authoring-pattern validation. But the pieces don't compose
into a working end-to-end arc. The 2026-05-21 smoke test exposed the
seam: pre-built Notion tools are loaded into the catalog at boot, yet
when the agent calls `notion_write_page`, the live-integration
dispatcher refuses with `"not classified by the dispatch gate
(classification='unknown')"`.

The failure is not a single bug; it's three coupled missing contracts
between layers (Codex round 1 deepened this picture):

1. **Classification** doesn't reach the gate from descriptor-bearing tools.
2. **Dispatch routing** at the live integration path only knows kernel
   and MCP destinations — even after we fix classification, the call
   would route as MCP, not stock/workspace.
3. **The dispatch-time policy gate** (covenant + owner-confirm for
   hard_write) is acknowledged-future on the live integration path
   (`live_wiring.py:31` comment); only `classify_tool_effect` runs.
   So any non-`unknown` classification proceeds to execution without
   confirmation.

All three must close together or we'd swap one bug ("unknown refusal")
for another ("hard_write tools execute without owner confirmation").

Drafting this BEFORE the implementation specs serves two purposes:
1. Bug-chasing ends. Every subsequent fix is anchored here.
2. The agent gets a coherent story about what it can do with tools,
   not a pile of underdocumented primitives that mostly work.

## Current state (truth, not memory)

(From the 2026-05-21 audit + Codex spot-check round 1. Pin: when this
spec ships, recheck before implementing each sub-spec — file lines drift.)

### What exists and is wired

- **Stock-tool loading** (`workspace.py:669`) — `register_stock_tools()`
  walks `kernos/kernel/integrations/*/*.tool.json`, registers each into
  the catalog. Called at handler boot (`handler.py:1031`). The 2 Notion
  tools ARE in the catalog after boot.
- **Agent-callable registration** (`workspace.py:376`) — `register_tool`
  primitive accepts a descriptor filename in a workspace, validates,
  parses extended fields, runs authoring-pattern checks, registers.
  Conflict policy: registration refuses non-workspace name collisions
  (`workspace.py:456`).
- **Descriptor schema + parser** (`tool_descriptor.py`) — `.tool.json`
  supports `name`, `description`, `input_schema`, `implementation`,
  PLUS extended fields: `service_id`, `authority[]`, `operations[]`
  (per-op `classification`), `audit_category`, `domain_hints[]`,
  `aggregation`, `stateful`. Hand-rolled validator (no jsonschema).
  `ToolDescriptor` immutable dataclass with `.classification_for(op)`
  resolution (per-op → tool-level → soft_write default).
- **Classification→gate-effect mapping** (`tool_gate_routing.py:52`)
  — `gate_effect_for(classification)` maps descriptor enum values to
  the gate vocabulary, e.g. `delete` → `hard_write`. Any consumer of
  descriptor classifications **must** route through this, not raw
  enum values.
- **Catalog** (`tool_catalog.py`) — in-memory dict of `CatalogEntry`
  per tool. Sources today: `kernel | mcp | workspace`. Workspace
  entries store `service_id`, `registration_hash`, `force_registered`,
  `stock_dir`, `stateful`. **`classification`, `audit_category`,
  `authority`, `domain_hints`, per-op classifications are NOT stored**
  — they're re-parsed from disk at dispatch time (only by the
  service-bound path; the live-integration path never re-parses).
- **ServiceRegistry** (`services.py:502`) — stock services
  (`kernos/kernel/services/*.service.json`) load at boot. Cross-validates
  `service_id` + `authority[]` during descriptor parsing.
- **Service-bound dispatch** (`workspace.py:770` `_execute_service_bound_tool`)
  — six-step path: re-parse descriptor, enforce_invocation (hash, auth,
  scope, sandbox), build runtime context, call execute(), audit, return.
  Reachable today ONLY via the legacy workspace-tool path
  (`execute_workspace_tool`), which the live-integration dispatcher
  does not call.
- **CapabilityRegistry** (`capability/registry.py`, `capability/known.py`)
  — hand-maintained list of capabilities with `tool_effects` dict
  (tool name → classification). Used by the gate as one of its
  classification sources.
- **Authoring-pattern validation** (`tool_validation.py`) — regex
  scan for hardcoded paths, raw fs access, secret env reads. Rejects
  unless `force=True`. Independent from runtime enforcement.

### What's partial

- **Capability registry vs. catalog separation.** Capabilities
  (MCP-discovered or hand-listed) carry classifications via
  `tool_effects`. Stock + workspace tools carry classifications via
  their descriptor's `operations[]`. The two storage paths don't
  cross-talk.
- **Audit coverage.** Service-bound dispatch audits every call
  with a rich `ToolInvocationAuditEntry`. Legacy subprocess dispatch
  does not. The live-integration dispatcher emits its own audit
  entries via `_emit_audit` (`live_wiring.py:442` for `tool_call_failed`
  and `live_wiring.py:467` for `tool_call_succeeded`), separate from
  the event-stream `tool.called` / `tool.result` events it also
  emits (`live_wiring.py:413`, `live_wiring.py:459`). So a call
  through both the live path and the service-bound path produces
  TWO mismatched audit entries; a call through only the live path
  gets the dispatcher's audit but misses descriptor-derived
  `audit_category` and operation context. Duplicate / context-poor
  audit is the real risk for the spec to address.
- **Catalog rehydration after restart.** `ensure_registered`
  (`workspace.py:1025`) re-registers entries from on-disk descriptors
  but does not parse extended metadata. Even today's stored fields
  may not survive process restart faithfully.
- **Test coverage.** Unit tests for each piece exist; no end-to-end
  "agent registers tool, then agent calls it through the live
  dispatch path with audit" integration test.

### What's not wired (the bug class)

- **Live-integration dispatch path does not reach workspace/stock tools.**
  `LiveIntegrationDispatcher.dispatch` calls
  `classify_tool_effect`, refuses on `unknown` (`live_wiring.py:386`),
  then calls `reasoning.execute_tool` (`live_wiring.py:424`).
  `ReasoningService.execute_tool` (`reasoning.py:817`) only routes
  kernel tools or `_mcp.call_tool` — there's no branch to
  `WorkspaceManager.execute_workspace_tool`. So even if we fix
  classification, the call would route to MCP (wrong destination
  for stock/workspace tools).
- **Gate ignores catalog classifications.** `classify_tool_effect`
  (`gate.py:94`) consults: hardcoded `_KERNEL_READS`/`_KERNEL_WRITES`,
  action-dependent branches, capability registry's `tool_effects`. It
  does NOT consult the catalog entry — even though the descriptor's
  `operations[].classification` is the authoritative classification
  for stock and workspace tools.
- **Live-integration dispatch path runs only `classify_tool_effect`,
  not the full policy gate.** `DispatchGate.evaluate` (covenant
  check + loss-cost model + confirmation flow for hard_write) is
  not invoked. Any non-`unknown` classification proceeds to execution
  — the file's own comment (`live_wiring.py:31`) flags this as a
  follow-up. Combined with the prior gaps, this means once we fix
  classification + routing, agent-authored `hard_write` tools would
  execute on first call without owner confirmation. **This is the
  largest substrate-level safety gap revealed by this design pass.**
- **No operator introspection.** `/dump` doesn't list current tools.
  Operator can't easily see "what does the bot have right now,
  classified how, dispatched through which path."

## End-to-end arc — the contract

```
                  ┌─────────────────────────────────────────────────┐
                  │  Agent decides: "I need a tool for X"           │
                  └────────────────────┬────────────────────────────┘
                                       │
            ┌──────────────────────────┴──────────────────────────┐
            │                                                      │
   (already exists)                                       (new — agent path)
            │                                                      │
            ▼                                                      ▼
   ┌─────────────────────┐                              ┌──────────────────────┐
   │  Stock integration  │                              │  Agent authors:      │
   │  shipped with code  │                              │  - descriptor file   │
   │  (.tool.json + .py) │                              │  - implementation    │
   └─────────────────────┘                              │  in workspace        │
            │                                            └──────────┬───────────┘
            │                                                       │
            │           ┌──────────────────────────┐                │
            │           │ Authoring-pattern check  │◄───────────────┤
            │           │ (tool_validation.py)     │                │
            │           └──────────┬───────────────┘                │
            │                      │ pass/fail (force= overrides)   │
            │                      ▼                                │
            │           ┌──────────────────────────┐                │
            │           │ Service cross-validation │                │
            │           │ (authority ⊆ service)    │                │
            │           └──────────┬───────────────┘                │
            ▼                      ▼                                │
   ┌─────────────────────────────────────┐                          │
   │  register_stock_tools                │                          │
   │  / register_tool                     │                          │
   │  →  CatalogEntry created             │                          │
   │  →  classification stored on entry   │  ◄── NEW: today the      │
   │  →  per_op_classifications stored    │      catalog throws this │
   │  →  audit_category stored on entry   │      away                │
   │  →  authority + domain_hints stored  │                          │
   └────────────────────┬────────────────┘                          │
                        │                                            │
                        ▼                                            │
   ┌─────────────────────────────────────┐                          │
   │  (sub-spec) Owner-approve at         │  ◄── NEW for hard_write  │
   │  registration time for:              │      AND for             │
   │   - external_agent_read (network)    │      external_agent_read │
   │   - hard_write (since live path can't│      since the live      │
   │     enforce dispatch-time confirm)   │      dispatch path lacks │
   └────────────────────┬────────────────┘      a full evaluate gate │
                        │                                            │
                        ▼                                            │
   ┌─────────────────────────────────────────────────────────────────┘
   │  Tool surfaced to the agent's tool-block                         │
   │  (existing — tool_catalog already feeds this)                    │
   └────────────────────┬────────────────────────────────────────────┘
                        │
                        ▼
              ┌─────────────────────┐
              │  Agent calls tool   │
              └──────────┬──────────┘
                         │
                         ▼
   ┌────────────────────────────────────────────────────────────────┐
   │  LiveIntegrationDispatcher / LiveExecutor                       │
   │    ↓                                                             │
   │  DispatchGate.classify_tool_effect                              │
   │  Resolution order (NEW, single source of truth):                │
   │    1. Hardcoded kernel reads/writes                              │
   │    2. Action-dependent kernel branches (manage_*, restart_self) │
   │    3. Catalog entry classification (NEW — stock + workspace)    │
   │       via gate_effect_for(descriptor classification)             │
   │    4. CapabilityRegistry tool_effects (MCP)                     │
   │    5. "unknown" → refuse                                        │
   └────────────────────┬────────────────────────────────────────────┘
                        │ classified
                        ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  Dispatch-time policy gate (NEW — full evaluate, not just       │
   │  classification): covenant check, hard_write owner-confirm,     │
   │  loss-cost model. Same DispatchGate.evaluate that the           │
   │  conversational path uses; the live-integration path must       │
   │  reach it.                                                       │
   └────────────────────┬────────────────────────────────────────────┘
                        │ passes
                        ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  Source-aware routing (NEW):                                     │
   │    - source=kernel → reasoning.execute_tool                      │
   │    - source=stock|workspace → execute_workspace_tool             │
   │      → service-bound or subprocess path                          │
   │    - source=mcp → capability client                              │
   │    member_id threaded through so service-bound auth scope works  │
   └────────────────────┬────────────────────────────────────────────┘
                        │
                        ▼
              ┌─────────────────────┐
              │  Exactly ONE        │  ◄── single canonical
              │  ToolInvocationAudit│      audit-log entry per
              │  Entry              │      dispatch. Event-stream
              │  + event-stream     │      tool.called / tool.result
              │  tool.called /      │      still emit once for UI,
              │  tool.result        │      trace, cost consumers.
              └─────────────────────┘
```

## Design decisions

### D1 — The unblocker: catalog classification + source-aware dispatch + full policy gate, all together

(Codex finding 1, 2, 4, 7 folded.)

The smallest change that actually unblocks Notion (and every future
agent-authored tool) is NOT just "gate consults the catalog." It must
bundle three things, because they're interdependent:

1. **Catalog entry stores resolved classification + per-op
   classifications + audit metadata at registration time.** No
   re-parsing on the hot path. `gate_effect_for(...)` is the
   normalization step (so `delete` correctly resolves to `hard_write`).
2. **Gate consults the catalog entry** before falling through to the
   capability registry. Resolution order (single source of truth):
   1. Hardcoded kernel reads/writes (substrate primitives, not overridable)
   2. Action-dependent kernel branches (`manage_*`, `restart_self`)
   3. CatalogEntry classification (via `gate_effect_for`) — stock + workspace
   4. CapabilityRegistry `tool_effects` — MCP only
   5. `unknown` → refuse
3. **Live-integration dispatch routes by source**, so a stock/workspace
   call doesn't dead-end at MCP. After classification passes, the
   dispatcher checks the catalog entry's source and routes to:
   - `kernel` → existing `reasoning.execute_tool` kernel branch
   - `stock` / `workspace` → `WorkspaceManager.execute_workspace_tool`
     (member_id threaded for service-bound auth scope)
   - `mcp` → existing `_mcp.call_tool`
4. **`DispatchGate.evaluate` runs on the live path**, not just
   `classify_tool_effect`. This closes the hard_write-without-confirm
   gap (Codex r1#2). The conversational path already uses `evaluate`;
   the live path needs the same call.

   Codex r2#1 made this concrete: "full evaluate" can technically
   run while still not reproducing the conversational gate behavior
   unless the live path threads the gate-context inputs that
   `evaluate` requires. The unblocker sub-spec must define how the
   live path supplies all of:
   - `user_message` — the originating user turn that triggered the
     tool call (lookup via the live dispatcher's enclosing request
     context)
   - `messages` — recent conversation history for the gate's
     reactive/proactive classifier
   - `active_space_id` — the space the call originates from
   - `member_id` — the calling member (already required for
     service-bound auth scope; same threading covers both needs)
   - `is_reactive` — whether this dispatch follows a user message
     in the immediate-prior turn (the gate's loss-cost model
     differentiates reactive from proactive moves)
   - `approval_token` — when the agent is retrying after a prior
     confirm-required response, the token returned by that response

   When `evaluate` returns "confirm required," the live path must
   surface a user-visible pending action (the same mechanism the
   conversational path uses — emit a confirm-required response,
   pause the dispatch, await operator approval, then resume with
   the approval token). A bare refusal here would be a regression.

5. **Operation resolution is the single contract** for classification,
   routing, runtime enforcement, and audit. (Codex r2#2.) For
   per-op classified tools the resolution flow is:
   - Live dispatcher resolves the operation from `(tool_id, args,
     inputs)` using the descriptor's `operations[]` and any explicit
     `operation` kwarg
   - The same resolved operation is passed to: classify → evaluate
     → execute → enforce_invocation → audit
   - Ambiguous multi-op calls (operation can't be uniquely resolved)
     **fail closed before execution** with a clear error, NOT
     classified as the safest op
   - The existing `resolve_operation` at
     `kernos/kernel/tools/operation_resolver.py:76` (already used by
     `kernos/kernel/enactment/dispatcher.py:333`) is the single
     source of truth; the live-integration dispatcher must adopt
     it rather than rolling its own resolution

What changes: today, classification lives in three places (descriptor
on disk, `tool_effects` in known.py, hardcoded gate sets) with no
crosswalk, AND the live dispatch path bypasses the full policy gate.
After this spec, classification reaches the gate through the catalog
entry for descriptor-bearing tools, the full policy gate runs on
every call regardless of dispatch path, and source-aware routing
lands the call where it belongs.

**Implementation sub-spec:** `LIVE-DISPATCH-UNBLOCKER-V1`. The
biggest of the sub-specs, but the only one that fully restores the
ability to dispatch descriptor-bearing tools safely.

### D2 — Stock vs. workspace vs. capability tools: same primitive, three sources

(Codex finding 5 deepened.)

There is ONE tool concept. Three sources differ only in WHERE the
descriptor + implementation live:

- **Stock**: `kernos/kernel/integrations/<name>/` (ships with code)
- **Workspace**: `data/<instance>/spaces/<space>/<tool_name>/` (agent-authored)
- **MCP**: discovered from a connected MCP server (no descriptor on disk;
  capability registry holds the classification)

All three land in the same catalog and go through the same dispatch
path. The CatalogEntry's `source` field distinguishes them for
introspection, authorization, AND dispatch routing.

What changes: today, "stock" is set as `source="workspace"` with a
`stock_dir` side-marker, which conflates the concepts. After this
spec, `source` is one of `kernel | stock | workspace | mcp`, and the
dispatch path checks `source` to decide invocation route.

Call sites that today assume `source == "workspace"` for any
descriptor-bearing tool and would break with the split:

- `execute_workspace_tool` (`workspace.py:584`) — check accepts `workspace` only; must accept `stock` too.
- Schema loading (`handler.py:6657`) — same assumption.
- Catalog promotion (`handler.py:6702`) — same.
- `has_workspace_tool` (`tool_catalog.py:152`) — same.

**Implementation sub-spec:** `CATALOG-SOURCE-NORMALIZATION-V1`. Lands
alongside (or as part of) `LIVE-DISPATCH-UNBLOCKER-V1` since #1's
routing logic depends on `source` having clean semantics. Codex
finding 7: not a cosmetic later step; must ship with the unblocker.

### D3 — Authorization gate for agent-authored tools

(Codex finding 2 substantially reshapes this.)

The original spec assumed the dispatch-time gate would catch
unsupervised hard_write calls. **It doesn't on the live path today**
(`live_wiring.py:31` comment confirms full policy gate is
acknowledged-future). D1's sub-spec brings `DispatchGate.evaluate`
into the live path, which retroactively makes the dispatch-time
model real. But until D1 ships, the registration-time gate has to
cover hard_write too.

Registration-time approval table:

| Classification         | Scope               | Approval at register-time            |
| ---------------------- | ------------------- | ------------------------------------ |
| `read`                 | own space           | auto-approve                         |
| `read`                 | cross-space         | auto-approve                         |
| `soft_write`           | own space           | auto-approve                         |
| `soft_write`           | cross-space         | owner confirm-once                   |
| `hard_write`           | any                 | owner confirm-once at register-time (defense in depth — dispatch-time evaluate from D1 also runs per-call) |
| `external_agent_read`  | any                 | owner confirm-once (network egress)  |

Why register-time AND dispatch-time both gate hard_write: defense
in depth. Register-time confirms the operator agreed to expose a
hard_write tool at all; dispatch-time evaluates whether *this
particular call* in *this particular context* should fire. If D1
slips, register-time is the only gate; if D3 slips, dispatch-time
catches before harm.

**Implementation sub-spec:** `TOOL-REGISTRATION-AUTHORIZATION-V1`.
Adds the approval check inside `register_tool` for hard_write and
external_agent_read. Most paths still auto-approve. Ships ahead of
D1 if scheduling allows — register-time defense doesn't depend on
D1's dispatch-time work.

**Pending registrations are NOT active catalog entries.** (Codex r2#4.)
The shape:

- Agent calls `register_tool(descriptor_file=..., ...)` for a
  hard_write or external_agent_read tool.
- Substrate computes the descriptor hash, generates a `request_id`,
  writes a row to a durable **pending-registration store/outbox**
  keyed by `(request_id, descriptor_hash)`. No `CatalogEntry` is
  created.
- Agent receives the `request_id` synchronously. The tool is **not
  surfaced** in the agent's tool block (because it's not in the
  catalog yet).
- Operator approves or rejects via an existing notification surface
  (Discord DM, slash command, whatever the operator-confirm flow
  uses today).
- On approval: substrate creates the `CatalogEntry` from the
  pending row, deletes the pending row, surfaces the tool on the
  next assemble pass.
- On rejection: pending row marked rejected with reason; agent's
  next `register_tool` retry returns the rejection reason.
- On agent invoking a tool that's still pending (race, polling,
  whatever): **hard-fail at call time** with "tool pending approval,
  request_id=X." This is the only legitimate hard-fail; rejected
  or expired pending entries surface the same way.
- Pending rows survive restart (durable store, not in-memory).

### D4 — Observability surface

Operator (and the agent itself) need a single command to answer
"what tools exist right now, classified how, where do they live?"

Two new surfaces:

- **`/tools list`** slash command (operator) — outputs catalog dump:
  name, source, classification, audit_category, registered_at,
  approval status. Optional filter: `/tools list source=workspace`,
  `/tools list classification=hard_write`, etc.
- **`inspect_tools` kernel tool** (agent) — same data as JSON, callable
  inside a turn so the agent can self-audit before composing a
  multi-tool plan.

**Implementation sub-spec:** `TOOL-INTROSPECTION-V1`.

### D5 — Audit normalization

(Codex r1#6 substantially reshaped this; r2#3 corrects the audit-vs-event
distinction.)

**Audit-log entries and event-stream events are different things.**
The invariant:

- **Exactly one `ToolInvocationAuditEntry`** (audit log) per dispatch
  attempt — kernel, stock, workspace, or MCP. This is the rich,
  catalog-metadata-aware record used for compliance, forensics, and
  per-tool cost attribution.
- **Event-stream `tool.called` / `tool.result` events** continue to
  emit once per dispatch as they do today (`live_wiring.py:413`
  for `tool.called`, `live_wiring.py:459` for `tool.result`).
  UI surfaces, trace consumers, and cost aggregators depend on
  them. **Do NOT break these consumers.**

Today's gaps (the actual problem this spec fixes):

- Service-bound dispatch emits a rich `ToolInvocationAuditEntry`.
- `LiveIntegrationDispatcher` emits its own audit entries via
  `_emit_audit` (`live_wiring.py:442` for `tool_call_failed`,
  `live_wiring.py:467` for `tool_call_succeeded`).
- Calls through both paths would produce TWO audit entries
  describing the same dispatch.
- Calls through only the live path produce an audit entry missing
  the descriptor-derived `audit_category` and operation context
  (catalog metadata never reached the live-path audit constructor).

After this spec: one canonical audit-log entry per dispatch,
constructed at the dispatch entry-point (the live-integration
dispatcher), populated from catalog metadata (classification,
audit_category, service_id, resolved operation). Downstream paths
do NOT emit a second audit entry — they may emit narrower
diagnostic events (timing, errors) that reference the canonical
entry's id. Event-stream `tool.called` / `tool.result` are
untouched.

Prerequisite: catalog entries must carry the audit metadata, which
D1 already ships. D5 ships after D1.

**Implementation sub-spec:** `TOOL-AUDIT-NORMALIZATION-V1`.

### D6 — Notion as the validation test case

(Codex finding 8 expands the checklist.)

The end-to-end arc is considered shipped when the following all pass:

1. After `git pull && /restart`, Notion tools are catalog-loaded
   AND gate-classified via the catalog (assertion: dispatch a no-op
   `notion_read_page` against a known page through the live path,
   expect classification=`read`, exactly one audit entry, successful
   response).
2. The agent can author + register a brand-new tool in a workspace,
   call it via dispatch, and see exactly one canonical audit entry
   — without any code change between authoring and invocation.
3. `/tools list` shows kernel + Notion (stock) + agent-authored
   (workspace) entries with their resolved classifications,
   audit_categories, and approval status.
4. **Hard-write confirmation works on the live path.** An agent-
   authored `hard_write` tool, invoked through the live dispatch,
   triggers the owner-confirm prompt; the call does NOT execute
   until confirmed.
5. **Exactly one audit entry per call.** Assertion: a single
   notion_read_page call produces one canonical `ToolInvocationAuditEntry`
   and zero duplicate `tool.called` events.
6. **Restart fidelity.** After `/restart`, the catalog's extended
   metadata (classification, audit_category, etc.) is identical to
   pre-restart. No classification regressions on rehydration.
7. **Disabled-service refusal still works.** A stock tool whose
   service is disconnected at dispatch time returns a refusal with
   one audit entry, not a silent failure.
8. **Kernel/stock/workspace name collisions** are refused at
   registration time with a clear error (current behavior at
   `workspace.py:456` for non-workspace conflicts; expand to cover
   all combinations).
9. **Per-op classification respected.** A tool with multiple
   `operations[]` and different classifications per op dispatches
   correctly for each. Ambiguous multi-op calls fail closed.
10. **Both live seams exercised.** Validation covers both
    `LiveExecutor` and `LiveIntegrationDispatcher` dispatch paths,
    not just one — they enter `evaluate` and emit audit via
    different call chains today.
11. **Pending registration lifecycle.** A hard_write tool: agent
    calls register_tool → receives request_id, tool NOT surfaced in
    tool block, agent call returns "pending approval"; operator
    approves → CatalogEntry created, tool surfaces on next assemble;
    pending row survives `/restart` before approval and resumes
    correctly after.
12. **Rejection path.** Operator rejects a pending registration → row
    marked rejected, agent's retry returns the rejection reason, no
    CatalogEntry exists.
13. **Schema surfaced.** Stock + workspace tools' input_schemas reach
    the agent's tool block correctly (handler.py:6657 path) — the
    source-normalization refactor in D2 doesn't break surfacing.
14. **Member-id credential scoping.** A stock tool that requires
    per-member credentials (e.g., Notion) refuses with a clear error
    when invoked by a member who hasn't connected their integration
    — the live route threads member_id through to enforce_invocation
    correctly.
15. **Hard-write confirm continuation.** An agent-authored
    hard_write tool: first call returns "confirm required" with a
    token + audit entry; operator confirms; agent retries with the
    token; call proceeds; ONE additional audit entry (the
    confirmed-execution one, with a reference to the prior
    pending-confirm entry).
16. **Blocked-hard-write audit.** A hard_write call denied by gate
    evaluate (no confirm) still emits exactly one audit entry
    recording the refusal — silent denials are not allowed.

This is the live-bot validation gate for `TOOL-MAKING-ARC-V1` as a
whole. Until all 16 pass, the arc is not done.

The "exactly one canonical audit entry per dispatch" invariant from
D5 is asserted globally in checkpoint #5 — it applies to every other
checkpoint here too (not re-stated per-checkpoint).

## Sub-spec sequence

Lock the design first. Then ship in this order — each spec is
independently shippable, with its own Codex review loop:

1. **`TOOL-REGISTRATION-AUTHORIZATION-V1`** (D3). Register-time
   approval gate for hard_write and external_agent_read. Smallest;
   ships first because it's the only safety net until D2 lands. Does
   not block on D2.
2. **`LIVE-DISPATCH-UNBLOCKER-V1`** (D1 + D2 bundled). Catalog
   metadata storage, gate consults catalog, source-aware live
   dispatch routing, full `DispatchGate.evaluate` on live path,
   source field normalization, member_id threading. **This is the
   actual unblocker for Notion and every agent-authored tool.**
   Bigger than originally scoped — Codex finding 1 made the
   bundling necessary.
3. **`TOOL-AUDIT-NORMALIZATION-V1`** (D5). One canonical
   `ToolInvocationAuditEntry` per dispatch; remove duplicate
   audit-log entries (preserve event-stream `tool.called` /
   `tool.result` events for UI/trace/cost consumers); populate
   audit entry from catalog metadata. Lands after #2 (depends on
   metadata storage and dispatch-entry-point integration).
4. **`TOOL-INTROSPECTION-V1`** (D4). `/tools list` + `inspect_tools`.
   Validates that the prior specs actually composed.
5. **End-to-end integration test** — the 16-checkpoint D6
   validation becomes a real pytest harness, not a manual checklist.
   Lands alongside or just after #4 so all 16 are verifiable.

Each sub-spec lands with: (a) Codex spec review, (b) implementation,
(c) Codex code review, (d) commit + push. Standard convergence pattern.

## What this spec explicitly does NOT define

- The agent's prompting strategy for tool authorship. The arc enables
  the agent to author tools; teaching it to USE the capability well
  is a separate concern (template.py + few-shot examples).
- A version/upgrade story for existing tools. v1 is registration +
  dispatch + audit + introspection. Tool versioning is a follow-up
  if/when needed.
- A revocation primitive. Today tombstoning a catalog entry is
  enough; if the operator needs to revoke + clean disk + invalidate
  outstanding audit entries, that's a future spec.
- A cross-instance tool-sharing story. Each instance owns its own
  catalog; sharing is out of scope.
- Anything about MCP tools beyond preserving today's behavior.
- Performance work. Per-call descriptor re-parsing is dead after
  D1 (catalog stores resolved metadata); other optimizations are
  out of scope.

## Risk

(Codex finding 8 expands this section.)

- **Scope creep.** Five sub-specs is the spec's biggest risk — if
  even half slip, we still have a half-built arc. Mitigation: ship
  #1 (register-time auth) and #2 (live unblocker) first; #3-5 are
  improvements, not prerequisites.
- **#2 is bigger than a typical sub-spec.** Bundling catalog metadata
  + gate routing + dispatch routing + full evaluate-on-live + source
  normalization is necessary (Codex finding 1) but expands blast
  radius. Mitigation: heavy Codex review on the #2 spec, two-round
  code review minimum, smoke-test gate before push.
- **Catalog metadata storage may break catalog rehydration.**
  `ensure_registered` (`workspace.py:1025`) doesn't parse extended
  metadata today; #2 must extend it OR the metadata vanishes on the
  first lazy re-registration. Pin in #2's acceptance criteria.
- **Source normalization breaking call sites.** `execute_workspace_tool`,
  `has_workspace_tool`, schema loading, catalog promotion all assume
  `source == "workspace"` (Codex finding 5). Pre-audit before #2
  ships; fix all call sites in the same batch.
- **Duplicate audit entries during the transition.** Between #2 and
  #3, the live dispatch may emit both generic and rich audit entries for
  the same call. Mitigation: D5 ships immediately after D1, and the
  test suite asserts the exactly-one invariant from the start.
- **Classification vocabulary mismatch.** Descriptor uses
  `read | soft_write | hard_write | delete | external_agent_read`;
  gate uses `read | soft_write | hard_write | unknown` (no `delete`,
  no `external_agent_read`). `gate_effect_for` is the normalization;
  every consumer must route through it (Codex finding 4). Pin in #2.
- **Register-time confirm UX.** Owner-confirm at register-time means
  the agent's `register_tool` call may BLOCK on operator response —
  potentially for hours if the operator is asleep. Spec the
  async/queued behavior in #1: pending registrations live in a
  catalog "pending" state until confirmed/rejected; agent gets a
  request_id back immediately.

## Out of scope for this design

Stays put: STS substrate-tool spec (separate primitive), approval
flow for STS (separate, fancier — uses event-id), MCP discovery
mechanics (out of band), workshop sandbox isolation (future hard-mode
spec).

## Acceptance for this design spec

This spec is "GREEN" when Codex agrees:

1. The current-state audit matches what's actually in the codebase
   (Codex can verify by re-reading the cited files).
2. The end-to-end contract (D1-D5) actually closes the seams the
   Notion failure exposed AND the additional seams Codex round 1
   surfaced (live dispatch routing, hard_write live-path gap,
   duplicate audit, restart rehydration).
3. The sub-spec sequence is correct — specifically, that #1
   (register-time auth) ships first as a safety net, #2 is the
   true unblocker, and #3-5 are improvements that don't gate the
   Notion case.
4. No sub-spec is missing.
5. The 16-checkpoint D6 validation is sufficient — i.e., if those sixteen
   checkpoints pass, the arc is actually working.

Once GREEN, the design freezes. Implementation sub-specs are written
against this design as their north star. Bug fixes route through the
design, not around it.
