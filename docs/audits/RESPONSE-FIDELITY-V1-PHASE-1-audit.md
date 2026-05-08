# RESPONSE-FIDELITY-V1 — Phase 1 Audit Report

**Status:** IN PROGRESS — Phase 1 deliverable, audit-only
**Author:** CC (Claude Opus 4.7), 2026-05-08
**Spec:** `35affafef4db8147a79adae3892df3e9` (RESPONSE-FIDELITY-V1)
**Brief:** `35affafef4db815b8ae1d17fc5e545fb` (Phase 1 batch)
**Scope:** investigate where the response layer claims state without substrate-grounded receipts; produce structured findings to drive Phase 2 schema design.

---

## Doctrine

> The response/presence layer is a precise reporter of substrate reality, not a compensating layer that smooths over uncertainty.

## Four invariants under audit

1. **Action claims are receipt-backed** — completion claims must be grounded in observable receipts.
2. **Authorization and execution state are explicit** — requested / proposed / authorized / attempted / blocked / partial / completed / unverified each get distinct language.
3. **Presence renders substrate reality without laundering uncertainty** — translation is fine; smoothing is not.
4. **Partial state is preserved** — multi-step work surfaces structured per-step status, not all-or-nothing.

## Constraints honored during the audit

- Presence is renderer/interpreter, not investigator.
- Evidence classes broader than completion-receipts (read/query, drafts, inferred synthesis, missing/unavailable).
- Fail-closed scoped to claimed-completed-mutations-without-receipt only.
- Directive envelope is enforcement; wording is downstream.

---

## Methodology

For each action surface, I traced four mechanical layers:

1. **Dispatch path** — where the tool actually executes (kernel tool, MCP tool, slash command).
2. **Tool result shape** — what the dispatcher returns to the chain (success/failure, receipt content, partial-state info).
3. **Conv-log write** — what gets logged for the assistant message (the durable record the next turn sees).
4. **Directive shape** — what the integration runner emits to the renderer (instructive prose, structured envelope, or both).

For each surface I asked the seven audit questions in order. Findings are tagged:
- 🔴 **AUDIENCE-VISIBLE** — current behavior produces user-facing language that violates an invariant in observable cases.
- 🟡 **SILENT** — substrate state is misrepresented or aggregated, but typical rendering masks the gap.
- ⚪ **THEORETICAL** — pattern would violate the invariant under specific conditions; not currently observed in field traces.
- ✅ **HONORED** — surface already complies with the invariant.

---

## Section A — Architectural background (current substrate shape)

This section sets baseline before per-surface findings. The audit's heat map only makes sense if you know what receipts EXIST in the substrate today.

### A.1 Where receipts live

There is one canonical "tool receipt" structure in the substrate: the **tool trace entry**, built at every dispatch boundary in `kernos/kernel/enactment/dispatcher.py:_build_trace_entry` and `kernos/kernel/integration/live_wiring.py:LiveIntegrationDispatcher`:

```python
{
  "name": tool_id,                    # the tool that ran
  "input": dict(arguments),           # the args it ran with
  "success": not is_error,            # boolean; the failure flag
  "result_preview": str(output)[:200] # truncated string preview
}
```

Lifecycle:
1. `LiveExecutor` (full-machinery EXECUTE_TOOL) and `LiveIntegrationDispatcher` (thin path) both emit trace entries on each dispatch.
2. ReasoningService accumulates the per-turn trace in `_turn_tool_trace` (`kernos/kernel/reasoning.py:286`).
3. After reasoning completes, `handler.py:4463` drains the trace via `reasoning.drain_tool_trace()` into `ctx.tool_calls_trace`.
4. `phases/persist.py:61-78` formats successful entries into `"Tool effects this turn:\n[<name>] <preview>"` text, then appends as a `system/receipt` conv-log entry.
5. The next turn's agent sees this conv-log entry as part of recent conversation context — that is the receipt the agent reads to ground its claims.

**Receipt fidelity gap #1 (visible at substrate level):** the receipt is **success/failure binary + 200-char output preview**. Authorization-state, partial-state, attempted-but-blocked, and pending/in-flight states are NOT first-class receipt types. They get encoded ad-hoc in `result_preview` text if at all.

### A.2 Where the directive lives

The integration runner's `_finalize` (`kernos/kernel/integration/runner.py:851`) constructs a `Briefing` whose `presence_directive: str` field is **free-form prose** from the integration model:

```python
directive = str(tool_input.get("presence_directive") or "").strip()
if not directive:
    raise BriefingValidationError("model emitted briefing with empty presence_directive")
```

The integration model's `__finalize_briefing__` tool call passes `presence_directive` as one of several string fields. There is **no structured action-state envelope** — the directive is whatever prose the integration model decided to write.

The decided_action has 7 KINDS (RESPOND_ONLY, EXECUTE_TOOL, PROPOSE_TOOL, CONSTRAINED_RESPONSE, PIVOT, DEFER, CLARIFICATION_NEEDED), and EXECUTE_TOOL carries an `ActionEnvelope` (`intended_outcome`, `allowed_tool_classes`, `allowed_operations`). But these describe **this turn's planned next action**, not **what has already happened** in prior iterations of the integration loop. The substrate doesn't carry an "action state" structure summarizing what completed/partial/blocked.

**Receipt fidelity gap #2 (architectural):** there is no structured envelope between the substrate's binary tool trace and the renderer's free-form prose. The integration model's prose is the only enforcement of receipt-grounded language, and the renderer model's prose is the only enforcement that the directive's intent gets faithfully rendered. Two LLM-authored layers stacked, both without machine-checkable schema.

### A.3 What the renderer receives

`PresenceRenderer.render` (`kernos/kernel/enactment/presence_renderer.py:806`) builds the model's input as:

