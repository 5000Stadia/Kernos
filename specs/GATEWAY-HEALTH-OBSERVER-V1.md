# GATEWAY-HEALTH-OBSERVER-V1 — gateway/dispatch-layer signals into the friction catalog

**Status:** ready to implement (V1 scope), V2 + V3 parked
**Origin:** founder push 2026-05-19 against the proposed 3-signal watchdog augmentation — "the shape of this is not redundant to the element that we have for the friction cohort, and if so the spec should establish the penultimate solution to both functions"
**Replaces:** standalone `_discord_gateway_watchdog_loop` (eventually — V3)

## Problem

We have two parallel "things going wrong" detection systems shaping up:

| | Existing `FrictionObserver` | Proposed watchdog augmentation |
|---|---|---|
| When it runs | Post-turn (per `MessageHandler.process`) | Continuous background loop |
| What it observes | Turn substrate (tool_trace, user_message, response_text) | Gateway/dispatch state (latency, on_message rate, runner state) |
| Signal shape | `FrictionSignal(signal_type, description, evidence, context)` | Strike counter on a single `client.latency` metric |
| Action | Markdown report + `record_occurrence` against catalog pattern | Strike accumulation → `os.execv` restart |
| Catalog | Stable pattern IDs with lifecycle (active/resolved/reactivated/archived) | None |
| Recurrence tracking | Yes via `FrictionPatternStore.record_recurrence` | No (strike counter resets per-process) |

Building a parallel three-signal watchdog with its own strike counter, hardcoded thresholds, and direct execv call would re-implement at the gateway layer what the friction system already does at the turn layer — without the catalog, lifecycle, or recurrence machinery. Wrong shape.

## The unified shape

Both observers feed the **same** `FrictionPatternStore`. Same catalog, same signal data model, same lifecycle, same auto-classifier hook. They differ only in *when* they run and *what they detect*.

```
FrictionPatternStore (the catalog — already exists)
├── turn-level patterns (already seeded — provider-error-repeated, etc.)
└── gateway-level patterns (NEW — V1)
    ├── discord-gateway-deaf
    ├── space-runner-stuck
    ├── discord-heartbeat-blocked
    └── discord-connection-pool-leak

Observers (emit FrictionSignals into the catalog)
├── FrictionObserver (per-turn — already exists, unchanged)
└── GatewayHealthObserver (continuous — NEW, V1)
```

## V1 scope (this batch)

Ship the observer + the pattern seeds. **No declarative auto-remediation yet** — signals land in the catalog as diagnostic data; the existing watchdog stays as the safety net for restart-on-deaf-gateway. V1 is data-gathering; V2 is the remediation framework.

### Components

**New file:** `kernos/kernel/gateway_health.py`
- `GatewayHealthObserver` class with:
  - `start()`/`stop()` lifecycle (background asyncio task)
  - Internal poll loop (default 60s, env-tunable via `KERNOS_GATEWAY_HEALTH_POLL_SEC`)
  - One inspection method per pattern below — each returns `FrictionSignal | None`
  - `_classify_and_record` reused from `FrictionObserver` (extract the shared bit OR call into it)
- Observer holds references to: discord `Client`, `_last_inbound_event_ts`, the pattern store

**Pattern seeds added to** `kernos/setup/seed_friction_patterns.py`:

| pattern_id | display_name | signal_type | bias |
|---|---|---|---|
| `discord-gateway-deaf` | Discord gateway dispatching but on_message not firing | `DISCORD_GATEWAY_DEAF` | STRUCTURAL_ENFORCE |
| `space-runner-stuck` | on_message fired but runner did not produce a turn within N min | `SPACE_RUNNER_STUCK` | STRUCTURAL_ENFORCE |
| `discord-heartbeat-blocked` | client.latency went non-finite or > threshold | `DISCORD_HEARTBEAT_BLOCKED` | STRUCTURAL_ENFORCE |
| `discord-connection-pool-leak` | CLOSE_WAIT socket count > threshold for keepalive pool | `CONNECTION_POOL_LEAK` | SIMPLIFY |

