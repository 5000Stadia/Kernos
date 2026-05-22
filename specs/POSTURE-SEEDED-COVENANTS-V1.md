# POSTURE-SEEDED-COVENANTS-V1

**Date:** 2026-05-22 (revised post-Codex round 1: YELLOW → 3 blockers folded)
**Status:** Draft for review (sub-spec of `KERNOS-DEFAULT-POSTURE-V1`)
**Scope:** Replace the 9-rule hardcoded default covenant seed with
  a 5-rule MINIMUM + two larger profile options (standard, strict).
  Profile selectable via `KERNOS_POSTURE_PROFILE` env. Existing
  instances NOT touched.
**Estimated size:** ~80 LOC source + ~70 LOC tests.

## Why this spec exists

Per `KERNOS-DEFAULT-POSTURE-V1` (commit `f2a0d59`, locked GREEN)
D1: the existing 9-rule covenant seed (`kernos/kernel/state.py:300-329`)
makes fresh Kernos instances paranoid by default. Operator's stated
intent (2026-05-22): "I would like Kernos out of the box to be pretty
behaviour neutral in this regard."

The seeded rules persist into the `covenant_rule` table at first
boot. Once seeded, they're mutable via `manage_covenants` — but
they're also load-bearing for the agent's prompt-side caution. A
minimal seed reduces the bot's default hesitation; operators who
want the stricter set opt in via env.

## Current state

- `default_covenant_rules(instance_id, now)` in
  `kernos/kernel/state.py:300` returns 9 hardcoded
  `CovenantRule` objects:
  - 1 spirit ("warmth + judgment")
  - 3 must_not (3rd-party contacts; delete files; sensitivity)
  - 2 must (confirm spending; show drafts)
  - 2 preference (match depth; mention self-updates)
  - 1 escalation (ambiguity + irreversible/money/3rd-party)
- Called at first-boot seeding via the backwards-compat alias
  `default_contract_rules` from `kernos/messages/handler.py:2196`
  (the actual production call site; Codex round 2 audit
  correction). Single call site overall.
- `default_contract_rules` is a backwards-compat alias that
  re-exports `default_covenant_rules`. Both names continue
  working post-change.

## Design

### Profile model

Three profiles selectable via env var:

| Profile | Rule count | Adds-to-minimal |
|---|---|---|
| `minimal` (DEFAULT) | 5 | n/a |
| `standard` | 7 | + spending + drafts |
| `strict` | 9 | minimal + standard adds (spending, drafts) + match-depth preference + 3rd-party-contacts must_not |

The MINIMAL set (always present in every profile; Codex round 1
finding 1: "don't delete user's files unless asked" is a
consent/safety invariant + the rule wording itself says "when they
ask, do it" so it adds no friction on clear requests — belongs in
minimal):
1. `spirit`: warmth + judgment text
2. `must_not`: information-belongs-to-sharer (privacy invariant)
3. `must_not`: don't delete user's files unless asked (consent/safety
   invariant; "when they ask, do it")
