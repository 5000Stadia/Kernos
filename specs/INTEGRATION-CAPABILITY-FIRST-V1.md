# INTEGRATION-CAPABILITY-FIRST v1

**Status:** APPROVED FOR CC HANDOFF — the design review verdict 2026-05-03 (REVISE
NARROWLY → six edits folded → APPROVED). Design Review primer carries
"legacy is the oracle until equivalence is proven; then strike" as
hard architectural principle.

Resolves the C7-cutover gap surfaced 2026-05-02 during operator soak:
the decoupled-cognition thin path is anti-capability today because it
ships kind prompts that forbid tool calls (`RESPOND_ONLY` /
`CONSTRAINED_RESPONSE` / `PROPOSE_TOOL`) AND because
`IntegrationInputs.surfaced_tools` is empty so the integration LLM
never picks `EXECUTE_TOOL` — and `EXECUTE_TOOL` is itself unwired
behind `_UnwiredDescriptorLookup` (parked CCV1 follow-up
`INTEGRATION-WIRE-LIVE-WORKSHOP-BINDING`).

This spec restores end-to-end tool execution on the C7 thin path and
re-enables the C7 default flip — gated on demonstrated equivalence
with the legacy path, not just green probes.

## Why

Today's diagnosis (commit `1133c19` reverts default to legacy):

> User: "search the web for today's weather in Boston"
> Agent: "no live web/search/weather lookup was performed for this turn."

The agent is faithfully obeying its instructions — every kind-aware
system prompt the C7 thin path emits literally tells the model "no
tool calls" or "Do NOT execute the tool." Tools are in the body
(verified via `KERNOS_CODEX_LAST_PAYLOAD` receipts) — substrate
fidelity is correct. But the system prompt then immediately tells the
model not to use them.

This is structurally anti-capability. Per the design review's guidance
(saved 2026-05-02 as `feedback_capability_first_posture`):

> Kernos should be impressively capable, not a stack of "don't"s.
> The default posture across the substrate, integration phase, kind
> prompts, directives, and agent responses should be **yes, can do**
> — even when there's a real limitation, lean toward "let me try" or
> "let me build a solution," not "I can't."

The current C7 thin path violates this guidance at every layer.

## Thin-path contract definition (the design review edit, codified as architectural fact)

The decoupled-cognition architecture defines two paths through the
model-call seam:

- **Thin path** = conversational/render path + bounded read-only
  observation loop.
- **Full machinery** = write/destructive/multi-step envelope actions,
  dispatched through `EXECUTE_TOOL` action kind via planning, gating,
  and confirmation flow.

This contract is codified in the design review primer. Future migrations
inherit it.

## Implementation strategy — three batches + decommission

the design review's revision: ship as three batches with explicit gating criteria,
plus a separate decommission commit. CC's original "B first, fast
win" ordering is REVERSED because B-before-C is a structural risk:
kind prompts encouraging tool use before the loop exists means the
model can call tools and the answer vanishes silently.

### Batch 1 — A + C + B together (C internally before B)

#### A. Thread `surfaced_tools` end-to-end

* `kernos/kernel/reasoning.py:3266` — `_run_via_turn_runner_provider`
  builds `TurnRunnerInputs.from_api_messages` without `surfaced_tools`.
  Populate from `request.cognitive_context.tool_surface.all_tools()`,
  mapped to `tuple[SurfacedTool, ...]` with `gate_classification`
  resolved from `kernos/capability/known.py`.
* `kernos/kernel/turn_runner.py:171, 295` — verify accepts and
  forwards `surfaced_tools` to `IntegrationInputs`. Default-empty
  stays the fallback for callers that genuinely have no surface.

#### C. Tool-use loop in thin-path render method (BEFORE B internally)

`kernos/kernel/enactment/presence_renderer.py:_render` currently calls
`chain_caller` once and extracts text. If the model returns a
`tool_use` block, it's silently dropped. Add a bounded tool-use loop
mirroring the legacy path's tool-use semantics. Lands BEFORE B
internally so the loop exists when the kind prompts start encouraging
tool use.

