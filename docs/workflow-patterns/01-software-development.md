---
scope: team
type: note
pattern: software-development
consumer: gardener
---

# Software development

The reference pattern. Build-with-shipping discipline. Applies when a member's intent is to produce running code or a system that can be deployed and maintained over time.

## Dials

- **Charter volatility: LOW.** Once the philosophical commitments are authored (what this software is, what it refuses to be, its core pillars), they're defended hard. Pillar refinements happen rarely — usually in response to a "feel moment" during implementation that reveals a pillar was misstated.
- **Actor count: MEDIUM-HIGH.** Design Review + implementer + verifier + possibly infra + possibly design + occasional contributors. Even solo development involves multiple agents (planning agent, implementation agent, verifier agent).
- **Time horizon: LONG.** Code outlives any given spec cycle. Decisions made six months ago must remain legible now.

## Artifact mapping

- **Charter: YES.** Stable outer frame. Philosophical DNA. Every spec declares which pillar it serves.
- **Architecture: YES.** Named primitives with their relationships. Updated when a new primitive is introduced or a relationship is redefined.
- **Phase: YES.** Current focus, last shipped, next, explicitly deferred. Scrolls forward through project life.
- **Spec Library: YES.** Versioned with explicit supersession chains. This is where most content accumulates.
- **Decision Ledger: YES.** Append-only. Every significant call tagged to Charter pillar and Architecture component.
- **Manifest: YES.** What exists in code today. Strict authorship — only the implementer updates, on merge.

All six apply. Software development is the pattern where no artifact can be cut.

## Initial canvas shape

- `charter.md` (note, team) — pillars, what this refuses to be, philosophical commitments
- `architecture.md` (note, team) — named primitives, their relationships, interfaces
- `phase.md` (log, team) — current focus + last-shipped + next + deferred, updated weekly
- `specs/` — subdirectory of decision-type pages, versioned, supersession-chained
  - `specs/_template.md` — page template new specs are forked from, includes pillar-declaration and scope-bounding prompts in frontmatter
- `ledger.md` (log, team, append-only) — significant decisions, each tagged `pillar:` and `component:` in entry frontmatter
- `manifest.md` (note, team, single-author-enforced) — what's shipped, maintained by implementer
- `preamble.md` (note, team) — the three-sentence frame-anchoring template, loaded into every spec-work session

Frontmatter conventions:

```yaml
# specs/<n>.md
pillar: [<pillar-id>]
component: [<component-id>]
status: current | draft | superseded | deferred
supersedes: <prior-spec-id>
superseded-by: <successor-spec-id>
scope-in: [<bullet>, <bullet>]
scope-out: [<bullet>, <bullet>]
```

Status transitions are routable targets. `status: approved` can route to a context space with implementation standing orders. `status: superseded` routes to the spec's prior readers notifying them the canonical version has moved.

## Evolution heuristics

**Spec Library growth:**
- Spec count in `specs/` exceeds 12 without subdivision → propose `specs/<subsystem>/` structure based on the most-referenced components in recent specs
- Single spec grows past ~400 lines → propose splitting into parent + sub-specs; the parent keeps the scope-declaration and pillar-tags, sub-specs carry the detail
- Three specs reference the same deferred item → propose promoting the deferred item to its own tracking page or to the next phase

**Charter stability surveillance:**
- Charter untouched 90+ days but ledger shows 3+ entries that refine or narrow pillars → flag for curation, the implicit Charter has drifted from the explicit one
- A spec is authored that doesn't declare any pillar → flag before approval; spec cannot route to implementation without pillar declaration
- Two specs declare service to the same pillar but operationalize it contradictorily → flag for Charter-level resolution

**Phase Map drift:**
- Phase Map `current focus` unchanged for 3+ weeks while ledger shows active work on unrelated items → propose phase transition; current focus is stale
- `deferred` list grows past ~8 items → propose deferred-item-review at next curation
- A spec shipped (manifest updated) that was never in the phase's scope → flag as scope-creep event; may warrant Charter-level discussion

**Manifest-reality drift:**
- Manifest shows shipped component X; ledger shows decision to remove X; manifest not updated → whisper to implementer
- Manifest shipped entries that have no corresponding spec → propose retro-speccing for stability
- Manifest hasn't been updated in 4+ weeks while ledger shows merge activity → flag implementer-side for manifest sync

