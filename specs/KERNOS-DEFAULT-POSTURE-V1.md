# KERNOS-DEFAULT-POSTURE-V1 — Design spec

**Date:** 2026-05-22 (revised post-Codex round 1: YELLOW → 8 findings folded)
**Status:** Draft for review (design spec, not implementation)
**Scope:** Recalibrate Kernos's default posture so a fresh instance
  behaves like a "willing capable assistant" rather than a cautious
  gatekeeper. Covers two coupled surfaces: **paranoia** (seeded
  covenants, gate classifications, gate evaluation behavior) AND
  **tool surfacing** (intent-driven JIT availability, preflight
  visibility, withhold receipts, capability discovery). Both rooted
  in the same default-safe stance; both are tunable per-instance.
**Estimated size:** 0 LOC. This is a design.

## Why this spec exists

The operator's verbatim asks across 2026-05-22:

> "I have a lot of paranoia in Kernos about hard writes. After some
> thought I would like Kernos out of the box to be pretty behaviour
> neutral in this regard."

> "I'd like us to consider the real value of tools NOT being surfaced
> vs being surfaced and how we can improve their availability even if
> we have more and more tools."

Two coupled problems with the same root cause: Kernos's defaults
are **safe** rather than **neutral**. The substrate ships with a
gate model that classifies many actions as hard_write, a seeded
covenant table that defines several must_not rules, and a
surfacing layer whose default working set was tuned for caution.
The agent inherits all of this AND adds its own prompt-side
hesitation on top, producing a bot that's reflexively cautious
about anything mutating, even when the operator has clearly
authorized the work.

The bot itself articulated the design principle cleanly during a
canvas test (2026-05-22):

> "Not surfacing every tool all the time is correct. A giant tool
> surface is worse... The ideal design, to me, is not 'surface
> everything.' It's more like: small default working set;
> intent-driven just-in-time surfacing; capability discovery
> fallback; explicit effect classifications; preflight
> availability checks; escalation from read → write when
> authorized; receipt-backed failure mode."

> "Do not invent a personal preference against hard writes. If
> Kabe clearly authorizes an action and the surfaced/classified
> tool can perform it, proceed. Gate only where the substrate,
> ambiguity, shared impact, or specific covenants require it."

That's the target posture. This spec rewires the substrate
defaults to actually deliver it.

## Current state (truth, not memory)

(From 2026-05-22 audits across paranoia + surfacing surfaces.
Pin: recheck before each sub-spec implementation; surfaces drift.)

### Paranoia layer (the "is this safe to do" gate)

- **Seeded covenants** (`kernos/kernel/state.py:300-329`): 9 rules
  seeded into every new instance's `covenant_rule` table at first
  boot. Source=`default`. **HARD-CODED defaults; mutable runtime
  via `manage_covenants`.**
  - 1 spirit, 3 must_not, 2 must, 2 preference, 1 escalation.
  - Examples: "Never delete user's files unless asked," "Confirm
    before spending money," "Show drafts before sending to 3rd
    parties on open channels."
- **Gate classifications** (`kernos/kernel/gate.py:94-249`):
  hardcoded `read | soft_write | hard_write | unknown` per tool.
  Hard_write triggers via explicit hardcoded branches:
  `canvas_create`, `restart_self`,
  `respond_to_parcel(action="accept")`. Descriptor-bearing
  tools whose `.tool.json` declares hard_write (e.g.
  `notion_write_page`) currently classify as `unknown` —
  the gate consults the capability registry only, not the
  catalog/descriptors (Codex round 3 correction). The
  catalog-descriptor classification path is
  `TOOL-MAKING-ARC-V1` D1 future work; until that ships,
  the live-integration dispatcher refuses these as unknown
  rather than executing them.
