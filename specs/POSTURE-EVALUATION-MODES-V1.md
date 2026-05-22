# POSTURE-EVALUATION-MODES-V1

**Date:** 2026-05-22
**Status:** Draft for review (sub-spec of `KERNOS-DEFAULT-POSTURE-V1`)
**Scope:** Introduce three gate evaluation modes
  (`permissive` / `balanced` / `strict`) via a `GateModePolicy`
  object that alters control flow in `DispatchGate.evaluate`,
  not just prompt wording. Default = `permissive` (behavior-
  neutral out of the box).
**Estimated size:** ~120 LOC source + ~150 LOC tests.
**Blocking dependency:** `TOOL-MAKING-ARC-V1` D1 must NOT ship
  before this lands. The descriptor-driven gate consult depends
  on a stable mode contract.

## Why this spec exists

Per `KERNOS-DEFAULT-POSTURE-V1` (commit `f2a0d59`, locked
GREEN) D3: `DispatchGate.evaluate` currently runs a fixed flow
that errs cautious — ambiguous model responses default to
`confirm`, surfacing a friction prompt to the user even when
the user's intent appears clear. Operator's stated intent
(2026-05-22): "I would like Kernos out of the box to be pretty
behaviour neutral in this regard."

The mode policy lets operators configure how cautious the gate
is without changing covenant or classification logic. Three
discrete profiles cover the realistic operational spectrum.

## Current state

`DispatchGate.evaluate` (`kernos/kernel/gate.py:354+`) runs:

1. Denial tracking (per-tool consecutive block limit)
2. Approval token validation
3. Permission override check (`always-allow` per capability)
4. **Reactive soft_write bypass** (`gate.py:404+`):
   `is_reactive=True` + `effect=="soft_write"` + no relevant
   must_not rule → auto-approve, skip model call.
5. **Model evaluation** (`_evaluate_model`, `gate.py:452+`):
   Returns one of APPROVE / CONFLICT / CLARIFY / default-CONFIRM.

The current control-flow is hard-coded. There is no env var or
runtime configuration for mode. The reactive-soft_write bypass
at step 4 is `if is_reactive and effect == "soft_write": ...`
with no mode-awareness.

The "ambiguous" branch (model returns something other than
APPROVE / CONFLICT / CLARIFY) currently maps to `reason="confirm"`
at `gate.py:583-586`. This is what surfaces a confirm prompt
to the user.

`_evaluate_model`'s system prompt (`gate.py:509-536`) is also
hard-coded — no per-mode preamble.

## Design

### Mode policy object

```python
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class GateModePolicy:
    """Per-mode tuning of DispatchGate.evaluate's behavior.

    Read at construction time. The policy is consulted at three
    branch points: reactive-soft_write bypass eligibility, the
    model system-prompt preamble, and the fallback for ambiguous
    model responses.
    """
    name: str  # "permissive" | "balanced" | "strict"

    # Bypass-rule overrides
    reactive_soft_write_auto_proceed: bool

    # NEW (reserved for future spec): when set, an explicit
    # "the user named this exact action" signal also bypasses
    # for hard_write. v1 holds this False across ALL modes per
    # POSTURE-V1 D3 Codex round 2 finding 1 — adding the
    # auto-bypass requires a stronger signal than is_reactive=True.
    reactive_hard_write_auto_proceed: bool

    # System-prompt preamble injected at the top of
    # _evaluate_model's system_prompt. Mode-specific wording
    # that biases the model toward the chosen posture WITHOUT
    # overriding the fundamental APPROVE/CONFIRM/CONFLICT
    # decision criteria.
    prompt_preamble: str

    # What to do when the model returns CONFIRM or an
    # unparseable response (the "ambiguous" branch).
    # - "proceed" → translate to allowed=True with
    #   reason="approved_by_mode" so the dispatch flows.
    # - "confirm" → current behavior (block with
    #   reason="confirm", surfaces confirm prompt).
    # - "refuse"  → block with reason="refused_by_mode",
    #   no confirm prompt offered (operator opted into
    #   strict pure-block).
    ambiguous_fallback: Literal["proceed", "confirm", "refuse"]
```

### Policy table

| Mode | reactive_soft_write_auto_proceed | reactive_hard_write_auto_proceed | ambiguous_fallback | Preamble bias |
|---|---|---|---|---|
| `permissive` | True | False | `proceed` | "Default to APPROVE unless a covenant clearly blocks the action." |
| `balanced` | True | False | `confirm` | "Default to APPROVE for reactive actions matching the user's request. Use CONFIRM when the action exceeds the request." |
| `strict` | False | False | `refuse` | "Default to CONFIRM unless the action is read-only or the user named this exact action verbatim." |

