# TOOL-ARG-REPAIR-V1

**Date:** 2026-06-09 (v2 — Codex r2 GREEN)
**Status:** ✅ GREEN (Codex spec-review r2, session `019ead99`) —
  implementation-ready. r1 YELLOW (4 BLOCKING) → v2 folds → r2 GREEN, no residual
  blockers. Deep-dive `019ead1e`; r1 `019ead8f`. Non-blocking implementer notes
  pinned below.

## Implementation notes (Codex r2 GREEN — non-blocking, pinned)

1. **Phase 3: actually RENDER `input_schema` in the planner prompt.** Resolving
   schemas into the catalog isn't enough — `_build_planner_user_message`
   currently prints only id/class/operation/description; it must also render the
   schema (or a compact arg summary) for the model to benefit.
2. **Preserve direct `classify_tool_effect` alias behavior for non-dispatch
   callers.** Some callers invoke the gate's name-canonicalization outside the
   four dispatch ingress points; folding name-repair into `repair_tool_call()`
   must not regress those direct callers — keep the alias path working for them.
3. **Give `ToolError` a plain-text fallback for legacy direct `execute_tool`
   consumers.** Callers that consume the tool's return as a string must still get
   a sensible message (e.g. `str(ToolError)` → its `message`), so the typed
   result degrades gracefully on the non-live paths.
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

1. **No CENTRAL arg-repair; only ad-hoc per-tool repair.** `tool_aliases.py`
   (~line 21): *"V1 only repairs the tool NAME. Argument-shape repair is out of
   scope."* Tool NAMES get one central repair (`tool.alias_repaired`). Arg repair
   is NOT absent — each tool has its own ad-hoc repair (the
   `_SCHEDULE_TIME_FIELDS` fold in `scheduler.py`; the harness recovery in
   `external_agents/tool.py`; the descriptor coercion in `tool_descriptor.py`) —
   but it's hand-enumerated **field-name allow-lists**, duplicated per tool, with
   no shared seam. Enumeration can't keep up: the model put the reminder time in a
   `due_at` field no list named.

2. **Schemas don't bind the model.** The Codex provider sends every tool with
   `strict: null` (`kernos/providers/codex_provider.py` ~line 149, load-bearing
   per the 2026-05-02 mutation matrix). So `additionalProperties: false` is
   advisory — the model freely invents fields (`due_at`, `kind`, `notes`).

3. **The plan-spine gives the model EMPTY schemas.** The self-test runs through
   the self-directed plan spine; `LivePlannerCatalog` passes `input_schema={}`
   for every tool (`kernos/kernel/integration/live_wiring.py` ~line 874). On
   exactly the path the self-test uses, the model has zero arg-shape guidance →
   maximal fumbling.

4. **Returned semantic failures are recorded as success.** A tool that RAISES →
   `is_error=True`, but a tool that RETURNS its failure (rather than raising) is
   wrapped `is_error=False`, `corrective_signal=""`. This happens in BOTH live
   dispatch boundaries — `LiveExecutor` (`live_wiring.py` ~459) and
   `LiveIntegrationDispatcher` (~676/684, which also emits
   `tool.result is_error=False`). Failures are surfaced in mixed shapes — bare
   error strings ("I couldn't determine when", "missing implementation"), **JSON
   strings** (consult returns `json.dumps(InvalidConsultCall...)` at
   `reasoning.py:1152`), and `{"ok": False, "error": ...}` dicts — and ALL are
   treated as success. JSON-string errors are especially invisible. So **semantic
   failures are invisible to the orchestration layer**: the plan marks the step
   complete and advances, nothing retries. This is why patches never compound
   into reliability and why the agent "didn't retry" register_tool. (A naive
   `dict-has-"error"` detector is therefore wrong on two counts: it misses the
   JSON-string failures, and it would mis-flag legitimately-successful outputs
   that happen to carry an `error` key — see §2.3.)

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

