# The Gardener — Canvas Shape Authority

> A bounded cohort whose judgment space is canvas shape and nothing else. Picks the initial shape at canvas creation, runs continuous-evolution heuristics as content accumulates, proposes reshapes when the shape drifts from what the work needs.

## The problem

Canvas v1 gave members a shared-state primitive: scoped directories of markdown pages with YAML frontmatter, routes, state transitions, section markers. All the machinery works. But it doesn't keep itself well-shaped.

Members don't want to design review upfront. Asked *"which of these eighteen patterns is your work?"* — most people want to do the work, not the meta-work. Asked *"how should we reorganize this page that's become 10,000 lines?"* — they usually haven't thought about it and don't want to.

The gap is judgment: *what shape should this canvas have, given what the member is doing with it?* Canvas v1 leaves that to the member. The Gardener closes the gap.

## The principle

**Members stay out of architecture decisions by default. The system picks. If wrong, the system reshapes. When members express intent, the system adopts it as persistent preference.**

The Gardener is the piece of Kernos that picks.

## Asynchrony invariant

**Every Gardener action except retrieval is fire-and-forget from the primary agent.** A turn that triggers three Gardener actions costs the same end-to-end as the same turn with zero Gardener actions — the Gardener work completes in the background, not in the primary-agent path.

