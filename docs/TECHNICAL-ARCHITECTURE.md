# KERNOS Technical Architecture Document

> **What this is:** A map of what exists today — components, data structures, data flows, and interfaces. The agent reaches this via `request_reference` (REFERENCE-PRIMITIVE-V1; the legacy direct-path `read_doc` was retired). If the code and this document disagree, fix this document.
>
> **Last updated:** 2026-06-04 (reflects: through Multi-Member V1, EXTERNAL-AGENT-CONSULTATION, CROSS_SPACE_REQUESTS, AUTO-UPDATE, WORKFLOW-TRIGGERS-CONSOLIDATION v1, KERNEL-TOOL-REGISTRY-V1, CRB bring-up, REFERENCE-PRIMITIVE-V1, `read_doc` retirement, REQUEST-APPROVAL-ACTION-V1, ASYNC-IO-CONVERSION-V1 Tier 1, LONG-HORIZON-PROJECT-V1, IMPROVEMENT-LOOP-RECOVERY-V1, improve_kernos loop hardening — git-identity / fidelity-gate / proportionality / scratch-cleanup / completion-wake, RECURSIVE-SELF-HEAL-V1, SELF-MAINTENANCE-REVIEW-V1, FRICTION-RESPONSE-V1 incl. conversational surface. Kernel tool surface = 76 — incl. the agent-callable `run_self_review` tool.)
>
> **For depth on recent substrate:**
>
> - Reference primitive (catalog + cohort + scope visibility + the seven tools): [`architecture/reference-primitive.md`](architecture/reference-primitive.md)
> - Cataloging cohort: [`architecture/cohort-cataloging.md`](architecture/cohort-cataloging.md)
> - Trigger runtime (WTC v1): [`architecture/trigger-runtime.md`](architecture/trigger-runtime.md)
> - CRB (Conversational Routine Builder): [`architecture/crb.md`](architecture/crb.md)
> - Kernel-tool registry: [`architecture/kernel-tool-registry.md`](architecture/kernel-tool-registry.md)
> - Full tool catalog: [`capabilities/tool-surface.md`](capabilities/tool-surface.md)

---

## System Overview

KERNOS is a personal intelligence kernel that serves the full breadth of one person's life — from professional work to personal projects, health to hobbies, finances to family. It receives messages via platform adapters (Discord, SMS), processes them through a template-driven agent with kernel-managed memory, and returns responses. The kernel owns all infrastructure — persistence, context assembly, capability routing, safety enforcement, and identity. The agent's only job is to think.

```
[Discord Bot]  ──┐
                  ├──→ [Message Gateway / Adapters] ──→ [Normalized Message]
[Twilio SMS]   ──┘                                            │
                                                              ▼
                                                    [Message Handler]
                                                      │    │     │
                                          ┌───────────┘    │     └───────────┐
                                          ▼                ▼                 ▼
                                  [LLM Router]      [Task Engine]      [State Store]
                                          │                                  │
                                          ▼                                  ▼
                                    [Soul + Template]        [Context Spaces (Hierarchy)]
                                                           │
                                                           ▼
                                                   [Reasoning Service]
                                                        │       │
                                        [LLM Provider]   [Retrieval Service]  [MCP Tool Calls]
```

---

## The Turn Pipeline

Every inbound message flows through six phases:

1. **Provision** — Load soul, tenant profile, initialize spaces
2. **Route** — LLM router determines target space. Downward search for query_mode. Work_mode logging for domain-specific work.
3. **Assemble** — Build system prompt (Cognitive UI), select tools (budgeted window), run Message Analyzer (combined classification + knowledge selection + preference detection). Analyzer runs in parallel with covenant query.
4. **Reason** — LLM reasoning with tool use loop. Dispatch gate evaluates write actions. Tools execute (kernel, MCP, workspace).
5. **Consequence** — Confirmation replay, projectors, soul update, cross-domain signal check, tool promotion.
6. **Persist** — Store messages, conversation log, compaction check (includes fact harvest), domain assessment, child briefings.

### Key Methods

- `_get_or_init_soul()` — Loads Soul from State Store or creates new; auto-provisions General + System spaces; migrates legacy "Daily" spaces to "General"
- `_phase_route()` — LLM router, query_mode/work_mode handling, space switching, departure context, workspace lazy registration, catalog version check
- `_phase_assemble()` — Cognitive UI block construction, budgeted tool window, Message Analyzer (classification + knowledge + preference in one call)
- `_phase_reason()` — ReasoningRequest construction, task engine execution
- `_phase_consequence()` — Post-turn processing, cross-domain signals
- `_phase_persist()` — Conversation logging, compaction trigger, domain assessment, child briefings

### Async-Safe Hot-Path File I/O

**Files:** `kernos/kernel/conversation_log.py`, `kernos/kernel/runtime_trace.py`, `kernos/messages/handler.py`

The 14 every-turn file-I/O sites from ASYNC-IO-CONVERSION-V1 Tier 1 are async-safe so synchronous disk reads/writes do not block the asyncio loop that also carries Discord gateway heartbeats. `ConversationLogger` uses `aiofiles` for log append/read/seed paths and `run_in_executor` for meta JSON helpers. `RuntimeTrace.append_turn()` wraps its mkdir/append/rotation read-write sequence in the executor and serializes it with a per-trace-path `asyncio.Lock`. The message handler uses `aiofiles` for parent-briefing reads and workspace tool descriptor reads. Tier 2/3 conversion remains deferred.

---

## 1. Routing

### Space Router

**File:** `kernos/kernel/router.py` — `LLMRouter` class

**Mechanism:** Every message is routed by a lightweight LLM call that reads the message, recent history, and the space list with hierarchy markers. No bypass — always fires, even for single-space tenants.

**RouterResult fields:**
- `tags: list[str]` — Space IDs this message belongs to
- `focus: str` — Single space ID for the agent's main focus
- `continuation: bool` — Short affirmation riding conversational momentum
- `query_mode: bool` — Quick question about another domain (stay in current space, downward search)
- `work_mode: bool` — Domain-specific work intent (route to the domain where context lives)

**Router prompt structure:**
- Unbounded life scope: "serves the full breadth of one person's life"
- HIERARCHY section: step up for broad domain content, step down for specific work, stay for universal actions
- Universal actions (calendar, time, search, memory, files) stay in current space
- Domain-specific work routes to the domain
- Cost asymmetry: staying wrong is cheap, switching wrong is expensive

**Alias resolution:** If the LLM returns an old space name, aliases are checked to resolve to the current ID.

### Downward Search

When `query_mode=True` and the router suggests a different space, the handler searches child/sibling domains for the answer without switching:
1. Router detects query_mode → identifies target domains from tags
2. Knowledge from target spaces + their children collected
3. Cheap LLM resolves the answer from collected context
4. Answer injected into current space's RESULTS block
5. User stays in current space

### Work Mode

When `work_mode=True`, the router signals intentional domain work. The handler logs `WORK_MODE:` and allows the space switch — this is real work in a specific domain, not a casual mention.

---

## 2. Context Spaces

### Hierarchy

**File:** `kernos/kernel/spaces.py` — `ContextSpace` dataclass

**Structure:** Tree. General (root default, depth 0) → Domain (depth 1) → Subdomain (depth 2). System space is a separate root plane.

**Fields:** `id`, `instance_id`, `name`, `description`, `space_type` ("general" | "domain" | "subdomain" | "system"), `status`, `is_default`, `parent_id`, `depth`, `aliases`, `posture`, `active_tools`, `local_affordance_set`, `last_catalog_version`, `renamed_from`, `renamed_at`, `created_at`, `last_active_at`

**Posture:** Working style note set by the domain assessment LLM. Injected into the NOW block for non-default spaces. Examples: "Creative and improvisational", "Precise and action-oriented".

### Space Creation: Compaction-Driven Assessment

**Sole creation path.** After compaction completes in any general or domain space (depth < 2), a cheap LLM assesses whether the compacted content constitutes a coherent domain.

- Reads the freshly produced compaction document (Ledger + Living State)
- Checks existing spaces for duplicates and drift (`_is_similar_topic`)
- HIGH confidence only — medium does NOT create
- Produces: domain space with name, description, posture, parent_id, depth, compaction state, reference-based origin document
- Also checks for explicit renames ("let's call it X") — updates name, populates aliases, sets `renamed_from`/`renamed_at`

