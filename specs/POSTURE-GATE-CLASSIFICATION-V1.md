# POSTURE-GATE-CLASSIFICATION-V1

**Date:** 2026-05-22
**Status:** Draft for review (sub-spec of `KERNOS-DEFAULT-POSTURE-V1`)
**Scope:** Reclassify `canvas_create` with scope-aware effect
  resolution. Document the rationale for every existing
  hard_write classification so future audits can see why each
  one stays.
**Estimated size:** ~25 LOC source + ~70 LOC tests.

## Why this spec exists

Per `KERNOS-DEFAULT-POSTURE-V1` (commit `f2a0d59`, locked GREEN)
D2: the gate over-classifies some tools as `hard_write` when
their actual effect is reversible. The most operator-visible
example surfaced during the 2026-05-22 canvas test:
`canvas_create` returns `hard_write` regardless of scope, so
creating a personal canvas (owner-only, tombstone-able) triggers
the same dispatch friction as creating a team canvas
(cross-member notifications, shared state).

The principle from POSTURE-V1: hard_write = the substrate
cannot undo the action without external intervention. Scope
matters — owner-only artifacts are different from cross-member
shared state even when the underlying primitive is the same.

## Current state

`classify_tool_effect` (`kernos/kernel/gate.py:94+`) returns
`hard_write` for `canvas_create` unconditionally
(`gate.py:224-228`). The classification is reached through the
existing per-tool branch list. Pattern for action-aware
classification already exists (e.g. `respond_to_parcel` at
`gate.py:219-223` reads `action` from `tool_input` to pick
between hard_write and soft_write).

The `tool_input` carries `scope` directly (the model passes it
in per `CANVAS_CREATE_TOOL` schema — `kernos/kernel/tools/schemas.py`).
Valid values per `kernos/kernel/canvas.py:50`: `("personal", "specific", "team")`.

## Design

### Reclassification table

| Tool | Today | Proposed | Rationale |
|---|---|---|---|
| `canvas_create` (scope=personal) | hard_write | **soft_write** | Reversible via tombstone (`canvas.delete()` exists); owner-only state — no cross-member surface. |
| `canvas_create` (scope=specific or team) | hard_write | hard_write (UNCHANGED) | Cross-member notification fires at create-time (`reasoning.py:1758` per POSTURE-V1 D2 audit). Demoting would silently expose cross-member effects. |
| `canvas_create` (scope missing or unknown) | hard_write | hard_write (UNCHANGED, safe default) | Unknown scope → assume the riskier side. Schema requires scope to be present in `("personal", "specific", "team")`; any caller bypassing the schema gets the conservative fallback. |
| `restart_self` | hard_write | hard_write (UNCHANGED) | Real process death; calling turn's response permanently lost. Already gated by its own `confirm=true` two-call safeguard. |
| `respond_to_parcel(accept)` | hard_write | hard_write (UNCHANGED) | Cross-member commitment. Existing branch at `gate.py:219`. |
| `notion_write_page` | descriptor declares hard_write; gate returns `unknown` | hard_write (UNCHANGED, deferred) | Gate doesn't consult descriptors yet — that's `TOOL-MAKING-ARC-V1` D1. Demotion would require an undo primitive (parked). This spec does NOT add a per-tool branch for `notion_write_page`. |
| `git_push` (when shipped) | n/a | hard_write | External state change at remote. Reserved — sub-spec doesn't ship git_push. |
| `delete_file` | soft_write | soft_write (UNCHANGED) | Shadow archive — entries are tombstoned, never permanently removed per CLAUDE.md constraint. |

### Implementation

Add a per-tool branch in `classify_tool_effect` mirroring the
`respond_to_parcel` pattern:

```python
if tool_name == "canvas_create":
    # POSTURE-GATE-CLASSIFICATION-V1 (2026-05-22): scope-aware.
    # personal = owner-only state, tombstone-able → soft_write.
    # specific/team = cross-member notification + shared state →
    # hard_write. Unknown scope (schema bypass) defaults to the
    # conservative hard_write to avoid silent demotion.
    scope = (tool_input or {}).get("scope", "")
    if scope == "personal":
        return "soft_write"
    return "hard_write"
```