- **Gate evaluation** (`gate.py:DispatchGate.evaluate`):
  full covenant + loss-cost model EXISTS as a code path. But
  **both** the conversational live path AND the
  live-integration dispatcher route through
  `LiveIntegrationDispatcher` (`live_wiring.py:363`), which
  classifies and refuses only `unknown` — it does NOT call
  `DispatchGate.evaluate` (Codex round 2 finding 3
  correction; the full policy gate is acknowledged-future per
  `TOOL-MAKING-ARC-V1` D1). So in v1 production today the
  evaluate model isn't running on the active dispatch path
  at all; the agent's hesitation comes from the prompt +
  covenant layer + the gate's classification refusal of
  unknown classifications. This spec's D3 (mode policy)
  therefore defines behavior that takes effect WHEN
  `TOOL-MAKING-ARC-V1`'s D1 lands and wires full evaluate
  onto the live path. **D3 must ship BEFORE TOOL-MAKING-ARC's
  D1** so the bot doesn't get more paranoid in the
  transition.

### Surfacing layer (the "is this tool available" gate)

- **Three-tier assemble pipeline**
  (`kernos/messages/phases/assemble.py:484-701`):
  pinned (24 always-surfaced tools) + active (8K token budget,
  LRU eviction with signal-driven ranking) + catalog scan
  (LLM-ranked by intent).
- **`ALWAYS_PINNED`** (`tool_catalog.py:51-84`): 20 hardcoded
  tools (remember, write_file, request_tool, consult,
  dump_context, etc.). Never evicted. ~25% of token budget.
- **`TOOL_TOKEN_BUDGET`** (env, default 8000): active zone size.
- **Catalog scan**: LLM ranks the remaining catalog by intent;
  top-N within budget surface for this turn.
- **`request_tool`** (`reasoning.py:1543`): agent-callable
  capability discovery. Adds to `space.active_tools` list; next
  turn's assemble picks up. Recently pinned (2026-05-21).
- **TOOL_SURFACING log**: structured (tier, surfaced count,
  total_available, evicted list). Not exposed beyond logs.

### What's NOT wired (the gaps)

- **`canvas_create` classified as hard_write** despite being
  reversible (tombstone-able). Triggers full gate model for
  what's effectively a soft_write.
- **No preflight tool availability check** — agent cannot ask
  "is X currently surfaced?" before claiming capability. This
  is the canvas-test gap the bot articulated.
- **No structured withhold receipts** — when a tool is evicted
  or withheld, only log lines; no queryable receipt.
- **No per-member surfacing differentiation** — all members see
  the same surface (modulo system-space tools).
- **Intent classification at surfacing is generic** — LLM-ranks
  by relevance; no explicit "this is a write-intent turn, prefer
  write tools" branch.
- **Gate-evaluation cannot be turned down** — no
  strict/balanced/permissive knob; current behavior is fixed.
- **Seeded covenants apply identically** — no per-instance
  posture-profile (e.g., "minimal," "default," "strict") seed
  selection.
- **Live-integration dispatcher's deferred full-evaluate
  cuts both ways**: today's live path is LOOSER than the spec
  suggests; if `TOOL-MAKING-ARC-V1`'s D1 sub-spec lands and
  adds full evaluate on the live path, the bot will GET MORE
  paranoid, not less, unless this spec ships first to tune
  the evaluation behavior.

## End-to-end contract — the calibrated posture

A fresh Kernos boot under default posture should:

1. **Seed only the minimum covenants** that capture genuinely
   important commitments (sensitivity, 3rd-party caution,
   ambiguity escalation). The "confirm before spending,"
   "show drafts before sending" rules become OPT-IN profiles,
   not defaults.
2. **Classify tools by their actual effect class**, with
   reversibility weighted: anything tombstone-able or
   undoable-via-edit defaults to soft_write. Hard_write reserved
   for genuinely irreversible (delete, restart, money, send-to-
   3rd-party).
3. **Default gate evaluation mode = permissive**: on clear
   authorization, proceed; only escalate when ambiguity AND
   irreversibility AND 3rd-party-impact intersect.
4. **Tool surfacing intent-aware**: catalog scan recognizes
   write-intent in the user message and prefers write tools
   for those turns; otherwise prefers conversational/read.