**Ingress coverage (RESOLVED — Codex r1).** There is NO single choke point
today: name-repair is independently duplicated in `ReasoningService.execute_tool`
(`reasoning.py:1055`), `DispatchGate.classify_tool_effect` (`gate.py:364`), and
`StepDispatcher.dispatch` (`dispatcher.py:350`); the live paths classify before
executing (`live_wiring.py:360`, `:525`). Therefore: create ONE shared
`repair_tool_call(tool_name, args, ctx)` helper that does name-canonicalization
+ arg-normalization together, and CALL it at each ingress before classification /
descriptor lookup / operation resolution:
- `LiveExecutor.execute` (before gate classification),
- `LiveIntegrationDispatcher.__call__` (before classification),
- `StepDispatcher.dispatch` (before descriptor lookup / operation resolution),
- `ReasoningService.execute_tool` (the direct-call fallback path).
Migrate the existing scattered name-repair calls into this one helper so we don't
deepen the copy-paste smell. (Unifying name+arg repair is the point — they share
the same context and the same four ingress points.)

### 2.2 Per-tool normalizers (value/role based)

**`manage_schedule`.** Stop folding from `_SCHEDULE_TIME_FIELDS`. Instead, the
normalizer (or `_extract_schedule_params` directly) receives the **raw
tool-input JSON** and:
- resolves the message text by role (description/title/message/text/...),
- scans EVERY non-empty scalar/list string value for schedule signal
  (relative time "in N hours", ISO/date/time, weekday/month, am/pm, cron,
  "tomorrow", etc.) and includes matched values,
- keeps timezone supplemental (only attached when real content exists).
- Preferred (Codex r1): pass the RAW tool-input JSON to the extractor (which today
  only sees `Description: {description}` at `scheduler.py:560`) PLUS the
  deterministically-selected message text + time candidates, so it sees `due_at`
  without any key being named. Do not rely on fold-only. The current failure is
  the extractor sees only the folded description and returns "couldn't determine
  when" (`scheduler.py:572`).
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

**(V) Make semantic failures visible (RESOLVED — Codex r1; do NOT use
`{"error":...}` detection).** A naive "dict has an `error` key" detector is wrong:
it misses consult's JSON-string failures (`reasoning.py:1152`) AND would mis-flag
a legitimately-successful tool output that carries an `error` field. Instead,
introduce a typed failure result the tools EMIT and the dispatch boundaries MAP:
- A structured `ToolError` (or `ToolFailure`) carrying `is_error=True`, `code`
  (e.g. `invalid_consult_call`, `schedule_underspecified`, `descriptor_invalid`),
  `message`, and `pre_side_effect: bool`.
- The forgiving-dispatch tools that today `return`/`json.dumps` an error
  (consult, manage_schedule, register_tool) return this typed result.
- BOTH dispatch boundaries map it: `LiveExecutor` (`live_wiring.py:453/459`) and
  `LiveIntegrationDispatcher` (`:676/684`) set `is_error=True` (+ a
  `corrective_signal`) and the `tool.result` event reflects it.
- `StepDispatcher` must then yield `StepDispatchResult.completed=False`
  (`dispatcher.py:590`) so the plan does NOT advance over a failed tool. (Today it
  advances whenever `is_error` is false; `EnactmentService` proceeds on
  `TierRouting.PROCEED` at `service.py:830`.)
- Transitional allow-lists mapping the existing string/JSON failures to typed
  results are acceptable as a bridge, but the END state is the typed result.
This unblocks the plan-spine NOT completing over a failure, and enables a single
bounded auto-retry (see auto-retry note below). Auto-retry is a FALLBACK — the
seam should make the first call succeed.