### Space Switching

- **Departure context:** On switch, summary from departing space (6 entries, ~600-800 chars) injected into new space's context
- **Session exit:** When focus shifts away, `_run_session_exit()` fires async — reviews session via LLM, updates space name/description if content has drifted

### Scope Chain

- **Memory:** `remember()` walks UP the parent chain. Facts in a parent are visible from children.
- **Files:** `read_file` walks UP the parent chain. Local files shadow parent files with the same name.
- **Archives:** `_search_archives` walks the chain — searches current space first, then parent, then grandparent.
- **Write policy:** `write_file` defaults to current space. `target_space_id` parameter allows writing to a parent (universal updates). Non-ancestor writes rejected.

### Cross-Domain Signals

Post-turn check: if entities mentioned in the current turn have knowledge entries in OTHER domains (outside the scope chain), and the turn contains a meaningful update (status change, commitment, factual update), a signal is deposited in the target domain. Signals are one-time delivery — surfaced in RESULTS on next entry, then cleared.

### Parent Briefings

After compaction in a parent space, `_produce_child_briefings()` runs for each child. A cheap LLM extracts 3-8 bullet points of durable truths. Stored as `briefing_{child_id}.md`. Injected into child's MEMORY block during assembly. Briefings can be stale — the scope chain is the freshness valve.

---

## 3. Compaction

**File:** `kernos/kernel/compaction.py` — `CompactionService`

### Mechanism

- **Trigger:** Estimated token count of conversation log exceeds threshold (default ~8,000 tokens via `KERNOS_COMPACTION_THRESHOLD`)
- **Process:** LLM reads full conversation log → produces Ledger entry (topic index) + Living State (current operational reality)
- **Log rotation:** Old log archived as `log_NNN.txt`. New log created with seeded messages from old log.
- **Reference-based:** Compaction documents point back to source logs. Full text retrievable via `remember_details()`.

### Adaptive Seed Depth

The compaction LLM determines how many trailing messages to carry forward. Outputs `SEED_DEPTH: N` at end of response. Clamped 3-25, default 10. A creative scene might need 15-20; quick factual questions might need 3-5.

### Living State

Current operational reality. What's TRUE RIGHT NOW — active scene state, pending decisions, in-progress work, outstanding tasks. NOT a topic summary. Rewritten on every compaction cycle.

### Ledger

Append-only topic index. Each entry has topic label, date range, source log reference. Enables retrieval of exact conversation text via `remember_details(log_NNN)`. Entries are never edited or removed.

---

## 4. Tool Surfacing

### Universal Tool Catalog

**File:** `kernos/kernel/tool_catalog.py` — `ToolCatalog`, `CatalogEntry`

All registered tools with one-line descriptions. `CatalogEntry`: name, description, source ("kernel" | "mcp" | "workspace"), registered_at, plus workspace metadata (home_space, implementation, stateful). Version counter increments on every add/remove. Kernel tools registered at boot, MCP tools at server connection.

### Three-Tier Surfacing

- **Tier 1 (Common Check):** Every turn, no LLM call. All kernel tools + common MCP tools (`COMMON_TOOL_NAMES`) + preloaded MCP tools + space's `local_affordance_set` + session-loaded tools. Handles ~80% of turns. Console: `TOOL_SURFACING: tier=common`
- **Tier 2 (Catalog Scan):** Fallback when Tier 1 insufficient. Surfacer LLM scans full catalog descriptions, picks relevant tools by intent. Console: `TOOL_SURFACING: tier=catalog_scan selected=[...]`
- **Tier 3 (Promotion):** Successful uncommon tool use promotes into space's `local_affordance_set`. Next turn it's in Tier 1. Console: `TOOL_PROMOTED: tool=X space=Y`

**General bloat guard:** Domain-specific tools do NOT promote into General. Only truly universal tools (from capabilities marked `universal=True`) promote in the root space.

### Lazy Version Promotion

Each space stores `last_catalog_version`. On space entry, if `space.last_catalog_version < catalog.version`, new workspace tools are scanned for relevance to this domain via cheap LLM. Relevant tools promoted into `local_affordance_set`. Console: `TOOL_CATALOG_SCAN`.

### Preloaded Tools

All calendar tools have full schemas always in context (`PRELOADED_TOOLS`): list-events, search-events, get-event, get-freebusy, list-calendars, get-current-time, create-event, create-events, update-event, delete-event, respond-to-event. No stub schemas for these — prevents empty argument issues.

---

## 5. Cognitive UI (System Prompt)

### Block Structure

Static prefix (cacheable):
- **RULES** — Operating principles + behavioral contracts + bootstrap. Includes: DEPTH structural confidence, MEMORY, SCHEDULING, GATE, WORKSPACE guidance.
- **ACTIONS** — Connected services, tool descriptions, outbound channels.

Dynamic suffix (changes every turn):
- **NOW** — Current time, platform, auth level, space posture.
- **STATE** — Soul identity + USER CONTEXT (knowledge entries with source tags, deduplicated).
- **RESULTS** — Receipts, system events, awareness whispers, cross-domain signals, downward search answers.
- **PROCEDURES** — Domain-specific workflows from `_procedures.md` files in the scope chain.
- **MEMORY** — Compaction Living State + Ledger index + parent briefings.

### USER CONTEXT

Knowledge entries deduplicated by normalized content. Each tagged with provenance: `[stated]`, `[observed]`, `[established]`, `[remembered]`, `[recent]`, `[known]`. Entries attributing the agent's name to the user are filtered.

### DEPTH Statement

"Your context for this turn is curated — not everything you know. Deep memory, archived conversations, files across spaces, schedule data, and connected service state are all available on demand via remember() and tool calls. What's here is what matters now. When you need more, retrieve it. You are not reconstructed from summaries — you are precisely briefed for this turn with full retrieval capability behind you."

---

## 6. Memory & Knowledge

### Knowledge Entries

**File:** `kernos/kernel/state.py` — `KnowledgeEntry`

Fields: id, instance_id, content, lifecycle_archetype, context_space, confidence, source_event_id, source_description, last_referenced, tags, storage_strength, salience, foresight_signal, foresight_expires, entity_node_id, created_at, expired_at, valid_at, invalid_at.

**Lifecycle archetypes:** identity, habitual, structural, episodic, contextual, ephemeral.

### Three-Tier Injection

- **Tier 1 (Always):** Identity facts (lifecycle_archetype == "identity")
- **Tier 2 (Never):** Ephemeral, expired, stale contextual (>14 days)
- **Tier 3 (LLM-shaped):** Remaining candidates selected by cheap LLM for relevance to this turn's message

### Fact Harvest

Post-turn cohort agent. Reads conversation and extracts/updates knowledge entries. Operations: add (new), update (modify), reinforce (bump storage_strength). Fires on space departure and pre-compaction.

### Retrieval

**File:** `kernos/kernel/retrieval.py` — `RetrievalService`

- `remember(query)` — Searches knowledge entries (semantic + scope chain) + entity graph + compaction archives. Three concurrent searches via asyncio.gather. Returns formatted readable text within 1500-token budget.
- `remember_details(source_ref, query)` — Retrieves exact text from archived log file.

---

## 7. Reasoning & Tool Dispatch

### ReasoningService

**File:** `kernos/kernel/reasoning.py`

Handles the full tool-use cycle. When the LLM returns tool_use, blocks are classified as concurrent-safe (read) or sequential (write). Read-only tools execute in parallel; write tools sequentially. Up to 10 iterations before safety valve.

### Provider Chains

**Files:** `kernos/providers/base.py` (ChainEntry, ChainConfig), `kernos/providers/chains.py` (builder)

Three named chains: **primary** (main reasoning), **simple** (extraction, compaction, analysis), **cheap** (gate, routing, classification). Each chain is an ordered list of `ChainEntry(provider, model)` pairs. On failure, the next entry in the chain is tried automatically.

`build_chains_from_env()` reads `KERNOS_LLM_PROVIDER` and `KERNOS_LLM_FALLBACK` env vars and builds all three chains. The data structure is `ChainConfig = dict[str, list[ChainEntry]]` — designed so a future config loader can point at `config/providers.json` with zero consumer changes.

`_call_chain()` is the single entry point for chain fallback — used by both `reason()` (primary chain) and `complete_simple()` (simple/cheap chains). Replaces the previous duplicated fallback loops.

### Dispatch Order