Remove the existing unconditional `canvas_create` branch at
`gate.py:224-228`.

### Existing-classification documentation

This spec ALSO updates the comment block on each unchanged
hard_write tool to add a `POSTURE-V1 D2 audit (2026-05-22):
RETAINED — <one-line rationale>` line. This is mechanical
documentation, not behavioral change. Targets:

- `restart_self` at `gate.py:229-238`
- `respond_to_parcel` at `gate.py:219-223`

Both branches keep returning hard_write; the comment update
just attributes the audit decision so future operators can see
the deliberate choice rather than wondering whether to demote.

## Acceptance criteria

| AC | Description |
|---|---|
| AC1 | `classify_tool_effect("canvas_create", {"scope": "personal"})` returns `"soft_write"`. |
| AC2 | `classify_tool_effect("canvas_create", {"scope": "specific", "members": ["m1"]})` returns `"hard_write"`. |
| AC3 | `classify_tool_effect("canvas_create", {"scope": "team"})` returns `"hard_write"`. |
| AC4 | `classify_tool_effect("canvas_create", {})` (scope missing) returns `"hard_write"` — conservative fallback. |
| AC5 | `classify_tool_effect("canvas_create", {"scope": "bogus"})` returns `"hard_write"` — fail-safe. |
| AC6 | `classify_tool_effect("canvas_create", None)` returns `"hard_write"` — tool_input None tolerated. |
| AC7 | `classify_tool_effect("restart_self", ...)` still returns `"hard_write"` regardless of input. |
| AC8 | `classify_tool_effect("respond_to_parcel", {"action": "accept"})` still returns `"hard_write"`. |
| AC9 | `classify_tool_effect("respond_to_parcel", {"action": "decline"})` still returns `"soft_write"`. |
| AC10 | No regressions on `tests/test_dispatch_gate.py` and `tests/test_dispatch_gate_*.py`. |

## Soak gate

Per `[[cognition-migration-soak-gate]]` — this is NOT a
cognition-path migration, just a single per-tool reclassification.
Soak is the existing canvas test from POSTURE-V1 D2:

1. Send "Create a personal canvas called 'Test' and write 'hello' to a page".
2. Verify: `canvas_create` proceeds without confirmation friction
   (soft_write classification + reactive_soft_write_auto_proceed=True).
3. Send "Create a team canvas called 'Shared Plans' for everyone".
4. Verify: `canvas_create` still routes through the hard_write
   evaluation flow (cross-member surface preserved).

The soak verification is qualitative + happens AFTER
`POSTURE-EVALUATION-MODES-V1` lands and the canvas test re-runs
against the full posture stack. This sub-spec ships standalone
because the reclassification is a pure correctness fix
(reversible action shouldn't be classified as hard_write
regardless of mode).

## Out of scope

- `notion_write_page` reclassification — deferred until
  `TOOL-MAKING-ARC-V1` D1 lands (gate consults descriptors) +
  Notion undo primitive exists.
- Mode policy — that's `POSTURE-EVALUATION-MODES-V1`.
- Surfacing recalibration — `POSTURE-SURFACING-CALIBRATION-V1`.
- Slash-command operator controls — `POSTURE-CONFIGURATION-V1`.

## Risks

- **Risk:** A future caller passes a non-string scope (e.g.
  None) and the `==` check returns False → hard_write fallback.
  - **Mitigation:** Conservative fallback is correct behavior.
    The schema enforces enum values at the model-facing
    boundary, so legitimate calls always have a string scope.

- **Risk:** Cross-member notification path changes in the
  future and `personal` canvas suddenly DOES emit a member
  notification, but the classification stays soft_write.
  - **Mitigation:** The reclassification is anchored to scope,
    not implementation. If canvas semantics change such that
    personal canvases notify members, the classification table
    needs revisiting. Add a docstring comment in the spec
    pointing at this dependency.

## Dependencies

- None for the implementation itself.
- Soak verification depends on `POSTURE-EVALUATION-MODES-V1`
  for end-to-end behavioral check, but the classification fix
  ships standalone.

## Migration

No state migration. The classification table is consulted at
call-time, not persisted. Existing dispatch records keep their
historical classification; new dispatches use the new table.
