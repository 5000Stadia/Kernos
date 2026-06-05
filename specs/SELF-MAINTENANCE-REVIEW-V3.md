# SELF-MAINTENANCE-REVIEW-V3 — Comprehensive functional map + improvement docket

**Status:** Draft (for Codex spec review)
**Builds on:** SELF-MAINTENANCE-REVIEW-V2 (signal-promoted selection + targeting, already
implemented), FRICTION-RESPONSE-V1 (the friction observer + report substrate).
**Modules:** `kernos/kernel/self_maintenance_review.py`, `kernos/kernel/friction.py`,
`kernos/messages/handler.py`.

## Why

V2 gave the review a good *engine* (signal-promoted pick, rotation floor, on-demand
targeting) but ran it over only the 11 hand-curated slices — ~11% of the codebase. Two
upgrades make the daily review trustworthy and content-driven:

1. **Comprehensive functional map** — replace the 11 slices with an intention-defined map
   that spans *all* of KERNOS, and a coverage-gap check so it can't silently fall behind as
   the system grows.
2. **Improvement docket** — capture lived "this could be better" moments (a clumsy retry, a
   future-need aside) as *opportunity*-class friction notes, and let the daily review work
   them during downtime. Background self-improvement, content-driven.

Non-goals: changing the two-lens method, the evolution discipline, the constitutional
human-gating, or the friction observer's pure-sink contract.

## Part A — Comprehensive functional map

Replace the hand-coded `REVIEW_SLICES` with a **functional-element map**: each element is a
conceptual "what this is FOR" capability (subsystem-grain, not per-file), with `name`,
`intent` (the documented intention the review judges against), `paths`, `constitutional`.
~40 elements covering all ~311 modules. Selection (V2) is unchanged — one element/day,
signal-promoted, coverage floor — now over the full map.

Element list (name — intent — primary paths; `🔒` = constitutional / human-gated):