1. **Kernel tools** — Intercepted before MCP. Canonical allowlists live in `ReasoningService._KERNEL_TOOLS` / `_DISPATCHABLE_KERNEL_TOOLS`; schemas live in `kernos/kernel/kernel_tool_registry.py`. Notable surfaces include memory/files, workspace, covenants/capabilities/channels, schedule, project tools (`start_project`, `record_project_decision`, `surface_project_status`), plans, members, runtime diagnostics, references, canvas, external-agent consultation, self-improvement, and git/deployment tools. (`read_doc` retired in REFERENCE-PRIMITIVE-V1.)
2. **MCP tools** — Routed via MCPClientManager.call_tool()
3. **Workspace tools** — Detected via `catalog.has_workspace_tool()`. Executed via `workspace.execute_workspace_tool()` in the tool's home space.

### Dispatch Gate

**File:** `kernos/kernel/gate.py` — `DispatchGate`

Philosophy: reactive user-requested actions (soft_write) are approved. Gate only evaluates hard_write, proactive, and third-party actions.

Steps: (0) denial limit check, (1) approval token bypass, (2) permission override, (3) reactive soft_write bypass, (4) model evaluation → APPROVE / CONFIRM / CONFLICT / CLARIFY.

Action-based tools classified by action param: manage_covenants, manage_capabilities, manage_channels, manage_workspace, manage_members, manage_plan (list/status → read, others → soft_write).

**Denial tracking (IQ-4):** 3 consecutive gate blocks on the same tool per turn → stop retrying. Reset on new turn or approval.

---

### Dispatch Reliability Stack (TOOL-ARG-REPAIR-V1 arc, 2026-06-09/10)

Four layers, each closing a verified live failure mode of model-driven tool
calls:

1. **Syntax presentation** (`kernos/kernel/tool_signatures.py`): the dynamic
   developer message ENDS with a generated `## TOOL CALL SIGNATURES` endcap —
   one compact signature per surfaced tool (required args first, `?` marks
   optional, enums inline) under the exact provider wire names, plus curated
   examples for high-fumble tools that name their anti-patterns. The Codex
   provider additionally leads every tool description with a generated
   `SIGNATURE:`/`EXAMPLE:` header. Both surfaces are generated from the same
   schemas the provider sends — no second source of truth. (Schemas alone are
   advisory: the transport requires `strict: null`.)

2. **Failure visibility** (`kernos/kernel/tool_failure.py`): `ToolFailure`
   subclasses `str` — a tool's returned failure IS its message (legacy
   consumers unchanged by construction) while both live dispatch boundaries
   (`LiveExecutor`, `LiveIntegrationDispatcher`) detect the type and record
   `is_error=True` + events/audit, so `StepDispatcher` yields
   `completed=False`. Carries `code` + `pre_side_effect` (conservative
   default False — only explicitly-tagged pre-side-effect validation errors
   are candidates for a future bounded auto-retry).

3. **Argument repair** (per-tool, value/role-based): `manage_schedule` folds
   time-bearing fields into the extraction description; `register_tool`
   coerces object-shaped `implementation` / missing `input_schema` (security
   guards intact); `consult` resolves harness by ROLE (near-miss aliases,
   prompt-in-harness, label-with-real-question → default codex) with an
   explicit unsupported-agent denylist that gates ALL recovery branches.

4. **Step-completion discipline** (`execution.verify_step_completion` + the
   spine gate in `_execute_self_directed_step`): a plan step completes when
   its NAMED actions ran, not when the turn produced text. One cheap
   strict-contract model call audits the step description against the
   agent's own report (fail-open); a named deficit re-dispatches the SAME
   step once as a CONTINUATION carrying the deficit (`plan.step_incomplete`
   event emitted; bounded; budget-gated; skipped for non-active plans —
   blocked steps are HELD: partial receipts recorded, step reset to
   `pending` for resume). `manage_plan(continue)` issued from inside a plan
   turn (conversation id `plan_<id>`) defers to the spine — the spine is the
   single dispatcher. Env-disable: `KERNOS_STEP_COMPLETION_CHECK=off`.

## 8. Agentic Workspace

### Execute Code (AW-1)

**File:** `kernos/kernel/code_exec.py`

`execute_code` kernel tool runs Python in a sandboxed subprocess. Hard security walls: clean environment (no API keys, no parent env), cwd scoped to space's files directory, PYTHONPATH restricted, no Kernos internals. Timeout default 30s, max 300s. Output budget: stdout 4000 chars, stderr 2000. Optional `write_file` parameter persists code before execution.

### Workspace Manifest (AW-2)

**File:** `kernos/kernel/workspace.py` — `WorkspaceManager`

`workspace_manifest.json` per space tracks all built artifacts. Four-layer model: Artifact → Descriptor → Surface → Store. `manage_workspace` kernel tool: list, add, update, archive. No destructive deletion.

### Tool Registration (AW-3)

`register_tool` validates `.tool.json` descriptors (name, description, input_schema, implementation) and registers in the universal catalog with `source="workspace"`. Auto-adds to manifest. Descriptor is single source of truth.

### Builder Flow (AW-4)

The agent builds tools in-conversation: `execute_code` (write + test) → `register_tool` (register) → `manage_workspace` (track). Two workspace artifact shapes: **Tools** (callable capabilities registered in catalog) and **Workspace Projects** (bodies of work: files + structure, not registered). Long-horizon projects are a separate product surface described in section 10b.

Operating principles guide build-fast-iterate: propose concrete, write code, test before presenting, register, offer to refine.

---

## 9. Procedural Knowledge

Two systems for domain-specific knowledge in the hierarchy tree. They solve different problems and remain separate:

### Covenants = How to Behave

Short behavioral rules. Auto-captured by the kernel. Space-scoped via `context_space` field on `CovenantRule`. Loaded via scope chain (current space + ancestors + global). Injected into RULES block with source attribution: `[global]`, `[Health]`, `[D&D]`.

Child-level covenants take precedence over parent-level when they conflict on the same topic.

### Procedures = What to Do

Multi-step workflows with tool references. Written to `_procedures.md` in each space's files directory. Loaded via file scope chain on space entry. Injected as a PROCEDURES section in the system prompt between RESULTS and MEMORY.

Parent procedures appear with `[From ParentName]` attribution. Local `_procedures.md` shadows same-named sections in parent.

### Capture Path

- **Behavioral rule** → covenant (auto-captured by preference parser)
- **Multi-step workflow** → agent writes to `_procedures.md` via `write_file`

The agent's operating principles include guidance on distinguishing these.

---

## 10. Self-Directed Execution

**File:** `kernos/kernel/execution.py`

For complex multi-step tasks, the agent creates a plan (`_plan.json` in workspace space) and executes it autonomously. Each step is a full turn through the pipeline via `continue_plan` kernel tool.

### Plan Structure

JSON plan with phases, steps, budget ceilings (max_steps, max_tokens, max_time_s), usage counters, discoveries list. Markdown view (`_plan.md`) auto-generated on save.

### Execution Flow

1. Agent creates plan, saves via `save_plan()`
2. Calls `continue_plan(plan_id, step_id, step_description)` at end of turn
3. Handler reads plan, checks budgets, builds `ExecutionEnvelope`, enqueues self-directed turn
4. Turn runs through full pipeline with `is_self_directed=True` on `TurnContext`
5. Self-directed turns skip: preference detection, cross-domain signals
6. Budget ceiling hit → plan paused, user decides continuation
7. User messages always interrupt — priority over plan steps

### Discovery Surfacing

`notify_user` parameter on `continue_plan` sends progress/discoveries to user's channel. Plan discoveries list tracks findings across steps.

---

## 10b. Long-Horizon Projects

**Files:** `kernos/kernel/projects.py`, `kernos/kernel/state.py` (`ProjectState`), `kernos/kernel/state_sqlite.py` (`project_state`), `kernos/kernel/canvas.py`, `kernos/kernel/scheduler.py`, `kernos/messages/handler.py`

Long-horizon projects are first-class bindings over existing primitives, not a separate memory substrate. A project row binds a `ContextSpace`, a pinned canvas, `project_decision` knowledge entries, and best-effort scheduler check-in fields.

### Project Tools

