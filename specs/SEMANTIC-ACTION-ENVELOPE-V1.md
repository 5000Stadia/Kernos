# SEMANTIC-ACTION-ENVELOPE-V1 (namespaced tool catalog)

**Status:** DRAFT for Codex review
**Date:** 2026-06-06
**Origin:** v1 self-test bug loop — recurring dotted-tool hallucinations, now mature corpus.
**Supersedes consideration:** the "A-plus / envelope" candidate parked in
[[project_tool_call_schema_redesign_verdict]] pending corpus maturity.

## 1. Why now

Across the v1 self-test loop the model reliably called tools with a dotted
`domain.verb` shape we don't have: `external_agent.consult`, `files.write_file`,
`planning.manage_plan`, `member_management.manage_members`,
`repository_inspection.report`. Four of six self-test bugs were this exact shape.

Asked to introspect, KERNOS diagnosed the source precisely (not its catalog,
which is correctly flat, but the *surrounding presentation*):
- a provider-injected `multi_tool_use` wrapper whose schema says **"the format
  must be `<tool_name>.<function_name>`"**;
- `## Namespace: functions` / `## Namespace: multi_tool_use` headers;
- our own dotted internal labels (`surfaced: cognitive_context.tool_surface`);
- "grouped by capability area" + domain section headers sitting beside the
  callable tools.

Its summary: *"The catalog says flat names; the presentation environment says
namespaces and domains exist everywhere around you. I blurred them."*

**Design principle (founder, 2026-06-06):** a consistent hallucination is design
signal — adopt the better shape, don't just repair to the existing one
([[feedback_hallucination_as_design_signal]]). The model's entire environment is
namespaced (provider meta-tools, MCP `mcp__server__tool`, our domain groupings);
our flat snake_case catalog is the *outlier*. The model is correctly
generalizing its world. So the better end-state is a namespaced catalog, not flat
names defended forever by a repair layer.

## 2. Hard constraint that shapes the design

Provider function-calling APIs restrict tool/function names. OpenAI (KERNOS's
gpt-5.5 path via the Codex provider) enforces `^[a-zA-Z0-9_-]{1,64}$` — **dots
are invalid in a function name.** Therefore:

- The model's literal `.` reflex **cannot** become a canonical function name. It
  can only be (a) **repaired** at dispatch (already shipped this loop:
  registry-aware dotted-suffix canonicalization), or (b) expressed with a
  provider-valid separator.
- The provider-valid separator that *already reads as a namespace to the model*
  is `__` (double underscore) — exactly the MCP convention it sees every turn
  (`mcp__claude_ai_Notion__notion-fetch`).

So "adopt namespacing" concretely means: **present canonical tool names as
`area__tool` (double-underscore), matching the MCP convention**, not literal
dots. Dotted `.` calls remain a repaired alias, never a presented form.

## 3. Design

Namespacing is a **presentation skin over a stable flat canonical**. Internally
the dispatch/gate/audit/alias layers keep keying on the existing flat names
(`write_file`, `manage_plan`); only the model-facing schema list and the accepted
input forms change. This keeps blast radius small.

### 3.1 Namespace taxonomy
Reuse the existing capability-area grouping in `tool_introspection._AREA`
(already maps each kernel tool to an area). Proposed namespaces: `files`,
`memory`, `planning`, `members`, `external`, `canvas`, `references`, `schedule`,
`channels`, `admin`, `git`, `improvement`, `diagnostics`. Each kernel tool gets
exactly one namespace (single-owner, like the self-maintenance map).

### 3.2 Presentation
When assembling the model-facing tool list, render each kernel tool's `name` as
`{namespace}__{tool}` (e.g. `files__write_file`, `planning__manage_plan`,
`external__consult`). Descriptions unchanged. MCP tools already carry their
`mcp__service__tool` shape — leave as-is (the catalog now visually matches them).

### 3.3 Dispatch (dual-accept)
Accept all of: the namespaced canonical (`files__write_file`), the bare flat name
(`write_file`, back-compat), and the dotted hallucination (`files.write_file`,
repaired). The canonicalizer already handles dotted→flat; extend it to strip a
leading `area__` / `area.` prefix to the flat canonical when the suffix is a
known tool. Internal execution stays on the flat canonical.

### 3.4 Adopt better argument shapes (parallel, same principle)
Fold in the arg-shape signal too: the file tools' field is `name`, but the model
reliably reaches for `path`. Make `path` the canonical schema field (clearer,
conventional), keep `name` as an accepted alias (already resolved by
`_resolve_file_name`). Audit other tools for similar "model prefers X" arg
signals from the alias-repair corpus.

## 4. What this is NOT
- Not a deep rename of internal tool ids (flat canonical stays the source of
  truth — gate classification, receipts, `_KERNEL_TOOLS`, tests untouched).
- Not literal dotted function names (provider-invalid).
- Not removing the repair layer — it stays as the safety net for novel shapes.

## 5. Phasing
- **Phase 1:** namespace taxonomy + `area__tool` presentation + dual-accept
  dispatch + `path` arg adopt. Low risk (presentation + input-normalization
  only). This is the bulk of the UX win.
- **Phase 2 (optional, defer):** richer envelope (e.g. `gate_intent` field,
  structured action envelope) if Phase 1 evidence shows residual need. Keep
  parked unless corpus argues for it.

## 6. Open questions for Codex
1. **Separator:** is `area__tool` the right call given the dot is provider-
   invalid? Or is the shipped dotted-repair + flat presentation already
   "good enough," making a presentation change low-value churn?
2. **Token cost:** namespacing every tool name adds tokens to the (already
   large) tool list each turn. Worth it?
3. **Hidden flat-name assumptions:** any model-facing or validation path that
   assumes the function name equals the flat tool id (function-calling schema
   validators, the `multi_tool_use` wrapper, tool-choice forcing, surfacing/
   affordance bookkeeping keyed on name)?
4. **MCP normalization:** leave MCP as `mcp__…` or unify under the same scheme?
5. **De-dot of internal labels** (`cognitive_context.tool_surface`): under a
   `__` namespaced catalog, is de-dotting our own labels still useful, or moot?
   (Founder: do it only if it still has a function post-redesign.)
6. **Is presentation-skin the right depth,** or does single-owner namespacing
   want to be the real catalog identity (deeper but cleaner)?

## 7. Acceptance (Phase 1)
- Model-facing tool list shows `area__tool` names; descriptions intact.
- Dispatch accepts namespaced, flat, and dotted forms → same handler.
- `path` accepted as canonical file arg; `name` still works.
- All existing tests green (they use flat names → dual-accept covers them).
- A self-test loop run shows the dotted/namespaced reflex now lands first-try
  (alias-repair receipts for `*.tool` drop toward zero).
- **Guidance follows the syntax.** Whatever name shape we land on, the
  system-prompt tool-syntax guidance in `template.py` is updated to teach it
  exactly (today it says "flat snake_case, no dots"; under `area__tool` it must
  teach the `area__tool` convention with examples). The guidance must never lag
  the established syntax — a stale instruction is itself a confusing signal
  (founder, 2026-06-06). This is a hard Phase-1 deliverable, not a follow-up.
