# TOOL-ARG-REPAIR-V1

**Date:** 2026-06-09 (v1 draft — pre-Codex-review)
**Status:** 🟡 DRAFT — author + Codex deep-dive done (session
  `019ead1e-90d0-7a21-bc5d-703c7e41d565`); not yet Codex-spec-reviewed.
**Origin:** Live v1 self-test kept failing Tests 6/7/16 across reruns with a
  DIFFERENT malformed tool-arg shape each run, despite a long arc of
  forgiving-dispatch patches. Root-cause confer with Codex (2026-06-09)
  identified four structural causes; this spec closes them.
**Scope:** A unified tool-argument repair seam (the missing twin of the existing
  tool-NAME alias-repair) + two orchestration-visibility fixes. Replaces
  per-tool, field-name allow-lists with value/role-based normalization at one
  dispatch boundary.
**Estimated size:** ~400 LOC source + ~450 LOC tests (phased; see Sequencing).

---

## 1. Problem & root cause (all four verified in code)

The agent (gpt-5.5) emits tool calls with malformed/novel argument shapes. Each
forgiving-dispatch patch fixed one observed shape; the next live run produced a
new one. The failures are not three missing cases — they are four structural
gaps:

1. **Arg-repair is explicitly out of scope.** `kernos/kernel/tool_aliases.py`
   (~line 21): *"V1 only repairs the tool NAME. Argument-shape repair is out of
   scope."* Tool NAMES get one central repair (`tool.alias_repaired`); tool ARGS
   are repaired ad-hoc inside each tool with hand-enumerated field-name lists
   (`_SCHEDULE_TIME_FIELDS` in `scheduler.py`; the harness logic in
   `external_agents/tool.py`). Enumeration can't keep up — e.g. the model sent
   the reminder time in a `due_at` field that no list named.

2. **Schemas don't bind the model.** The Codex provider sends every tool with
   `strict: null` (`kernos/providers/codex_provider.py` ~line 149, load-bearing
   per the 2026-05-02 mutation matrix). So `additionalProperties: false` is
   advisory — the model freely invents fields (`due_at`, `kind`, `notes`).

3. **The plan-spine gives the model EMPTY schemas.** The self-test runs through
   the self-directed plan spine; `LivePlannerCatalog` passes `input_schema={}`
   for every tool (`kernos/kernel/integration/live_wiring.py` ~line 874). On
   exactly the path the self-test uses, the model has zero arg-shape guidance →
   maximal fumbling.

