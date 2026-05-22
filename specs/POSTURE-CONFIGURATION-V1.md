# POSTURE-CONFIGURATION-V1

**Date:** 2026-05-22
**Status:** Draft for review (sub-spec of `KERNOS-DEFAULT-POSTURE-V1`)
**Scope:** Two-layer posture config (env vars + persisted
  `instance_posture` row) + owner-only `/posture` slash command
  with `profile`, `mode`, `reset-covenants`, and no-arg status
  subcommands. Closes POSTURE-V1 D7.
**Estimated size:** ~250 LOC source + ~180 LOC tests.

## Why this spec exists

After POSTURE-SEEDED-COVENANTS-V1, POSTURE-GATE-CLASSIFICATION-V1,
POSTURE-EVALUATION-MODES-V1, and the predictive-surfacing
migration shipped, the POSTURE arc is behaviorally complete but
operationally clunky: every posture knob is env-only. Operators
can't tune posture at runtime without editing env + restart.
And changes made via env don't persist across self-update / execv
unless the env is part of the runner's permanent config.

This spec adds the operator-facing layer: persisted config that
survives restart and a slash command that mutates it without
requiring a process restart.

## Current state

- `_resolve_posture_profile()` in `kernos/kernel/state.py` reads
  `KERNOS_POSTURE_PROFILE` env (or `minimal` default; invalid →
  `strict` + ERROR log per fail-loud pattern).
- `_resolve_gate_mode_policy()` in `kernos/kernel/gate.py` reads
  `KERNOS_GATE_MODE` env (or `permissive` default; invalid →
  `strict` + ERROR log).
- `DispatchGate.__init__` calls `_resolve_gate_mode_policy()`
  once. `set_mode_policy()` contract surface exists for live
  swap but has no caller in v1.
- No persistence layer. No slash command.

## Design

### `instance_posture` table

New SQLite table in `instance.db`. Single row per `instance_id`
(provisioned with NULLs at instance creation; populated only
when an operator explicitly sets a value).

```sql
CREATE TABLE IF NOT EXISTS instance_posture (
    instance_id      TEXT PRIMARY KEY,
    posture_profile  TEXT,   -- NULL → fall through to env / default
    gate_mode        TEXT,   -- NULL → fall through to env / default
    last_updated_at  TEXT,   -- ISO timestamp; NULL on first row
    last_updated_by  TEXT    -- member_id (or 'env' if seeded from env)
);
```

Persisted values are AUTHORITATIVE when present — they override
env vars. Env vars remain the fallback for fields that haven't
been operator-set yet.

### Resolution order (at every read)

```
1. instance_posture row's field (if present + non-NULL)
2. KERNOS_<FIELD> env var (if persisted absent or NULL)
3. Hardcoded default (minimal / permissive)
```

This applies to:
- Covenant seed → reads `posture_profile`
- Gate mode → reads `gate_mode`

Existing `_resolve_*` helpers gain a `state_store` parameter
and an `instance_id` parameter. They become async (since the
state read is async). Callers updated accordingly.

### Async resolution and gate construction

The gate currently resolves mode synchronously in `__init__`.
Moving to async resolution means gate construction must EITHER
become async OR defer mode resolution until first
`evaluate()` call.

**Decision: defer to first-use.** `__init__` stays synchronous,
stores `(state_store, instance_id)` references. First call to
`evaluate()` lazily resolves the mode policy via the state-store
chain. Subsequent calls reuse the cached policy. `set_mode_policy()`
+ `clear_resolved_policy_cache()` (NEW) allow runtime updates
from the slash command.

Trade: every gate evaluation gains an `if self._mode_policy is None`
check (cheap). Avoids a constructor-shape change that would
ripple across every test and integration point that builds a
gate.

### `/posture` slash command

Owner-only. Lives next to the existing slash commands in
`kernos/messages/handler.py`.

**Subcommands:**

| Form | Effect |
|---|---|
| `/posture` | Show current resolved values + their source (persisted / env / default) + last_updated_at, last_updated_by |
| `/posture profile <minimal\|standard\|strict>` | UPDATE `posture_profile` in row. Acknowledges that this affects FUTURE seeds only — existing covenants stay unless `reset-covenants` runs. |
| `/posture mode <permissive\|balanced\|strict>` | UPDATE `gate_mode` in row + call `DispatchGate.set_mode_policy()` on the live gate so the change takes effect immediately. |
| `/posture reset-covenants <minimal\|standard\|strict>` | Drop all `source=default` covenant rules for the instance + re-seed from the named profile. Updates `posture_profile` to match. **Requires CONFIRM** per `DURABLE-APPROVAL-RECEIPTS-V1` — two-step: first call returns a receipt with the exact rule-change summary; second call with the receipt token executes. |

**Auth:** all four require the caller to be the instance owner.
Re-uses the existing owner-check pattern from `/restart`.

**Profile validation:** the four valid profile names and the
three valid mode names are hardcoded sets. Invalid input → error
reply, no state mutation.

### Bootstrap

At instance provisioning (`bring_up_substrate.py`), insert a
blank row in `instance_posture` (all fields NULL except
`instance_id`). This is purely so subsequent UPDATE statements
don't need an INSERT-OR-UPDATE branch.

Existing instances that pre-date this spec: lazy migration via
the resolution helpers. If the row is missing on read, treat
as "all NULL" (which falls through to env / default). On the
first `/posture` mutation, INSERT.

