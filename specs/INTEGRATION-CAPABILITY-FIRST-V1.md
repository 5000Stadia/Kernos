# INTEGRATION-CAPABILITY-FIRST v1

**Status:** Draft for founder + Kit approval before implementation.
Resolves the C7-cutover gap surfaced 2026-05-02 during operator soak:
the decoupled-cognition thin path is anti-capability today because it
ships kind prompts that forbid tool calls (`RESPOND_ONLY` /
`CONSTRAINED_RESPONSE` / `PROPOSE_TOOL`) AND because
`IntegrationInputs.surfaced_tools` is empty so the integration LLM
never picks `EXECUTE_TOOL` — and `EXECUTE_TOOL` is itself unwired
behind `_UnwiredDescriptorLookup` (parked CCV1 follow-up
`INTEGRATION-WIRE-LIVE-WORKSHOP-BINDING`).

This spec restores end-to-end tool execution on the C7 thin path and
re-enables the C7 default flip.

## Why

Today's diagnosis (commit `1133c19` reverts default to legacy):

> User: "search the web for today's weather in Boston"
> Agent: "no live web/search/weather lookup was performed for this turn."

The agent is faithfully obeying its instructions — every kind-aware
system prompt the C7 thin path emits literally tells the model "no
tool calls" or "Do NOT execute the tool." Tools are in the body
(verified via `KERNOS_CODEX_LAST_PAYLOAD` receipts, 50KB body, 19
tools, `strict: None`, `cache_key` set) — the substrate fidelity is
correct. But the system prompt then immediately tells the model not
to use them.

This is structurally anti-capability. Per the architect's guidance
(saved 2026-05-02 as feedback memory `feedback_capability_first_posture`):

> Kernos should be impressively capable, not a stack of "don't"s.
> The default posture across the substrate, integration phase, kind
> prompts, directives, and agent responses should be **yes, can do**
> — even when there's a real limitation, lean toward "let me try" or
> "let me build a solution," not "I can't."

The current C7 thin path violates this guidance at every layer. This
spec realigns it.

## Scope

### Ships in v1

#### A. Thread `surfaced_tools` end-to-end (Codex pinpoint)

* **`kernos/kernel/reasoning.py:3266`** — `_run_via_turn_runner_provider`
  builds `TurnRunnerInputs.from_api_messages` without `surfaced_tools`.
  Populate from `request.cognitive_context.tool_surface.all_tools()`,
  mapped to `tuple[SurfacedTool, ...]` with `gate_classification`
  resolved from `kernos/capability/known.py`.
* **`kernos/kernel/turn_runner.py:171, 295`** — verify
  `TurnRunnerInputs.from_api_messages` accepts and forwards
  `surfaced_tools` to `IntegrationInputs`. Default-empty stays the
  fallback for callers that genuinely have no surface.
* **Pin tests** at `tests/test_thin_path_surfaced_tools_plumbing.py`:
  - reasoning builds `TurnRunnerInputs.surfaced_tools` from
    `cognitive_context.tool_surface`
  - each `SurfacedTool` has `gate_classification` non-empty
  - `TurnRunnerInputs.surfaced_tools` reaches `IntegrationInputs.surfaced_tools`

#### B. Capability-first kind prompts

Per saved feedback memory: kind prompts should encourage tool use, not
forbid it. Rewrite the four affected prompts at
`kernos/kernel/enactment/presence_renderer.py:170-244`:

* **`_SYSTEM_PROMPT_RESPOND_ONLY`** — drop "No tool calls." trailing
  line. Default posture: respond conversationally; if a tool would
  serve the user better, call it.
* **`_SYSTEM_PROMPT_CONSTRAINED_RESPONSE`** — replace "Plain text."
  with "Use tools where they help fulfill the request within the
  constraint; constraint applies to scope, not to capability."
* **`_SYSTEM_PROMPT_PROPOSE_TOOL`** — replace "Do NOT execute the
  tool — the proposal is the output." with "If the tool is read-only
  / non-destructive, call it directly. Propose first only when the
  effect is irreversible or affects others."
* **`_SYSTEM_PROMPT_PIVOT`** — verify pivot doesn't accidentally
  forbid tool calls.

Rationale per architect memory: constraints come from the user, not
from the system inventing them. Read-only / observation tools have no
ambiguity — agent should call them inline. Write/destructive tools
are where proposal-then-confirm makes sense.

* **Pin tests** at `tests/test_capability_first_kind_prompts.py`:
  - no kind prompt contains the literal string "No tool calls"
  - no kind prompt contains "Do NOT execute"
  - kind prompts reference tool use as supportive of the action

#### C. Add tool-use loop to thin-path `_render`

`kernos/kernel/enactment/presence_renderer.py:_render` currently calls
`chain_caller` once and extracts text. If the model returns a
`tool_use` block, it's silently dropped. Add a bounded tool-use loop
mirroring the legacy path's tool-use semantics (cap iterations, log
every tool call, append tool_result blocks before next iteration).