- `message-pipeline` — six-phase turn pipeline + handler orchestration + slash commands — `messages/handler.py`, `messages/pipeline.py`, `messages/phases/`, `messages/phase_context.py`, `messages/models.py`, `messages/reference.py`
- `message-adapters` — platform adapters (Discord/Telegram/SMS), handler-isolated — `messages/adapters/`, `sms_poller.py`, `telegram_poller.py`
- `reasoning` — tool loop, provider chains, kernel-tool dispatch, cost logging — `kernel/reasoning.py`, `providers/chains.py`, `kernel/exceptions.py`, `kernel/turn_runner.py`, `kernel/turn_runner_provider.py`
- `providers` — provider-agnostic model backends + model routing — `providers/base.py`, `providers/anthropic_provider.py`, `providers/codex_provider.py`, `providers/ollama_provider.py`, `models/`, `kernel/model_routing.py`
- `cognitive-context-assembly` — the typed cognitive substrate + 7 Cognitive-UI zones + response delivery — `kernel/cognitive_context/`, `kernel/response_delivery.py`
- `context-routing` — message→space routing, candidates, per-space evidence — `kernel/router.py`, `kernel/space_candidates.py`, `kernel/space_evidence.py`, `kernel/spaces.py`
- `dispatch-gate` — action-based tool classification + scoped amortization + binding diagnostics — `kernel/gate.py`, `kernel/dispatch_diagnostics.py`, `kernel/tools/operation_resolver.py`
- `stewardship-compaction` — compaction + boundary fact harvest (values, tensions, sensitivity) + token accounting — `kernel/compaction.py`, `kernel/fact_harvest.py`, `kernel/tokens.py`, `kernel/token_estimator.py`
- `knowledge-retrieval` — the memory moat: retrieval, entity resolution, dedup, embeddings — `kernel/retrieval.py`, `kernel/resolution.py`, `kernel/entities.py`, `kernel/dedup.py`, `kernel/embeddings.py`, `kernel/embedding_store.py`, `kernel/note_this.py`
- `projectors` — post-response tier1/tier2 extraction coordinator — `kernel/projectors/`
- `awareness` — whispers + suppression, ambient not demanding — `kernel/awareness.py`
- `reference-primitive` — cataloging cohort, hash-validated injection, baked catalog — `kernel/reference/`
- `canvas` — scoped markdown pages, wiki-link index, Gardener — `kernel/canvas.py`, `kernel/canvas_reference_index.py`, `kernel/gardener.py`, `cohorts/gardener.py`, `cohorts/gardener_prompts.py`, `kernel/cohorts/gardener_cohort.py`, `setup/seed_canvases.py`
- `multi-member-identity` — per-member profiles, hatching, member mgmt, display names, Soul shim — `kernel/members.py`, `kernel/soul.py`, `kernel/display_names.py`, `kernel/conversation_log.py`
- `relationships-covenants-disclosure` — relationships, permission profiles, covenants, disclosure gate, preference reconcile, messenger stewardship — `kernel/covenant_manager.py`, `kernel/contract_parser.py`, `kernel/disclosure_gate.py`, `kernel/preference_parser.py`, `kernel/preference_reconcile.py`, `kernel/cohorts/covenant_cohort.py`, `cohorts/messenger.py`, `cohorts/messenger_prompt.py`, `cohorts/admin.py`
- `member-coordination` — relational messaging + parcels + cross-space request dispatch — `kernel/relational_messaging.py`, `kernel/relational_dispatch.py`, `kernel/relational_tools.py`, `kernel/parcel.py`, `kernel/cross_space/`
- `event-stream` — append-only timeline, runtime trace, log buffer — `kernel/events.py`, `kernel/event_stream.py`, `kernel/event_types.py`, `kernel/runtime_trace.py`, `kernel/log_buffer.py`
- `state-store` — State Store (JSON/SQLite), instance.db, shadow archive — `kernel/state.py`, `kernel/state_json.py`, `kernel/state_sqlite.py`, `kernel/instance_db.py`, `persistence/`
- `task-engine` — Task model + engine + execution + protocols — `kernel/engine.py`, `kernel/task.py`, `kernel/execution.py`, `kernel/protocols.py`
- `introspection-dump` — "what Kernos believes" views for /dump — `kernel/introspection.py`
- `tool-catalog-registry` — universal catalog + canonical registry + schemas/aliases/audit/introspection — `kernel/tool_catalog.py`, `kernel/kernel_tool_registry.py`, `kernel/tools/`, `kernel/tool_aliases.py`, `kernel/tool_audit.py`, `kernel/tool_introspection.py`, `kernel/tool_gate_routing.py`
- `workshop-tool-primitive` — tool-making: descriptors, runtime enforcement, authoring validation — `kernel/tool_descriptor.py`, `kernel/tool_runtime.py`, `kernel/tool_runtime_enforcement.py`, `kernel/tool_validation.py`, `kernel/services.py`, `kernel/self_admin_tools.py`
- `capability-registry` — three-tier capability graph + MCP client — `kernel/capabilities.py`, `kernel/channels.py`, `capability/`
- `capability-install-bus` — CRB proposal/approval flow + SubstrateTools install/query facade — `kernel/crb/`, `kernel/substrate_tools/`
- `workflows` — trigger-driven background workflows (registry, engine, action library, ledger) — `kernel/workflows/` (excl. self-improvement helpers, see `improvement-loop`)
- `triggers-scheduler` — unified time+event trigger runtime + scheduler + webhooks — `kernel/triggers/`, `kernel/scheduler.py`, `kernel/webhooks/receiver.py`
- `drafts-primitive` — persistent conversational workflow drafts (WDP) — `kernel/drafts/`
- `cohorts-and-drafter` — cohort fan-out substrate + the Drafter cohort — `kernel/cohorts/` (descriptor/registry/runner/redaction/_substrate/synthetic), `kernel/cohorts/drafter/`, `kernel/cohorts/memory_cohort.py`
- `four-layer-cognition` — PDI enactment + integration prep + agent/inbox registries — `kernel/enactment/`, `kernel/integration/`, `kernel/agents/`
- `external-agents-consult` — consult + external-agent harnesses + ACPX bridge — `kernel/external_agents/`, `kernel/coding_session_bridge.py`
- `builders-codeexec` — agentic workspace + sandboxed build/exec + file service — `kernel/workspace.py`, `kernel/code_exec.py`, `kernel/builders/`, `kernel/sandbox_preamble.py`, `kernel/files.py`
- `mcp-integrations` — concrete MCP tools (Notion/Drive) + browser MCP server — `kernel/integrations/`, `browser/`
- `credentials` — provider + per-member credential resolution, OAuth, onboarding CLI — `kernel/credentials.py`, `kernel/credentials_member.py`, `kernel/credentials_cli.py`, `kernel/oauth_device_code.py`
- `projects-long-horizon` — long-horizon project tools (space+canvas+workflow) — `kernel/projects.py`
- `friction-and-diagnostics` — friction observer + response loop + patterns + gateway/dispatch health — `kernel/friction.py`, `kernel/friction_response.py`, `kernel/friction_patterns.py`, `kernel/pattern_heuristics.py`, `kernel/diagnostics.py`, `kernel/gateway_health.py`, `kernel/behavioral_patterns.py`, `setup/seed_friction_patterns.py`
- `🔒 approval-receipts` — durable approval receipts + fix authorization (human-gating record) — `kernel/approval_receipts.py`, `kernel/fix_authorization.py`
- `🔒 improvement-loop` — autonomous spec→impl→approval→commit→deploy→verify + ledger/workspace/git/self-test/closure — `kernel/improvement_loop_workflow.py`, `kernel/improvement_review_protocol.py`, `kernel/improvement_ledger.py`, `kernel/improvement_workspace.py`, `kernel/git_operations.py`, `kernel/self_test_gate.py`, `kernel/closure_store.py`, `kernel/workflows/self_improvement_helper.py`, `kernel/workflows/user_initiated_improvement_helper.py`, `kernel/workflows/loop_health_helper.py`, `kernel/workflows/closure_tools.py`, `kernel/workflows/autonomy_tools.py`, `kernel/workflows/autonomy_emitters.py`
- `🔒 self-healing` — bounded recovery lane: classify, runaway bound, constitutional guard, hermetic verify — `kernel/recursive_self_heal.py`
- `🔒 self-maintenance-review` — HOW KERNOS reviews+evolves itself (this system) — `kernel/self_maintenance_review.py`
- `🔒 governing-intention` — operating principles, identity, hatching, conservative-by-default — `kernel/template.py`
- `🔒 boot-deploy-bringup` — setup, boot-guard rollback, self-update, bring-up, entrypoints (boot-guard/self-update are human-gated) — `setup/`, `server.py`, `cli.py`, `chat.py`, `repl.py`, `utils.py`
- `evals-soak` — substrate-fidelity eval + soak harnesses — `evals/`, `soak.py`