5. **Preflight availability check**: agent can call
   `inspect_tool_availability(tool_name)` to verify before
   claiming capability.
6. **Withhold receipts**: any time a tool is evicted from
   surface or withheld by policy, emit a structured
   `tool.withheld_from_surface` event with reason.
7. **Operator can tune all of the above** via env vars + a
   new `/posture` slash command for runtime adjustment.

## Design decisions

### D1 — Seeded covenants: minimal set + posture profile

Replace the 9-rule seed with a 4-rule MINIMUM (Codex round 1
finding 1: "sensitive info belongs to sharer" is a privacy
invariant, not hard-write paranoia — stays in minimal):
- spirit (warmth + judgment)
- must_not: sensitive info belongs to sharer (privacy invariant)
- escalation (ambiguity + irreversible + 3rd-party)
- preference: mention self-updates naturally (operator-visibility)

The other 5 rules become **profile-selectable**: a
`POSTURE_PROFILE` choice at first boot. Three profiles:

- `minimal` (DEFAULT) — just the 4 above
- `standard` — adds: "don't delete files unless asked,"
  "confirm spending," "show drafts before sending to 3rd
  parties"
- `strict` — adds all 9 current rules + a few more

Env var: `KERNOS_POSTURE_PROFILE=minimal|standard|strict`
(default: `minimal`).

**Migration**: existing instances are NOT touched. Their
current covenants stay. New seeding behavior applies to
new instances + an explicit `/posture reset-covenants
<profile>` slash command.

**Implementation sub-spec:** `POSTURE-SEEDED-COVENANTS-V1`.

### D2 — Gate classification: reclassify the over-paranoid

Per-tool reclassification (Codex round 1 findings 2 + 3 folded —
scope-aware canvas, no-op for notion until undo primitive
exists):

| Tool | Today | Proposed | Rationale |
|---|---|---|---|
| `canvas_create` (scope=personal) | hard_write | soft_write | Reversible (tombstone); personal canvas is owner-only state |
| `canvas_create` (scope=specific or team) | hard_write | **hard_write** | Stays — cross-member notification + shared state; the existing handler emits notifications to declared members (reasoning.py:1758). Demoting would silently expose cross-member effects. |
| `restart_self` | hard_write | hard_write | Stays — real process death |
| `respond_to_parcel(accept)` | hard_write | hard_write | Stays — cross-member commitment |
| `notion_write_page` | (descriptor declares hard_write; gate returns `unknown` today — see audit) | **descriptor stays hard_write** | When `TOOL-MAKING-ARC-V1` D1 lands and the gate consults catalog descriptors, this becomes effectively hard_write. Posture v1 preserves the descriptor classification; demotion would require an undo primitive (parked). |
| `git_push` (when shipped) | hard_write | hard_write | Stays — external state change |
| `delete_file` | soft_write | soft_write | Stays — already reversible via shadow archive |

The principle: hard_write = the substrate cannot undo it
without external action. Tombstone-able / overwrite-able /
append-with-history are soft_writes. **Scope matters**:
personal artifacts the owner controls are different from
cross-member shared state, even when the underlying primitive
is the same.

Implementation impact: `classify_tool_effect` for
`canvas_create` reads `tool_input.get("scope", "personal")` and
returns `soft_write` if `scope == "personal"`, else
`hard_write`. Mirrors the action-dependent branching for
`manage_covenants`, `manage_capabilities`, etc. already in
`gate.py:187+`.

**Implementation sub-spec:** `POSTURE-GATE-CLASSIFICATION-V1`.

### D3 — Gate evaluation mode: strict / balanced / permissive

`DispatchGate.evaluate` currently runs a fixed flow that errs
cautious. Introduce three modes via a **mode policy object**
(Codex round 1 finding 4 — preamble-only is insufficient
because the reactive-soft_write bypass at `gate.py:389` happens
BEFORE the model call; strict mode needs to alter control
flow, not just prompt wording):