* **Pin tests** at `tests/test_thin_path_tool_use_loop.py`:
  - chain_caller returns tool_use → `_render` executes the tool and
    appends `tool_result` before next chain call
  - max iterations cap fires correctly with friendly error
  - text-only response returns immediately without looping

#### D. Wire workshop binding (parked CCV1 follow-up)

This is the original `INTEGRATION-WIRE-LIVE-WORKSHOP-BINDING`
parked work. Replace `_UnwiredDescriptorLookup` and `_UnwiredExecutor`
in `kernos/server.py:483-502` (and `kernos/repl.py` mirror) with
production-wired versions:

* `descriptor_lookup` reads from the live tool catalog
* `executor` dispatches through the existing kernel-tool dispatch
  path used by the legacy handler
* Audit + receipt parity with legacy path

* **Pin tests** at `tests/test_thin_path_executor_wiring.py`:
  - executor.execute on a kernel tool returns valid `ToolExecutionResult`
  - descriptor_lookup returns valid descriptor for known tool ids
  - tool execution receipts land in conversation log identical to legacy

#### E. Verification — soak runbook re-run

Re-run probes A–D + scenarios 1-4 from
`data/diagnostics/live-tests/COGNITIVE-CONTEXT-V1-live-test.md`
against the C7 thin path with all four pieces above landed. Pass
criteria:

- Probe A: ✅ already passed (substrate)
- Probe B: tool actually invoked + result reaches model + included
  in response (not just "substrate PASS")
- Probe C: live procedures probe executes
- Probe D: ✅ already passed (compaction-carry)
- Scenarios 1-4: behavioral equivalence with legacy path

#### F. C7 default flip

Once all probes/scenarios pass on the thin path, flip
`KERNOS_USE_DECOUPLED_TURN_RUNNER` default back to `1` in `start.sh`.
Legacy stays reachable via `=0` for the stabilization window. Strike
commit (legacy removal) gates on the same criteria as the original
C7 spec.

### Defers

* **Stewardship-aware tool gating** — for now, all tools the legacy
  path would surface are surfaced on the thin path. Per-relationship
  / per-sensitivity gating is its own follow-up.
* **Streaming tool calls** — tool execution stays non-streamed in v1;
  streaming during multi-step tool loops lands later if soak proves
  out non-streamed UX is acceptable.

## Architecture

The decoupled-cognition design always intended tool execution to live
on the thin path — the parked `INTEGRATION-WIRE-LIVE-WORKSHOP-BINDING`
follow-up acknowledged it. This spec lands that wiring plus the
upstream `surfaced_tools` plumbing the integration LLM needs to even
*classify* requests as tool-needing in the first place. The
capability-first kind-prompt rewrite is independent but landed in the
same spec because shipping any of A/C/D without B leaves the agent
still telling the model "don't use tools."

## Acceptance criteria

- [ ] All four code pieces (A, B, C, D) shipped with pin tests green
- [ ] Full pytest suite passes (5,300+ tests)
- [ ] Architectural-constraint check clean (handler/adapter isolation,
      no stray `tenant_id`)
- [ ] Soak runbook probes A-D pass on thin path with
      `KERNOS_USE_DECOUPLED_TURN_RUNNER=1`
- [ ] Soak scenarios 1-4 pass on thin path with behavioral equivalence
      to legacy path
- [ ] No "No tool calls" / "Do NOT execute" strings in any kind prompt
- [ ] Legacy path still works (`KERNOS_USE_DECOUPLED_TURN_RUNNER=0`)
- [ ] `_UnwiredDescriptorLookup` removed; descriptor lookup wired

## Implementation order (for the executor)

1. **B first** (kind prompts, ~30 min) — single file, obvious win,
   immediately matches architect's saved feedback. Even without A/C/D
   landing, the change in tone is visible in the next operator soak.
2. **A next** (surfaced_tools threading, ~1-2 hr) — Codex's pinpointed
   plumbing. Unblocks integration LLM's ability to classify
   correctly.
3. **C** (tool-use loop, ~2-3 hr) — adds the loop semantics to thin
   path's `_render`.
4. **D** (workshop binding, ~3-5 hr) — replaces unwired stubs with
   production wiring.
5. **E** (re-run soak) — operator-driven, ~30 min.
6. **F** (default flip) — single line change in `start.sh`, gates on E.

Total CC scope: ~6-10 hours of focused work plus operator soak. This
is multi-commit batch territory, fold-and-review per the existing
`feedback_codex_implementation_review` memory.

## Out of scope (explicit nos)

* New ActionKind types beyond what already exists
* Substrate restructure (RULES / NOW / STATE / etc. zones unchanged)
* Provider chain changes (current chain stays)
* Wire-shape changes to the Codex provider (today's `strict: None` +
  conversation_id work is correct and stays — this spec doesn't touch
  it)