The authoritative map (full paths) lives as the `REVIEW_SLICES` literal in
`self_maintenance_review.py`; this list is its summary.

### Path semantics + single-owner assignment (Codex spec review)

`paths` entries are **exact files** or **directory prefixes** (ending `/`) only — no globs
(matching the existing `_path_matches`). Overlap is resolved by a **single-owner
assignment** built once at load: each module is attributed to exactly one element — the one
with the **most-specific** matching path (an exact file beats a dir prefix; a longer prefix
beats a shorter; ties break by `REVIEW_SLICES` order). So `improvement-loop`'s explicit
`workflows/self_improvement_helper.py` wins over `workflows`' `kernel/workflows/` prefix;
`dispatch-gate`'s explicit `tools/operation_resolver.py` wins over `tool-catalog-registry`'s
`kernel/tools/` prefix; the covenant/gardener cohort files win over a bare `kernel/cohorts/`.
Churn/friction signal and the coverage scan both use this single assignment, so no module is
double-counted. A load-time invariant (asserted in tests) verifies **every** substantive
module resolves to exactly one element (no unassigned, no ambiguous-by-design overlaps left
unresolved).

### Coverage-gap check (self-completing map)

State gains two fields: `shape_fingerprint` (last scanned) and
`gap_surfaced_fingerprint` (the shape for which a gap was last surfaced). The fingerprint is
a stable hash of the set of substantive `kernos/**/*.py` module paths (`__init__.py`
excluded). On each daily tick (and on `/selfreview`), recompute the fingerprint:
- record it as `shape_fingerprint` on every successful scan (cheap; tracks "what we last
  saw");
- compute `unassigned` = modules whose single-owner assignment is empty (no element's
  `paths` match);
- if the fingerprint changed **and** `unassigned` is non-empty **and** it differs from
  `gap_surfaced_fingerprint`, surface ONE coverage-gap note to the System space — "N modules
  aren't in the functional map yet: …; which element do they belong to?" — and set
  `gap_surfaced_fingerprint` **only on a successful surface** (so a failed notification
  re-surfaces next tick rather than being silently marked seen);
- never auto-regroup (intention is semantic — a human/agent slots new code in). Best-effort;
  a fingerprint/scan failure never breaks the review.

This is *structural* (file set), so ordinary content edits don't trigger it — only adds/
removes. A new internal tool (a new module) shows up here once, gets slotted, done.

## Part B — Improvement docket (opportunity-class friction)

The friction observer (`kernel/friction.py`) **stays a pure write-only sink** — no feedback
loop to the agent (existing contract). It gains the ability to record *opportunity* notes
(suboptimal-but-worked / future-need), distinct from *error* notes:

- Friction reports gain a **class**: `error` (default — the existing behaviour) or
  `opportunity`. Encoded in the report front-matter and detectable by readers (back-compat:
  a report with no class is `error`).
- **New opportunity detectors:**
  - `better_method_on_retry` — within one turn, a tool attempt **failed** and a *different*
    tool then **succeeded and its result was used**. Detected from the **per-turn drained
    tool-calls trace** (`ctx.tool_calls_trace`, whose entries carry a `success` flag —
    `kernel/enactment/dispatcher.py`), NOT `RuntimeTrace.read(filter="tool")` (which only
    records failure event names). Note: "Y succeeded where X failed — consider making Y the
    default here." Conservative: requires failure → *different*-tool success in the same turn
    plus evidence the successful result was used — never a plain same-tool retry.
  - `deferred_capability_request` — a user message expresses a future/deferred need
    (high-precision phrasing: "down the road", "eventually", "at some point", "i'll need",
    "someday", "later we", paired with a build/tool/capability cue). Note: "Future capability
    requested: …". Conservative keyword+cue gate to avoid noise.
- Enrichment goes **into the note** (what was tried, what worked, the suggested default), not
  out to the agent live. Same `friction_signature` + report substrate.

### Two lanes, one substrate (class-aware wiring — Codex spec review)

A `report_class(report_text) -> "error" | "opportunity"` parser reads the report front-matter;
**a class-less report is `error`** (back-compat with every existing report).

- **Observer write path stays a pure sink, and opportunity notes skip escalation.** When the
  observer writes an `opportunity` report it ONLY writes/enriches the file — it does **not**
  go through `friction.py::_classify_and_record` (the active-occurrence / threshold-crossing /
  autonomy-emit path). That path is for errors that escalate; routing opportunities through it
  would create a feedback path and undermine "wait for Shape A." (Pure-sink contract intact.)
- **Shape B (friction-response, reactive) processes `error`-class only.** `list_open_signatures`
  (and the grouping/`respond_once` path) filter out `opportunity`-class reports up front, so an
  opportunity is never diagnosed/surfaced/auto-fixed reactively — it waits for Shape A. (Tests.)
- **Shape A (daily review) reads open `opportunity` notes as content.** After the slice review,
  it folds the most-relevant open opportunities (recency + whether they touch the reviewed
  element's assigned paths) into what it surfaces. Critically, opportunity content is reflected
  in **both** the surfacing predicate (`has_anything_to_say` becomes opportunity-aware) **and**
  the rendered text (`to_whisper_text` includes the folded opportunities) — so a healthy-slice
  review with open opportunities still surfaces them rather than silently dropping them. They
  also continue to promote their area via the V2 churn/`_friction_scores` signal (intentional:
  promote *and* address).
- **Close the loop:** an opportunity archives-by-signature (existing `friction_resolved/` shadow
  archive) when `improve_kernos` ships the corresponding change or the owner says it's handled.
  Opportunity signatures get their own dedup/anti-loop handling, separate from error signatures.

A read-only System-space **docket view** renders the open opportunities so the owner can see the
backlog; storage remains the friction substrate (no parallel primitive).

## Part C — Enablement

Flip `KERNOS_SELF_MAINTENANCE_REVIEW=1` in the live `.env` (code default stays OFF).
`KERNOS_SMR_INSTANCE_ALLOWLIST` available for multi-instance hosts. `start.sh` untouched.

## Acceptance criteria

1. The functional map covers every substantive `kernos/**/*.py` module, and each resolves to
   **exactly one** element via most-specific single-owner assignment (load-time invariant:
   no unassigned module, no double-counted module).
2. Selection (V2) works unchanged over the full map: signal promotion, coverage floor,
   targeting by any element name, unknown-target lists valid names. Churn/friction scoring
   uses the single-owner assignment (a changed file promotes exactly one element).
3. Coverage-gap check: `shape_fingerprint` is recorded on every successful scan;
   `gap_surfaced_fingerprint` updates **only after a successful surface**. Adding a new module
   under no element surfaces exactly one gap note; it does not re-surface on the next tick once
   surfaced; a **failed** surface re-tries next tick (not marked seen); a content-only edit does
   not trigger it; scan failure doesn't break the review.
4. A friction report can be written `class=opportunity`; readers default a class-less report
   to `error` (back-compat).
5. `better_method_on_retry` is detected from `ctx.tool_calls_trace` (per-entry `success`),
   **not** `RuntimeTrace.read`; it fires only on a failure → *different*-tool success whose
   result was used in the **same turn**. A plain success, a same-tool retry, or a
   failure-with-no-successful-alternative does not produce an opportunity note.
6. `deferred_capability_request` fires on the high-precision future-need phrasing + a
   capability cue, and not on ordinary future-tense chatter.
7. The friction-response (Shape B) `list_open_signatures`/grouping path filters out
   `opportunity`-class reports — they are never diagnosed/surfaced/auto-fixed reactively.
8. The daily review folds open `opportunity` notes into what it surfaces, biased to the
   reviewed element + recency; opportunity content is reflected in **both**
   `has_anything_to_say` and `to_whisper_text` (a healthy slice with open opportunities still
   surfaces them); addressed opportunities archive by signature.
9. The friction observer remains a pure sink — opportunity reports are written/enriched only
   and do **not** pass through `_classify_and_record` (no active-occurrence / threshold /
   autonomy-emit); no new feedback path to the agent.
10. Default-off preserved at the code level; only the live `.env` enables the daily loop.