```python
@dataclass(frozen=True)
class GateModePolicy:
    """Per-mode tuning of DispatchGate.evaluate's behavior.
    Read at evaluation time from the configured mode."""
    name: str  # "permissive" | "balanced" | "strict"
    # Bypass-rule overrides
    reactive_soft_write_auto_proceed: bool  # currently True
    reactive_hard_write_auto_proceed: bool  # NEW per mode
    # Model-call prompt preamble shift
    prompt_preamble: str
    # Fallback when the model returns ambiguous
    ambiguous_fallback: Literal["proceed", "confirm", "refuse"]
```

| Mode | reactive_soft_write_auto_proceed | reactive_hard_write_auto_proceed | ambiguous_fallback |
|---|---|---|---|
| `permissive` | True | False | proceed |
| `balanced` | True | False | confirm |
| `strict` | False | False | refuse |

Note (Codex round 2 finding 1): `is_reactive=True` only means
"responding to a user message," NOT "explicitly authorized
this exact hard write." We do NOT add a reactive-hard_write
auto-bypass in v1 even under permissive mode, because that
would let ambiguous hard writes skip the model entirely on
any user-triggered turn. Permissive mode differs from balanced
only in its `ambiguous_fallback` ("proceed" instead of
"confirm") + the prompt preamble's bias. Future spec MAY add
an explicit "authorized_action_signal" (set when the user's
message clearly names the exact action) that would unlock
reactive-hard_write_auto_proceed.

Plus a mode-aware prompt preamble that biases the model toward
the chosen posture (without overriding the fundamental
loss-cost reasoning).

Env var: `KERNOS_GATE_MODE=permissive|balanced|strict`
(default: `permissive`).

`DispatchGate.__init__` reads the env once, stores the
`GateModePolicy`, evaluate-path branches consult it. Restart
re-reads (env-only at construction; `/posture mode` slash
command updates the live store + signals the gate to swap
policy objects).

**Implementation sub-spec:** `POSTURE-EVALUATION-MODES-V1`.

### D4 — Tool surfacing: intent-aware + per-member hook

Two changes to the catalog scan (`assemble.py:484-701`).
Codex round 1 finding 5: an explicit pre-classification LLM
call is redundant — the catalog scan already uses the user
message at `assemble.py:633`. Use cheap local heuristics +
deterministic ranking boosts:

1. **Local intent classifier** (substrate-internal regex / keyword
   matcher; ~50-100 LOC, no LLM call). Classifies the user
   message into a small effect-set: `{read, write, delete,
   send, spend, schedule}`. Examples:
   - "write" "create" "add" "save" "post" "edit" "update" "make"
     → write
   - "delete" "remove" "drop" "archive" "cancel" → delete
   - "send" "email" "message" "ping" → send
   - "schedule" "remind" "later" "tomorrow" → schedule
   - default: read (no intent signal)
2. **Deterministic ranking boost**: the existing catalog scan
   gets the intent set as extra input. Tools whose declared
   effect classification matches the intent get a boost in
   the LLM rank. NOT a re-call; the existing scan's prompt
   gains a "user intent appears to be: write" line.
3. **Per-member surfacing hook**: optional callback the
   surfacer consults to filter or boost tools per member
   identity. Default: no-op (preserves current behavior). A
   future per-member-config spec can wire actual policies.

Plus: **`canvas_create` + `page_write` become co-surfaced** —
when one is in the pinned/active zone and the conversation has
a recent canvas context, the other is auto-promoted. Closes
the "I can read canvas but can't write to it" surface gap
without per-tool eviction handling. Implementation: a
small association table `_CO_SURFACING_PAIRS` consulted by
the active-zone ranker.

**Implementation sub-spec:** `POSTURE-SURFACING-CALIBRATION-V1`.

### D5 — Preflight tool availability check

New agent-callable kernel tool: `inspect_tool_availability`.
**Pinned in the always-surfaced set** (Codex round 1 finding 6 —
would be useless if it could itself be evicted; pin is required
for the preflight contract to hold). Read classification.