- **System prompt:**
  - Cognitive substrate slice (RULES + NOW + STATE) — render via `_render_substrate(packet)`.
  - Kind-aware prompt — e.g., "You are Kernos's presence renderer. The decided action is a conversational reply…"
  - `## Directive\n{briefing.presence_directive}` — free-form prose.
- **User message:**
  - (Recently added by RECEIPT-FORWARD-V1) `### Prior tool results (already fetched this turn)` — verbatim per-call results from integration synthesis, capped at 8K/entry and 24K total.
  - `_user_message_for_briefing(briefing)` body — kind-specific framing of the user's original ask.
- **Tools list** — `packet.tool_surface.all_tools()` if surfaced.

The renderer's chain produces text that becomes `ctx.response_text` and lands in conv-log via `phases/persist.py:56`.

**Receipt fidelity gap #3 (rendering layer):** the renderer's input contains both the directive (instructing what to claim) and the prior tool results (raw substrate content). Its prose output can drift from either. The substrate doesn't enforce that the rendered text's claims match the trace entries' success flags.

### A.4 What conv-log shows the next-turn agent

For a typical action turn, three conv-log entries land per turn:

1. **User entry** (`assemble.py:243`) — the user's message, logged with timestamp + platform + tags.
2. **Assistant entry** (`persist.py:56`) — the renderer's text response, exactly as rendered.
3. **System/receipt entry** (`persist.py:71`) — `"Tool effects this turn:\n[name] preview…"` for each successful tool call.

The next-turn agent reading recent conversation sees:
- What the user said.
- What the assistant said (which can claim ANY state — completion, in-flight, blocked, etc., regardless of what actually happened).
- What tools succeeded (with truncated previews).

There is currently **no entry** that says "what the assistant claimed AND whether the substrate state matches it." The cross-check is implicit: the agent has to read the assistant text + receipts and infer whether they're consistent. This is exactly the failure mode the agent self-identified ("natural-language completion gets ahead of durable state").

### A.5 Where INTEGRATION-FAIL-LOUD already operates

INTEGRATION-FAIL-LOUD-V1 (commits `1c9a4de`, `b46bf35`, `4acca74` from 2026-05-07) hardened the **failure** path:

- `IntegrationAttemptFailed` exception carries structured per-attempt diagnostic state (`component`, `reason`, `iterations`, `tools_called`, `iteration_metrics`, `tool_results`).
- Retry harness writes a friction report on exhaustion (`data/diagnostics/friction/`).
- `system_error_briefing` produces a load-bearing user-facing directive: "INTEGRATION SYNTHESIS FAILED. … Surface this transparently to the user. Do NOT attempt to answer their original question; do NOT apologize for 'limited context'."
- `iteration_cap_briefing` (RECEIPT-FORWARD-V1, 2026-05-07) extends this to the iteration-cap case with three-option choice + receipt block.

This discipline operates at the **failure** boundary of the integration runner. RESPONSE-FIDELITY-V1 generalizes the same shape to **non-failure** boundaries — successful completion, in-flight, partial, proposed, blocked-by-policy.

### A.6 The audit's structural lens

Given A.1–A.5, every per-surface finding will land in one of these categories:

- **Trace-level fidelity** — does the substrate's tool trace capture enough state for honest receipt-grounding (e.g., does an external API call's tool result preserve "scheduled" vs. "queued for sending" vs. "delivered" distinctions)?
- **Directive-level fidelity** — does the integration model's directive prose accurately reflect the trace? Or does it upgrade "attempted" to "completed" because the underlying tool returned a 200 OK?
- **Render-level fidelity** — does the renderer's prose preserve the directive's distinctions? Or does it smooth over "in-flight" into "done" for warmth?

The fix shapes Phase 2 will land in:
- **Substrate schema additions** — first-class receipt classes (read/query, attempted, completed, partial, blocked, in-flight) with structured fields.
- **Directive envelope** — replacing free-form prose with a structured action-state object that the renderer maps to language.
- **Renderer rules** — kind-specific templates per state class, with linguistic discipline grounded in the envelope rather than wording instructions.

---

## Section B — Per-surface findings

Severity legend repeated for ease: 🔴 audience-visible / 🟡 silent / ⚪ theoretical / ✅ honored. Findings keyed to substrate file:line where possible.

### B.1 Mutating surfaces

#### B.1.1 Calendar (create/update/delete)

Calendar tools are MCP-surfaced via the Google Calendar integration; not a kernel tool. Tool result depends on the MCP handler's return shape. Without trace-driving inspection it's plausibly:

- 🟡 **Receipt-backing** — Google Calendar API returns the created event ID + start/end on success. That's a real receipt. Risk is the directive collapses "API responded with event id" to "I added it to your calendar"; both true today.
- 🟡 **State-rendering** — list/read paths and create/update paths both return event objects; the language layer needs to distinguish "I see X on your calendar" from "I added X to your calendar." Likely the integration model handles this well in practice but no substrate enforcement.
- ⚪ **Uncertainty** — no in-flight state in calendar API; sync. No observed gap.
- 🔴 **Partial-state** — multi-event ops (e.g., "schedule three meetings with conflicts on one") return PER-EVENT results from MCP; if the agent issues three separate tool calls and one fails, the integration model's prose has to aggregate. No substrate primitive helps here. The spec's partial-state test category targets exactly this.
- 🟡 **Directive-metadata** — directive is free-form prose; nothing structured tells the renderer "completed" vs "blocked due to conflict."
- ⚪ **Cross-surface consistency** — calendar specifically fine; the broader gap is MCP vs. kernel tool inconsistency in receipt shape.

#### B.1.2 File write / read / list / delete