### Detection rules (V1 — conservative)

- **discord-gateway-deaf**: count `MESSAGE_CREATE` socket events via `on_socket_event_type` over rolling 10-min window; if `MESSAGE_CREATE` count > 0 BUT `_last_inbound_event_ts` (set in on_message) is older than 10 min, emit.
- **space-runner-stuck**: per-runner `mailbox_oldest_ts` — if any space runner has an item in its mailbox older than 5 min and no TURN_TIMING fired for that space in that window, emit. Requires a small instrumentation hook in `_run_space_loop`.
- **discord-heartbeat-blocked**: `client.latency` non-finite OR > `KERNOS_DISCORD_WATCHDOG_LATENCY_THRESHOLD_SEC` (same threshold as current watchdog uses). Existing logic moves into the observer.
- **discord-connection-pool-leak**: `len([s for s in process.connections() if s.status=='CLOSE_WAIT']) > KERNOS_HTTP_POOL_CLOSE_WAIT_THRESHOLD` (default 30). Bounded by httpx pool size 20 normally; growing past it indicates a real leak.

### Wiring

- Start `GatewayHealthObserver` in `bring_up_substrate.py` next to where `FrictionPatternFrequencyEmitter` starts (around line 599)
- Observer reads/writes the **same** `FrictionPatternStore` instance the per-turn observer uses
- Observer publishes its FrictionSignals via the **same** `_classify_and_record` path — uniform catalog records regardless of source

## V2 scope (parked follow-up)

Declarative remediation policy on FrictionPattern records:

- New field on `FrictionPattern`: `remediation_workflow: str | None` (workflow_id)
- New field: `remediation_threshold: {window_sec: int, min_occurrences: int}`
- When `record_occurrence` runs, if the pattern has a remediation_workflow AND its recent-occurrence count crosses the threshold, fire the workflow
- Workflows for gateway-deaf patterns = restart workflow (wraps `os.execv` in a workflow shape so it gets the same audit + dedup as other workflows)
- This is what would let us delete the standalone watchdog: its strike+restart logic becomes a declarative pattern policy

## V3 scope (parked follow-up)

Once V2 ships:
- Migrate the existing watchdog's `_is_gateway_heartbeat_unhealthy` + strike counter into the `discord-heartbeat-blocked` pattern's remediation policy
- Delete `_discord_gateway_watchdog_loop`, `_watchdog_tick`, `_gateway_unhealthy_strikes`
- Tests for the watchdog get retargeted to verify the remediation policy fires correctly

## Why this sequencing

V1 is small + safe — it adds observation without changing decision-making. Once we have a week of data in the catalog about gateway-layer signal frequency, V2's threshold defaults can be calibrated from real evidence instead of guessed. V3 is mechanical cleanup once V2 proves out.

The existing watchdog stays as the safety net across V1 and V2 because:
- V1 emits signals but doesn't act on them
- V2 introduces the remediation policy framework but gateway patterns might still need tuning
- V3 deletes the watchdog only after the policy-driven path proves it can recover gateway death in production at least once

## Out of scope for V1

- The auto-remediation policy framework (V2)
- Migrating turn-level patterns to also declare remediation policies (V2 will land the framework for them too, but explicit migration is out of V1)
- The OpenAI/codex CLOSE_WAIT leak fix at the connection level (V1 only DETECTS it via `connection-pool-leak` signal; the actual `aclose` discipline fix per Codex's diagnosis is a separate small spec)

## Acceptance criteria

- All 4 new pattern_ids appear in the catalog after bring-up
- `GatewayHealthObserver` background task starts + ticks on the configured interval
- Each of the 4 detection methods has a focused unit test with mocked state
- Bring-up tests verify the observer is constructed with the SAME `FrictionPatternStore` instance the per-turn observer uses (no parallel catalog)
- 24h live soak: the catalog accumulates real occurrence rows for at least the `discord-connection-pool-leak` pattern (we know the leak is present at low level); operator-visible diagnostic data
- Existing watchdog tests still pass (the watchdog isn't removed in V1)