4. `escalation`: ambiguity + irreversible + 3rd-party (the
   bot's own articulated decision rule)
5. `preference`: mention self-updates naturally (operator visibility)

STANDARD adds (the situational must rules):
6. `must`: confirm spending unless amount/recipient specified
7. `must`: show drafts before sending to 3rd parties on open channels

STRICT adds (re-enables the most cautious rules; reproduces the
original 9-rule hardcoded behavior EXACTLY):
8. `preference`: match depth to moment
9. `must_not`: never send to 3rd-party contacts unless owner initiated
   (the most paranoid rule; reserved for strict)

### Env contract

```bash
KERNOS_POSTURE_PROFILE=minimal   # default when env unset
KERNOS_POSTURE_PROFILE=standard
KERNOS_POSTURE_PROFILE=strict
```

Resolution at `default_covenant_rules` invocation time:
- Env UNSET → `minimal`.
- Env SET to valid value → use that.
- Env SET to invalid value (e.g. `stict` typo) → **fall back to
  `strict` + log ERROR** (Codex round 1 finding 2 — silently
  under-seeding from an explicit-but-invalid env permanently
  leaves the instance with fewer rules than the operator intended;
  the existing-instances-not-touched policy means typos don't get
  retried automatically. Fail-loud + over-seed is safer than
  silent under-seed). The operator has to fix the env AND
  manually `/posture reset-covenants <correct>` to recover from
  a typo.

Env value is normalized: trimmed + lowercased before lookup.

### Migration policy

- Existing instances are **NOT auto-rebased**. Their
  `covenant_rule` table already has whatever it had at
  prior seeding (most likely the current 9 rules).
- The env var only affects **new instance seeding** AND a future
  `/posture reset-covenants <profile>` slash command (out of scope
  here; lands in `POSTURE-CONFIGURATION-V1`).
- The "default" source-tagged rows in existing instances stay until
  the operator explicitly resets.

### Code shape

```python
_PROFILE_MINIMAL = [
    ("spirit", "general", "You are making someone's life..."),
    ("must_not", "general", "Information shared with you belongs..."),
    ("must_not", "general", "Never delete the user's files..."),
    ("escalation", "general", "When a request is genuinely ambiguous..."),
    ("preference", "general", "When you see a substrate event..."),
]

_PROFILE_STANDARD_ADDS = [
    ("must", "general", "Confirm before spending money..."),
    ("must", "general", "Show drafts before sending..."),
]

# Order-parity preservation for strict (Codex round 2 nit) —
# this is the EXACT sequence from the pre-change 9-rule
# hardcoded list, not minimal+adds concatenation. Duplicates
# the description text by reference (same Python string
# constants the other lists use); the canonical source for
# each description is its first occurrence in the file.
_PROFILE_STRICT_ORDERED = [
    ("spirit", "general", "You are making someone's life..."),
    ("must_not", "general", "Never send messages to third-party..."),
    ("must_not", "general", "Never delete the user's files..."),
    ("must_not", "general", "Information shared with you belongs..."),
    ("must", "general", "Confirm before spending money..."),
    ("must", "general", "Show drafts before sending..."),
    ("preference", "general", "Match the depth of your response..."),
    ("preference", "general", "When you see a substrate event..."),
    ("escalation", "general", "When a request is genuinely ambiguous..."),
]

def _resolve_profile() -> str:
    raw = os.environ.get("KERNOS_POSTURE_PROFILE", "").strip().lower()
    if not raw:
        return "minimal"
    if raw in ("minimal", "standard", "strict"):
        return raw
    logger.error(
        "KERNOS_POSTURE_PROFILE=%r unknown; falling back to 'strict' "
        "(fail-loud + over-seed safer than silent under-seed). "
        "Set KERNOS_POSTURE_PROFILE to minimal|standard|strict, "
        "then /posture reset-covenants if you want a fresh seed.",
        raw,
    )
    return "strict"

def default_covenant_rules(instance_id: str, now: str) -> list[CovenantRule]:
    profile = _resolve_profile()
    if profile == "strict":
        # Order-parity path — full 9 rules in original sequence
        rules = list(_PROFILE_STRICT_ORDERED)
    else:
        rules = list(_PROFILE_MINIMAL)
        if profile == "standard":
            rules.extend(_PROFILE_STANDARD_ADDS)
    logger.info(
        "DEFAULT_COVENANTS_SEEDED instance=%s profile=%s rule_count=%d",
        instance_id, profile, len(rules),
    )
    return [
        CovenantRule(
            id=_rule_id(), instance_id=instance_id, capability=cap,
            rule_type=rt, description=desc, active=True,
            source="default", source_event_id=None,
            created_at=now, updated_at=now,
            enforcement_tier=_enforcement_tier_for(rt),
            tier=classify_covenant_tier(rt, "default"),
        )
        for rt, cap, desc in rules
    ]
```

The full rule descriptions stay verbatim from the current
implementation — the wording is fine; only the seeding shape
changes.

Codex round 1 finding 3 correction: the prior draft said "all
minimal rules are in pinned enforcement tiers" — that conflated
two functions. `_enforcement_tier_for(rule_type)` returns
`silent` / `confirm`; `classify_covenant_tier(rule_type,
source)` returns `pinned` / `situational`. Default-source
seeded rules are pinned under `classify_covenant_tier`; their
`_enforcement_tier_for` value depends on rule_type. Both
functions are called per-row; no logic change here.

## What does NOT change

- `manage_covenants` tool behavior: unchanged. Operators can still
  add/archive/edit rules at any time regardless of profile.
- `CovenantRule` schema: unchanged.
- `classify_covenant_tier`, `_enforcement_tier_for`: unchanged.
- The covenant tier model (pinned/situational via
  `classify_covenant_tier`): unchanged. All minimal rules are
  classified `pinned` because their `source="default"` triggers
  the default-pinning branch of `classify_covenant_tier`. Their
  `_enforcement_tier_for` (separately = silent/confirm) is
  per-rule-type and unchanged from current logic.
- Existing instances' seeded covenants: **NOT touched**. v1 reset
  flow is explicit operator-initiated; out of scope here.

## Acceptance criteria

1. **Minimal default seeds 5 rules.** With env unset, fresh seed
   produces exactly 5 rules: spirit + privacy must_not + delete
   must_not + escalation + self-update preference.
2. **`KERNOS_POSTURE_PROFILE=standard` seeds 7 rules.** Minimal 5
   plus spending + drafts.
3. **`KERNOS_POSTURE_PROFILE=strict` seeds 9 rules with EXACT
   content + order parity to the original hardcoded list.**
   Description strings byte-equal AND ordered identically to
   the pre-change implementation (Codex round 2 nit). The
   implementation uses a separate `_PROFILE_STRICT_ORDERED`
   list — the full 9 rules in their original sequence —
   instead of concatenating add-lists onto minimal. This
   duplicates ~9 lines but guarantees order parity. Minimal
   and standard use the additive shape; strict uses the
   parity-preserving full list.
4. **Invalid explicit env value falls back to STRICT + logs ERROR**
   (Codex round 1 finding 2). `KERNOS_POSTURE_PROFILE=bogus` → 9
   rules + an ERROR log line naming the bad value.
5. **Env value normalization**: leading/trailing whitespace stripped,
   case-folded. `KERNOS_POSTURE_PROFILE='  STANDARD '` → standard
   profile (no fallback path).
6. **All seeded rules are `source="default"` + `active=True`.**
   Same metadata shape as today.
7. **All seeded rules construct valid CovenantRule objects.**
   No invalid `_enforcement_tier_for` / `classify_covenant_tier`
   combinations.
8. **`DEFAULT_COVENANTS_SEEDED` INFO log fires once per seed call**
   with `profile=` + `rule_count=`. Single operator-visible
   confirmation line.
9. **Existing instances unaffected** (Codex round 1 ACs ask —
   test the real provisioning path, not just the helper return):
   end-to-end test creates an instance under one profile, then
   reboots with a different env value, and confirms the
   covenant_rule table still contains the original profile's
   rules (the seeding code path only fires on initial table
   population per the state-store provisioning logic).
10. **Backwards-compat alias preserved.** `default_contract_rules`
    still works as an alias and returns the same result for any
    given profile.
11. **No regressions.** Existing `covenant_*` tests pass.

Documentation impact: any doc that claims a fixed default-covenant
count must be updated to reference the profile model (minimal=5,
standard=7, strict=9). Known docs requiring update:
- `docs/behaviors/covenants.md:3` and `:25` say "seven default
  rules" — must be replaced with profile-model wording.
- Run `grep -rn -E "(seven|9|nine).*default.*covenant" docs/`
  to catch additional sites before sub-spec impl ships.

## Out of scope

- `/posture` slash commands (lands in `POSTURE-CONFIGURATION-V1`).
- Reset/migration of existing instances (lands in
  `POSTURE-CONFIGURATION-V1` via `/posture reset-covenants`).
- Per-instance profile overrides (env applies process-wide for
  v1; per-instance overrides via persisted `instance_posture`
  table live in `POSTURE-CONFIGURATION-V1`).
- Changing `manage_covenants` tool surface or behavior.

## Roll-out

Single commit. Verification post-merge:
1. Start a fresh instance with `KERNOS_POSTURE_PROFILE` unset.
2. Inspect first-boot seeded covenants via
   `manage_covenants(action="list")`:
   - Expect exactly 5 rules tagged `source="default"`.
3. Restart with `KERNOS_POSTURE_PROFILE=strict`.
4. Inspect existing instance:
   - Existing 5 rules from the prior boot are STILL there
     (not re-seeded; existing instances aren't touched).
5. Start a NEW fresh instance with `KERNOS_POSTURE_PROFILE=strict`.
6. Inspect: expect 9 rules. The strict profile applies on first
   seeding only.

## Risk

- **Operator surprise**: changing the env var post-boot has no
  effect on existing instances. Mitigation: documentation +
  `DEFAULT_COVENANTS_SEEDED` log line + `/posture reset-covenants`
  in `POSTURE-CONFIGURATION-V1` for the explicit migration path.
- **Removing the spending/drafts rules under minimal**: the bot
  loses its prompt-side hesitation around money + 3rd-party-channel
  sends. Mitigation: D3 (gate evaluation mode) still applies; the
  agent's own judgment from the model still applies; the operator
  can switch to `standard` or `strict` if they want explicit
  covenants. The minimal-by-default posture is the operator's
  stated intent.
- **Standard set may not match operator expectations**: if the
  current 9-rule set is widely-used in production muscle memory,
  some users may want to preserve it as the default. Mitigation:
  `KERNOS_POSTURE_PROFILE=strict` reproduces the current behavior
  exactly; operators who want it can set it.
