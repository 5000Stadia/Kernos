# Cohort: Covenant

Fourth cohort adapter targeting the **COHORT-FAN-OUT-RUNNER**
contract. First cohort to ship with `required=True` AND
`safety_class=True` — the canonical safety anchor. Surfaces the
active covenant set per turn so integration can pivot response
shape *before* presence generates.

## What the adapter does

The covenant cohort is a **per-turn read surface** for active
covenant rules. Every turn:

```
ctx.member_id + ctx.active_spaces
    │
    ▼
StateStore.query_covenant_rules(scope=active_spaces+global)
    │  (no LLM, no mutation, read-only)
    ▼
Python-side member filter (instance-level + ctx.member_id only)
    │
    ▼
Safety-priority ranking + 50-rule cap
    │  (must_not+block first; preferences last)
    ▼
covenant_cohort_run(ctx) → CohortOutput(visibility=Restricted)
    │
    ▼
CohortFanOutRunner → IntegrationRunner → presence
                            │
                            └─ on covenant cohort failure:
                               required_safety_cohort_failures
                               populated → integration's filter
                               phase forces defer / constrained
```

The cohort does NOT replace `validate_covenant_set` (the
post-write LLM hook). Validation (LLM, post-write, mutating) and
surfacing (read-only, per-turn, observation) are different jobs;
the cohort separates them.

## Visibility model: whole-output Restricted

`CohortOutput.visibility = Restricted{reason: "covenant_set"}`.
V1's redaction invariant applies automatically — Restricted
content NEVER leaks into briefing text fields. Integration sees
rule descriptions during reasoning and encodes their effect into
`presence_directive` as behavioral instruction; presence never
sees the rule text directly.

Per the design review edit #4, **directives must not quote `rule.topic`,
`rule.target`, or `rule.description`** even though those are
inside the redaction boundary. Wrong: `"do not reference
therapy"` (leaks the topic). Right: `"decline this cross-member
disclosure and ask for explicit permission"`. When the user's own
message contains the topic word, presence sees it via the
conversation thread — directives shape *how* presence handles it,
not what the topic is.

Per-item visibility (some covenants Public, some Restricted) is
explicit future work. Whole-output Restricted is the v1 default.

## required + safety_class — the canonical safety anchor

| Flag | Value | Meaning |
|---|---|---|
| `required` | `True` | Cohort failure marks the fan-out as degraded |
| `safety_class` | `True` | Required + safety_class failure forces integration's filter phase to produce `defer` or `constrained_response` |

Without the COHORT-ADAPT-COVENANT C1 plumbing, this label would
be decorative — fan-out computed `required_safety_cohort_failures`
but the signal never crossed into integration's filter phase. C1
extended `IntegrationInputs` with the field and added structural
post-finalize enforcement so the safety_class label is now
load-bearing.

### Safety-degraded fail-soft

Per the design review's load-bearing input: **safety-degraded fail-soft must
never be respond_only.** When `required_safety_cohort_failures`
is non-empty AND fail-soft engages for any reason (model produced
no tool_use, iteration budget exhausted, redaction violation,
unexpected exception, model produced a forbidden decided_action),
the runner returns a Defer briefing — never a RespondOnly. The
BudgetState carries `required_safety_cohort_failed=True` so
auditors see the cause.

This holds across every fail-soft trigger. The architecture's
safety quality is preserved structurally: if Kernos can't read
its covenants, it doesn't proceed at full strength.

## Output payload schema

```
CohortOutput {
  cohort_id: "covenant"
  cohort_run_id: "{turn_id}:covenant:provisional"  # runner re-mints
  visibility: Restricted{reason: "covenant_set"}
  output: {
    rule_count: int  # post-cap
    has_principle_layer: bool
    has_practice_layer: bool
    rules: List[CovenantSummary]
    scope_resolution: ScopeInfo
  }
}

CovenantSummary {
  rule_id: str
  capability: str
  rule_type: str  # must | must_not | preference | escalation
  layer: str  # principle | practice
  description: str  # capped at 2000 chars
  description_truncated: bool
  enforcement_tier: str  # silent | notify | confirm | block
  fallback_action: str  # ask_user | stage_as_draft | log_and_proceed | block_with_explanation
  scope: str  # global | <space_id> | member:<member_id>
  topic: str  # Messenger cross-member topic (verbatim user phrasing)
  target: str  # member_id or relationship-profile identifier
  trigger_tool: str  # MCP tool name or empty
  action_class: str  # "email.delete.spam" etc.
}

ScopeInfo {
  active_spaces: list[str]
  member_id: str
  instance_level_rules: int
  member_specific_rules: int
  space_scoped_rules: int
  truncated: bool  # True when more than RULE_COUNT_CAP applied
  truncation_dropped: list[str]  # rule_ids dropped under cap
}
```