`kernos/kernel/files.py:162-319`. Receipts are formatted strings:
- write_file: `"{action} '{name}' ({len(content)} chars). Description: {description}"` (line 222)
- read_file: returns the file text or `"Error: File '{name}' not found."`
- list_files: `"Files in this space ({total}):\n..."`
- delete_file: `"Deleted '{name}'. File preserved for recovery."` (line 319)

Findings:
- ✅ **Receipt-backing** — direct filesystem write. The receipt fires AFTER the write returns; high fidelity.
- 🟡 **State-rendering** — `read_file` returns raw content with no source-prefix. The integration model rendering "the file says X" doesn't structurally distinguish "verbatim file content" from "agent-paraphrased file content"; relies on directive prose.
- ⚪ **Uncertainty** — synchronous; no in-flight state.
- 🟡 **Partial-state** — no batch file ops in current toolset. If introduced, no primitive to carry per-file status.
- 🟡 **Directive-metadata** — same free-form prose limitation as everywhere else.
- 🟡 **Evidence-class** (read_file) — no marker that distinguishes "this is what the file said" from "agent inference about the file." Receipt preview includes the literal text; renderer can re-render any way.

Status: relatively clean surface. Heat is moderate — no clear violations, but the lack of structured evidence-class metadata limits how strictly invariants can be enforced.

#### B.1.3 Channel send (Discord, SMS)

`kernos/kernel/reasoning.py:1047-1081`. Receipt:

```
return f"Message sent to {ch_info.display_name}."
```

This fires immediately after `send_outbound` returns without exception.