The one synchronous exception is **retrieval**: when the primary agent explicitly asks the Gardener for context (e.g., a consultation surface that informs the primary's next move) and awaits the response. That's synchronous by necessity — the primary needs the answer to proceed.

Three disciplines *compose* with the invariant; they don't replace it. Removing any one of them would erode turn-latency protection even while the invariant technically held:

- **Cheap-chain routing** — cohort-primitive consultations run on the lightweight model tier, not the primary
- **24-hour coalescing** — high-confidence proposals for a canvas coalesce into at most one surface per window, so proposal density stays sparse regardless of dispatch frequency
- **High-confidence-only surfacing** — low and medium confidence matches log for pattern-tuning audit but don't wake members

**Why this matters.** Gardener work scales with canvas count × event frequency × declared-heuristic count. Pattern migrations will increase declared heuristics; preference capture (deferred Pillar 5) will add new judgment surfaces; future pattern-library expansion will bring more canvases into the dispatch set. The async invariant is what lets that scaling stay invisible to per-turn latency. Any proposal to introduce a blocking Gardener call in the primary-agent path is a Charter-adjacent change and requires design review-level review — it's not just adding a feature, it's altering a load-bearing property of the agent experience.

## Bounded judgment

The Gardener's authority is explicitly narrow:

- **Canvas shape only.** It picks patterns, instantiates initial pages, proposes reshapes. It does not generate content for pages, it does not make decisions outside shape, and it does not act on content the member wrote.
- **Non-destructive.** No destructive deletions — the canvas primitive's core invariant. Reshaping means moving, splitting, merging, or summarizing. Never discarding.
- **Asynchronous.** The Gardener never blocks member-facing turns. Canvas creation returns immediately; the Gardener fills in pages in the background. Continuous-evolution proposals surface via whispers, not inline.
- **Bounded by the library.** Heuristics come from the Workflow Patterns library (Pattern 00's cross-pattern rules plus each pattern's domain-specific heuristics). The Gardener does not invent rules.

## The three judgment kinds

### 1. Initial-shape judgment

Fires at `canvas_create`. The member (or the member's agent) passes an optional `intent` string describing what the canvas is for. The Gardener:

1. Reads the Workflow Patterns library (cached in-process; invalidated on library changes)
2. Scores the intent against each pattern's (dials + domain cues) declaration
3. Picks the highest-scoring pattern
4. Instantiates the pattern's "Initial canvas shape" — parses the pattern's bullet list, creates each declared page with its declared type and scope, seeds a short scaffold body
5. Records `pattern: <name>` in the canvas's `canvas.yaml`

If no pattern matches cleanly, the canvas lands with `pattern: unmatched` in its frontmatter. Follow-on evolution judgments skip pattern-specific heuristics on unmatched canvases; the cross-pattern Pattern 00 rules still apply.

An **explicit-pattern escape hatch** bypasses the Gardener: pass `pattern: software-development` to `canvas_create` directly when you already know which pattern fits.

### 2. Continuous-evolution judgment

Fires on canvas page events (`canvas.page.created`, `canvas.page.changed`, `canvas.page.state_changed`). The Gardener runs pattern-declared heuristics plus Pattern 00's cross-pattern rules against the changed page.

**Pattern 00 cross-pattern heuristics** (always applied, regardless of pattern):

| Heuristic | Signal | Action |
|---|---|---|
| Split | Page with 3+ sections each exceeding the section-line threshold | `propose_split` |
| Staleness | Page `last_updated` older than the staleness threshold (90 days) | `flag_stale` |
| Scope mismatch | Page declares a scope contradicting the canvas scope | `flag_scope_mismatch` |
| Merge | Two pages with 40%+ content overlap | `propose_merge` (deferred in v1) |
| Index promotion | Page referenced from 3+ others | `promote_to_index` (deferred in v1) |

The merge and index-promotion heuristics require a cross-page index that doesn't yet exist; they ship in a follow-on batch.

**Pattern-specific heuristics** compose with the cross-pattern set. Per the the design review design-review round, v1 ships cross-pattern heuristics only; pattern-specific heuristics come next batch. A pattern declares heuristics like *software development*'s "spec count exceeds 12 without subdivision" or *long-form campaign*'s "NPC mentioned in 3+ sessions promote to page" — and the Gardener runs the applicable ones on matching events when they land.

### 3. Section management

Sub-judgment within continuous-evolution. When a section's `<!-- section: summary=... tokens=... -->` marker drifts from the actual body, the Gardener can regenerate the summary (a concrete non-destructive action that auto-applies under `auto-non-destructive` consent). Section splits are proposal-class even under `auto-all` because reorganizing page structure is the kind of edit where human-in-the-loop serves the outcome.

## Confidence floor and coalescing

Two mechanics keep the Gardener from inducing consent fatigue:

**Confidence floor.** Each heuristic match produces a confidence score (`low`, `medium`, `high`). Only `high` surfaces as a proposal. Low and medium log for pattern-tuning audit but don't wake members. `GardenerDecision.surfaces` is the property gate.

**24-hour coalescing window.** Proposals for a given canvas buffer in `ProposalCoalescer`. The window is per-canvas (one canvas's noise doesn't coalesce with another's). When the window elapses, the buffered proposals surface as a single coalesced whisper rather than N individual ones.

The design target: members working with a Gardener-managed canvas see proposals at most once or twice a week under typical use.

## Consent modes

Canvases declare a `gardener_consent` preference in `canvas.yaml`. Four modes:

| Mode | Behavior |
|---|---|
| `propose-all` (default) | Every high-confidence action surfaces as a proposal |
| `auto-non-destructive` | Summary regeneration auto-applies; splits/merges propose |
| `auto-all` | Everything non-destructive auto-applies; splits/merges still propose |
| `propose-critical-only` | Critical-content reshapes propose; routine auto-applies |

v1 ships `propose-all` as the default and parses all four modes. Auto-apply currently services only `regenerate_summary`; split/merge stay proposal-class under every mode because reorganizing page structure deserves explicit consent. `propose-critical-only` parses but behaves as `propose-all` until critical-content detection ships alongside the deferred preference-capture batch.

## Cascade prevention

Every event the Gardener emits (`canvas.reshaped`, `canvas.pattern_applied`) carries `source: gardener` in its payload. When that event fans back through the event-stream subscriber path, the Gardener's `on_canvas_event` handler short-circuits at the `source` check and skips its own events. This is the v1 resolution for Hazard B (cascading reshapes).

## Pattern cache

The Gardener reads the Workflow Patterns canvas on demand and caches the parsed pages in-process. The cache invalidates automatically when any event fires on the Workflow Patterns canvas — operator edits to patterns propagate to the Gardener's next judgment. First-time consultation populates the cache lazily; the cache persists for the life of the process.

This is the v1 resolution for Hazard A (library-load latency).

## Preferences — persistent member-intent capture

Preferences are the Gardener's **canvas-scoped** memory for how a specific canvas should behave. A covenant says *"always share your thought process"* — a universal rule of engagement across all contexts. A preference says *"on this canvas, don't surface drafts unprompted"* or *"staleness for this one is 180 days"* — guideline-force, scoped to the canvas, shaping Gardener dispatch rather than binding the agent.

The two systems are **layered, not separated**. Covenants execute agent-side (dispatch gate, system-prompt injection, tool-call validation). Preferences execute Gardener-side (pre-heuristic-fire check on the `suppressed_by_preference` / `threshold_preference` fields of heuristic declarations). When a single utterance produces both a covenant candidate and a preference candidate, both can persist — different execution surfaces, no runtime race.

### Subject-matter validation

Preferences are strictly about **canvas behavior**: suppression of a heuristic class, overriding a declared threshold. Anything else the Gardener's preference-extraction consultation classifies as `effect_kind: other` and **does not capture** — the utterance falls through to the normal covenant / standing-order path without a confirmation whisper. This is load-bearing: members never see a confirmation for a preference that wouldn't do anything, because the trust contract depends on every surfaced proposal being actionable.

Two effect kinds are wired in v1:

- **`suppressed_by_preference: <name>`** — when a truthy confirmed preference exists under `<name>`, the heuristic doesn't fire
- **`threshold_preference: <name>`** — when a confirmed preference exists under `<name>`, its value overrides the declaration's `threshold` or `threshold_days` at evaluation time

Other effect kinds (routing-override, scope-modifier, authority-delegation) ship their own extraction activation in follow-on batches (`CANVAS-PREFERENCE-ROUTING`, `CANVAS-PREFERENCE-SCOPE`, `CANVAS-PREFERENCE-AUTHORITY`). Capturing without wiring is the trust hazard; extraction stays silent until the effect is real.

### Confirmation discipline

Preferences are **opt-in, not opt-out**. Even on a canvas set to `auto-all` or `auto-non-destructive`, preference capture requires explicit member confirmation — this is the one Gardener path that auto-apply consent modes do not extend to. Preferences are interpretive; auto-capture with no member in the loop is exactly where social noise lives.

The flow:

1. Agent calls `canvas_preference_extract(canvas_id, utterance)` with the member's verbatim words
2. Consultation runs on the lightweight chain, validates subject matter, applies novel-preference downgrade if the extracted name isn't in the pattern's intent-hook vocabulary
3. High-confidence + wired-effect match lands in `canvas.yaml`'s `pending_preferences` with a 24h TTL
4. Agent surfaces to the member, gets a clear yes/no
5. Agent calls `canvas_preference_confirm(canvas_id, preference_name, action)` — `confirm` promotes to `preferences`, `discard` moves to `declined_preferences` for audit

Pending preferences the member never engages with auto-expire at 24h on the next Gardener dispatch. Confirmation whispers coalesce per canvas per 24h window, matching the Pillar 4 reshape-proposal discipline.

### Canvas-layer storage

`canvas.yaml` carries three preference-related keys:

| Key | Purpose |
|---|---|
| `preferences:` | Confirmed member-captured preferences, the source heuristic dispatch reads from |
| `pending_preferences:` | Awaiting confirmation, 24h TTL, populated by extraction |
| `declined_preferences:` | Audit trail of rejected preferences with evidence, consulted to avoid re-offering recently-declined utterance shapes |

Preferences are plaintext. An operator can edit `canvas.yaml` directly to set, rename, or remove them; there's no dedicated UI.

### The trust contract

The Gardener holds itself to one hard commitment about preference capture: **no captured-but-unapplied preferences**. If the effect isn't wired, the extraction silently no-ops; if the confidence is low, it logs for audit but doesn't surface; if the preference is novel (not in the pattern's declared intent-hook vocabulary), the confidence downgrades one tier before the surface-or-not gate applies. Everything members see as a proposed preference actually does something.

## Relationship to other cohorts

The Gardener is the third cohort to land, alongside the Messenger (disclosure-time welfare judgment on cross-member messages) and the Friction Observer (post-turn diagnostic). Each cohort holds a bounded judgment space:

- **Messenger** — should this cross-member message, as written, actually be sent?
- **Friction Observer** — what didn't go well this turn that should be visible later?
- **Gardener** — what shape should this canvas have as it accumulates?

They share architectural discipline: cheap-chain model tier, structured output, bounded authority, non-blocking. They don't share state or communicate directly — the event stream is their only integration surface.

## What the Gardener is NOT

- **Not a general-purpose agent.** Its judgment space is canvas shape only. Asking it anything else is a category error.
- **Not a content generator.** Pages start empty or with a short scaffold derived from the pattern's description of that page. The member fills in substance. The Gardener shapes.
- **Not a replacement for the member.** Members override proposals trivially (decline the whisper). The Gardener is a floor of shape hygiene, not a ceiling of decision authority.
- **Not destructive.** Canvas v1's core invariant — no method permanently deletes data — applies to every Gardener action.

## Deferred to follow-on specs

- **Member-intent as persistent preference** (Pillar 5, `CANVAS-GARDENER-PREFERENCE-CAPTURE`). In v1, overrides live as single-turn declines; preferences persist after Gardener shape judgments prove out.
- **Merge + back-reference-promotion heuristics.** Need a cross-page index first.
- **Pattern-specific heuristics.** Ship in the batch after Pattern 00 heuristics have been observed in real use.
- **Auto-apply for split/merge under `auto-all`.** Structural reorganization stays human-in-the-loop in v1.
- **`propose-critical-only` consent mode.** Needs critical-content detection (content classifier). Ships alongside preference-capture.

## Code map

| Concern | File |
|---|---|
| Cohort consultation + decision types | `kernos/cohorts/gardener.py` |
| Prompt templates | `kernos/cohorts/gardener_prompts.py` |
| Service (cache, coalescer, dispatch, heuristics) | `kernos/kernel/gardener.py` |
| Pattern parser + section markers | `kernos/kernel/canvas.py` |
| Handler integration (lazy getter, event fan-out) | `kernos/messages/handler.py` |
| Initial-shape trigger in `canvas_create` | `kernos/kernel/reasoning.py` |
