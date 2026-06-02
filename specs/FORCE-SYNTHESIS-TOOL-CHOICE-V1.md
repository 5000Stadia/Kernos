# FORCE-SYNTHESIS-TOOL-CHOICE-V1 (draft — design to reviewable point)

**Status:** DRAFT for review. NOT implemented. Authored 2026-06-02 after a
live `no_tool_use` synthesis failure blocked an agent turn. Touches the
agent's core reasoning/synthesis hot path — ship only after review + thorough
test. The immediate workaround is to RETRY the prompt (the failure is
intermittent).

## 1. Problem

The integration runner's synthesis phase (`kernos/kernel/integration/runner.py`
~640–680) calls the model and **requires** a `tool_use` block (a `finalize`
call) to produce the final response. If the model returns bare **text**, it
raises `IntegrationAttemptFailed(component="no_tool_use")`. It retries 3× and
fails loudly.

Root cause: `kernos/providers/codex_provider.py:585` hardcodes
`"tool_choice": "auto"`. "auto" lets the model choose text *or* a tool call —
so in the synthesis phase, where a tool call is mandatory, the model is
*permitted* to answer with text, and intermittently does → `no_tool_use`.
This is a recurring, pre-existing failure class (the dump shows prior
`briefing_validation`/`no_tool_use` retry failures), not a new regression.

## 2. Fix options

**Option A — force tool_choice on synthesis (preferred; prevents the problem):**
- Give `Provider.generate` (and `codex_provider`) a `tool_choice` parameter
  that **defaults to `"auto"`** — zero change to every existing caller.
- Thread `tool_choice` through `build_resilient_chain_caller` /
  `_resilient_chain_caller` (turn_runner_provider.py) and the telemetry
  wrapper, default `"auto"`.
- The synthesis call site (runner.py ~640) passes `tool_choice="required"`
  (OpenAI: model MUST call ≥1 tool). The model then can't return bare text;
  it calls `finalize` (or another integration tool the runner already
  handles at line 686).
- **Blast radius is bounded by the default:** normal reasoning turns pass
  nothing → get `"auto"` exactly as today. ONLY the synthesis call changes.

  Risks to verify: (1) every provider in the chain (not just codex) accepts
  the new kwarg — audit `Provider` subclasses; a fallback provider missing it
  breaks the fallback path. (2) confirm the codex/gpt endpoint honors
  `tool_choice="required"`. (3) telemetry wrapper passes it through.

**Option B — graceful fallback (lower blast radius, different semantics):**
- At the runner's `no_tool_use` branch, instead of failing, treat the model's
  text as the final response (synthesize from text). Bounded to the runner;
  no provider-interface change. Risk: loses the structured-briefing shape the
  `finalize` tool enforces; may degrade downstream consumers expecting the
  briefing structure.

**Recommendation:** Option A. It removes the failure class at the source and
keeps the structured-briefing contract; the default-`auto` design bounds the
risk to the one call site that needs the change. Option B is a fallback if
the provider-interface audit (A's risk #1) turns up too many call sites.

## 3. Acceptance criteria (Option A)

- `Provider.generate` accepts `tool_choice` (default `"auto"`); all chain
  providers accept it; resilient chain-caller + telemetry wrapper forward it.
- The synthesis call passes `"required"`; a model that *would* have returned
  text now returns a `finalize` (or integration) tool call.
- Normal reasoning turns are byte-for-byte unchanged (default `"auto"`).
- Tests: provider passes tool_choice through; synthesis forces a tool call;
  a simulated "model returns text" no longer raises `no_tool_use` under
  `"required"`; fallback-provider path still works.

## 4. Connection to the broader direction

This IS the "structured output, forced at the tool layer" pattern flagged as
the #2 adoption from Claude Code's Workflow tool (see the workflow-comparison,
2026-06-01) — forcing a schema/tool at the boundary instead of hoping the
model complies, then parsing/failing. Same idea, applied to the synthesis
gate.