🔴 **Receipt-backing** — **load-bearing violation candidate.** "Message sent to your phone" is claimed when the substrate truth is "Twilio API accepted the request." For SMS specifically, downstream failures (bad number, carrier opt-out, network) are not uncommon and produce no signal back to the agent. The user can be told "I sent that to your phone" when no SMS ever arrives. **Severity 🔴 audience-visible** for SMS; 🟡 silent for Discord (delivery rarely fails after API ack but still asserted as completed when only ack'd).

🟡 **State-rendering** — no distinction between "API ack'd" vs "delivered" vs "user read."

⚪ **Uncertainty** — fully smoothed over; the receipt language asserts certainty when substrate carries none.

⚪ **Partial-state** — N/A.

🔴 **Directive-metadata** — directive carries no notion of "the send is in-flight; delivery confirmation comes later or never." This is fundamental: the integration model can only see "tool call returned without error" → encodes as "send completed" → renderer says "Sent." Architecture provides no path to express anything else.

This is **the cleanest example** of the principle the spec wants to harden. It's the canonical case Phase 2 should address.

#### B.1.4 Canvas update (page write / archive / delete)

`kernos/kernel/canvas.py:1259+` (page_write) returns `CanvasOpResult` (`ok: bool, canvas_id, page_path, error, extra`). This is the **most structured receipt shape in the kernel** today.

- ✅ **Receipt-backing** — `CanvasOpResult` carries enough structure for principled receipt-grounding; integration model can read `ok` and `page_path` directly.
- 🟡 **State-rendering** — page write triggers cross-member consent prompts via the canvas spec's existing flow; the consent-pending intermediate state isn't currently distinguished by directive metadata. Rendering can say "I updated X" when user-confirmation gate is still pending.
- 🟡 **Partial-state** — `extra` dict can carry partial-state info; not currently used systematically.
- 🟡 **Directive-metadata** — `CanvasOpResult.to_dict()` is the structured payload, but it gets serialized to JSON and embedded in the textual tool result; integration model has to parse it back and the directive prose loses the structure.

Status: **best-shaped surface architecturally; under-utilized.** Phase 2 should look at Canvas as a model for what mutating-tool result shape should look like across all surfaces.

#### B.1.5 Member management (invite / remove / scope / permission)

`manage_members` routes to `_handle_manage_members` in handler. Returns formatted strings (e.g. "Invited X.", "Connected X to Y."). Mutations in `instance_db`.

- 🟡 **Receipt-backing** — mutations are direct DB writes; high fidelity at substrate.
- 🔴 **State-rendering** — invite is the highest-stakes case. The receipt "Invite code generated for X" doesn't carry the actual code consistently across paths, AND "invite issued" is sometimes rendered as "X has joined" by the renderer drifting through prose. Worth concrete repro.
- 🟡 **Partial-state** — bulk-invite N/A in current toolset.

#### B.1.6 Knowledge memory mutation

The `remember` tool itself is read-only (search). Knowledge mutations happen via `fact_harvest` during compaction (background) and via `request_space_action` for cross-space writes.

- 🟡 **Receipt-backing** — fact_harvest is async/background; the agent never sees a receipt for "this knowledge entry was added." This means the agent CAN'T claim "I remembered that" with substrate grounding — the entry might land minutes later via compaction.
- 🔴 **Directive-metadata** — when the user says "remember that I prefer X," the agent often replies "Got it, I'll remember." There is no tool call dispatched at that moment in the typical conversational shape — knowledge harvesting happens during compaction. So **the claim "I'll remember" has no synchronous receipt at all**. This is a textbook violation.
- 🟡 **Cross-surface consistency** — same surface treats "agent says I'll remember" and "compaction extracts a fact" as if they were the same event; they're not — one is intent, the other is substrate.

#### B.1.7 Plan execution

`manage_plan` routes step transitions. Plan state lives in instance_db. Receipt: "Plan {id} step {n} completed."

- ✅ **Partial-state** — plan substrate naturally carries per-step state. This is one of the few surfaces where partial-state is a first-class substrate concern.
- 🟡 **Receipt-backing** — step transitions are atomic DB writes; high fidelity. Risk is in mid-step rendering (when a step is in-flight, the directive says "I'm working on X" without a specific in-flight receipt class).
- 🟡 **Directive-metadata** — plans have richer structured state than other surfaces but the renderer still consumes the directive as free-form prose.

#### B.1.8 Tool registration

`register_tool` (`kernos/kernel/workspace.py:556`) returns `"Registered tool '{name}'. It's now available across all spaces via the universal catalog."`

- ✅ **Receipt-backing** — direct catalog write, high fidelity.
- ⚪ **State-rendering** — fine; one-shot atomic op.

Status: low heat. Solid surface.

#### B.1.9 Scheduled actions and watchers

`manage_schedule` (`kernos/kernel/scheduler.py:680-707`) returns:

```
Scheduled: {description}
Next fire: {next_fire[:19]}
Type: {action_type} | ID: {tid}
```

- ✅ **Receipt-backing** — direct trigger-store write. High fidelity at the "trigger created" boundary.
- 🔴 **State-rendering** — the receipt says "Scheduled" but the spec explicitly calls out distinguishing **schedule-created**, **trigger-fired**, **action-executed**, **notification-delivered** as separate state transitions. Today they're all collapsed: trigger creation is the only logged event; firings produce out-of-band messages but the original "I'll remind you 30 min before X" claim has no follow-up receipt linkage. The user gets a reminder OR doesn't, and the substrate doesn't tie the original promise to the realized reminder.
- 🔴 **Cross-surface consistency** — "I'll remind you" (trigger created) and "I reminded you" (trigger fired) and "I notified you on Discord" (notification delivered) all share one substrate path with one receipt class. Phase 2 schema work.

#### B.1.10 Capability and channel management

`manage_capabilities`, `manage_channels`. Returns formatted strings.

- 🟡 **Receipt-backing** — DB writes, high fidelity at mutation boundary.
- 🔴 **State-rendering** — `manage_capabilities` enable/install can mean "credential file copied" vs "MCP server actually launched and responsive." The receipt "Enabled X" doesn't distinguish. If the MCP server fails to start, the agent has already reported success.
- 🟡 **Uncertainty** — install ops could be in-flight (server starting up); not modeled.

#### B.1.11 Memory/covenant mutation

`manage_covenants` (`kernos/kernel/reasoning.py:1005-1031`) returns `cov_result` from `handle_manage_covenants`. Then **spawns an async `validate_covenant_set` task AFTER returning** (line 1022).

🔴 **Receipt-backing** — the receipt "Updated rule" fires before validation completes. If validation surfaces a conflict (e.g., the new rule conflicts with another covenant), the user has already been told the rule was updated. The async validation produces a SEPARATE notification turn later, but the original claim is in conv_log already.

This is a **timing-fidelity** violation: the substrate is in-flight after the receipt fires. The cleanest fix shape is: receipt waits on validation, OR receipt explicitly says "rule updated, validation pending." Today neither — it's just "Updated."

🔴 **Directive-metadata** — no notion of "this covenant is provisionally updated, validation may revise."

#### B.1.12 Workspace artifact lifecycle

`manage_workspace` (`kernos/kernel/workspace.py:300-328`) returns `"Added artifact '{artifact.name}' ({artifact.id}) to workspace."`

- 🟡 **Receipt-backing** — direct DB write; receipt accurate at the "added to manifest" boundary.
- 🔴 **State-rendering** — the spec calls out artifact lifecycle states (drafted/proposed/active/archived/superseded) as a Phase 2 candidate. Today there's no state field at all; everything is just "added." So claims like "I started the project" / "I finished the project" / "I archived the project" all collapse to "added/updated artifact." Major drift surface.
- 🔴 **Cross-surface consistency** — artifacts ARE the target of long-running work; collapsing their lifecycle into "added/updated" is exactly the partial-state-as-all-or-nothing pattern the spec calls out.

Per spec composes-with note (item 7), this is a candidate Phase 2 spec on its own. Audit confirms it's load-bearing.

#### B.1.13 Relational messaging

`send_relational_message` (`handler.py:5288-5338`). Receipt:

```
Sent (id={msg_id}, conversation={conv_id}, state={state}). Their agent will see it
{'now' if urgency == 'time_sensitive' else 'on their next turn'}.
```

**Best-shaped receipt in the kernel today.** It explicitly carries `state` (`delivered` = queued; `surfaced` = recipient agent saw it; `resolved` = recipient agent processed it). The lifecycle is structured.

- ✅ **Receipt-backing** — `state=delivered` accurately reflects "in queue, not yet read by recipient."
- 🟡 **State-rendering** — but the prefix "Sent" can mislead. Rendering "I sent X to Harold" with state=delivered overstates: it's queued, not read. Phase 2 fix could be "Queued for Harold's next turn (delivered, not yet seen)" — that's more honest.
- ✅ **Uncertainty** — explicit "on their next turn" hedge for non-time-sensitive.
- 🟡 **Cross-surface consistency** — only surface that has lifecycle in the receipt; everywhere else collapses. The relational-messaging shape is what other mutating surfaces should emulate.

### B.2 Read-only / evidentiary surfaces

#### B.2.1 Calendar list/read

MCP tool. Returns event lists. Evidentiary, not mutating.

- 🟡 **Evidence-class audit** — no marker distinguishing "calendar API said X" from "agent inferred Y from the calendar." The integration model's directive can say "you have a meeting at 3pm" and the renderer renders that — both grounded in tool result, but no structured "this came from calendar tool" prefix.

#### B.2.2 External web/search/retrieval

MCP tools. Receipts are search-result lists / page contents.

- 🟡 **Evidence-class audit** — same. The spec specifically calls out distinguishing "search result says X" / "page content says X" / "agent inferred Y." Today the directive prose handles this implicitly via integration model discipline; no schema enforcement.
- 🟡 **State-rendering** — read paths can render with "I checked X" language that implies more than was checked.

#### B.2.3 Knowledge memory inspection

`remember` (search). Returns retrieved knowledge entries.

- 🟡 **Evidence-class audit** — `remember` returns a formatted string with retrieved entries; the directive can render this as "I remember X" but the substrate-truth is "search found this entry." Important distinction the model handles by convention; no schema enforcement.

#### B.2.4 Canvas read

`page_read` returns markdown content.

- 🟡 **Evidence-class audit** — same evidence-class issue. "The canvas says X" vs "I summarize the canvas as X."

---

## Section C — Cross-surface patterns

Patterns that emerged repeatedly across surfaces:

### C.1 The "API ack ≠ effect realized" gap

The substrate's success flag is set when the underlying tool function returns without raising. For surfaces that wrap an external API (channel send via Twilio/Discord, calendar via Google, MCP tools), this means "API ack'd" gets receipt-encoded as "completed." For Twilio especially, downstream delivery failures are not visible to the substrate.

**Affected surfaces:** B.1.3 (channel send — most acute), B.1.1 (calendar via MCP), B.1.10 (capability install via MCP server launch), all MCP tools generally.

**Root:** receipt class is binary `success`. There is no structured field for "external system accepted the request but realized state may differ."

### C.2 The "claim before validation" gap

Some kernel paths fire the user-visible receipt BEFORE substrate-side validation completes. `manage_covenants update` is the cleanest example: receipt "Updated rule" fires, then async `validate_covenant_set` runs and may surface a conflict in a separate turn. The original claim is durable in conv_log.

**Affected surfaces:** B.1.11 (covenants), potentially B.1.10 (capability install if MCP startup is async), partially B.1.6 (knowledge mutation via async fact_harvest).

**Root:** validation/consequence-surfacing is async; receipt fires sync. No "provisional" or "pending validation" receipt class.

### C.3 The "synchronous claim of intent without dispatch" gap

The most subtle and arguably the most prevalent: the agent commits to a claim conversationally without dispatching a tool that would create a substrate receipt. "I'll remember that" is the canonical example — there's no tool call at that moment; knowledge harvesting happens asynchronously during compaction.

**Affected surfaces:** B.1.6 (most acute — `remember` is a search tool, not a write tool, so "I'll remember" has no synchronous write path), B.1.7 (when the agent says "I'll get back to you on that" without scheduling a follow-up), various conversational claims about future state.

**Root:** agent prose is the durable claim; no enforcement layer ties claims to required-tool-dispatch.

### C.4 The "lifecycle collapsed to add/update" gap

Mutating surfaces with natural lifecycles (workspace artifacts, scheduled triggers, capability state, even covenants) report all transitions as "added/updated/removed" without distinguishing between **lifecycle states** (drafted/proposed/active/archived/superseded). The receipt and the rendered language both flatten the lifecycle.

**Affected surfaces:** B.1.12 (most acute), B.1.7 (plan steps — partially mitigated by step-state field), B.1.9 (triggers — created vs fired collapsed), B.1.4 (canvas pages — frontmatter has state but receipt doesn't surface it).

**Root:** receipt structure is operation-shaped (`add/update/delete`), not state-shaped. Spec's item 7 (lifecycle states) is the substrate-side complement.

### C.5 The "no partial receipt" gap

When work plausibly should partially-succeed (multi-event calendar batches, multi-step plans with one failure, multi-recipient sends), the substrate has no primitive for "2 of 3 done; here's per-step status." Each tool call returns binary success; aggregation is the integration model's prose problem.

**Affected surfaces:** B.1.1 (calendar batches), B.1.5 (bulk member ops if any), all multi-step paths.

**Root:** tool result schema is per-call; multi-call aggregation lives only in directive prose.

### C.6 The "evidence class undifferentiated" gap (read-only surfaces)

Read-only/evidentiary surfaces all return content the integration model can render as either "the source said X" or "I infer Y from the source." No structural distinction between **direct quote of substrate**, **agent paraphrase of substrate**, **inference beyond substrate**, **missing/unavailable evidence**.

**Affected surfaces:** B.2.1 / B.2.2 / B.2.3 / B.2.4 — all read-only surfaces uniformly.

**Root:** evidentiary tool results are textual content, not structured `{class: direct_quote, content: "..."}` records. The model's prose is the only enforcement.

### C.7 The "failed dispatches not in conv_log" gap

`persist.py:64` filters tool trace entries by `success=True` before formatting receipts. Failed tool calls don't appear in the receipt block at all. The next-turn agent has no record that a tool was attempted-and-failed.

This means: if the agent on turn N says "let me try X" and the tool fails, turn N+1's agent sees the assistant's claim AND the receipts of any successful tools — but NOT the failure. The agent has to infer from the absence of an expected receipt that the attempt failed. This compounds the C.3 gap.

**Affected surfaces:** all mutating surfaces where failures happen.

**Root:** receipt log filters on success. Failed-attempt class isn't represented in the agent's next-turn context.

### C.8 The "directive is prose all the way down" pattern

The integration model authors `presence_directive` as a free-form string. The renderer consumes it as text. There is no machine-checkable schema between them. Every fix shape that depends on "the directive should carry X" can only be enforced by prompt discipline today; Phase 2's structured envelope is the substrate-side answer.

**Affected surfaces:** all surfaces.

**Root:** the integration → presence boundary is a prose handoff. This is the architectural fact the spec wants to address.

---

## Section D — Heat map

Surfaces ranked by total fidelity drift, weighted toward audience-visible (🔴) findings.

### D.1 Highest heat (audience-visible, structural)

1. **B.1.3 Channel send (especially SMS)** — "API ack'd" claimed as "delivered." The cleanest violation; canonical case for Phase 2.
2. **B.1.6 Knowledge memory mutation** — "I'll remember" has no synchronous write path; pure prose claim. Compounds with compaction's async harvesting.
3. **B.1.11 Memory/covenant mutation** — async validation fires after the receipt; user told "Updated" before validation surfaces conflicts.
4. **B.1.12 Workspace artifact lifecycle** — lifecycle entirely collapsed to add/update; "drafted/proposed/active" distinctions don't exist.

### D.2 Medium heat (silent or partial-state)

5. **B.1.9 Scheduled actions and watchers** — "Scheduled" doesn't tie to "trigger fired" / "notification delivered." Multi-stage lifecycle collapsed.
6. **B.1.5 Member management (invite path)** — "Invite issued" can drift to "X joined" via free-form rendering.
7. **B.1.13 Relational messaging** — best-shaped surface today (state field in receipt) but the "Sent" prose-prefix can mislead. Worth using as the model for fix shape elsewhere; not itself most-broken.
8. **B.1.10 Capability and channel management** — install/launch state ambiguous (credential copy vs server up).
9. **B.1.7 Plan execution** — partially mitigated by step substrate; in-flight rendering is the gap.
10. **B.1.1 Calendar (mutating)** — relatively well-receipted via MCP; partial-state on batch ops is the main gap.

### D.3 Lower heat (mostly evidence-class)

11. **B.1.2 File write** — clean substrate, weak directive metadata for partial state.
12. **B.1.4 Canvas update** — best-structured tool result, under-utilized at directive layer.
13. **B.1.8 Tool registration** — clean atomic op.

### D.4 Read-only / evidentiary

All four read-only surfaces (B.2.1–B.2.4) share the **C.6 evidence-class gap** uniformly. Phase 2 schema work for these is one shape (an `evidence_class` field on directive metadata), not four separate fixes.

### D.5 Cross-surface heat

The four "cross-surface" patterns (C.1–C.8) each touch multiple surfaces. Phase 2 fix shapes for **C.1 (API ack ≠ effect)**, **C.2 (claim before validation)**, **C.3 (claim of intent without dispatch)**, **C.4 (lifecycle collapsed)**, and **C.7 (failures not in conv_log)** each address multiple surface findings simultaneously. This argues for **structural/schema fixes over per-surface patches**.

---

## Section E — Proposed fix shapes (Phase 2 input)

Each fix shape names the category from the spec ("directive metadata change / presence rendering rule / linguistic discipline / fail-closed scope") and identifies which findings/patterns it addresses.

### E.1 Directive-metadata schema additions

The single highest-leverage Phase 2 work. Replaces (or augments) the free-form `presence_directive: str` with a structured envelope. Closes patterns C.1, C.2, C.3, C.4, C.5, C.6, C.7 — that is, most of the audit.

The Internal-Assessment-proposed shape from the spec page is the right starting point. Concretely:

```python
@dataclass
class ActionStateRecord:
    action_id: str
    surface: str             # calendar / file / channel / canvas / ...
    operation: str           # specific operation name
    operation_class: Literal[
        "read", "propose", "mutate", "delete", "send",
        "schedule", "register", "manage",
    ]
    authorization_state: Literal[
        "requested", "confirmed", "denied", "not_required",
    ]
    execution_state: Literal[
        "not_attempted", "attempted", "completed",
        "partial", "blocked", "failed", "unknown",
    ]
    receipt_refs: tuple[str, ...]    # tool trace entry refs
    affected_objects: tuple[str, ...]
    partial_state: dict | None       # per-step status if applicable
    user_visible_summary: str        # substrate-authoritative summary
    risk_level: Literal["low", "medium", "high"]
    missing_metadata: bool
    evidence_class: Literal[         # for read paths
        "direct_quote", "paraphrase", "inference",
        "missing", "unverified",
    ] | None
```

Briefing.audit_trace gains `action_state_records: tuple[ActionStateRecord, ...]`. The integration runner populates it from per-iteration tool dispatch + finalize-time analysis. The renderer consumes it.

This single shape closes:
- **C.1** via `execution_state` (`completed` reserved for verified-realized; `attempted` for API-ack'd-only).
- **C.2** via a new `pending_validation` execution state and a separate covenant turn that updates the record.
- **C.4** via `operation_class` + lifecycle-aware affected_object metadata.
- **C.5** via `partial_state` field as a structured per-step breakdown.
- **C.6** via `evidence_class`.
- **C.7** via failed-attempt records (still added to action_state_records even when `tool_calls_trace` filters out failures).

Spec name: **DIRECTIVE-ACTION-STATE-V1** seems right; this is the substrate primitive.

### E.2 Presence rendering rules

Once E.1 lands, the renderer's prompt becomes a function of action-state records. Specifically:

- For each record with `execution_state=completed` AND a non-empty `receipt_refs`: the renderer is permitted to use completion language ("I scheduled X").
- For `execution_state=attempted` with no completion verification: the renderer uses attempted language ("I sent that to your phone — Twilio confirmed receipt of the request, no delivery confirmation yet").
- For `execution_state=partial`: the renderer is REQUIRED to surface partial breakdown via a structured rendering rule (not a templated string, but a constrained shape).
- For `execution_state=blocked`: the renderer surfaces the blocker reason explicitly.
- For `evidence_class=direct_quote`: the renderer marks the source ("the file says…").
- For `evidence_class=paraphrase`: the renderer marks it ("I read the file as saying…").
- For `evidence_class=inference`: the renderer marks it ("based on what the file said, I think…").

These rules are enforced at the **prompt layer for the renderer** — not by hard substring filters. The discipline composes with the existing kind-aware prompts in `presence_renderer.py`.

This addresses spec invariants 2 and 3 directly.

### E.3 Linguistic discipline (downstream of E.1 + E.2)

The wording-as-enforcement layer the spec explicitly warns against being load-bearing — but still useful as supporting prompt discipline once envelope-as-enforcement is real.

- "I sent" / "I scheduled" / "I updated" reserved for `execution_state=completed`.
- "I tried to send" / "I queued" / "I requested" for `execution_state=attempted`.
- "I started" / "I'm working on" for in-flight (`execution_state=attempted` + multi-step).
- "I drafted" / "I proposed" for `execution_state=not_attempted` with proposal artifact.

Per the constraint: not load-bearing. Envelope is the enforcement.

### E.4 Fail-closed scope (narrow)

Per spec constraint 3, fail-closed only on:
- A response asserts completion language ("I scheduled X") AND there is no `ActionStateRecord` with `execution_state=completed` AND matching `receipt_refs`.

That is the entire fail-closed scope. Emit a friction report (mirroring INTEGRATION-FAIL-LOUD pattern), trigger a re-render, OR surface the fidelity violation to the user as a "the substrate says I attempted but didn't verify" hedge — design choice falls out of Phase 2 design.

Don't fail-closed on:
- Missing optional metadata (degrade gracefully).
- Read-only paths (only directive metadata is missing/unknown for evidence_class).
- Conversational-only responses (no action claims to verify).

### E.5 Receipt-class extension at the trace layer

Adjacent fix: extend `tool_trace` entries themselves to carry more structured state. Today `{name, input, success, result_preview}`. Phase 2:

```python
{
  "name": tool_id,
  "input": dict(arguments),
  "execution_state": "completed" | "attempted" | "partial" | ...,
  "result_preview": str(output)[:200],  # unchanged
  "structured_result": dict | None,     # parsed from tool result if structured
  "receipt_refs": tuple[str, ...],      # IDs of created/affected objects
  "affected_objects": tuple[str, ...],
}
```

Tools opt in to richer receipts (initially: send_to_channel, manage_covenants, manage_schedule, manage_workspace, send_relational_message). Other tools fall back to the current binary success+preview.

This is the **substrate-side** complement to E.1's directive envelope. Without it, the integration model has to infer execution_state from tool result text.

### E.6 Failed-attempt logging in conv_log

Address pattern C.7 directly. `phases/persist.py:64` should NOT filter on `success=True`. Failed entries get logged with a different prefix:

```
Tool effects this turn:
  [send_to_channel] Message sent to Discord.
  [manage_covenants] Failed: rule conflicts with rule_X
```

The next-turn agent's recent-conversation context now contains the failed-attempt record. This closes the "absence-of-evidence ≠ evidence-of-absence" failure mode the agent identified.

This is a small, mechanical fix — could ship as part of Phase 2 batch 0 (preparatory).

---

## Section F — Recommendation: batch vs sequenced

**Recommendation: sequenced, with a small preparatory batch first.**

Reasoning:

1. The **directive-metadata envelope (E.1)** is the central change; if it lands cleanly, most of the audit's drift cases close mechanically. But it's a substrate-shape change touching the integration runner, the briefing dataclass, the renderer, and per-tool result parsers. Doing it as one mega-batch risks "we shipped a schema, the migration is half-done, render rules are mixed in." The CCV1 cutover-then-strike pattern (audit recently parked items) is the right precedent: ship the substrate primitive first, migrate surfaces to it incrementally, strike the legacy free-form-prose path only after equivalence soak.

2. The **per-surface drift** (D.1–D.3) lands in 13 mutating + 4 read-only surfaces with non-uniform shape. Receipt-class-extension (E.5) needs per-tool work. Trying to enrich all tool results at once is brittle — better to migrate the highest-heat 3-4 surfaces first (channel send, covenants, scheduler, workspace) and validate the schema works.

3. The **C.7 failed-attempt logging fix** is independent and mechanical. Could land as a preparatory batch 0 — small, immediate audit value, no schema dependencies.

### F.1 Proposed Phase 2 sequencing

**Batch 0 (preparatory, mechanical):**
- C.7 fix: failed-attempt logging in conv_log (E.6).
- Optional: extend tool trace entry to carry `execution_state` field as a string, default `"completed"` for back-compat. No schema-wide migration; just opens the door.

**Batch 1 (substrate primitive, no migration):**
- E.1: introduce `ActionStateRecord` dataclass.
- Wire it into `Briefing.audit_trace` as `action_state_records`.
- Integration runner populates it from existing tool trace (with default fields where richer info isn't available yet).
- Renderer reads it but doesn't yet enforce render rules — just surfaces it for inspection.
- Tests pin the schema and round-trip; no behavior change yet.

**Batch 2 (renderer rules + first migrated surface):**
- E.2: renderer rules consuming action-state records.
- Migrate channel send (B.1.3, the highest-heat surface) to populate richer fields. Renderer applies completion-vs-attempted discipline for this surface.
- Embedded live test from spec's Phase 2 test categories: receipt-backed claim test, state-rendering test.

**Batch 3 (incremental surface migration):**
- Migrate scheduler (B.1.9), covenants (B.1.11), workspace (B.1.12) — the next three highest-heat.
- Each migration includes its receipt-class extensions (E.5).
- Live tests cover each migrated surface.

**Batch 4 (read-only surfaces, evidence-class):**
- Migrate read paths to populate `evidence_class`.
- Renderer enforces evidence-class language discipline.
- Live test: evidence-class differentiation across read surfaces.

**Batch 5 (cleanup + strike):**
- Migrate remaining surfaces.
- Tighten fail-closed scope to E.4.
- Strike legacy free-form-only directive paths if any remain.

### F.2 Why not one batch

The spec's own framing — "two phases" — already implies multi-batch. The argument for sequenced over single Phase 2:

- Substrate schema changes that touch every action surface have a long failure tail. Soak per migrated surface catches the regressions early.
- Render-layer changes are worth letting bake on a small surface (channel send) before applying to all 17.
- INTEGRATION-FAIL-LOUD parallel: that discipline shipped in three escalating batches (V1 → V2 → V3). RESPONSE-FIDELITY-V1 is a strictly larger scope; sequencing follows the same pattern.

### F.3 What to skip / consolidate

- Calendar batch operations and member management batch ops (B.1.1 and B.1.5 partial-state heat) probably don't need their own batches; can fold into Batch 3 with a `partial_state` schema demo.
- B.1.4 Canvas and B.1.7 Plan are already best-shaped — they get migrated last, mostly to update directive language not substrate state.
- B.1.8 Tool registration is too clean for special handling.

---

## Section G — Architectural questions surfaced for architect

These are spec-not-anticipated questions that surfaced during the audit. Per spec's escalation discipline, surfacing for architect ratification before Phase 2 design rather than resolving unilaterally.

### G.1 Where does C.3 (claim of intent without dispatch) belong?

The "I'll remember that" pattern is genuinely cross-cutting. It can be addressed via:

(a) **Substrate enforcement:** require any claim of future state to dispatch a corresponding tool (e.g., "I'll remember" requires a `commit_to_remember` tool call that creates a real knowledge entry synchronously, NOT relying on async fact_harvest).

(b) **Linguistic discipline:** the prompt makes "I'll remember" off-limits; the agent says "I'll let the system remember that during compaction" or similar.

(c) **Hybrid:** add a synchronous `note_for_memory` tool the agent can dispatch when it commits to remembering; if not dispatched, the prompt requires hedging language.

(a) is architecturally clean but adds tool surface and may friction normal conversational flow. (c) is the middle path. Architect's call on which shape Phase 2 takes.

### G.2 How does action-state metadata interact with cross-space requests?

`request_space_action` writes to other spaces. The action-state record schema needs to handle:
- Origin space's view: "I requested X happen in space Y"
- Target space's view (when the request fires): "X happened here, requested by space Z"

These are two different action-state records of the same operation. Phase 2 schema needs to handle this correctly. Worth confirming the design accommodates.

### G.3 Is C.7 (failed-attempts in conv_log) a separate spec or part of RESPONSE-FIDELITY-V1?

The fix is mechanical (one line in `phases/persist.py`). But it's a real semantics change for what the next-turn agent sees in conversation. Could be:
- Part of RESPONSE-FIDELITY-V1 Batch 0 (preparatory).
- Its own small spec called RECEIPT-FAILURE-LOGGING-V1.
- Part of an existing observability-tier spec.

The Audit recommends Batch 0; architect's call on whether that's the right scope partition.

### G.4 Does E.1's `evidence_class` need to be on the directive envelope OR on tool trace entries?

Both are plausible. On directive envelope, it's a render-time hint per claim. On tool trace, it's a property of the source itself. The right answer is probably "both" — tool trace carries the underlying source class; directive envelope carries the claim's relationship to the source (direct quote, paraphrase, inference). Worth confirming the design supports both layers.

### G.5 Is the "presence-as-renderer-not-investigator" constraint compatible with re-render-on-fail-closed?

Spec constraint 1 says presence renders, doesn't investigate. But fail-closed scope (E.4) implies that on a violation, presence should re-render with constraint feedback. Re-rendering is technically presence-investigating-its-own-output.

Likely the resolution is: re-render is governed by the substrate (via fresh directive that incorporates the violation feedback), not by presence inspecting its own first draft. But Phase 2 design needs to make this explicit — otherwise re-render becomes ad-hoc presence-layer logic that violates the constraint.

### G.6 What about the assistant's text getting logged BEFORE the receipt-cross-check?

`phases/persist.py:56` logs the assistant's text. `phases/persist.py:71` logs receipts. These happen in the same phase. If we add fail-closed re-render in Phase 2, when does it fire — before persist? Mid-persist? The ordering matters. Worth pinning in Phase 2 design.

### G.7 Should "Tool effects this turn" become "Action state this turn"?

Today the receipt block in conv_log is labeled "Tool effects this turn." If the substrate primitive is `ActionStateRecord` with explicit execution-state classes, the conv_log block could carry richer per-record information:

```
Action state this turn:
  [send_to_channel] state=attempted (Twilio API ack'd, no delivery confirmation)
  [manage_covenants] state=pending_validation (rule_X update; conflict check running)
  [page_write] state=completed (canvas/notes/audit.md, version 3)
```

The next-turn agent reading recent context now has structured execution_state visible. This is one of the higher-leverage substrate changes — but it's also a semantic shift in what conv_log carries. Worth a deliberate architect call on whether this is in-scope for Phase 2 or a separate spec.

---

## Closing — Phase 1 deliverable status

This audit is complete enough to drive Phase 2 design. The structural pattern is clear: **the principle is honored sporadically by integration-model and renderer-model prose discipline, but the substrate provides almost no enforcement.** Phase 2 is fundamentally a substrate primitive (`ActionStateRecord` or equivalent) plus per-surface migration. The fix shape is heavily structural; per-surface linguistic discipline is supporting layer, not load-bearing.

Top-3 leverage points for Phase 2:
1. `ActionStateRecord` substrate primitive (Section E.1) — closes ~6 of 8 cross-surface patterns.
2. Channel send migration as the canary (Section F.1, Batch 2) — most acute single surface.
3. C.7 failed-attempts in conv_log (Section E.6) — small, mechanical, immediate audit-value gain.

Architect's call on:
- Does the Phase 2 scope agree with the heat map and sequencing recommendation?
- Resolutions on G.1–G.7 architectural questions before Phase 2 design begins.
- Whether to ratify-and-open Phase 2 or close RESPONSE-FIDELITY-V1 entirely if the principle is judged sufficiently honored by INTEGRATION-FAIL-LOUD + relational-messaging's existing structure.

Closeout to architect to follow under `Inbox: CC → Architect`.
