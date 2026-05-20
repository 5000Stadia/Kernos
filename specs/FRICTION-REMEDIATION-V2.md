# FRICTION-REMEDIATION-V2 — declarative auto-remediation on FrictionPattern records

**Status:** shipped (this batch)
**Origin:** founder approval 2026-05-20 — "Yeah go for it" after the 2026-05-20 03:56 UTC silent-gateway failure where the parallel watchdog stopped firing and only the GatewayHealthObserver kept emitting signals. V1.5 inline strike counter shipped as MVP safety net; V2 generalizes the same shape into the catalog as a declarative policy any pattern can opt into.

## Problem

V1 (gateway-health observer) emits friction signals into the catalog but takes no action. The parallel `_discord_gateway_watchdog_loop` was supposed to handle the "heartbeat blocked → restart" automation, but its background task can die silently (the 2026-05-20 failure: no STRIKE log entries for hours despite heartbeat being NaN). The observer was the only working detection path. Reactive options at the time:

1. Inline V1.5 — observer counts consecutive heartbeat-blocked ticks, force-restarts at threshold. Hardcoded for one signal. Shipped.
2. V2 — generic declarative policy on FrictionPattern records. Any pattern can declare a remediation; threshold check + cool-off live in the store.

This spec describes V2.

## Design

Three new fields on `FrictionPattern` (and the `friction_pattern` DDL):

- `remediation_action: str` — name of a registered callback (e.g. `"restart_kernos"`). Empty = no policy.
- `remediation_threshold_count: int` — fire when this many occurrences within the window. 0 = no policy.
- `remediation_threshold_window_sec: int` — width of the count window. 0 = no policy.

All three must be non-zero/non-empty to enable. Defaults `("", 0, 0)` mean "no remediation" — every existing pattern is backward-compatible.

### Callback registry

```python
store.register_remediation_handler(
    "restart_kernos",
    async_handler_callable,
)
```

Handler signature: `async def handler(*, instance_id: str, pattern_id: str, occurrence_count: int) -> None`. Handlers own their side effects (logging, restart, notify). Multiple patterns can share an action.

### Trigger flow

After every successful `record_occurrence`:

1. Read the pattern's three remediation fields. Skip if any are zero/empty.
2. `_count_recent_occurrences(pattern_id, window_sec)` — SQL count of occurrence rows within the window.
3. If count >= threshold, check `_remediation_in_cool_off(pattern_id, window_sec)`.
4. Sentinel file at `data/diagnostics/friction/remediation/<instance>__<pattern_id>.last_fired` — contains ISO timestamp of last fire. If sentinel's age < window_sec, log `SKIPPED_COOL_OFF` and return.
5. Otherwise: `_mark_remediation_fired` (write the sentinel atomically BEFORE calling the handler — critical because handler might be `os.execv` which never returns), then call the handler.

### Cool-off via sentinel — the critical safety pin

If the action is `restart_kernos` (`os.execv`) and the underlying condition isn't actually fixed by restart, naive remediation would loop-restart forever. The sentinel file:

- Lives on disk → survives bot restart
- Contains the last-fire timestamp
- Read at every threshold-crossing check; if newer than `window_sec` ago, skip fire

For `discord-heartbeat-blocked` with `window_sec=600`: bot restarts at most once per 10 min, regardless of how badly the heartbeat is broken. If the condition resolves naturally after restart, the catalog stops firing → sentinel ages out → ready to fire again if the condition returns later.

### Wire-up

`bring_up_substrate.py` registers the `restart_kernos` handler:

```python
async def _restart_kernos_handler(*, instance_id, pattern_id, occurrence_count):
    logger.error("FRICTION_REMEDIATION_RESTART_KERNOS: ...")
    for h in logging.getLogger().handlers:
        h.flush()
    os.execv(sys.executable, [sys.executable] + sys.argv)

_si_pattern_store.register_remediation_handler(
    "restart_kernos", _restart_kernos_handler,
)
```

### Seed update path

Critical: bots that were seeded before V2 have rows in `friction_pattern` with empty remediation policy. Without an upgrade path, the live bot would never get the V2 policy applied. So `seed_friction_patterns_on_first_boot` now:

- For patterns already in the catalog, calls `_maybe_update_remediation_policy` which compares DB remediation columns to the seed's; updates them via direct SQL when they differ.
- Idempotent: if DB already matches seed, no-op.
- Logs `FRICTION_PATTERN_REMEDIATION_POLICY_UPDATED` when an update happens.

This means: when the bot next runs the seed (on every bring-up), the `discord-heartbeat-blocked` pattern gets `remediation_action="restart_kernos"` etc. without needing a full re-seed.

## V2 → V3 (parked)

Once V2 has proven in production (at least one live execv recovery via the V2 path), V3 deletes:

- The standalone `_discord_gateway_watchdog_loop` (replaced by `discord-heartbeat-blocked` pattern policy)
- The V1.5 `_consecutive_heartbeat_strikes` inline counter in GatewayHealthObserver (replaced by catalog-side count_recent_occurrences + threshold check)

The observer keeps emitting signals; remediation is entirely catalog-driven.

## Test coverage (this batch)

14 tests in `tests/test_friction_remediation_v2.py` covering:

- Schema: dataclass + create_pattern accepts new fields, round-trips via list_patterns
- Backward-compat: default values mean no policy
- Trigger: handler fires when threshold crossed, doesn't below, doesn't without policy, logs warning when no handler registered
- Cool-off (the critical safety pins):
  - Second fire within window skipped (loop-prevention)
  - Sentinel persists to disk with correct path + format
  - Simulated restart (fresh store, same data_dir) respects on-disk sentinel
  - Backdated sentinel allows re-fire after window expires
- Seed integration: discord-heartbeat-blocked declares restart_kernos with the right thresholds; other patterns intentionally have no policy
- Upgrade path: `_maybe_update_remediation_policy` applies seed's policy to existing pattern row

Plus 9 pre-existing seed tests + 30 gateway-health-observer tests + 7 self-admin-tools tests all green (263 total in the regression sweep).