```python
inspect_tool_availability(
    tool_name: str,
) -> {
    "available": bool,
    "tier": str,           # "pinned" | "active" | "catalog" | "absent"
    "reason_if_absent": str, # "evicted_for_budget" | "not_registered" |
                              # "withheld_by_policy" | "disabled_service"
    "request_tool_suggestion": str,  # textual hint or empty
}
```

Backed by a **per-turn surfacing snapshot** captured at the
end of the assemble phase. The snapshot records: tier of each
catalog tool for THIS turn + reason for absence (if any).
Lookup is O(1) against the snapshot dict; no re-running the
ranker.

Why backed by a snapshot rather than re-evaluating: surfacing
is per-turn and uses the user message for ranking; mid-turn
re-evaluation would need to reconstruct the same input.
Snapshot is constructed once + held until next turn's assemble.

Agent usage:

> "I can write to that canvas — `inspect_tool_availability(tool_name="page_write")`
> → `available=True, tier='active'`. Proceeding."

vs.

> "`page_write` isn't currently surfaced
> (`inspect_tool_availability` → `tier='absent', reason='evicted_for_budget'`).
> Calling `request_tool(capability_name='canvas', description='I need page_write to update the canvas')`
> to add it to next turn's surface."

Note (Codex round 2 finding 2): `request_tool` activates
**connected MCP capabilities** only (`reasoning.py:1561`); it
does NOT promote kernel/catalog tools. For tools sourced
differently the preflight surface returns source-aware
suggestions:

- `source=mcp_capability` → suggestion =
  `request_tool(capability_name="<cap>", description="...")`
- `source=kernel` AND `tier=absent (evicted_for_budget)` →
  suggestion = "Tool is registered but evicted from this
  turn's surface. It will re-rank on a turn whose intent
  matches its effect class; alternatively raise
  `KERNOS_TOOL_TOKEN_BUDGET` per `POSTURE-CONFIGURATION-V1`."
- `source=stock` (e.g. canvas tools) AND `tier=absent` →
  suggestion = "Tool is registered but not surfaced. The
  `POSTURE-SURFACING-CALIBRATION-V1` intent-aware ranker
  should promote it when intent matches; if your intent is
  clear, retry after re-stating it explicitly."

A future spec MAY extend `request_tool` to support kernel +
stock + workspace tool promotion (a "force-pin for next turn"
shape). v1 keeps `request_tool` MCP-only and surfaces the
distinction through the preflight tool's structured
suggestion field.

**Implementation sub-spec:** part of `POSTURE-SURFACING-CALIBRATION-V1`.

### D6 — Structured withhold receipts

New event type: `tool.withheld_from_surface`. Emitted by the
surfacer when:
- A tool is evicted due to budget pressure
- A tool is filtered by service-disabled
- A tool is filtered by policy (future per-member hook)

Payload:
```json
{
    "tool_name": "page_write",
    "reason": "evicted_for_budget",
    "tier_attempted": "active",
    "instance_id": "...",
    "space_id": "...",
    "turn_id": "..."
}
```

Operator can grep / query the event stream for withhold
events. Friction observer can detect patterns
("page_write withheld 5x while user asked to write" → surface
to operator).

**Implementation sub-spec:** part of `POSTURE-SURFACING-CALIBRATION-V1`.

### D7 — Configuration model: env + `/posture` command

Posture state lives in TWO layers (Codex round 1 finding 7 —
in-memory runtime mutation is too surprising across restarts;
slash-command changes MUST persist):

- **Env vars apply at first-seed AND as fallback defaults
  when persisted state is absent**:
  - `KERNOS_POSTURE_PROFILE=minimal|standard|strict`
  - `KERNOS_GATE_MODE=permissive|balanced|strict`
  - `KERNOS_TOOL_TOKEN_BUDGET=8000` (existing)
- **Persisted instance posture config** (new table
  `instance_posture` keyed by `instance_id`, single row per
  instance) stores the LIVE settings:
  - `gate_mode` (current mode)
  - `posture_profile` (last applied profile)
  - `last_updated_at`, `last_updated_by`