**Component/architecture growth:**
- Same component referenced in 5+ decisions → propose component-specific decision-page promotion
- New primitive named in a spec that isn't in `architecture.md` → propose architecture-map update before spec approval
- Two primitives with overlapping roles in decisions → propose architecture-level disambiguation

**Curation rhythm:**
- Weekly: Gardener compiles past week's ledger entries for operator/planning-agent review, highlights entries without pillar-tags, flags any Charter-drift signals
- Monthly: Gardener proposes Architecture Map review — any primitives added, any relationships changed, any deprecations due
- Per-phase-close: Gardener proposes phase archival and new phase authoring

```yaml
# Structured declarations of the heuristics described in the prose above.
# The Gardener dispatches from this block; the prose remains the
# authoritative human-readable source. Disabled entries exist for
# library-maintenance audit but do not fire until their blockers clear
# (noted inline).
heuristics:
  - id: spec-count-subdivision
    trigger: page-created
    scope:
      path_glob: "specs/*.md"
    signal:
      type: deterministic
      check: page-count
      params:
        path_glob: "specs/*.md"
        threshold: 12
    action:
      kind: propose_subdivide
      params:
        target: "specs/<subsystem>/"
    confidence: deterministic-high
    coalesce:
      key: spec-count
    status: active

  - id: spec-size-split
    trigger: page-changed
    scope:
      path_glob: "specs/*.md"
    signal:
      type: deterministic
      check: page-size-lines
      params:
        threshold: 400
    action:
      kind: propose_split
      params:
        parent_keeps: [scope_declaration, pillar_tags]
    confidence: deterministic-high
    coalesce:
      key: spec-size
    status: active

  - id: phase-focus-stale
    trigger: page-changed
    signal:
      type: deterministic
      check: duration-since-write
      params:
        page_path: "phase.md"
        threshold_days: 21
    action:
      kind: propose_transition
      params:
        target: "phase.md"
        note: "current focus unchanged for 3+ weeks"
    confidence: deterministic-high
    coalesce:
      key: phase-stale
    # Member "don't spam me on every ledger entry" → weekly-digest mode
    # suppresses per-event staleness prompts; members who prefer batch
    # curation don't want individual phase-stale pings.
    suppressed_by_preference: ledger-routing
    status: active

  - id: manifest-sync-lag
    trigger: page-changed
    signal:
      type: deterministic
      check: duration-since-write
      params:
        page_path: "manifest.md"
        threshold_days: 28
    action:
      kind: flag
      params:
        surface: implementer
        note: "manifest untouched 4+ weeks while work continues"
    confidence: deterministic-high
    coalesce:
      key: manifest-sync
    # Member "don't let the plan fall behind the code" →
    # preferences.manifest-drift-alarm: 14-days overrides the 28-day
    # default. Maps to the pattern's declared intent hook verbatim.
    threshold_preference: manifest-drift-alarm
    status: active

  - id: spec-missing-pillar
    trigger: page-state-changed
    scope:
      path_glob: "specs/*.md"
      to_state: approved
    signal:
      type: deterministic
      check: missing-frontmatter-field
      params:
        field: pillar
    action:
      kind: flag
      params:
        surface: pre-approval-block
        note: "spec cannot route to implementation without pillar declaration"
    confidence: deterministic-high
    coalesce:
      key: missing-pillar
    status: active

  # --- Disabled: blocked on CANVAS-CROSS-PAGE-INDEX ---------------------
  # These require a cross-page reference index that doesn't ship until a
  # follow-on batch. Declarations live here so library authors can audit
  # the full Pattern 01 heuristic set.

  - id: deferred-promotion
    trigger: page-changed
    signal:
      type: deterministic
      check: reference-count
      params:
        target: "deferred-items"
        threshold: 3
    action:
      kind: propose_promote
      params:
        target: "tracking-page-or-next-phase"
    confidence: deterministic-high
    coalesce:
      key: deferred-promotion
    status: disabled

  - id: component-decision-pressure
    trigger: page-changed
    scope:
      path_glob: "ledger.md"
    signal:
      type: deterministic
      check: reference-count
      params:
        target: "component-tag"
        threshold: 5
    action:
      kind: propose_promote
      params:
        target: "component-specific-decision-page"
    confidence: deterministic-high
    coalesce:
      key: component-pressure
    status: disabled

  # --- Disabled: semantic heuristics ------------------------------------
  # Per CANVAS-GARDENER-PATTERN-HEURISTICS spec: Pattern 01's two
  # semantic heuristics ship declared but disabled by default. Enable
  # requires confirming the Gardener consultation path handles the
  # prompt_key inputs as described.

  - id: pillar-conflict
    trigger: page-state-changed
    scope:
      path_glob: "specs/*.md"
      to_state: approved
    signal:
      type: semantic
      prompt_key: pillar-conflict-detector
      inputs:
        new_spec_body: $PAGE_BODY
        same_pillar_specs: $SAME_PILLAR_SPECS
    action:
      kind: flag
      params:
        surface: charter-level-resolution
    confidence: llm-judgment
    coalesce:
      key: pillar-conflict
    status: disabled

  - id: primitive-overlap
    trigger: page-changed
    scope:
      path_glob: "ledger.md"
    signal:
      type: semantic
      prompt_key: primitive-overlap-detector
      inputs:
        recent_decisions: $RECENT_DECISIONS
        architecture_body: $ARCHITECTURE_BODY
    action:
      kind: flag
      params:
        surface: architecture-level-disambiguation
    confidence: llm-judgment
    coalesce:
      key: primitive-overlap
    status: disabled
```