#### B. Capability-first kind prompts

Per `feedback_capability_first_posture`: kind prompts should encourage
tool use, not forbid it. Rewrite the four affected prompts at
`kernos/kernel/enactment/presence_renderer.py:170-244`:

* `_SYSTEM_PROMPT_RESPOND_ONLY` — drop "No tool calls." trailing line.
* `_SYSTEM_PROMPT_CONSTRAINED_RESPONSE` — replace "Plain text." with
  "Use tools where they help fulfill the request within the
  constraint; constraint applies to scope, not capability."
* `_SYSTEM_PROMPT_PROPOSE_TOOL` — replace "Do NOT execute the tool"
  with "If read-only / non-destructive, call inline. Propose only when
  effect is irreversible or affects others."
* `_SYSTEM_PROMPT_FULL_MACHINERY_TERMINAL` — verify it correctly
  encourages tool use for execute-tool kind.

Per-prompt **load-bearing check** applies: name what each currently
constrains, why, whether load-bearing, where the constraint moves
under the new prompt.

#### Batch 1 — required acceptance criteria (the design review edits 5 + 6)

* **Receipt-grade tool-loop pin tests** at
  `tests/test_thin_path_tool_use_loop.py`:
  - (a) multiple `tool_use` blocks handled, OR explicit "first only"
    policy pinned in the test
  - (b) `tool_use_id` preserved through to corresponding `tool_result`
  - (c) `conversation_id` forwarded every iteration
  - (d) trace/audit/event parity with legacy path
  - (e) max-iteration friendly failure (not silent drop)
  - (f) telemetry increments once per actual dispatch, not per attempt
* **Capability-readiness contract test** at
  `tests/test_capability_readiness_contract.py` — semantic, not
  string-ban: weather/calendar-style request + read tool surfaced ⇒
  model invocation receives prompt that allows/encourages tool call,
  not one that says "no tool available." Plain-text-no-tools default
  behavior must affirmatively go away, not just lose its forbid-tools
  string.
* **Conservative classification fallback**: missing/unknown
  classification defaults to propose/blocked, not silently read-safe.
  Pin test enforces.
* **Plumbing pin tests** at
  `tests/test_thin_path_surfaced_tools_plumbing.py`:
  - reasoning builds `TurnRunnerInputs.surfaced_tools` from
    `cognitive_context.tool_surface`
  - each `SurfacedTool` has `gate_classification` non-empty
  - `TurnRunnerInputs.surfaced_tools` reaches
    `IntegrationInputs.surfaced_tools`

### Batch 1 — Codex review fold (2026-05-03)

End-of-batch Codex review verdict: PARTIAL → fixes folded; two
architect-input items deferred for Batch 2 fold:

**Folded into Batch 1:**
- Action-dependent tools (`manage_covenants`, `manage_capabilities`,
  `manage_channels`, `manage_members`, `manage_plan`,
  `manage_workspace`, `respond_to_parcel`) classified `"unknown"`
  at surfacing time rather than silently `"read"` — dispatch-time
  enforcement (Batch 2) is the source of truth. Pin test added.
- Capability-readiness contract test rewritten to use a real
  cognitive-context tool surface and a real chain caller assertion
  on the system prompt sent to the model — not gaming the loop.
- Receipt-grade criterion (d) test reframed: pins MESSAGE-THREAD
  parity with legacy (assistant→user/tool_result alternation).
  Trace/audit/event parity is owned by the dispatcher layer, not
  the renderer loop — Batch 2 wires the dispatcher with audit.

**Architect-input items for Batch 2 fold:**

1. **Dispatcher signature mismatch.** PresenceRenderer's loop expects
   keyword-style `(tool_name, tool_input, tool_use_id, conversation_id)`;
   server.py's `_integration_dispatcher` is positional
   `(tool_id, args, inputs)`. Batch 2 wiring must define an adapter,
   not a direct wire. Two callable shapes serve two distinct seams:
   integration runner read-only dispatch vs presence renderer
   observation-loop dispatch.