## Truncation

### Description hard cap (2000 chars)

Pathological-rule defense (the design review edit #5). "Typically short" is not
a safety property. Truncated descriptions get
`description_truncated: True`; integration sees enough to reason
about constraint type even if specific phrasing is clipped.
Without the cap, a covenant could be an unbounded hidden
prompt-injection surface.

### Rule count cap (50) with safety-priority order

Per the design review edit #6. Recency-only truncation could surface 50
preferences while silently dropping a `must_not + block` safety
rule — defeating the cohort's purpose. The priority order makes
the truncation safety-preserving by construction:

| Priority | rule_type / enforcement | Notes |
|---|---|---|
| 0 | `must_not` + `block` | Hard blocking safety rules |
| 1 | `must_not` + `confirm` | Confirm-required negative constraints |
| 2 | `must_not` + `notify` / `silent` | Lower-enforcement negative |
| 3 | `must` | Positive obligations |
| 4 | `escalation` | Escalation routing rules |
| 5 | `preference` / other | Soft preferences (lowest priority) |

Within each tier: recency (newest first). `truncation_dropped`
exposes the rule_ids dropped so integration can surface operator
attention if budget capacity becomes a real concern.

## Member filtering — Python-side, not SQL

Per the design review edit #7. `StateStore.query_covenant_rules` abstract
signature does NOT take `member_id` (the sqlite implementation
has it as an extra parameter; the JSON store does not). Pushing
the filter into the SQL layer would couple to a specific store
implementation. The cohort:

1. Calls `query_covenant_rules` WITHOUT `member_id`
2. Receives all rules matching `context_space_scope` + `active_only`
3. Filters Python-side: keeps `member_id == ""` (instance-level)
   + `member_id == ctx.member_id` (member-specific). Drops other
   members' member-specific rules.

Implementation-portable across stores. Tests verify
`query_covenant_rules` was called without `member_id`.

## Audit log redaction

Per the design review edit #8: audit entries do NOT contain `description`,
`topic`, OR `target` strings. The audit category
`cohort.fan_out` already redacts cohort output payloads to
references-only; the covenant cohort's contribution to that
audit is `cohort_id` + `outcome` + `cohort_run_id` only.

## Empty + error cases

- **Empty active set** (member with no rules): `rule_count: 0`,
  empty `rules` array, runner outcome `success`. Normal state for
  new instances.
- **Query failure** (database unavailable, schema error):
  propagates to the runner. Runner registers `outcome: error`
  with redacted `error_summary`. Because the cohort is
  `required + safety_class`, this lands in
  `CohortFanOutResult.required_safety_cohort_failures`. The
  integration runner's filter phase then forces
  `decided_action: defer | constrained_response`.

This is the load-bearing reason `safety_class: True` exists. If
Kernos can't read its covenants, it doesn't proceed at full
strength.

## Cohort descriptor

| Field | Value |
|---|---|
| `cohort_id` | `"covenant"` |
| `execution_mode` | `ASYNC` |
| `timeout_ms` | `300` |
| `default_visibility` | `Restricted{reason: "covenant_set"}` |
| `required` | `True` |
| `safety_class` | `True` |

## What this adapter does NOT change

- `kernos/kernel/covenant_manager.py` unchanged.
  `validate_covenant_set` post-write hook continues to function
  exactly as today.
- `kernos/kernel/state.py` unchanged. Cohort uses existing
  `query_covenant_rules` read surface.
- V1 `CohortOutput` schema unchanged.

## Architectural placement

```
kernos/kernel/cohorts/
└── covenant_cohort.py          # the adapter (this spec)

kernos/kernel/integration/
└── runner.py                    # IntegrationInputs schema extension
                                 # + post-finalize safety enforcement
                                 # + safety-degraded fail-soft branch

kernos/kernel/cohorts/
└── runner.py                    # build_integration_inputs_from_fan_out
                                 # helper plumbs the safety-failures
                                 # field across the boundary
```

## Path forward

- **PRESENCE-DECOUPLING** — with all four cohorts (gardener,
  memory, covenant) producing real CohortOutputs and the
  safety-class policy now load-bearing, presence decoupling
  becomes reviewable against actual data flow.
- **INTEGRATION-WIRE-LIVE** — the final pipeline ship.
- **Future per-item visibility** — formal V1 schema extension
  if integration's reasoning needs to distinguish covenant
  categories at the visibility layer.
- **Future user-consent cohort** — second `safety_class: True`
  cohort for consent-shaped constraints; covenant adapter is
  the architectural template.