## Member intent hooks

Natural-language patterns the Gardener should translate into persistent preferences:

- "Track what's shipped" → `preferences.manifest-routing: operator-on-change` — manifest updates route to operator surface
- "Keep me honest about scope" → `preferences.scope-enforcement: strict` — specs without scope-out declarations cannot transition to approved
- "Don't let specs drift" → `preferences.supersession-required: true` — any content change to an approved spec requires supersession frontmatter
- "What did we decide about X" → no preference; on-demand search of ledger filtered by component-tag or pillar-tag, returning supersession-chained current states
- "I want to know when [pillar] gets touched" → `preferences.pillar-watches.<pillar-id>: operator-surface` — ledger entries touching that pillar route to operator
- "Don't spam me on every ledger entry" → `preferences.ledger-routing: weekly-digest` — Gardener batches ledger entries into weekly curation instead of per-entry operator surfaces
- "Archive finished phases" → `preferences.phase-archival: auto-on-close` — closed phases move to `phase/archive/` subfolder automatically
- "Keep the Charter in front of me during spec work" → `preferences.charter-pin: spec-sessions` — Charter loads into context on any spec-session open
- "Don't let the plan fall behind the code" → `preferences.manifest-drift-alarm: 14-days` — 14 days between merge and manifest update triggers operator surface
- "I want [agent] to own the manifest" → `preferences.manifest-author: <member-or-agent-id>` — only the named author can update manifest; others' attempts surface as proposals

## Domain-specific rituals

**Session preamble at spec-work open:**

Every spec-session opens with a three-sentence frame:
1. Which Charter pillar(s) this serves
2. Where it sits in the current Phase Map (scope-in, scope-out)
3. What's explicitly deferred from this session

The preamble is not prose — it's three declarative sentences at the top of the spec page, populated before substantive work begins. If the agent begins writing spec content before the preamble is complete, the Gardener surfaces the missing frame.

**Decision entry at the moment of decision:**

Decisions are written at the moment of commitment, not retrospectively. Entry frontmatter:

```yaml
pillar: [<id>]
component: [<id>]
date: <iso>
author: <member-id-or-agent-id>
superseded-by: (filled if later decision overrides)
```

Body is 2-3 sentences: what was decided, what it was chosen over, why. No essays.

**Manifest as commit-side artifact:**

Manifest updates are coupled to merge events. The implementer writes the manifest entry as part of the commit that ships the work — not in a follow-up. If manifest and merges diverge, that's an alarm.

## Composition notes

Software development canvas often sits alongside:

- `client-project` canvas when the software is built for an external principal (specs inherit Charter pillars but add client-acceptance gates)
- `open-source-maintenance` canvas when the project has external contributors (adds contributor-coordination shape on top of this pattern)
- `research-lab` canvas when the software is an experiment artifact (experiment protocols provide scope; this pattern provides build discipline)

The Gardener proposes multi-canvas shapes when composition is indicated.