2. **PROPOSE_TOOL effect data plumbing.** The Batch 1 prompt rewrite
   asks the model to distinguish read vs destructive effects, but
   `ProposeTool` briefing carries no `effect` field and the propose
   user message includes only tool id and reason. Today the safety
   property depends on model inference. Options for Batch 2 fold:
   (a) thread effect through `ProposeTool` dataclass and the propose
   user-message renderer; (b) make dispatch-time enforcement
   block destructive calls regardless of inline-vs-propose decision;
   (c) keep model inference and accept the residual. Architect call.

3. **Dispatch-time enforcement using actual args.** Batch 2 must
   enforce read-only at the dispatcher level using the actual call
   arguments, not only `SurfacedTool.gate_classification` which is
   set at surfacing time before args exist. This is the canonical
   safety boundary for action-dependent tools.

### Batch 2 — D, with four live bindings (the design review edit 3)

D = workshop binding, expanded scope per the design review. If any of the four
stay fake, integration/planning stays partially blind even after
executor wiring. Half-fix is worse than no-fix because tests look
complete.

1. **Descriptor lookup** — replace `_UnwiredDescriptorLookup` in
   `kernos/server.py:483` (and `kernos/repl.py` mirror) with a
   production version reading from the live tool catalog.
2. **Executor** — replace `_UnwiredExecutor` in
   `kernos/server.py:497` with kernel-tool dispatch through the
   legacy handler's existing path.
3. **Planner tool catalog** — currently `StaticToolCatalog()` is
   empty at `kernos/server.py`. Wire to the live catalog so planning
   can see real tools.
4. **Integration read-only dispatcher** — currently
   `_integration_dispatcher` returns `{}`. Wire to the live
   read-only dispatch path.

Pin tests at `tests/test_thin_path_executor_wiring.py`:
- executor.execute on a kernel tool returns valid `ToolExecutionResult`
- descriptor_lookup returns valid descriptor for known tool ids
- planner catalog reflects live registrations
- integration dispatcher returns real read-only tool results
- tool execution receipts land in conversation log identical to legacy

### Batch 3 — equivalence soak + default flip (legacy retained behind flag)

Re-run the operator soak runbook against the thin path with all four
pieces from Batches 1+2 landed. Default flip is gated on
**demonstrated equivalence with legacy**, not just green probes.

#### Equivalence soak — required before default flip

* **Same-input parity scenarios.** Run each operator soak scenario
  through both paths (`KERNOS_USE_DECOUPLED_TURN_RUNNER=0` for legacy,
  `=1` for thin) with identical inputs. Capture model invocation
  receipts, user-facing response shape, tool calls executed, side
  effects landed. Compare. Document any divergence as either:
  (a) intentional improvement on thin path with rationale,
  (b) intentional removal of legacy quirk with rationale, or
  (c) regression that blocks the flip.
* **Read-only tool capability.** Calendar lookup, memory recall, web
  fetch, file read — each runs end-to-end on thin path with response
  equivalent to legacy.
* **Write/destructive tool capability.** Tool propose-then-execute on
  thin path produces the same audit, gate, and confirmation behavior
  as legacy.
* **No-tool conversational turns.** Plain conversational turns
  (greetings, simple Q&A) on thin path produce equivalent or better
  response quality.
* **Hatching turn.** Fresh-install hatching reaches the model with
  bootstrap content (CCV1 invariant holds) and the agent uses tools
  when appropriate (capability-first holds).
* **Multi-member disclosure scenario.** Cross-member sensitivity
  gates apply correctly on thin path.
* **Covenant-conflict scenario.** User covenants honored on thin
  path; gate behavior matches legacy.

Any regression at this stage routes back to Batches 1 or 2 for fix;
default does not flip with known regressions.

#### Default flip