- `start_project(name, initial_note="", checkin_cadence="weekly")` creates a domain `ContextSpace`, creates a canvas with `CanvasService.create(..., pinned_to_spaces=[space_id])`, seeds `index.md`, `overview.md`, `decisions.md`, `timeline.md`, `open-loops.md`, and `next-steps.md` with `CanvasService.page_write()`, inserts `project_state`, switches the active space, and best-effort creates a plain reminder through `handle_manage_schedule(action="create", ...)`.
- `record_project_decision(project_id="", decision, subject="")` resolves the explicit project or active-space project, appends a dated entry to `decisions.md`, appends a timeline line when possible, and writes a `KnowledgeEntry(category="project_decision", tags=["project:<id>", "space:<id>", "project_decision"])`. Canvas and knowledge writes are sequential best effort; knowledge failure returns `partial=True`.
- `surface_project_status(project_id="")` assembles compact status from `project_state`, recent `project_decision` knowledge, canvas summaries for timeline/open loops/next steps, and stored reminder fields.

### Command Surface

`/project start "Name"`, `/project status [name-or-project-id]`, `/project list`, and `/project complete [name-or-project-id]` are handled in `MessageHandler._handle_project_command()`. Completion marks `project_state.lifecycle_state='completed'`, records completion fields, and best-effort removes the stored check-in trigger.

---

## 11. Improvement Loop — Behavioral Self-Improvement

**File:** `kernos/kernel/behavioral_patterns.py`

The agent improves itself through covenants and procedures without touching source code. Three connected mechanisms:

### Behavioral Pattern Detection

Post-turn, the friction observer tracks recurring user corrections. Four pattern types with thresholds: format_correction (3), workflow_correction (3), boundary_correction (2), preference_drift (2). Correction fingerprints (first 80 chars, normalized) are accumulated in `data/{instance}/state/behavioral_patterns.json`.

