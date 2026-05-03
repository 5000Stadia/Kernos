# Cohort: Gardener

First cohort adapter targeting the **COHORT-FAN-OUT-RUNNER**
contract. Adapts gardener's existing canvas-shape state into a
per-turn `CohortOutput` integration consumes alongside other
cohorts (memory, weather, covenant — landing in subsequent specs).

## What the adapter does

The gardener cohort is a **status surface** (Option A from the
spec's conceptual question). It does NOT run new gardener
consultations per turn. Instead, every turn it reads what
gardener has already concluded — pending proposals, recent
evolution decisions — and packages that as a `CohortOutput`.

```
gardener (runs on canvas events)
    │
    ├─ ProposalCoalescer (per-canvas 24h window)
    └─ EvolutionRecord ledger (per-canvas, capped 10)
                │
                ▼
GardenerService.current_observation_snapshot()
                │  (frozen snapshot, no LLM, no mutation)
                ▼
gardener_cohort_run(ctx) → CohortOutput
                │
                ▼
CohortFanOutRunner → IntegrationRunner → presence
```

Per-turn consultation (Option B) and hybrid escalation (Option C)
are explicit future work — gated on integration-audit data
showing per-turn consultation pays off in specific scenarios.

## Snapshot read surface

`GardenerService.current_observation_snapshot(*, instance_id,
member_id, canvas_id) → GardenerSnapshot` returns a frozen
dataclass:

| Field | Type | Notes |
|---|---|---|
| `canvas_id` | str | The canvas this snapshot is for |
| `pending_proposals` | tuple[PendingProposal, ...] | Tuple-copy of the coalescer buffer; non-mutating |
| `recent_evolution` | tuple[EvolutionRecord, ...] | Per-canvas capped deque (max 10) |
| `observation_age_seconds` | int \| None | Age of newest observation in either list |

Guarantees:

- **No LLM invocation.** The method is a pure state read.
- **No mutation** of the proposal coalescer or the evolution
  ledger. Repeated calls are idempotent and leave subsequent
  `drain()` semantics unaffected.
- **No event emission.** The snapshot is purely observational.

The evolution ledger is populated alongside `consult_evolution`
and `consult_section` — the existing flows are unchanged; the
ledger writes are purely additive. Tests can also seed the
ledger directly via `_record_evolution` to avoid running real
consultations.

## Active-canvas resolution

Per the design review edit #5, the rule is explicit and conservative:

> Exactly one canvas space in `ctx.active_spaces` → use it.
> Zero or multiple → `has_active_canvas: False`.

`make_canvas_resolver(instance_db)` implements the rule. When
`instance_db` is supplied, candidate spaces are filtered to those
whose `get_canvas(space_id)` returns truthy (real canvas rows in
the registry); the exactly-one rule applies to the filtered set.
Tests omit `instance_db` for convenience and treat every space
as a canvas candidate.

No silent picking — when the rule is unmet, the cohort emits
`has_active_canvas: False` and integration filters cleanly via
`reason_filtered: "no active canvas for member"`.

## Output schema

```
CohortOutput {
  cohort_id: "gardener"
  cohort_run_id: "{turn_id}:gardener:provisional"  # runner re-mints
  visibility: Public
  output: {
    has_active_canvas: bool
    canvas_id: str | None
    pending_proposals: List[ProposalSummary]
    recent_evolution: List[EvolutionSummary]  # capped at 3
    observation_age_seconds: int | None
  }
}

ProposalSummary {
  proposal_id: str  # synthetic: "{canvas_id}:{captured_at}:{seq}"
  pattern: str
  action: str
  confidence: "low" | "medium" | "high"
  rationale_short: str  # truncated to 200 chars
  affected_pages: list[str]
  captured_at: ISO 8601
}

EvolutionSummary {
  decision_id: str
  action: str
  confidence: str
  pattern: str
  occurred_at: ISO 8601
  consultation: "evolution" | "section"
}
```

## Visibility model

Per the design review edit #3: `CohortOutput.visibility` is **whole-output**
in V1. Gardener filters restricted items at source — restricted
proposals/evolution records are absent from the payload entirely
(not marked, not redacted, just not present). The output stays
`Public`.

Restriction is determined by a `restricted_pattern_check`
predicate the wiring layer supplies. v1 ships with a no-op
default (no patterns restricted). When pattern privacy becomes a
real concept (e.g., pattern frontmatter declares
`visibility: restricted`), the wiring passes the appropriate
predicate; the cohort's interface doesn't change.

Per-item visibility on a single CohortOutput is explicitly
deferred. If mixed-visibility cohort data becomes important,
that's a formal V1 schema extension — not a payload-level
informal smuggle. See `INTEGRATION-LAYER-V1` Section 3.

## Cohort descriptor

| Field | Value |
|---|---|
| `cohort_id` | `"gardener"` |
| `execution_mode` | `ASYNC` |
| `timeout_ms` | `200` |
| `default_visibility` | `Public` |
| `required` | `False` |
| `safety_class` | `False` |

Gardener absence is non-fatal — integration proceeds without
canvas-shape awareness. Gardener is not a safety primitive;
covenant-class cohorts are the safety surface.

## What this adapter does NOT change

- **Gardener's existing public behavior.** `on_canvas_event`,
  `consult_initial_shape`, `consult_evolution`, `consult_section`,
  `consult_preference_extraction`, `apply_initial_shape` —
  unchanged signatures, unchanged return shapes.
- **Canvas-event firing pattern.** `MessageHandler._canvas_emit`
  continues to fan out to gardener via `on_canvas_event`. The
  cohort adds a per-turn read surface alongside.
- **V1 CohortOutput schema.** No per-item visibility extension.
- **ProposalCoalescer mutation patterns.** `add` / `drain` /
  `should_surface` continue to function as today; the new
  `snapshot_pending` accessor is purely additive.

## Architectural placement

```
kernos/kernel/cohorts/
├── gardener_cohort.py   # the adapter (this spec)
└── registry.py          # CohortRegistry (FAN-OUT-RUNNER)

kernos/kernel/gardener.py  # GardenerService + observation snapshot
                           # ProposalCoalescer + EvolutionRecord
                           # GardenerSnapshot
```

Production wiring (when `INTEGRATION-WIRE-LIVE` ships) calls
`register_gardener_cohort(registry, gardener_service, ...)` from
the boot path with a real `instance_db` and a
`restricted_pattern_check` predicate (default no-op until pattern
privacy is formalized).

## Path forward

- **COHORT-ADAPT-MEMORY** — next adapter; substantially more
  complex (memory retrieval is currently a tool call inside
  reasoning; decoupling is real work).
- **COHORT-ADAPT-PATTERNS** — surface pattern heuristics
  standalone.
- **COHORT-ADAPT-COVENANT** — decouple covenant validation from
  its post-write state hook; safety-class.
- **PRESENCE-DECOUPLING + INTEGRATION-WIRE-LIVE** — wire the full
  pipeline.
- **Future Option C (hybrid)** — gate per-turn consultation on
  integration relevance signals, falling back to the status
  surface when not needed. Requires integration-audit data first.