When equivalence soak is green, the default flip commit lands:
`KERNOS_USE_DECOUPLED_TURN_RUNNER` becomes unset-defaults-to-1 (thin
path is the default), legacy reachable only via explicit
`KERNOS_USE_DECOUPLED_TURN_RUNNER=0`.

**Legacy is NOT decommissioned at this point.** It stays reachable
behind the flag for the stabilization window.

### Stabilization window

After default flip, legacy retained as oracle for 2-4 weeks of real
production use (owner-decided duration). Catches regressions that
didn't surface in soak under realistic conversational load. During
this window:

- Any reported anomaly on thin path can be cross-checked against
  legacy by setting `KERNOS_USE_DECOUPLED_TURN_RUNNER=0`.
- Capability-readiness contract tests + receipt-grade tool-loop pin
  tests + same-input parity assertions stay strict-passing on every
  commit.
- New thin-path-only features (if any ship during the window) are
  explicitly named as thin-path-only; legacy not held to feature
  parity going forward.

### Batch 4 — Legacy decommission (separate commit)

Legacy `assemble.py` path strike commit ships only when ALL the
following hold:

- [ ] Stabilization window passed (2-4 weeks of real production use)
- [ ] No reported regressions on thin path that required falling back
      to legacy via the flag
- [ ] All contract tests, capability-readiness tests, tool-loop pins,
      and same-input parity assertions continue to pass
- [ ] Owner explicit signoff that the criteria are met
- [ ] Per-section load-bearing check (CCV1 discipline) applied to
      every legacy code section being removed: name what it currently
      provides, where that function moved on thin path, contract test
      that proves the move, why it's obsolete rather than merely
      inconvenient

Decommission is a follow-up commit, not part of Batch 3. Naming it
explicitly so the work doesn't become "we'll get to it" — it's a
tracked roadmap item with explicit criteria.

## Why this matters

The earlier framing ("default flip and we'll see") would have repeated
the CCV1 mistake one architectural layer up: shipping a substrate
transition without proving it satisfies the legacy contract, then
discovering the gap in production. The capability-first migration
touches the same model-call seam CCV1 just stabilized. Same
discipline applies: prove equivalence, then flip; soak under real
load, then strike.

**Legacy is the oracle until equivalence is proven, then strike. Not
before.**

## Definition of done

- [ ] Batch 1 ships: A + C + B with C-before-B internally; six
      receipt-grade tool-loop tests green; capability-readiness
      contract test green (semantic not string-ban); conservative
      classification fallback pinned.
- [ ] Batch 2 ships: all four D bindings live (descriptor lookup,
      executor, planner catalog, integration dispatcher); none stay
      as empty stubs.
- [ ] Batch 3 ships: equivalence soak runbook green; default flip
      lands; legacy retained behind flag.
- [ ] Stabilization window passes (2-4 weeks).
- [ ] Batch 4 ships: legacy strike with all five decommission
      criteria green.
- [ ] Thin-path contract codified in design review primer (complete
      2026-05-03).
- [ ] Capability-first posture codified as architectural principle
      (complete 2026-05-03).

Default remains legacy until Batch 3 ships equivalence-green.
Operators are not blocked. The wire-shape work shipped 2026-05-02
(commits `757ca64` through `e008156`) is correct and stays under
both paths.

## Architectural-constraint check (always enforced per CLAUDE.md)

- Adapter/handler isolation maintained
- `instance_id` keying preserved
- Graceful errors on every new path
- MCP-for-capabilities discipline preserved
- Single source of truth for tool catalog (no shadow registry)

## Out of scope (explicit nos)

* New `ActionKind` types beyond what already exists
* Substrate restructure (RULES / NOW / STATE / etc. zones unchanged)
* Provider chain changes (current chain stays)
* Wire-shape changes to the Codex provider (2026-05-02
  `strict: None` + `conversation_id` work is correct and stays —
  this spec doesn't touch it)
* Stewardship-aware tool gating (per-relationship / per-sensitivity
  gating) — its own follow-up
* Streaming tool calls during multi-step loops — defers; v1 ships
  non-streamed tool execution