4. **Returned error strings are recorded as success.** `LiveExecutor`
   (`live_wiring.py` ~line 459): a tool that RAISES → `is_error=True`, but a tool
   that RETURNS an error string — how every forgiving-dispatch tool surfaces
   failure ("I couldn't determine when", "InvalidConsultCall", "missing
   implementation") — is wrapped `is_error=False`, `corrective_signal=""`. So
   **semantic failures are invisible to the orchestration layer**: the plan
   marks the step complete and advances, nothing retries. This is why patches
   never compound into reliability and why the agent "didn't retry" register_tool.

### The three live shapes this must make robust (regression corpus)

| Tool | Live malformed shape | Current outcome |
|------|----------------------|-----------------|
| `manage_schedule` | `["action","kind","title","due_at","timezone","notes"]` — time in `due_at` | time dropped → extractor returns "couldn't determine when" → agent asks user |
| `register_tool` | descriptor with NO `implementation` field (but `coin_flip.py` IS in the workspace) | hard-rejected; agent did not retry |
| `consult` | `harness`=the step LABEL ("s16 synchronous consult verification"), `question`=real prompt | `InvalidConsultCall`; recovered only on the agent's own next attempt |

---

## 2. Design

### 2.1 Central arg-repair seam

A single ingress function, the twin of `canonicalize_tool_name`:

```
normalize_tool_call(tool_name, args, ctx) ->
    (normalized_args, repairs: list[Repair], hard_error: str | None)
```

- Runs AFTER tool-name canonicalization and BEFORE `gate.classify_tool_effect`
  and execution.
- Dispatches to per-tool **normalizers** registered in a table keyed by canonical
  tool name. A tool with no registered normalizer is a pass-through (no-op).
- Normalizers are **value/role-based, not field-name allow-lists** (see 2.2).
- Returns `hard_error` (a clean, listed message) when the call is genuinely
  ambiguous — never guesses past ambiguity.
- Each applied repair emits a first-class `tool.args_repaired` event
  (original shape, normalized shape, confidence, reason) — mirroring
  `tool.alias_repaired`, so operators can audit new repairs and we can measure
  how often each fires (telemetry feeds future schema-tightening).

**Ingress coverage.** Name-repair is currently copy-pasted across multiple
dispatch paths (`ReasoningService.execute_tool`, `LiveExecutor`,
`LiveIntegrationDispatcher`, the plan `StepDispatcher`). To avoid the same smell,
`normalize_tool_call` MUST be invoked at the same single choke point as name
canonicalization. **Open implementation question for Codex:** is there one shared
helper both repairs can live in, or do the four paths need the call added
individually? Prefer unifying name + arg repair into one `repair_tool_call()`
entry the four paths already (or should) call.

### 2.2 Per-tool normalizers (value/role based)

**`manage_schedule`.** Stop folding from `_SCHEDULE_TIME_FIELDS`. Instead, the
normalizer (or `_extract_schedule_params` directly) receives the **raw
tool-input JSON** and:
- resolves the message text by role (description/title/message/text/...),
- scans EVERY non-empty scalar/list string value for schedule signal
  (relative time "in N hours", ISO/date/time, weekday/month, am/pm, cron,
  "tomorrow", etc.) and includes matched values,
- keeps timezone supplemental (only attached when real content exists).
- Preferred: pass the raw input to the Haiku extractor so it sees `due_at`
  without any key being named. The current failure is that the extractor only
  sees the folded description and returns "couldn't determine when"
  (`scheduler.py` ~line 572).
- **Hard-fail (clean ask), don't guess**, when there is action text but NO
  schedule signal anywhere (so a "June roadmap" note doesn't become a reminder).

**`register_tool`.** Move `implementation` inference into
`WorkspaceManager.register_tool` (NOT the pure descriptor parser). After loading
the descriptor + validating `name`, if `implementation` is absent, infer ONLY
from bounded, high-confidence workspace context, in priority order:
1. exact `<tool_name>.py`,
2. `<descriptor-stem>.py`,
3. the manifest's recorded implementation for this tool,
4. exactly ONE adjacent `.py` next to the descriptor.
If zero or multiple candidates → **hard-fail listing the candidates**. Normalize
the descriptor (write the inferred `implementation` back) BEFORE parse / hash /
authoring-validation / activation, so the existing security checks (path
traversal, non-.py, authoring-pattern scan) all still run on the inferred file.

**`consult`.** Role-based field collection in `validate_consult_input`:
- harness candidates: `harness`/`target`/`agent` (+ alias + squashed-separator map),
- question candidates: `question`/`prompt` + the longest free-text field,
- if a harness candidate is valid → use it; else if there's a clear question and
  the invalid harness value looks like a **label/step-title** (long, spaced, not
  an agent-like token) → default to `codex`;
- **HARD-FAIL** (no silent default) when the harness is an explicit *unsupported
  agent name* (e.g. `aider`, `cursor`, `perplexity`, or any short agent-like
  token not in the alias map) — defaulting there would silently run the wrong
  agent. Keep the existing missing-`question` guard untouched.

### 2.3 Orchestration-visibility fixes (higher leverage than any single tool)

**(V) Make semantic failures visible.** Tools that return an error-shaped result
must surface as `is_error=True` so the orchestration layer SEES them. Options for
Codex to weigh:
- (a) Detect error-shaped returns at the `LiveExecutor` boundary
  (`{"error": ...}` dict, or a sentinel the tools already emit) and set
  `is_error=True` + a `corrective_signal`.
- (b) Standardize forgiving-dispatch tools to return a typed
  `ToolError`/structured result the executor maps to `is_error=True`.
  Preferred long-term; (a) is the smaller first slice.
This unblocks: the plan-spine no longer completes a step over a failed tool, and
a **single bounded auto-retry** becomes possible for typed,
known-pre-side-effect validation errors (the call hasn't mutated anything yet, so
re-dispatch with the repaired args is safe). Auto-retry is a FALLBACK, not the
contract — the seam should make the first call succeed.

**(S) Feed real schemas to the planner.** Resolve each tool's real `input_schema`
into `LivePlannerCatalog` (`live_wiring.py` ~line 874) from the handler's
`_tool_descriptors` registry instead of `{}`, so the model has arg-shape framing
during plan execution. Pure improvement to first-call success; tightening schemas
is a *reducer* of fumbles, never the safety boundary (because of `strict:null`).

---

## 3. Risks & hard-fail boundaries

Aggressive value/role inference is WRONG in these cases — the seam must hard-fail
with a clean error, not guess:
- **Scheduling:** a version number, a date inside a filename, or "June roadmap"
  reads as date-like. Only create when there is BOTH action text AND a schedule
  signal; otherwise return a clean ask.
- **Consult:** silently defaulting to `codex` ignores an intended harness.
  Hard-fail for explicit unsupported agents; only default when the bad harness is
  clearly a label/non-agent string.
- **register_tool:** inferring the wrong `.py` registers unrelated/unsafe code.
  Infer only on exact/singleton confidence; else fail with the candidate list.
  The authoring-pattern security scan MUST run on the inferred file.

---

## 4. Acceptance criteria

1. The three regression-corpus shapes (§1) all succeed on the FIRST call:
   `manage_schedule` with `due_at` creates the reminder; `register_tool` with no
   `implementation` (but `<tool>.py` present) registers; `consult` with a label
   in `harness` + a real `question` dispatches to a valid harness.
2. The hard-fail boundaries (§3) each return a clean, specific error (NOT a
   guess): no-schedule-signal text, an explicit unsupported consult agent, and a
   0-or-many `.py` ambiguity.
3. A tool that returns an error-shaped result is recorded `is_error=True`; a plan
   step does NOT advance as "complete" over it; one bounded auto-retry fires for
   the typed pre-side-effect validation case and is observable in events.
4. `tool.args_repaired` is emitted with original/normalized/confidence/reason on
   every applied repair; pass-through tools emit nothing.
5. `LivePlannerCatalog` entries carry the real `input_schema` (not `{}`).
6. Existing tool-NAME alias-repair behavior is unchanged; security guards
   (path traversal, non-.py, authoring scan, gate classification) all still run.
7. `pytest` green; new tests cover each normalizer's success + each hard-fail +
   the is_error visibility + one auto-retry.

## 5. Non-goals

- Not removing the model's own retry loop (kept as a last-resort fallback).
- Not changing `strict:null` (load-bearing for the Codex provider).
- Not a schema redesign of every tool (schema-tightening is a follow-up reducer,
  tracked separately — relates to the parked tool-schema-diet item).

## 6. Sequencing (phased; front-load the highest-leverage slice)

- **Phase 0 — visibility (smallest, biggest payoff):** §2.3 (V) — make returned
  error-shaped results `is_error=True` + stop the plan-spine completing over
  them. This alone restores the agent's own correction loop.
- **Phase 1 — the seam:** `normalize_tool_call` + telemetry + ingress wiring.
- **Phase 2 — the three normalizers** (schedule raw-input, register_tool infer,
  consult role-based), each behind the seam.
- **Phase 3 — planner schemas** (§2.3 S) + one bounded auto-retry.

## 7. Open questions for Codex spec review

1. Unify name + arg repair into one `repair_tool_call()` ingress, or keep two
   functions called at the same choke point? Where is the single choke point that
   covers all four dispatch paths without re-copy-pasting?
2. Best mechanism to mark returned error-shaped results as failures without
   misclassifying legitimate `{"error": ...}`-shaped *successful* tool outputs
   (e.g. a tool that legitimately returns an `error` key in its data)? Sentinel
   type vs. dispatcher convention.
3. Should the schedule normalizer fold-and-pass, or fully delegate to passing raw
   input into `_extract_schedule_params`? Which is more robust to novel shapes?
4. Auto-retry: confirm which validation errors are provably pre-side-effect
   (safe to re-dispatch) vs. which tools may have partially acted before raising.