When threshold is met, a whisper is generated proposing a covenant or procedure. Proposals are classified:
- **behavioral** → propose covenant (tier="situational" via Pass 1 selective injection)
- **workaround** → flag as SYSTEM_MALFUNCTION (don't paper over code bugs)
- **uncertain** → surface both options to user

User approves → covenant/procedure created. User declines → not re-proposed (resets after 3 more occurrences).

### Covenant Selective Injection

`tier` field on CovenantRule: "pinned" (always loaded) or "situational" (loaded when MessageAnalyzer deems relevant). Zero additional LLM calls — MessageAnalyzer's schema expanded with `relevant_covenant_ids`.

### System Malfunction Whispers

When the friction observer detects SYSTEM_MALFUNCTION signals (schema errors, provider errors, empty responses), an informational whisper is generated so the user knows something went wrong. Diagnostic reports still written to `data/diagnostics/friction/`.

---

## 11b. Improvement Loop Tier 2 — Spec-Driven Code Improvement

### Runtime Trace Log

**File:** `kernos/kernel/runtime_trace.py`

Per-tenant JSONL ring buffer (200 turns) capturing structured events: provider errors, tool failures, gate decisions, timing, plan lifecycle, covenant injection. Agent reads via `read_runtime_trace` kernel tool.

### Diagnostic Tools

**File:** `kernos/kernel/diagnostics.py`

Three tools for the agent to investigate and propose fixes:
- `diagnose_issue` — gathers runtime trace + source + friction evidence, LLM synthesizes diagnosis
- `propose_fix` — writes structured spec to `data/{instance}/specs/proposed/`. Protected boundary check blocks gate/auth/credentials/security.
- `submit_spec` — moves proposed → submitted, generates whisper notification

### /debug Command

Discord slash command: `/debug friction`, `/debug trace`, `/debug specs`. Ephemeral output for developer visibility.

### Operational Safety & Self-Monitoring (2026-06-01)

The autonomous `improve_kernos` loop commits + pushes + redeploys real changes, so it is wrapped in a defense-in-depth + self-observation stack:

- **Deploy:** `improve_kernos` runs each spec/impl review round as an ACPX consult; on author+reviewer convergence it pauses at the **human approval gate** (`awaiting_commit_approval`) — nothing commits/goes live without an explicit yes. Review iteration caps: spec 8, impl 6 (`KERNOS_IMPROVEMENT_{SPEC,IMPL}_ITERATION_MAX`). Improvement consults get an explicit 1800s timeout (the orchestrator default is 600s; the loop overrides via `KERNOS_IMPROVEMENT_CONSULT_TIMEOUT_SEC`).
- **Auto-update** (`kernos/setup/self_update.py`): interval-poll mode (`KERNOS_AUTO_UPDATE_INTERVAL_SEC`, default 600s) checks origin and, when *safe* (no recent inbound activity, no in-flight attempt), pulls + restarts to apply pushes with no manual `/restart`. interval=0 falls back to the legacy daily window.
- **Boot-guard auto-rollback** (`kernos/setup/boot_guard.py`): an applied update is marked `.update_pending` on probation; `start.sh` runs `boot_guard pre-launch` and, after `KERNOS_BOOT_GUARD_MAX_ATTEMPTS` failed boots, `git reset --hard`s to `.last_known_good` (promoted only on a clean `on_ready`) and drops a `.rollback_notice` surfaced as a whisper. `start.sh` is **off-limits to autonomous modification** (it can't roll back itself).
- **Self-monitoring stall surface** (`find_stalled_improvement_attempts` + a server poller): in-flight attempts with no ledger progress past `KERNOS_IMPROVEMENT_STALL_THRESHOLD_SEC` (default 720s) surface a whisper so a hung consult is reported instead of going silent. Read-only; never aborts the attempt.
- **Terminal notify:** on reaching the approval gate, on any abort, *and on success completion*, a whisper tells the user in the agent's own voice (no more silent terminal states). The success case is its own path: a landed run deploys via restart, so the in-process notify can't fire for `completed` — instead `MessageHandler.inject_improvement_completed_wake()` (fired via the orchestrator's `completed_wake_fn`) wakes the origin space *after* the post-restart self-test passes, so a clean end-to-end run announces itself rather than going quiet (founder ask: "shouldn't it acknowledge it's complete?").
- **ACPX step-trace** (`ACPX_STEP` log lines): structural coding-agent events are logged as they stream, so a stalled dispatch's last log line shows where it hung — zero token cost.
- **ACP orphan reaper** (`reap_orphaned_acp_agents`): `codex-acp`/`claude-acp`/`gemini-acp` leaves that `setsid` out of the dispatch tree + reparent to systemd are swept at boot (age-guarded > `MAX_TIMEOUT_SECONDS`, so live consults are never touched).

#### Loop hardening (2026-06-04)

Five reliability/fidelity fixes folded after the first live end-to-end runs (test #178 onward):

- **Git-identity injection** (`kernos/kernel/git_operations.py` `_commit_identity_args`): pull-only deploy clones often have no `user.name`/`user.email`, so `git commit` died with "Author identity unknown." The commit path now injects a fallback Kernos author via per-command `git -c user.name=… -c user.email=…` flags **only** when the worktree has no identity of its own (env-overridable: `KERNOS_GIT_AUTHOR_{NAME,EMAIL}`). This was the root-cause blocker that unstuck the loop.
- **Request-fidelity gate** (`improvement_review_protocol.py` + `improvement_loop_workflow.py` `spec_requirement` threading): the operator's *original* request is injected into the **final** code review so the reviewer can return `NEEDS_REVISION` on a fidelity gap — the diff must fulfill what was actually asked, not the drift-prone relayed `spec.md`. The split is deliberate: **latitude on HOW, fidelity on WHAT**. (Caught a live wrong-deliverable: a `humanize_duration` request that drifted into an unrelated `ensure_terminal_newline` change.)
- **Proportionality:** review prompts now match scrutiny to change size (`_SHARED_FRAMING`), to stop 6-round gold-plating of trivial diffs.
- **Scratch-pollution cleanup** (`_unstage_loop_scratch` + `_LOOP_SCRATCH_ARTIFACTS = ("spec.md", "impl_notes.md")`): the loop's own scratch files are never committed to `main` — unstaged before the commit step and swept from prior soak debris. Keeps landed commits to the actual deliverable.
- **Completion-wake:** see the amended Terminal-notify bullet above (`inject_improvement_completed_wake`).

### Post-restart recovery (IMPROVEMENT-LOOP-RECOVERY-V1)

A landed improvement deploys by restart, so the loop's last act is a **post-restart self-test** (`handler.py` post-restart gate). The full terminal flow is `landed → self-test → {done | recover | abandon}`:

- **Pass →** the completion-wake fires (above) and the attempt closes `completed`.
- **Fail →** the loop enters `awaiting_recovery_decision` and `MessageHandler.inject_improvement_recovery_wake()` wakes the agent with the failure detail, prompting a choice between two tools: `proceed_with_recovery` (attempt a bounded fix-up — **at most two** approval-gated fix-up commits, tracked via `recovery_iteration`) or `abandon_attempt` (roll back and close). `recovery_reason` classification records *why* recovery was entered.

This is distinct from the recursive-self-heal lane below: recovery handles a *landed* change that failed its own self-test (the operator-driven loop's own terminal branch); self-heal handles a *mid-attempt* abort caused by a bug in the loop machinery itself.

### Recursive self-heal lane (RECURSIVE-SELF-HEAL-V1 — default-off)

When an attempt aborts on a bug in KERNOS's *own* loop machinery (not a hard task / weak agent output), the supervisor can spawn ONE bounded child repair, verify it deterministically, and surface it. The design **is** the guardrails — "a bounded recovery lane, not a self-rewrite mandate." Inert unless `KERNOS_RECURSIVE_SELF_HEAL` is truthy (default OFF for v1). Module: `kernos/kernel/recursive_self_heal.py`; hook: `ImprovementLoopOrchestrator._maybe_self_heal` on the impl-cycle abort path.

- **Classifier** (`classify_failure`): every machinery signature needs a deterministic POSITIVE symptom **and** a NEGATIVE guard (a task failure can mimic a signature) — default is `task_failure` (never recurse). Signature #5 (worktree dirty-state invariant) requires the worktree be *objectively* dirty via an independent `git status --porcelain` read; a false-GREEN over a pristine worktree is correctly a task failure. A recognized class with no hermetic verifier + canonical fix is **propose-only**, never auto-run (`can_auto_run`). v1 ships #5 only.
- **Durable runaway bound** (`improvement_attempt_edges` table, `reserve_child_repair`): DB-enforced `UNIQUE(parent, recursive_repair)` (one child per parent) + `UNIQUE(root, signature, fingerprint)` (never repeat a fix per root) + `child_depth ≤ 1` checked **globally against `root_attempt_id`**. The child edge is reserved **transactionally before** any spawn, so a restart cannot launder depth (in-memory counters could).
- **Constitutional boundary** (`touches_constitutional_path`): a verified child repair that touches any guardrail file (orchestration / approval / dispatch / `external_agents/**` / the lane itself / `start.sh` / boot-guard / guardrail tests) routes to **human review** even when it passes — never auto-applied. The diff is listed via `_changed_files_incl_untracked` (porcelain) so an untracked guardrail file can't slip past.
- **Hermetic verification** (`verify_worktree_dirty_state`): a fresh temp git fixture with an untracked file, asserting the change detector sees it — a deterministic pass/fail, never a model judgment, no live LLM/gateway.
- **Honest v1 resume:** a verified repair is not yet committed/deployed, so the parent can't transparently resume on the still-buggy running process — the lane surfaces the verified child repair for the normal approval+commit+deploy path rather than fabricate a silent continuation. The supervisor owns every state transition; the child runs synchronously (no background loop) with `recursion_disabled`.

### Self-stewardship: two shapes (Shape A default-ON, Shape B default-off)

KERNOS has two distinct self-stewardship elements, deliberately separate — one reflective, one reactive. Both surface to the System space (admin) for *consideration*; every actual change still flows through approval-gated `improve_kernos`. They share only **operational governors** — a single maintenance mutex (`MessageHandler._remediation_lock`, max-concurrent remediation = 1 across both + recursive-heal), receipts, and the surface.

- **Shape A — Daily creative review (SELF-MAINTENANCE-REVIEW-V1).** Once a day reviews ONE rotating slice of its own code through a corrective lens (drift/decay) and a generative lens (a better way? does it still serve the whole?), with binding evolution discipline (≤1 minor reversible idea). V2/V3 made it comprehensive: a **42-element functional map** covering every module (single-owner), **signal-promoted selection** (recent friction/churn promotes the most-relevant element) with a **rotation-floor** coverage guarantee, a **coverage-gap** check that flags new unmapped modules, and an **improvement docket** of lived "this could be better" moments (opportunity-class friction notes) worked during downtime. The maintenance machinery itself is in the map, flagged constitutional → human-gated. `kernos/kernel/self_maintenance_review.py`; on-demand via owner `/selfreview [section]` (forces a run bypassing the daily gate). Also exposed to the agent as the **owner-gated, reflection-only `run_self_review` kernel tool** (pinned in `ALWAYS_PINNED`), so KERNOS *knows* it can run a real self-review and actually does it when the owner asks in plain language — rather than disclaiming the capability. Both the slash command and the tool share one core (`MessageHandler._run_self_review_now`), and both render the real run through KERNOS's own voice (`_render_selfreview_voice`) with a deterministic constitutional/health guard. **Default-ON** as of SELF-MAINTENANCE-REVIEW-V3 (reflection-only, idle-aware, ~one bounded LLM call/day); disable the background daily loop with `KERNOS_SELF_MAINTENANCE_REVIEW=0`. The on-demand tool/slash run works regardless.
- **Shape B — Immediate friction response (FRICTION-RESPONSE-V1).** `kernos/kernel/friction_response.py` + `MessageHandler._run_friction_response_loop`. A short-interval sweep (default-off, idle-aware) responds to the most-pressing eligible *open friction* (the lived-error reports): gate → diagnose (`diagnose_issue`) → surface a diagnosis to consider → close the loop (recurs ⇒ failed + remembered; quiet *with real detector activity* ⇒ resolved + shadow-archived by signature). Binding guards: a **two-key memory** — `friction_signature` (what the problem is, stable across all three report naming conventions) + `resolution_fingerprint` (what was tried) — with the anti-loop rule *never repeat a failed fingerprint for the same signature*; a **self-friction denylist** (content-detected) + **atomic in-flight reservation** (no feedback loop / reentrancy); per-signature cooldown + daily budget; idle ≠ resolved. **Verification states** (`PENDING_VERIFICATION` → `resolved` / `recurred_failed` / `unknown_no_observation`) gate archival: `verify_and_archive` only marks resolved after a post-deploy observation window, and `archive_resolved_signature` files the manifest by signature. Enable: `KERNOS_FRICTION_RESPONSE`. (Also fixed: the friction *readers* in `diagnostics.py`/`server.py` globbed only `FRICTION_*.md` and silently missed the timestamp-prefixed reports.)
    - **Conversational surface (§3A-i).** The human surface follows KERNOS's simplicity principle: when input is needed, KERNOS asks **once**, in plain language, with a single acceptable answer to move forward — no back-and-forth, no special commands. A plain "yes" binds to exactly **one** pending fix via `authorize_natural_yes(...)`, which enforces same-user + same-space + direct-reply-or-bare-next-turn + affirmative (`is_affirmative`, conservative: ≤4 words, no negation) + exactly-one-pending-in-space. The authorization is a **single-use** pending-fix grant (`PENDING_FIX_KIND="friction_fix_authorization"`, `ASK_TTL_SECONDS=900`) layered on the durable REQUEST-APPROVAL-ACTION-V1 receipt (which preserves the commit-binding: parent SHA + expected diff hash). The fail-closed **auto-without-asking** allowlist is **OFF in v1** (ask-once is the only active path). A mandatory durable audit receipt records every authorization. (Surface principle: operator gets receipts, user gets one sentence + one answer.)

### Daily self-maintenance review (SELF-MAINTENANCE-REVIEW-V1→V3 — default-ON)

A daily self-stewardship pass: once a day KERNOS reviews ONE element of its own code through two lenses and surfaces a reflection to itself **to consider** — reflection, not autonomy. **Default-ON** (V3): runs unless `KERNOS_SELF_MAINTENANCE_REVIEW` is explicitly in `{0,false,off,no}`. Module: `kernos/kernel/self_maintenance_review.py`; wired via `MessageHandler._run_self_maintenance_loop` (started per-instance alongside the AwarenessEvaluator, idle-aware, ~one bounded LLM call/day). Selection (V2): which element each day is **signal-promoted with a rotation floor** (`select_slice` — recent friction/churn + a hard `COVERAGE_MAX_DAYS` cap so nothing starves), tunable via `KERNOS_SMR_*`. `KERNOS_SMR_INSTANCE_ALLOWLIST` restricts the loop on multi-instance hosts.

- **Two lenses** (`build_review_prompt`): **corrective** (drift/decay/unguarded edge vs the documented intention) and **generative** (a more efficient/effective way, AND does this function's validity + role still hold up against the overarching intention of the whole system?). **Evolution discipline** is binding: ≤1 minor, reversible, serves-the-whole idea per review, enforced again at the parse boundary; honest-when-healthy and honest-when-nothing-to-evolve.
- **Comprehensive map, nothing exempt (V3):** **42 functional elements** covering all ~311 modules (single-owner: most-specific path wins). Per-element `last_reviewed` coverage state replaces the V1 cursor. The set **includes the maintenance machinery itself** — `self-maintenance-methodology`, `self-healing`, `governing-intention`, `approval-receipts`, `improvement-loop`, `boot-deploy-bringup` are flagged **constitutional**: ponderable, but any evolution is human-gated (surfaced to the founder, never self-applied). A **coverage-gap** check (`shape_fingerprint`) surfaces any new module not yet on the map so it self-completes.
- **Improvement docket (V3):** the daily review also works open **opportunity-class** friction notes (`open_opportunities` — lived "this could be better" moments captured by the pure friction observer: better-method-on-retry, deferred-capability), folded into what it surfaces. These skip the reactive Shape-B loop + all escalation.
- **Surface = the System space** (admin surface) as a reflection to consider; every real change still flows through approval-gated `improve_kernos`. **Dedup** (14-day TTL) committed only after a successful surface, so a failed whisper never buries a concern. A **parse failure** isn't counted as a clean review (no cursor advance). Per-review **JSONL audit receipts**. The live consult is a single **bounded** completion (`load_bounded_source` caps lines/file + total + files/dir, then `reasoning.complete_simple`), idle-aware (defers when any space has queued turns).

---

## 11c. Workflow Loop Primitive

Background-execution substrate for trigger-driven workflows that compose multiple actions, audit each step, and pause for approvals when configured. Sits on top of `event_stream`'s post-flush hook (no parallel event substrate) and dispatches into the existing canvas / tool / workshop / presence surfaces (no new world-effect machinery).

Shipped under SPEC-WORKFLOW-LOOP-PRIMITIVE (April 2026, 7 commits).

### Substrate composition

```
event_stream emit ─→ writer flush ─→ post_flush hook
                                          │
                            trigger_registry evaluates predicates
                                          │
                                  match → match_listener
                                          │
                            execution_engine enqueues WorkflowExecution
                                          │
                                  worker task runs action sequence
                                          │
                                  action_library verbs ─→ existing surfaces
```

### Modules

- `kernos/kernel/workflows/predicates.py` — canonical AST + evaluator (eq / contains / exists / in_set / time_window / actor_eq / event_type_starts_with / correlation_eq + AND/OR/NOT). Deterministic; no LLM at evaluation.
- `kernos/kernel/workflows/trigger_registry.py` — `Trigger` + `TriggerRegistry`. Subscribes to `event_stream`'s post-flush hook. SQLite tables: `triggers` + `trigger_fires` (idempotency). Multi-tenancy by `instance_id`.
- `kernos/kernel/workflows/workflow_registry.py` — `Workflow` + `Bounds` + `Verifier` + `ApprovalGate` + `ActionDescriptor` + `TriggerDescriptor` + `WorkflowRegistry`. SQLite table `workflows` (descriptor blob). Validation: bounds REQUIRED, verifier REQUIRED, gate_ref must resolve, `auto_proceed_with_default` requires `default_value` AND no irreversible downstream action (safe-deny).
- `kernos/kernel/workflows/descriptor_parser.py` — three loaders (.workflow.yaml / .workflow.json / .workflow.md with YAML frontmatter). Sharing-constraint enforcement: instance-specific values must be parameterised (`{installer.<name>}`) or guarded by `instance_local: true`.
- `kernos/kernel/workflows/trigger_compiler.py` — DSL parser (`event.payload.kind == "report"`, AND/OR/NOT, parens) + injectable English compiler.
- `kernos/kernel/workflows/action_classification.py` — verb reversibility lookup powering safe-deny.
- `kernos/kernel/workflows/action_library.py` — bounded set of verbs:
    - World-effect (action-loop instances, covenant-gated, with verifiers): `notify_user`, `write_canvas`, `route_to_agent`, `call_tool`, `post_to_service`.
    - Receipt/gate bridge: `request_approval` (`RequestApprovalAction`) creates a durable approval receipt bound to the current workflow execution and gate nonce.
    - Direct-effect (structural assertions only): `mark_state`, `append_to_ledger`.
- `kernos/kernel/workflows/agent_inbox.py` — `AgentInbox` Protocol + `InMemoryAgentInbox` (test/dev) + `NotionAgentInbox` (production stub). `route_to_agent` raises `AgentInboxUnavailable` when no provider is bound.
- `kernos/kernel/workflows/execution_engine.py` — `ExecutionEngine` + `WorkflowExecution`. Single asyncio queue, one worker task, sequential per-instance dispatch. Synthetic CohortContext built from trigger event + active spaces. Approval-gate semantics: action FIRST → pause AFTER → wait → resume; timeout per gate descriptor. Restart-resume reads `running` rows from SQLite, re-enqueues if next action is `resume_safe`, else aborts with `aborted_by_restart`.
- `kernos/kernel/approval_receipts.py` — durable approval receipts (REQUEST-APPROVAL-ACTION-V1). A reusable primitive *and* the workflow gate's backing store. **State machine:** `pending → approved → consumed` (plus `rejected` / `expired`), schema-enforced. `request_approval(...)` takes a `binding_payload`, `ttl_seconds` (default 86400 — durable, not the 5-min ephemeral kind), and `single_use` (default True); `find_pending_by_binding_field()` does an exact-match lookup on a `binding_payload_json` field — the API the friction-response conversational surface uses to find the one pending fix outside the workflow engine. In the workflow context, `find_terminal_by_binding()` returns the latest terminal receipt for `(instance_id, workflow_execution_id, gate_nonce)` and normalizes consumed receipts to approved decisions for recovery.
- `kernos/kernel/workflows/refs.py` — workflow reference resolver. `_STEP_SCOPES` includes `approval_outcome` so downstream branches can read `{step.<step_id>.approval_outcome.<field>}`.
- `kernos/kernel/workflows/ledger.py` — `WorkflowLedger`. Append-only markdown file at `data/{instance_id}/workflows/{workflow_id}/ledger.md`. Cross-instance path-isolation pin.
- `kernos/kernel/webhooks/receiver.py` — FastAPI `register_routes(app, registry)`. POST `/webhooks/{source_id}` with HMAC or bearer auth, optional schema validator, translates validated bodies to `event_stream.emit("external.webhook", ...)`.

### Request Approval Gate Action

Workflow descriptors can declare `action_type: request_approval` with a `gate_ref`. `RequestApprovalAction.execute()` calls `approval_receipts.request_approval()` using the engine-provided `_workflow_execution_id` and `_gate_nonce`; the engine then pauses on the referenced `ApprovalGate`. `_await_gate()` first installs the waiter, checks `find_terminal_by_binding()` for a terminal receipt, and otherwise waits for an `approval.decision_recorded` event matching the gate predicate plus execution id and nonce.

Gate release is fail-closed for approval events. `_clear_gate_and_advance()` maps the event to `approval_outcome` (`approved`, `decision`, `approval_id`, `decided_at`, `decided_by_actor`, `rejection_reason`), verifies the terminal receipt through `_approval_decision_event_has_terminal_receipt()`, and merges the outcome into the requesting step's existing `workflow_step_outputs` envelope in the same transaction that clears the nonce and advances the cursor. Approved single-use receipts are consumed best-effort after the cursor advances.

### Audit events

Emitted to `event_stream` with shared `correlation_id` per execution:

- `workflow.execution_started`
- `workflow.execution_step_succeeded` / `workflow.execution_step_failed`
- `workflow.execution_paused_at_gate` / `workflow.execution_resumed`
- `workflow.gate_receipt_short_circuited` / `workflow.gate_receipt_multi_terminal` / `workflow.gate_receipt_lookup_failed`
- `workflow.gate_auto_proceeded` / `workflow.owner_escalation`
- `workflow.execution_terminated`

### Authoring workflows

Operators author `.workflow.yaml` (or `.json` / `.md`) descriptors:

```yaml
workflow_id: morning-briefing
instance_id: inst_a
name: Morning briefing
version: "1.0"
owner: owner
bounds:
  iteration_count: 1
  wall_time_seconds: 30
verifier:
  flavor: deterministic
  check: briefing_delivered
action_sequence:
  - action_type: notify_user
    parameters:
      channel: primary
      message: Good morning.
      urgency: low
trigger:
  event_type: time.tick
  predicate: 'event.payload.cadence == "daily"'
```

Register via `await workflow_registry.register_workflow_from_file(path)`. Atomic across `workflows` + `triggers` SQLite tables in a single transaction.

### Composition with shipped primitives

- **EVENT-STREAM-TO-SQLITE:** EXTENDED with post-flush hook in C1; the existing event taxonomy carries new `workflow.*` event types; multi-tenancy by `instance_id` preserved.
- **ACTION-LOOP-PRIMITIVE:** EXTENDED. World-effect verbs ARE action-loop instances (intent-satisfaction verifiers). Direct-effect verbs are structurally asserted only — Anti-Goal compliance.
- **Cohort architecture:** Synthetic CohortContext-equivalent at execution start; world-effect verbs consult covenant via injected `covenant_gate` callable.
- **Notion-independence:** the workflow primitive ships with no Notion dependency. Vendor-specific imports / URLs / tool namespaces are confined to `NotionAgentInbox` in `agent_inbox.py`. Structural test in `tests/test_workflows_integration.py::TestNotionLeakWholeSpec` enforces.

---

## 12. Awareness & Scheduling

### Awareness Evaluator

**File:** `kernos/kernel/awareness.py`

Background task. Evaluates proactive insights ("whispers") on a timer (default 1800s). Whispers surfaced in RESULTS block. User dismisses via `dismiss_whisper` tool.

### Scheduler / Triggers

**File:** `kernos/kernel/scheduler.py`

`manage_schedule` creates time-based and event-based triggers. Time-based: cron-like or one-shot. Event-based: calendar event monitoring. Event sources: currently calendar only.

Long-horizon projects use the existing scheduler rather than a project-specific reminder loop. `start_project()` creates a plain check-in reminder through `handle_manage_schedule(action="create", ...)` and stores `checkin_trigger_id` / `next_checkin_at` on `project_state` when available. `/project complete` removes the stored trigger best-effort through `handle_manage_schedule(action="remove", ...)`.

---

## 13. State Storage

### SQLite Backend

**File:** `kernos/kernel/state_sqlite.py`

`SqliteStateStore` implements the `StateStore` ABC using SQLite + WAL mode. One database per instance (`data/{instance}/kernos.db`). Hybrid storage: frequently queried fields as indexed columns, rest in JSON overflow blob. Selectable via `KERNOS_STORE_BACKEND=sqlite` env var. `JsonStateStore` remains as fallback.

`project_state` lives in the per-instance database and stores `project_id`, `owner_member_id`, `space_id`, `canvas_id`, `name`, lifecycle fields, activity timestamps, check-in reminder fields, completion fields, and JSON overflow data. `insert_project_state()` validates that the referenced `ContextSpace` exists. Lookup methods include `get_project_state()`, `get_project_state_by_space()`, `list_active_projects()`, `mark_project_completed()`, and `update_project_activity()`.

### Instance Database

**File:** `kernos/kernel/instance_db.py`

Shared database (`data/instance.db`) for cross-instance state: members, member_channels, message_relay (V2), shared_spaces (V2). Nearly empty in V1 — just the owner as a member. Architectural slot for multi-instance without a second migration.

---

## 13b. Member Identity & Multi-Member

**File:** `kernos/kernel/instance_db.py`, `kernos/kernel/members.py`, `kernos/kernel/soul.py`

### The Model

One Kernos instance, many members. "Kernos" is the platform name, not the agent's identity. Each member hatches their own agent with its own name, personality, and relationship. The Soul dataclass is retained for JSON compat but all identity fields are per-member.

### Per-Member Soul

Agent identity lives in `member_profiles`: agent_name, emoji, personality_notes, hatched, hatched_at, plus relationship fields (display_name, timezone, communication_style, interaction_count, bootstrap_graduated). The instance-level Soul dataclass has all fields deprecated — kept for backward compat only.

**Hatching mode** (instance config, stored in platform_config): `unique` (default) — each member hatches their own agent from scratch. `inherit` — new members get a copy of the first member's agent identity.

**Graduation criteria**: display_name + agent_name + interaction_count. The agent naming IS the hatching moment.

### Member Profile Lifecycle

On invite claim: profile seeded with display_name. On first turn: profile auto-created if missing. Owner migration: Soul per-user fields copied to owner's profile on first boot.

### Per-Member Context

- **NOW block**: "Speaking with: {name} ({role})" — identifies current member each turn
- **STATE block**: Member's name and communication style, not the owner's
- **Knowledge**: `query_knowledge(member_id=X)` filters to own entries + unowned legacy
- **Covenants**: `member_id` field on CovenantRule. Instance-level (spirit) stays shared
- **Conversation logs**: Keyed to (instance, space, member). Lazy migration from legacy paths
- **Compaction**: Per-(instance, space, member). Same engine, member-scoped
- **Spaces**: `member_id` on ContextSpace. Each member has own General space. Router filters by member
- **Bootstrap**: Per-member prompt + graduation. Members with known names skip the name question

### Resolution Flow

Every incoming message is resolved to a member_id via instance.db before entering the handler pipeline. Known senders (platform + channel_id in member_channels table) → full pipeline. Unknown senders → static rejection, zero LLM calls.

### Invite Code System (KERN-XXXX)

One mechanism, three use cases: new user registration, existing user connecting a new platform, and spam rejection. Codes are one-time-use with configurable expiry (default 72h). `manage_members` kernel tool: invite, connect_platform, list, remove.

### Bjork Dual-Strength Memory

Knowledge entries ranked by `compute_retrieval_strength()` before the MessageAnalyzer sees them. Storage strength grows with compaction REINFORCE (user re-confirms a fact). Retrieval strength decays over time modulated by archetype (identity=730 days, ephemeral=1 day). Entries below 0.10 strength filtered. Entries touched on injection get reinforcement_count bumped.

### Follow-Up Tracking

Compaction extracts implicit follow-ups (FOLLOW_UPS section): USER_COMMITMENT, AGENT_COMMITMENT, EXTERNAL_DEADLINE, FOLLOW_UP. Creates triggers with `source="compaction_follow_up"`. Deduped against existing triggers. 90-day horizon cap.

### Whisper Hardening

Dedup by foresight_signal (no duplicate pending whispers). 48-hour expiry (stale whispers auto-expire). Busy-state suppression (non-interrupt whispers deferred during active plan execution).

---

## 13c. Platform Adapters

Three adapters: Discord (event-driven via discord.py), SMS (Twilio polling), Telegram (Bot API long polling). All follow the same BaseAdapter pattern — `inbound()` converts to NormalizedMessage, `send_outbound()` delivers responses. Adapters are dumb pipe; identity and authorization live in the handler.

### Invite Codes (Platform-Locked)

Codes are KERN-XXXX format, one-time use, platform-locked at generation. A code for Discord rejects on Telegram/SMS. The `manage_members` tool returns the code AND platform-specific instructions. If the platform isn't connected (no adapter registered), setup instructions are returned instead.

### Platform Identity Discovery

Each adapter discovers its public-facing identity on startup (Telegram: `getMe` → bot username, Discord: `client.user`, SMS: phone from env) and persists to the `platform_config` table in `instance.db`. `get_invite_instructions()` interpolates the actual handle into invite instructions — `@my_bot` instead of "find the Kernos bot."

Adapter development methodology: `docs/ADAPTER-GUIDE.md`.

### Secure Credential Input for Adapters

Extends the existing `secure api` flow (built for MCP capabilities) to platform adapter tokens. `SecureInputState` has two modes: `capability` (MCP key → secrets dir) and `platform` (adapter token → .env).

`_PLATFORM_CREDENTIALS` maps each platform to its primary env var, label, and whether it supports the paste flow. Telegram supports paste (single token). SMS requires manual .env (multiple credentials). Discord deferred to its own spec.

When setup instructions surface for a paste-capable platform, the agent is given three options to present: (1) paste via `secure api`, (2) manual .env edit, (3) cancel. On paste, `_write_env_var()` updates .env and sets `os.environ`, then `_start_platform_adapter()` hot-starts the adapter without a restart (currently implemented for Telegram).

---

## 14. Capabilities & MCP

### Connected Servers

- **google-calendar** — 13 tools (all preloaded with full schemas)
- **brave-search** — 2 tools (brave_web_search, brave_local_search)
- **web-browser** — 7 tools (in-tree Playwright-backed: goto, markdown, links, evaluate, semantic_tree, interactiveElements, structuredData; see `docs/architecture/browser.md`)

### Capability Registry

**File:** `kernos/capability/registry.py`

`manage_capabilities` — list, enable, disable MCP servers. `request_tool` — load a specific tool not in the current set (last resort).

---

## 14b. External Agents (ACPX Integration)

Kernos talks to other CLI agents (Claude Code, Codex, Gemini) through the [Agent Client Protocol](https://agentclientprotocol.com/) via [openclaw/acpx](https://www.npmjs.com/package/acpx), a headless ACP CLI. The bespoke per-CLI subprocess wrangling that v1 shipped with is now collapsed behind one dispatch boundary.

### Dispatch path

**File:** `kernos/kernel/external_agents/acpx_adapter.py`

`dispatch(target, prompt, session_id, workspace_dir, timeout_seconds) -> ConsultResult` — single async entry point. One-shot mode uses `acpx <agent> exec <prompt>`; multi-turn uses `acpx <agent> sessions ensure --name <id>` then `acpx <agent> -s <id> <prompt>`. NDJSON parsing accumulates `session/update` events with `sessionUpdate == "agent_message_chunk"`; completion fires on JSON-RPC `result.stopReason` or process exit with rc=0+text.

Session IDs are substrate-derived: `derive_session_id(instance_id, target, member_id, conversation_id)` returns a deterministic 16-char SHA-256 prefix — same coordinates always thread to the same ACPX session.

### Harness shims

**Files:** `kernos/kernel/external_agents/harnesses/{claude_code,codex,gemini}.py`

Thin compatibility wrappers. `consult()` calls `acpx_adapter.dispatch(target=self.name, prompt=_compose(question, context), session_id=session_id, workspace_dir=str(workspace_dir), timeout_seconds=timeout_seconds)`. Health-check still surfaces binary presence for the bring-up log. The legacy `_hex_to_uuid` and `_parse_codex_jsonl` helpers remain importable for back-compat consumers.

### Bridge watcher

**File:** `kernos/kernel/external_agents/bridge_watcher.py`

Two background loops launched in `server.py` startup:

- **Outbound** — polls `data/<instance>/coding_session_bridge/requests/` every 2s. For each new request: atomic `O_CREAT|O_EXCL` claim on `<req>.processing` sentinel, JSON-load request, dispatch via `acpx_adapter`, write response atomically to `…/responses/<req>.json` via tmp+rename. Closes the `ask_coding_session` relay gap so the Kernos agent can consult external CLIs without the user routing prompts by hand.
- **Inbound** — polls `data/<instance>/cc_inbox/`, dispatches read-only handlers, writes to `cc_outbox/`. Lets external CLI clients (CC in another session, Codex via ACPX, scripts) ask Kernos to introspect itself.

Supported inbound kinds:

| Kind | Behavior |
|------|----------|
| `free_text` | echoes `params.prompt` (smoke probe) |
| `inspect_state` | returns `data_root` path + existence flags |
| `read_file` | reads `data/<instance>/<path>`; rejects path traversal |
| `list_files` | lists directory entries; rejects path traversal |
| `sqlite_query` | runs SELECT/PRAGMA only; INSERT/UPDATE/DELETE/DROP rejected with `ValueError`. Limit clamps row count. |

Stale-lock recovery probes pid liveness via `os.kill(pid, 0)` and reclaims after `_STALE_LOCK_TTL_SEC`. Concurrent watchers on the same request resolve atomically via `O_CREAT|O_EXCL` — exactly one wins.

### Bring-up

**File:** `kernos/server.py` (after zombie-reaper registration)

- `is_acpx_available()` probes the `acpx` binary, logs `AGENT_PROTOCOL_AVAILABLE: acpx=<ver> (expected=<ver>)` or `AGENT_PROTOCOL_UNAVAILABLE: <reason>`.
- `KERNOS_ACPX_AUTO_INSTALL=1` opts into `npm i -g acpx@<EXPECTED_ACPX_VERSION>` at startup; default is fail-loud (don't silently install).
- `KERNOS_ACPX_VERSION` overrides the pinned version.
- Both watcher loops launched via `asyncio.create_task(...)` with `instance_id` from `KERNOS_INSTANCE_ID` (default `"default"`). Wrapped in try/except — `BRIDGE_WATCHER_LAUNCH_FAILED` log on failure; bot startup never blocked.

### Why ACP (vs. bespoke subprocess wrangling)

The plugin-subagent dispatch path through Claude Code's `codex:rescue` hangs in some environments — broker connects but stream events don't reach completion. ACPX bypasses that entirely by speaking ACP directly to the agent process. Background: <https://bighatgroup.com/blog/using-acp-with-openclaw-to-prevent-agent-hangs/>.

---

## 12. Identity & Covenants

### Soul

**File:** `kernos/kernel/soul.py`

Fields: agent_name, emoji, personality_notes, communication_style, user_name, bootstrap_graduated. Mutable via `update_soul`. Bootstrap graduation after sufficient interaction + user knowledge established.

### Behavioral Contracts (Covenants)

**File:** `kernos/kernel/covenant_manager.py`

Automatically captured from user behavioral instructions. Types: MUST, MUST NOT, PREFERENCE, ESCALATION. Managed via `manage_covenants` tool (list, remove, update). Infrastructure-level enforcement — agent thinks, kernel enforces.

---

## 13. Friction Observer

**File:** `kernos/kernel/friction.py`

Post-turn cohort agent. Detects friction signals and writes diagnostic reports to `data/diagnostics/friction/`.

**Active signals:**
- EMPTY_RESPONSE — Agent returned nothing to a non-empty message
- TOOL_REQUEST_FOR_SURFACED_TOOL — Agent requested a tool already available
- STALE_DATA_IN_RESPONSE — Time query without authoritative source (suppressed when NOW block provides time)
- GATE_CONFIRM_ON_REACTIVE — Gate blocked a reactive action
- SCHEMA_ERROR_ON_PROVIDER — Provider schema validation failure
- MERGED_MESSAGES_DROPPED — Multiple merged messages but very short response
- PREFERENCE_STATED_BUT_NOT_CAPTURED — Preference-shaped language missed by parser
- TOOL_AVAILABLE_BUT_NOT_USED — Trigger/reminder query without manage_schedule
- PROVIDER_ERROR_REPEATED — Multiple provider errors in one turn

---

## 14. Platform Adapters

Handler never knows about adapters. Adapters never know about the handler. All communication through NormalizedMessage.

- **Discord** — Primary interface. Full send/receive.
- **SMS (Twilio)** — Send/receive via polling.

---

## 15. Persistence

### State Store

**File:** `kernos/kernel/state_json.py` — JSON files in `data/` directory

Per-tenant: `profile.json`, `soul.json`, `knowledge.json`, `contracts.json`, `preferences.json`, `triggers.json`, `entities.json`, `identity_edges.json`, `spaces.json`, `space_notices.json`

Per-space compaction: `state.json`, `active_document.md`, `index.md`, `archives/`, `briefing_{child_id}.md`

Per-space files: `files/` directory with `.manifest.json`

Per-space workspace: `workspace_manifest.json`

### No Destructive Deletions

Shadow archive architecture. `delete_file` preserves files in `.deleted/`. Knowledge entries set `active: false`. Covenant rules set `superseded_by`. Nothing is permanently destroyed.

---

## 16. Standing Principles

- Conservative by default, expansive by permission
- Memory as the moat — trust earned through thousands of correct small actions
- Ambient, not demanding
- No destructive deletions — shadow archive architecture
- Every piece of state keyed to instance_id from day one
- Handler never knows about adapters; adapters never know about the handler
- Infrastructure-level enforcement — agent thinks, kernel enforces
- Subtraction principle — removal > structural enforcement > simplification > addition
- Provider neutral — no load-bearing features on specific LLM capabilities
- LLM routing over algorithmic fingerprinting