**Auto-retry safety (RESOLVED — Codex r1).** Retry ONLY errors explicitly tagged
`pre_side_effect=True`. Provably pre-side-effect (safe to re-dispatch with
repaired args): consult `InvalidConsultCall` raised BEFORE `orchestrator.consult`
(`reasoning.py:1152`), register-tool early validation BEFORE the file copy /
activation (`workspace.py:453`), and schedule extraction failure BEFORE
`_create_trigger` (`scheduler.py:660`). UNSAFE by default — never auto-retry:
anything after subprocess/external-agent/workspace/service execution, e.g.
`code_exec` may have run arbitrary code before `success=False`
(`code_exec.py:304`), project tools may partially write canvas/knowledge
(`projects.py:454`), workspace tools may have executed before `{"error":...}`
(`workspace.py:1107/1379`). Errors are unsafe unless explicitly tagged otherwise.

**(S) Feed real schemas to the planner (RESOLVED — Codex r1; source corrected).**
There is no handler `_tool_descriptors` registry. `LivePlannerCatalog` only gets
`tool_catalog` and hardcodes `input_schema={}` (`live_wiring.py:881/907`); and
`CatalogEntry` does not store `input_schema` (`tool_catalog.py:13`). Resolve
schemas at planner-catalog build time from the real sources: kernel tools via
`kernel_tool_schema_map()` (`kernel_tool_registry.py:416`); workspace tools from
their descriptor files; MCP tools from their capability schema. (Either thread a
resolver into `LivePlannerCatalog`, or extend `CatalogEntry` to carry
`input_schema` — implementer's call; the spec requires the planner sees real
schemas, not `{}`.) Pure first-call-success improvement; schema-tightening is a
*reducer*, never the safety boundary (because of `strict:null`).

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

- **Phase 0 — visibility (smallest correct slice, biggest payoff; Codex r1):**
  add the typed `ToolError` failure result; map it in BOTH live dispatch
  boundaries (`LiveExecutor` + `LiveIntegrationDispatcher`) to `is_error=True`;
  ensure `StepDispatcher` yields `completed=False` on it; tests proving the plan
  does NOT advance over a schedule / consult / register_tool validation failure.
  (Transitional allow-list mapping the existing string/JSON failures is fine
  here.) This alone restores the agent's own correction loop. NO auto-retry yet.
- **Phase 1 — the seam:** shared `repair_tool_call()` (name+arg) + `tool.args_repaired`
  telemetry + wiring at the four ingress points (§2.1).
- **Phase 2 — the three normalizers** (schedule raw-input+prefilter, register_tool
  infer-impl, consult role-based), each behind the seam, each returning typed
  `ToolError` (with `pre_side_effect=True`) on a clean hard-fail.
- **Phase 3 — planner schemas** (§2.3 S — low-risk reducer, can split earlier) +
  ONE bounded auto-retry gated on `pre_side_effect=True`.

## 7. Open questions — RESOLVED in Codex spec-review r1 (session 019ead8f)

1. **Single choke point?** None exists today; name-repair is duplicated across
   `reasoning.py:1055` / `gate.py:364` / `dispatcher.py:350` (live paths classify
   at `live_wiring.py:360/525`). → One shared `repair_tool_call()` called at the
   four ingress points in §2.1. (Folded.)
2. **Mark returned failures without misclassifying `{"error":...}` successes?**
   Do NOT detect `{"error":...}`; consult returns failures as JSON strings
   (`reasoning.py:1152`) a dict-detector misses, and success can carry `error`.
   → Typed `ToolError` (`is_error`/`code`/`message`/`pre_side_effect`), §2.3 (V).
   (Folded.)
3. **Schedule fold-and-pass vs raw-to-extractor?** Raw-input-to-extractor +
   deterministic prefilter (the extractor only sees the folded description today,
   `scheduler.py:560`). (Folded into §2.2.)
4. **Which errors are safe to auto-retry?** Only `pre_side_effect=True`: consult
   pre-`orchestrator.consult`, register-tool pre-file-copy, schedule pre-
   `_create_trigger`. Unsafe after any subprocess/external/workspace/service
   execution. (Folded into §2.3 auto-retry note.)

**Remaining for r2 confirm-GREEN:** verify the v2 folds are faithful; flag any
residual blocking issue.