CLARIFY responses (model's explicit "this is genuinely
ambiguous") are NEVER subject to `ambiguous_fallback` — they
always block with `reason="clarify"`. That's a distinct signal
from the unparseable/default-CONFIRM ambiguity that
`ambiguous_fallback` governs.

### Env var

```
KERNOS_GATE_MODE=permissive|balanced|strict   (default: permissive)
```

Resolution semantics (mirror `KERNOS_POSTURE_PROFILE` from
SEEDED-COVENANTS-V1):
- Unset → `permissive` (default).
- Set to a known value → that mode.
- Set to an unknown value → `strict` + ERROR log (fail-loud +
  fall-safe — silent permissive on a typo would silently
  loosen the gate).

### Integration

1. **`DispatchGate.__init__`** reads the env once at construction,
   resolves to a `GateModePolicy`, stores on `self._mode_policy`.
   No mid-life mutation in v1 (slash command `/posture mode`
   defers to `POSTURE-CONFIGURATION-V1`; will swap policy via
   `set_mode_policy(policy)`).

2. **Step 4 (reactive soft_write bypass)** consults
   `self._mode_policy.reactive_soft_write_auto_proceed`. If
   False (strict), skip the bypass entirely — every soft_write
   goes through model evaluation.

3. **`_evaluate_model`** injects `self._mode_policy.prompt_preamble`
   at the top of `system_prompt`, BEFORE the existing rule
   block. The preamble shifts the model's bias but doesn't
   override its CONFLICT/CLARIFY/APPROVE detection.

4. **Ambiguous-branch fallback** at `gate.py:583-586`:
   ```python
   # Was: return GateResult(allowed=False, reason="confirm", ...)
   # Becomes:
   if self._mode_policy.ambiguous_fallback == "proceed":
       return GateResult(
           allowed=True, reason="approved_by_mode",
           method=f"mode_{self._mode_policy.name}",
           raw_response=raw,
       )
   if self._mode_policy.ambiguous_fallback == "refuse":
       return GateResult(
           allowed=False, reason="refused_by_mode",
           method=f"mode_{self._mode_policy.name}",
           proposed_action=action_desc, raw_response=raw,
       )
   # ambiguous_fallback == "confirm" — current behavior
   return GateResult(
       allowed=False, reason="confirm", method="model_check",
       proposed_action=action_desc, raw_response=raw,
   )
   ```

### Logging

Per-evaluation, emit at INFO:
```
GATE_MODE mode=<name> reactive_soft_write_bypass=<bool>
```
when the mode policy is consulted on a branch decision. This
gives operators a single-line attribution per gated call.

Boot-time, emit at INFO:
```
GATE_MODE_RESOLVED mode=<name> ambiguous_fallback=<value>
```
once at `DispatchGate.__init__`, so the configured mode is
visible in startup logs.

### Cross-space evaluator

`evaluate_cross_space` calls `_evaluate_model` directly. The
mode policy applies transparently (since the preamble + fallback
logic live inside `_evaluate_model`). No additional wiring
needed.

### TOOL-MAKING-ARC dependency

`TOOL-MAKING-ARC-V1` D1 will route descriptor-classified tools
through the gate consult path. The mode contract surface
(`evaluate` accepting a `mode_policy` field on input, OR
consulting `self._mode_policy` for every evaluation) MUST be
stable before D1 lands — otherwise descriptor-driven tools
ship without mode awareness and we end up with two parallel
gate paths.

This spec resolves the contract by storing the mode policy on
`DispatchGate` itself. D1's catalog consult inherits the same
gate instance, so the policy applies uniformly to descriptor
tools without further wiring.

## Acceptance criteria

| AC | Description |
|---|---|
| AC1 | `KERNOS_GATE_MODE` unset → `DispatchGate._mode_policy.name == "permissive"`. |
| AC2 | `KERNOS_GATE_MODE=balanced` → `_mode_policy.name == "balanced"`. |
| AC3 | `KERNOS_GATE_MODE=strict` → `_mode_policy.name == "strict"`. |
| AC4 | `KERNOS_GATE_MODE=bogus` → `_mode_policy.name == "strict"` + ERROR log (fail-loud + fall-safe). |
| AC5 | Env normalization (whitespace + case) — `STRICT`, `  balanced  `, `Permissive` all resolve. |
| AC6 | In `permissive` mode, an ambiguous model response (model returns "CONFIRM" or junk) → `GateResult(allowed=True, reason="approved_by_mode", method="mode_permissive")`. |
| AC7 | In `balanced` mode, ambiguous response → `GateResult(allowed=False, reason="confirm", method="model_check")` (current behavior preserved). |
| AC8 | In `strict` mode, ambiguous response → `GateResult(allowed=False, reason="refused_by_mode", method="mode_strict")`. |
| AC9 | In `strict` mode, reactive-soft_write bypass does NOT fire — every soft_write goes through model evaluation. |
| AC10 | In `permissive` and `balanced` modes, reactive-soft_write bypass DOES fire (preserves current behavior). |
| AC11 | Model APPROVE → `allowed=True` regardless of mode. |
| AC12 | Model CONFLICT → `allowed=False` with `conflicting_rule` populated, regardless of mode. |
| AC13 | Model CLARIFY → `allowed=False` with `reason="clarify"`, regardless of mode (CLARIFY is NOT subject to `ambiguous_fallback`). |
| AC14 | `_evaluate_model`'s system_prompt includes the mode-specific preamble (substring check). |
| AC15 | `GATE_MODE_RESOLVED` INFO log fires once at `DispatchGate.__init__`. |
| AC16 | No regressions on `tests/test_dispatch_gate*.py` — existing tests construct DispatchGate without env set, so they get permissive by default; their assertions about `reason="confirm"` need to be pinned to `balanced` via monkeypatch (mirror the SEEDED-COVENANTS pre-existing-test pinning pattern). |

## Soak gate

Per `[[cognition-migration-soak-gate]]` — gate evaluation IS a
cognition path (the model call shapes downstream agent behavior).
Soak requirements:

1. **Automated**: tests above pin all branch points + the three
   modes' behavior at the unit level.
2. **Operator soak**: with `KERNOS_GATE_MODE=permissive` (the
   new default), run the canvas test from POSTURE-V1 §"Soak":
   - "Create a personal canvas called 'Test' and write 'hello'."
   - Verify the bot proceeds through both `canvas_create`
     (soft_write per GATE-CLASSIFICATION-V1) AND `page_write`
     (soft_write) without confirmation friction.
   - Verify `GATE_MODE` log lines attribute the mode.
3. Repeat with `KERNOS_GATE_MODE=strict` and verify the bot
   DOES surface a confirm prompt (because reactive-soft_write
   bypass is disabled).

## Out of scope

- Slash-command operator controls (`/posture mode`) —
  `POSTURE-CONFIGURATION-V1`.
- The `reactive_hard_write_auto_proceed` field is reserved but
  always False in v1; the future spec that adds an explicit
  "authorized_action_signal" can set it True under permissive.
- Per-space mode override (e.g. strict in System space,
  permissive elsewhere) — deferred to a future per-space
  policy spec if operator demand emerges.

## Risks

- **Risk:** Permissive default loosens behavior for existing
  operators who upgrade to this version without setting the env.
  - **Mitigation:** Document the upgrade impact in
    `docs/behaviors/covenants.md` and the spec changelog. The
    default-permissive choice is intentional per POSTURE-V1's
    behavior-neutral mandate.

- **Risk:** Strict mode + ambiguous_fallback=refuse means
  legitimate-but-poorly-understood requests get blocked
  outright with no confirm offer. User has to rephrase.
  - **Mitigation:** Strict is explicit opt-in; operator
    accepts the friction. The error message (TBD copy)
    should make clear they're in strict mode and suggest
    rephrasing.

- **Risk:** Preamble injection changes model behavior in
  ways the unit tests don't catch.
  - **Mitigation:** Soak gate above. Plus an integration test
    that asserts the preamble substring is in the actual
    system_prompt sent to the model.

## Dependencies

- `POSTURE-SEEDED-COVENANTS-V1` (commit `d27d11c`) — landed.
  Establishes the `KERNOS_*_PROFILE`-style env fail-loud
  pattern this spec mirrors.
- `POSTURE-GATE-CLASSIFICATION-V1` (commit `e73cb8f`) — landed.
  Soak's canvas test depends on canvas_create being
  soft_write under personal scope.
- Blocks: `TOOL-MAKING-ARC-V1` D1 (gate descriptor consult).

## Migration

No state migration. The mode policy is resolved at
`DispatchGate.__init__` and stored on the instance. Restart
re-reads.

Behavioral migration:
- Pre-V1 behavior == current `balanced` mode behavior on the
  ambiguous-fallback axis.
- Pre-V1 reactive-soft_write bypass was always-on; new
  default `permissive` preserves this.
- Net upgrade impact: ambiguous responses now `proceed` instead
  of `confirm`. Operators who want pre-V1 behavior set
  `KERNOS_GATE_MODE=balanced` explicitly.