### Telemetry

Each slash-command mutation emits:
```
POSTURE_CHANGED instance=<id> field=<name> old=<value> new=<value> actor=<member_id>
```
at INFO level + a `posture.changed` event with the same payload
fields. Lets the friction observer + operator log review track
posture drift over time.

## Acceptance criteria

| AC | Description |
|---|---|
| AC1 | `instance_posture` table created on schema bootstrap with the documented columns. |
| AC2 | Posture-profile resolution: persisted non-NULL wins over env. |
| AC3 | Posture-profile resolution: persisted NULL falls through to env. |
| AC4 | Posture-profile resolution: persisted-absent + env-unset → `minimal` default. |
| AC5 | Same three-tier resolution holds for `gate_mode` (default `permissive`). |
| AC6 | `/posture` (no args) shows current values + sources. |
| AC7 | `/posture profile standard` UPDATEs the row, replies confirming, does NOT alter existing covenants. |
| AC8 | `/posture mode strict` UPDATEs the row + calls `DispatchGate.set_mode_policy(strict)` on the live gate. Next dispatch sees strict. |
| AC9 | `/posture reset-covenants standard` first call returns a CONFIRM receipt summarizing what will change. |
| AC10 | `/posture reset-covenants standard <receipt-token>` executes: drops `source=default` rules + re-seeds from standard. Other rules (`source=user_stated` / `source=evolved`) preserved. |
| AC11 | `/posture` from non-owner returns an "owner-only" error, no state mutation. |
| AC12 | Invalid profile/mode name returns an error, no state mutation. |
| AC13 | `POSTURE_CHANGED` INFO log + `posture.changed` event fire per mutation. |
| AC14 | Restart preserves persisted values (round-trip through the table). |
| AC15 | Existing instances without an `instance_posture` row continue to function (lazy migration). |
| AC16 | Setting `KERNOS_POSTURE_PROFILE=standard` env on a fresh instance with NULL persisted row → `standard` resolves. |
| AC17 | No regressions on existing covenant / gate tests. |

## Soak gate

1. **Automated**: ACs above pin schema + resolution chain + slash
   command + auth + receipt flow + telemetry.
2. **Operator soak**:
   - Start with default env (no `KERNOS_POSTURE_PROFILE`,
     no `KERNOS_GATE_MODE`).
   - Run `/posture` → see `posture_profile=minimal (default)`,
     `gate_mode=permissive (default)`.
   - Run `/posture mode strict` → confirm strict mode active
     via a follow-up action that would trip strict's
     ambiguous-fallback=refuse path.
   - Restart bot. Run `/posture` again → strict persists.
   - Run `/posture reset-covenants standard` → see CONFIRM
     receipt. Run with token → covenants re-seeded.

## Out of scope

- Multi-instance posture (per-space) → reserved for a future
  spec if demand emerges. v1 is instance-wide.
- Per-member posture overrides → out of scope; member-level
  controls live in covenant rules (which already support
  member scoping).
- A WebUI / dashboard for posture → not v1. CLI / slash command
  suffices.
- Auto-rebase of existing instances when env changes →
  intentional non-feature. Operators explicitly invoke
  `/posture reset-covenants` if they want to rebase.

## Risks

- **Risk:** Lazy-resolution at first `evaluate()` means a stale
  cached mode policy persists if the operator updates the row
  externally (e.g., SQL).
  - **Mitigation:** `clear_resolved_policy_cache()` exposed for
    explicit invalidation. Slash command invokes it after
    UPDATE. External SQL editors will see stale until restart;
    documented in the spec + operator notes.

- **Risk:** CONFIRM-required reset-covenants flow depends on
  the DURABLE-APPROVAL-RECEIPTS-V1 substrate. The
  helpers-only ship from `96f4582` provides issue + redeem;
  we just need a thin wrapper that synthesizes the receipt
  text + binds it to the slash-command tool name.
  - **Mitigation:** Document the dependency. If the receipt
    issue path breaks, the slash command surfaces the error
    rather than silently destroying covenants.

- **Risk:** Async-resolution change to gate could surprise
  callers expecting synchronous mode access.
  - **Mitigation:** Mode is only consulted inside `evaluate()`
    (already async) and inside `_evaluate_model()` (already
    async). No synchronous read sites today.

## Dependencies

- `POSTURE-EVALUATION-MODES-V1` (commit `4e3458b`) — landed.
  Provides `GateModePolicy` + `set_mode_policy()` contract.
- `POSTURE-SEEDED-COVENANTS-V1` (commit `d27d11c`) — landed.
  Provides the profile-driven covenant seed.
- `DURABLE-APPROVAL-RECEIPTS-V1` (commit `96f4582`) — landed
  helpers-only. Provides issue / redeem for the reset-covenants
  CONFIRM gate.

## Migration

- **Schema**: `instance_posture` table added via the existing
  schema-bootstrap path. Lazy: on read, missing-row treated as
  all NULL. On first write, INSERT.
- **Existing instances**: keep working unchanged. Behavior
  doesn't shift until the operator explicitly runs `/posture`
  to mutate the row.
- **Env vars**: unchanged precedence model except that a
  persisted non-NULL value now wins over env. Operators who
  want to FORCE env precedence must clear the persisted row
  (future: `/posture reset-config`).