Resolution order at each read:
1. Persisted instance_posture row (if present)
2. Env var (if persisted row absent or field NULL)
3. Hardcoded default

**`/posture` slash command** (owner-only, runtime):
  - `/posture` — show current posture (from persisted row)
  - `/posture profile <name>` — change covenant profile
    (UPDATE persisted row; only affects FUTURE covenants —
    existing covenants stay unless explicitly reset)
  - `/posture mode <name>` — change gate evaluation mode
    (UPDATE persisted row; gate re-reads on next evaluate)
  - `/posture reset-covenants <profile>` — drop current
    covenants + reseed from profile (requires CONFIRM token
    per `DURABLE-APPROVAL-RECEIPTS-V1`)

All slash-command mutations write the persisted row so
restart, self-update, and execv all preserve the operator's
chosen posture.

**Implementation sub-spec:** `POSTURE-CONFIGURATION-V1`. Builds
on `DURABLE-APPROVAL-RECEIPTS-V1` for the CONFIRM token shape.

### D8 — Per-spec relationship to existing arcs

This spec interacts with three other locked specs:

- **`TOOL-MAKING-ARC-V1`** (commit ca3fa65): its D1 sub-spec
  proposes adding full `DispatchGate.evaluate` on the live
  dispatch path. Without this posture spec, that change makes
  the bot MORE paranoid. With this spec, the new full-evaluate
  reads the gate mode (D3) and behaves permissively by default.
  **Ship this spec's D3 BEFORE TOOL-MAKING-ARC-V1's D1.**
- **`KERNOS-AUTONOMOUS-IMPROVEMENT-LOOP-V1`** (commit ca3fa65):
  its D7 makes operator approval mandatory at the commit gate.
  **This stays mandatory regardless of posture mode** (Codex
  round 1 finding 8 — the locked autonomous-loop spec
  explicitly says fully-autonomous commit is not v1). Permissive
  mode CAN auto-approve `improve_kernos` initiation (starting an
  attempt) but the commit/push gate ALWAYS requires the operator
  receipt. The autonomous-loop's safety contract takes
  precedence over the posture mode.
- **`SELF-CONTROLLED-LOOP-LIVENESS-V1`** (commit 2758538):
  shipped; no interaction.

## Sub-spec sequence

Lock this design first. Then ship in this order:

1. **`POSTURE-SEEDED-COVENANTS-V1`** (D1). Minimal seed +
   `KERNOS_POSTURE_PROFILE` env + migration policy. No deps.
2. **`POSTURE-GATE-CLASSIFICATION-V1`** (D2). Reclassify
   canvas_create + notion_write_page + document each. No deps.
3. **`POSTURE-EVALUATION-MODES-V1`** (D3). Three modes +
   `KERNOS_GATE_MODE` env + mode-aware prompt template. No
   deps (gate code change only).
4. **`POSTURE-SURFACING-CALIBRATION-V1`** (D4 + D5 + D6).
   Intent-aware ranking + co-surfacing pairs +
   `inspect_tool_availability` kernel tool +
   `tool.withheld_from_surface` event emission. Bigger; ships
   after #1-3.
5. **`POSTURE-CONFIGURATION-V1`** (D7). `/posture` slash
   command + runtime mode switching + reset-covenants flow.
   Depends on the prior 4 + reuses
   `DURABLE-APPROVAL-RECEIPTS-V1` for CONFIRM tokens.
6. **End-to-end validation**: fresh-boot smoke + posture
   switching smoke + autonomous-loop interaction smoke.

Each sub-spec: Codex spec review → implementation → Codex
code review → ship.

## What this spec explicitly does NOT define

- A complete tool-effect taxonomy beyond read/soft/hard.
  v1 stays with the existing three-level classification + a
  documentation pass that captures rationale per-tool. A
  finer-grained taxonomy (delete vs spend vs send vs cross-
  member) is parked.
- Per-member posture differentiation. Member identity is a
  first-class concept in Kernos, but posture v1 is
  instance-global. Per-member overrides land in a future
  spec.
