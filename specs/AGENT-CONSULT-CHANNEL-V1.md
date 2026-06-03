# AGENT-CONSULT-CHANNEL-V1

Status: DRAFT v2 (synthesis — author CC; grounded in Codex ACP-shape map 019e8b78 + architecture review; Codex elegance review YELLOW→folded 019e8bae) 2026-06-03

## 1. Why

The coding-agent consult is the spine of the autonomous improvement loop, and it was built **fire-and-forget**: one-shot `acpx <agent> exec <prompt>` + `--approve-all`, returning a single text blob. That assumption is wrong — design/implementation agents **pause to ask** as a normal, frequent mode (permission, ambiguity, missing input). When they do, the request lands on a channel nobody is listening to, and the step stalls silently until a blunt timeout kills it. Eight live-fire patches this session (idle watchdog, stall monitor, boot reconcile, auto-proceed, durable surfacing, …) treated symptoms and have started to accrete.

This spec replaces the accretion with **one coherent bidirectional channel** and consolidates the patches into a minimal set of meaningful elements.

## 2. Invariants (non-negotiable)

- **Bidirectional.** Every consult is a two-way session. A backward signal (request/question/ambiguity) is *seen → routed → answered → resumed in the same step*. Never decays to silence.
- **Loud, legible failure.** A timeout/error propagates **up** carrying diagnostic context (last activity, stop_reason, stderr tail, counts) — not `str(exc)[:300]` → generic whisper.
- **Default-proceed.** Default = proceed on the assumed/intended direction (the `--approve-all` spirit). Escalate **only** a genuine either/or fork or a hard blocker. Answer as low on the ladder as possible (tool/KERNOS infer intent); surface to the **user** only for user-only decisions, with a **specific actionable ask** at the moment it's needed.
- **No junk drawer.** Each node has exactly one mechanism for its job. Two mechanisms doing one thing = design failure.

## 3. The shape — one conversation, four inbound kinds

Every inbound event from the agent resolves to exactly one of:

| kind | meaning | handler action |
|------|---------|----------------|
| `progress` | streamed work (message/tool_call/plan) | record + reset liveness |
| `request` | needs an answer (permission / question / missing-input) | **DecisionPolicy** → answer or escalate |
| `result` | done; carries status (GREEN / NEEDS_REVISION / BLOCKED) | return up |
| `failure` | died; carries diagnostic context | propagate up loudly |

Outbound (KERNOS → agent): `prompt` · `answer` · `continue` · `cancel`.

**Scope:** this spec covers the **blocking improvement-loop consult** path. `ask_coding_session` (`bridge_watcher.py:359`) is a second ACPX consult path — it becomes a `ConsultSession` *consumer* in a follow-up, not re-specced here.

## 4. Three abstractions that absorb the pile

| New element | Owns | **Collapses** (precise — not global primitives) |
|---|---|---|
| **`ConsultSession`** | dispatch, event stream → 4 kinds, resumable session, **one liveness rule**, retry | the ACPX idle-watchdog **and** the 720s orchestrator stall-monitor's *live-consult* watch → one liveness authority. **Keeps** `reconcile_orphaned_attempts` (`improvement_loop_workflow.py:1049`) — it handles *restart-killed* attempts, a different concern; it becomes a *consumer* of session/attempt state, not a parallel watcher. |
| **`DecisionPolicy`** | classify an ACP `request` (tool-permission / agent design-question) → proceed / answer-from-context / escalate-to-user (default proceed) | `--approve-all` hardcode + scattered tool-permission handling. **Does NOT touch** the durable commit-approval gate (`workflow_registry.py:278`) — irreversible-action approval is a separate, stays-as-is concern. |
| **`_surface_improvement_message`** (the *existing* survivor — no new abstraction) | the one legible improvement-surfacing API, up the ladder | the improvement-specific **wrappers** `_announce` / `_notify_terminal` route through it. **Does NOT delete** `send_outbound` / `save_whisper` — those are global substrate primitives the survivor *calls*. |

The orchestrator shrinks to: drive steps, consume `ConsultSession` lifecycle events. (Module split of `improvement_loop_workflow.py` is a maintainability win but satisfies no invariant — **deferred off the critical path**, §7.)

## 5. Tier 1 — legibility + protocol (keep the acpx CLI)

Ships value without re-architecting transport. Code-checked anchors:

1. **Structured `ConsultResult` on every call** (success too): `stop_reason`, `last_event_kind`, `event_count`, `stderr_tail`, `stdout_errors`, tool/permission summary. (`acpx_adapter.py` ConsultResult build ~1624; `_drain_stdout` ~1011.)
2. **Propagate context up** the 6 flattening points: adapter→ConsultResult; handler returns only `.response` (`handler.py` ~908–927); `_call_consult_fn` returns plain string (`improvement_loop_workflow.py` ~1205); loop runs `detect_status` on text (~669/753); `str(exc)[:300]` (~440); generic whisper. Each carries the structured context to the ledger + `surface`.
3. **`STATUS: BLOCKED <q>` / `NEEDS_INPUT <ask>` protocol** — extend `detect_status` (`improvement_review_protocol.py`). A `BLOCKED`/`NEEDS_INPUT` result routes through `DecisionPolicy`: KERNOS answers from spec/context first; escalate to user (with the actionable ask) only if user-only; answer feeds the next turn.
4. **Sessioned consults** — deterministic resumable `session_id` per (attempt, step); handler stops passing `session_id_raw=""` (`handler.py` ~914). `answer`/`continue` attach to prior context via `session/load`, with a **same-session-only guard** (no silent fork to a fresh session).
5. **Durable escalation state** (the load-bearing gap). When a `request` escalates to the user, the consult is *validly paused*, not failed — and must survive a restart. Add an `awaiting_consult_input` attempt state carrying: the pending request id, the question/ask, an expiry, and the answer command/tool. Boot reconcile must treat `awaiting_consult_input` like `awaiting_commit_approval` (surface a reminder, do **not** mark interrupted). The answer resumes the same session. This is where the bidirectional channel meets the trust-gap work already shipped.
6. **Same-session guard + concurrency**: resume must attach to the prior session or fail loudly — never silently `session/new` a fresh context. Exactly **one active turn per `(attempt, step, role, iteration)`** session (primary/reviewer and concurrent attempts each get distinct session ids).
7. **Collapse liveness + surfacing** (the consolidation): idle-watchdog becomes the single liveness rule inside `ConsultSession`; retire the stall-monitor's *live-consult* watch; route the improvement wrappers through `_surface_improvement_message`. **Done = the collapsed wrappers deleted, not just superseded.**

## 6. Tier 2 — own the decision channel

The CLI cannot delegate a permission request (only auto-modes); the `onPermissionRequest` hook exists **only in acpx's runtime API** (`live-checkpoint…:3627`, `withConnectedSession:4620`), and a permission request is an **in-flight** JSON-RPC request that must be held pending while KERNOS/user decides. So owning it requires a controlled client.

- **Decision: Node bridge on acpx's runtime** (vs a from-scratch Python ACP client). Rationale: reuses acpx's tested agent lifecycle + `onPermissionRequest`/`session/load`, avoids reimplementing the protocol + the unstable `elicitation/create`; exposes a thin stdio JSON line protocol to the existing Python adapter (which keeps its `ConsultResult` interface). A Python client is more code and re-derives what acpx already does. *(Open risk — validate first; see §8.)*
- `DecisionPolicy` maps a held request → allow / deny / answer, honoring default-proceed.
- **Off-channel TTY/stderr prompts**: always capture stderr tail (Tier 1 already); a tool that hard-blocks on a TTY is handled as a `failure` with context, not papered over with a PTY. PTY is a *diagnostic* fallback, not primary control.

## 7. Build sequence (dependency-ordered, hard done-gates)

Each stage: **done = tests green + the collapsed predecessor deleted + (gated stages) a live run.**

1. **`ConsultResult` structured + propagate up** (§5.1–5.2) — unblocks everything; *no behavior change, pure legibility*. **This stage's diagnostics on a live run answer risk #1 (§8) and gate how much of Tier 2 we build.**
2. **Surfacing consolidation** — ✅ *already satisfied* (verified Stage 2, 2026-06-03): `_surface_improvement_message` is the single push+whisper primitive; `_announce_to_origin` + `_notify_terminal` already route through it; nothing bypasses it to call `send_outbound`/`save_whisper` directly. No deletion work invented. Residual minor smell (the cold-path ledger-reread in `_notify_terminal`) is *accepted* — folding it would thread diagnostics through `notify_fn` + test fakes, adding complexity to a correct once-per-attempt path (net-negative on simplicity).
3. **Sessioned consults + same-session guard + concurrency ids** (§5.6) — prerequisite for any answer/resume.
4. **`BLOCKED/NEEDS_INPUT` protocol + `DecisionPolicy` v1 + durable `awaiting_consult_input`** (§5.3, §5.5) — parsing + answering ship **together** (answering needs stage 3). KERNOS-answers-from-context first; user escalation is restart-safe.
5. **Liveness consolidation** — idle-watchdog as sole authority; retire the stall-monitor live-consult watch (§5.7).
6. **Tier 2 spike** — minimal Node bridge proving cwd / MCP / version-pin / `session/load` / held `onPermissionRequest` all work (validates risk #3 *before* committing).
7. **Tier 2 build** — Node bridge + held permission requests → `DecisionPolicy`, *only as far as risk #1's data justifies*.
8. **Live gate** — a real `improve_kernos` run that hits a real decision point and resolves it without silence.
9. *(deferred, non-blocking)* module split of `improvement_loop_workflow.py`; bring `ask_coding_session` under `ConsultSession`.

## 8. Highest-risk unknowns (validate before committing code)

1. **Does the agent actually surface design questions as ACP `request`s, or as plain message text + stop?** Determines whether Tier 1's text protocol (`BLOCKED`) carries most of the value and Tier 2 is mostly permissions. *Validate with the Tier 1 diagnostics on a real run before building Tier 2.*
2. **Does `session/load` resume preserve enough agent context** for `answer`/`continue` to be productive, for this acpx/claude-acp version? *Probe with a 2-turn sessioned consult.*
3. **Node-bridge integration cost** — does the runtime API expose everything the CLI gave us (cwd, mcp, version pinning) cleanly? *Spike a minimal bridge before committing to it over the CLI.*

The theory this tests: **most silent hangs are crossroads `--approve-all` can't satisfy.** Tier 1's diagnostics confirm or kill it with data — and gate how much of Tier 2 we actually need.