- Automatic re-evaluation of existing instances on upgrade.
  Old instances keep their current state; explicit operator
  action (`/posture reset-covenants`) triggers the migration.
- A UI/canvas for posture management. v1 is env + slash
  commands.
- Anything about MCP discovery or non-Kernos-substrate tools'
  surface posture.

## Validation gates

Fresh-boot smoke:
1. Boot a default-profile instance.
2. Send "Create a personal canvas called 'Test' and write
   'hello' to its index page."
3. Expected:
   - `canvas_create` proceeds without confirmation (soft_write
     under D2).
   - `page_write` proceeds without confirmation.
   - No "I can't do hard writes" hesitation from the agent
     (covenants don't include the spending/draft rules under
     minimal profile).
4. Verify: 0 dispatch.gate model-evaluations triggered (no
   loss-cost call needed under permissive mode).

Posture switching smoke (Codex round 2 finding 5 — aligned
with the actual D3 + D7 contracts; mode change is NOT
CONFIRM-gated, only reset-covenants is):
1. `/posture mode strict` — updates the persisted
   instance_posture row directly (no CONFIRM required;
   reversible via `/posture mode permissive`).
2. Repeat the canvas test.
3. Expected: agent's gate.evaluate path returns
   `ambiguous_fallback="refuse"` more often + the strict
   prompt preamble biases the model toward caution. Soft_write
   reactive bypass disabled (per D3 table). Hard_write reactive
   bypass disabled as in permissive too. Net effect: writes
   that would have auto-proceeded under permissive now route
   through the model + confirm-or-refuse on ambiguity.

Autonomous-loop interaction:
1. Default permissive posture.
2. `improve_kernos` invoked (when that sub-spec ships).
3. Verify: commit gate still REQUIRES operator approval
   (D8 — hard_write + external-state-change always gated
   regardless of posture mode).

## Risk

- **Migration confusion**: existing instances on standard
  (current) covenants don't auto-rebase to minimal. Operator
  surprise: "I changed the profile env var but my bot still
  has the old rules." Mitigation: explicit `/posture reset-
  covenants` command + documentation that env applies at
  first-seed only.
- **Over-permissive defaults harm a different user**: not
  every operator wants minimal posture. Mitigation: profile
  choice + tunable per env + the bot's own judgment still
  applies (model-side caution doesn't disappear; only
  substrate friction does).
- **Re-classification breaks downstream assumptions**: if
  some other module depends on canvas_create being hard_write
  for special-case handling, demoting it might break that
  flow. Mitigation: pre-audit before sub-spec impl; grep
  for `canvas_create.*hard_write` + similar literals.
- **Intent heuristic misclassifies**: the local regex/keyword
  classifier (D4) may misread user intent — e.g., "do you
  remember when I deleted this?" reads as "delete intent"
  via keyword. Mitigation: heuristic is a RANKING BOOST,
  not a filter. Misclassification just changes the rank order
  inside the catalog scan; the LLM ranker still has final say.
  Worst case is suboptimal ranking, not silently withheld
  tools. Monitor via the `tool.withheld_from_surface` events
  (D6) — if the operator sees the same tool repeatedly
  withheld on write-intent turns, the heuristic table needs
  tuning.
- **Withhold receipts add event volume**: every surfacing
  decision could emit. Mitigation: emit only on EVICTION
  (not on natural exclusion); rate-limit per (tool, reason)
  to avoid storms.

## Acceptance for this design spec

GREEN when Codex agrees:

1. Current-state audit matches the codebase.
2. The two coupled problems (paranoia + surfacing) are
   correctly decomposed into the 8 design decisions.
3. Sub-spec sequence is correct + the D8 ordering note (this
   spec's D3 before TOOL-MAKING-ARC's D1) is correct.
4. The autonomous-loop interaction model (D8) doesn't break
   the loop's safety contract.
5. The migration story (don't touch existing instances; rely
   on explicit reset) is operationally sane.
6. The 3-validation gate is sufficient to call the recalibration
   "alive."
