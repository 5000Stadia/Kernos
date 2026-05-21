# HEARTBEAT-DETECTOR-LIVENESS-CROSSCHECK-V1

**Date:** 2026-05-20 (revised post Codex rounds 1 + 2)
**Status:** Draft for review
**Scope:** `kernos/kernel/gateway_health.py::_detect_heartbeat_blocked` +
  a new dedicated on_message-only timestamp wired from `server.py`.
**Estimated size:** ~50 LOC in detector + new ts provider in server.py + ~120 LOC tests.

## Problem (live evidence)

`_detect_heartbeat_blocked` reads `client.latency` and emits the
`DISCORD_HEARTBEAT_BLOCKED` friction signal whenever the value is
`None`, `nan`, non-positive, non-numeric, or above the threshold
(60s). That is correct *if* the gateway is actually broken — but
`discord.py`'s `client.latency` returns `nan` in failure modes where
the gateway is functionally alive:

- Cold-start window before the first heartbeat ACK lands
- Transient latency-tracker state where the deque is empty
- Shard restart / reconnect where the tracker resets but the
  websocket reattaches successfully

Live observation 2026-05-20 (post-restart at 20:24:17 PT):

```
20:19:45 INFO FRICTION_REMEDIATION_SKIPPED_COOL_OFF: count=11 threshold=5 ...
20:20:45 INFO FRICTION_REMEDIATION_SKIPPED_COOL_OFF: count=11 threshold=5 ...
20:21:45 INFO FRICTION_REMEDIATION_SKIPPED_COOL_OFF: count=11 threshold=5 ...
20:22:45 WARNING GATEWAY_HEALTH_FORCE_RESTART_SKIPPED_COOL_OFF: heartbeat
        unhealthy for 5 ticks but shared remediation sentinel says we
        restarted recently — gateway issue persists across restart, not
        a recoverable failure. Skipping V1.5 restart to prevent loop.
```

Friction report:
```
Description: Discord heartbeat unhealthy: client.latency non-finite: nan
```

But Telegram polling kept returning `HTTP/1.1 200 OK` every 30s and
a `consult` call to `claude_code` completed successfully at 03:11:10
UTC (consultation_log row, exit_status=0, 276-char response). The
HTTP layer is alive; on_message handlers fired; only `client.latency`
is lying.

The FRICTION-REMEDIATION-V2.1 batch (commit `5557494`) correctly
stops the restart loop via shared cool-off + escalation guard. The
**restart cycle is dead.** What remains is the *noise stream* — the
detector emits a friction signal every 60s for the entire bot
uptime, generating dozens of false reports per hour and burning
escalation-guard budget that real emergencies should be able to use.

## Root cause

`_detect_heartbeat_blocked` treats `client.latency` as the sole
liveness signal. That is incorrect when the gateway is alive
through other evidence (inbound MESSAGE_CREATE traffic,
on_message events firing). The detector needs to cross-check
against the substrate's other liveness signals before declaring
the gateway unhealthy.

The right signals to cross-check are available in the
GatewayHealthObserver constructor — but **with one caveat
Codex flagged**:

- `self._message_create_counter.count_in_window(now)` — strong
  evidence (socket-level MESSAGE_CREATE events).
- `self._inbound_event_ts_provider` — currently bumped by
  `on_message` AND `on_ready` / `on_resumed` (see
  `server.py:417`, `_last_inbound_event_ts`). **A gateway dying
  immediately after `on_resumed` would have a fresh timestamp
  with zero real message traffic.** We cannot use this for the
  cross-check as-is.

**Fix in this spec:** introduce a *new* on_message-only timestamp
in server.py (`_last_on_message_only_ts`), bumped only inside
`on_message`. Wire a new constructor param
`last_on_message_provider` into `GatewayHealthObserver`. The
cross-check uses MESSAGE_CREATE counter OR
`last_on_message_provider` — both are pure on_message evidence.
The existing `inbound_event_ts_provider` continues to feed
`_detect_gateway_deaf` (which legitimately wants lifecycle
events as well, because its inverse-check is "socket events
arrive but on_message doesn't").

## Latency classification (explicit table, addresses Codex
medium finding)

| `client.latency` value | Classification | Suppressible by cross-check? |
|---|---|---|
| Finite, > 0, ≤ threshold (60s) | Healthy | N/A — no emission |
| Finite, > threshold | Real high latency | **No** — emit |
| `None` | Tracker unreliable | **Yes** |
| `nan` | Tracker unreliable | **Yes** |
| `+inf` | Real (infinite latency = pathological) | **No** — emit |
| `-inf` | Tracker unreliable (nonsense) | **Yes** |
| ≤ 0 (zero or negative finite) | Tracker unreliable | **Yes** |
| Non-numeric (TypeError on isfinite) | Tracker unreliable | **Yes** |

Rationale: the cross-check exists because the *tracker* can be
broken while the *gateway* is alive. A finite high-latency value
means the tracker is computing — there is no reason to suppress.
`+inf` is treated as a real signal because it is the tracker
saying "infinitely slow"; that is a load-bearing read. Negative
infinity has no meaningful interpretation; suppressible.

Type-safety: classification is done in a `try/except` block
mirroring the existing detector to avoid `math.isfinite()` raising
on non-numeric input.

## Fix

```python
def _detect_heartbeat_blocked(self) -> FrictionSignal | None:
    import time as _time
    now = _time.time()
    latency = self._latency_provider()

    # Classify
    reason, tracker_unreliable = self._classify_latency(latency)
    if reason is None:
        return None  # healthy

    # Cross-check only the tracker-unreliable failure modes
    if tracker_unreliable:
        # Warm-up grace: first N seconds after observer start,
        # tracker NaN is normal until first heartbeat ACK lands.
        if self._uptime_sec() < _WARMUP_GRACE_SEC:
            self._note_heartbeat_suppression("warmup", reason)
            return None
        # Liveness cross-check via PURE on_message evidence
        # (NOT lifecycle-bumped inbound ts).
        if self._is_inbound_traffic_alive(now):
            self._note_heartbeat_suppression("live_traffic", reason)
            return None

    return FrictionSignal(
        signal_type="DISCORD_HEARTBEAT_BLOCKED",
        description=f"Discord heartbeat unhealthy: {reason}",
        evidence=[reason],
        context={
            "space": "",
            "member_id": "",
            "latency_threshold_sec": _HEARTBEAT_THRESHOLD_SEC,
        },
        heuristic=False,
    )

def _classify_latency(
    self, latency: Any,
) -> tuple[str | None, bool]:
    """Return ``(reason, tracker_unreliable)``. ``reason=None``
    means healthy. ``tracker_unreliable=True`` flags this as
    suppressible by the cross-check."""
    import math as _math
    if latency is None:
        return ("client.latency is None", True)
    try:
        finite = _math.isfinite(latency)
    except (TypeError, ValueError):
        return (f"client.latency non-numeric: {latency!r}", True)
    if not finite:
        # nan, +inf, -inf
        if _math.isnan(latency):
            return (f"client.latency non-finite: {latency}", True)
        if latency > 0:  # +inf
            return (
                f"client.latency non-finite: {latency} (infinite)",
                False,  # NOT suppressible — pathological tracker read
            )
        # -inf
        return (f"client.latency non-finite: {latency}", True)
    if latency <= 0:
        return (f"client.latency non-positive: {latency}", True)
    if latency > _HEARTBEAT_THRESHOLD_SEC:
        return (
            f"client.latency={latency:.1f}s exceeds threshold "
            f"{_HEARTBEAT_THRESHOLD_SEC}s",
            False,  # finite high latency is real
        )
    return (None, False)  # healthy

def _is_inbound_traffic_alive(self, now: float) -> bool:
    """True iff PURE on_message evidence shows the gateway is
    alive. Two independent signals — either is sufficient:

      * MESSAGE_CREATE socket-event counter > 0 in window
        (proves websocket is delivering dispatch)
      * last_on_message_provider() within window (proves the
        parser + handler dispatched a real message)

    Both signals are bumped ONLY by real message traffic.
    Lifecycle events (on_ready/on_resumed) are intentionally
    excluded — they would mask a post-resume gateway death.
    """
    # MESSAGE_CREATE check — Codex round 2: explicitly pass the
    # heartbeat-cross-check window so we don't inherit the
    # gateway-deaf counter's 600s window.
    if self._message_create_counter is not None:
        if self._message_create_counter.count_in_window(
            now, window_sec=_INBOUND_TRAFFIC_LIVENESS_WINDOW_SEC,
        ) > 0:
            return True
    # on_message-only check — independent of counter wiring
    last_on_message = (
        self._last_on_message_provider()
        if self._last_on_message_provider is not None
        else 0.0
    )
    if last_on_message > 0:
        idle = now - last_on_message
        if idle < _INBOUND_TRAFFIC_LIVENESS_WINDOW_SEC:
            return True
    return False

def _note_heartbeat_suppression(
    self, kind: str, reason: str,
) -> None:
    """Track suppression so operators can distinguish 'detector
    is suppressing correctly' from 'detector stopped running'.
    Per Codex round 1 medium finding: debug-only logs may be
    invisible during rollout; an INFO log every Nth suppression
    plus a counter exposed via test introspection makes the
    suppression observable without spamming."""
    self._heartbeat_suppression_count += 1
    self._last_suppression_kind = kind
    self._last_suppression_reason = reason
    # Log every Nth suppression at INFO so operators have a
    # heartbeat (pun intended) of the detector's health.
    if self._heartbeat_suppression_count % _SUPPRESSION_LOG_EVERY_N == 0:
        logger.info(
            "GATEWAY_HEALTH_HEARTBEAT_SUPPRESSED_NOMINAL: "
            "kind=%s reason=%s suppression_count=%d — "
            "detector is alive and correctly ignoring tracker "
            "noise (gateway has real traffic evidence).",
            kind, reason, self._heartbeat_suppression_count,
        )
```

### New constants

```python
_WARMUP_GRACE_SEC = int(
    os.getenv("KERNOS_GATEWAY_HEALTH_WARMUP_GRACE_SEC", "60"),
)
_INBOUND_TRAFFIC_LIVENESS_WINDOW_SEC = int(
    os.getenv("KERNOS_HEARTBEAT_LIVENESS_TRAFFIC_WINDOW_SEC", "300"),
)
_SUPPRESSION_LOG_EVERY_N = int(
    os.getenv("KERNOS_HEARTBEAT_SUPPRESSION_LOG_EVERY_N", "10"),
)
```

### New instance state

```python
# Set in __init__:
self._heartbeat_suppression_count = 0
self._last_suppression_kind: str = ""
self._last_suppression_reason: str = ""
self._observer_started_at: float = 0.0  # set in start()
self._last_on_message_provider: Callable[[], float] | None = None
    # passed via constructor param
```

```python
# In start():
import time as _time
self._observer_started_at = _time.monotonic()

# _uptime_sec helper:
def _uptime_sec(self) -> float:
    if self._observer_started_at == 0.0:
        return 0.0  # not yet started → treat as warm-up
    return _time.monotonic() - self._observer_started_at
```

`time.monotonic()` per Codex finding (clock skew immunity).

### New server.py wiring

```python
# In server.py near _last_inbound_event_ts (around line 413):
# Separate counter that is bumped ONLY by on_message — used by
# the gateway-health heartbeat cross-check, where lifecycle
# events (on_ready/on_resumed) would mask a post-resume death.
_last_on_message_only_ts: float = 0.0

def _bump_on_message_only_ts() -> None:
    global _last_on_message_only_ts
    _last_on_message_only_ts = _time_module.time()

# In on_message body (server.py:1714), in addition to the
# existing _last_inbound_event_ts bump:
_bump_on_message_only_ts()

# In GatewayHealthObserver construction:
last_on_message_provider=lambda: _last_on_message_only_ts,
```

## What does NOT change

- `_detect_gateway_deaf` is untouched and continues to use the
  lifecycle-inclusive `_inbound_event_ts_provider`. Its purpose
  is "socket events arrive but on_message doesn't" which is
  validly informed by lifecycle events.
- V1.5 / V2 remediation chains untouched.
- Friction-pattern catalog entry, diagnostic-report format,
  and signal type name unchanged.
- Real high-latency emission (finite > threshold) preserved.
- `+inf` emission preserved (pathological tracker read).

## Acceptance criteria (expanded per Codex round 1)

| # | Scenario | Expected detector output |
|---|---|---|
| 1 | `latency=nan`, recent MESSAGE_CREATE, past warm-up | None (suppressed: live_traffic) |
| 2 | `latency=None`, recent on_message-only ts, past warm-up | None (suppressed: live_traffic) |
| 3 | `latency=nan`, no traffic, no on_message, past warm-up | FrictionSignal emitted |
| 4 | `latency=nan`, no traffic, within warm-up | None (suppressed: warmup) |
| 5 | `latency=120.0` (real high), recent traffic | FrictionSignal emitted (real signal not suppressed) |
| 6 | `latency=0.05` (healthy), any state | None |
| 7 | `latency=nan`, counter=None, recent on_message ts | None (suppressed via on_message path, counter absence OK) |
| 8 | `latency=nan`, counter=None, no on_message ts | FrictionSignal emitted |
| 9 | Uptime within warm-up, `latency=120.0` | FrictionSignal emitted (warm-up does NOT suppress real signal) |
| 10 | `latency=+inf` | FrictionSignal emitted (pathological, not suppressible) |
| 11 | `latency=-inf` | None if recent traffic else emit (suppressible) |
| 12 | `latency="foo"` (non-numeric) | None if recent traffic else emit (suppressible) |
| 13 | `latency=-1.0` (negative finite) | None if recent traffic else emit (suppressible) |
| 14 | Traffic at exactly `now - window_sec` (boundary) | Spec: `idle < window_sec` is strict-less-than, so just past boundary → emit; just inside → suppress. Test both. |
| 15 | Counter=None, `inbound_event_ts_provider=lambda: now` (lifecycle-fresh), `last_on_message_provider=lambda: 0.0` (no real on_message ever), past warm-up, `latency=nan` | FrictionSignal emitted — proves lifecycle bumps do NOT leak into the cross-check. Must use these exact fixture values; weaker setups can pass for the wrong reason. |
| 15b | Wiring assertion: no call to `_bump_on_message_only_ts()` exists anywhere in `server.py` outside the `on_message` function body. Static-source test parses server.py AST and confirms. | Catches a future accidental addition to `on_resumed`/etc. |
| 16 | Suppressed tick does NOT increment V1.5 `_consecutive_heartbeat_strikes`; suppressed tick after non-suppressed strikes clears the counter to 0 | Verify both |
| 17 | After 10 suppressions, INFO `GATEWAY_HEALTH_HEARTBEAT_SUPPRESSED_NOMINAL` log line fires | Verify count + log emission |
| 18 | Finite high-latency emission still flows through V2.1 cool-off and escalation (smoke test, not end-to-end) | Verify the detector returns a FrictionSignal as it did pre-spec |

**Existing-test-update note:** the current test_gateway_health_observer.py
includes tests asserting immediate emission on `nan` / `None` / `0`. Those
tests will need to age the observer past warm-up (or set
`_observer_started_at` to a stale monotonic value via test hook) AND
either provide no traffic OR clear the counter / on_message ts. The
spec accepts that these tests will be updated; the new behavior IS the
spec.

## Out of scope (parked for future spec)

- **ASYNC-IO-CONVERSION-V1** — Codex-RCA'd as the deeper root cause
  (sync file I/O blocks asyncio loop → heartbeat task starves →
  tracker goes NaN). This spec treats the symptom; the conversion
  is the cure. Both can ship; this spec is the immediate
  noise-reduction. **Acknowledged trade-off** (per Codex round 1):
  with 5-tick strike threshold at 60s polling and a 300s traffic
  liveness window, a gateway that dies immediately after the last
  real message has up to ~10 minutes before remediation can fire.
  That is bounded; current behavior would fire dozens of false
  alarms per hour and burn the escalation guard. The trade is
  worth taking.
- **HTTP API liveness probe** — Telegram/HTTP success does not
  prove Discord gateway health (Codex round 1 medium); the
  in-process MESSAGE_CREATE counter + on_message timestamp are
  the right substrate-level evidence for this spec.
- **Structured operator surface for "currently suppressing"** —
  the INFO log every N suppressions is sufficient for v1. A
  per-tick metric / dump surface can land if operators ask.

## Implementation surface

- `kernos/kernel/gateway_health.py` — detector + classifier helper
  + cross-check helper + suppression accounting; also
  `_MessageCreateCounter.count_in_window` accepts an optional
  `window_sec` override so the heartbeat cross-check uses its own
  window, not the deaf-detector's 600s.
- `kernos/server.py` — new `_last_on_message_only_ts` global +
  `_bump_on_message_only_ts()` setter. Bump fires from `on_message`
  only — **not** from `on_ready`, `on_resumed`, or the bootstrap
  `_mark_inbound_event()` call.
- `kernos/setup/bring_up_substrate.py` (around line 668) — observer
  construction passes `last_on_message_provider=lambda:
  getattr(_srv, "_last_on_message_only_ts", 0.0)`. Codex round 2:
  the observer is constructed here, not in `server.py` directly.
- `tests/test_gateway_health_observer.py` — new
  `TestHeartbeatLivenessCrossCheck` class; update existing
  immediate-emission tests to account for warm-up + cross-check.

## Risk

- **Suppression hides a real failure.** Mitigation: cross-check
  requires *recent* on_message-only evidence (not lifecycle). If
  the gateway dies, on_message stops within the window, and the
  next tick after the window expires emits normally. Worst-case
  remediation delay ~10 min (5 strikes × 60s after window expires).
- **Warm-up grace masks early failure.** Mitigation: warm-up only
  suppresses tracker-unreliable cases. Real high-latency or `+inf`
  during warm-up still emits.
- **Env-var defaults are wrong for some deployment.** Both windows
  env-tunable.
- **Server-side bump missed in some new on_message path.** Adding
  a separate global with a separate bump site is a duplication
  risk. Mitigation: the bump is one line at one well-known site
  (`on_message`) and any future on_message variant must update it.
  Test scenario 15 specifically covers the case where lifecycle
  bumps fire but on_message does not — that test will fail loudly
  if the wiring drifts.

## Roll-out

Single batch. No flag. Verification post-merge:
1. Restart the bot.
2. Watch `data/discord_*/diagnostics/server.log` for the next 10
   minutes.
3. Expected: no `DISCORD_HEARTBEAT_BLOCKED` friction signals
   during normal operation. Every ~10 suppressions, one INFO
   `GATEWAY_HEALTH_HEARTBEAT_SUPPRESSED_NOMINAL` log so operators
   see the detector is alive.
4. Confirm `discord-heartbeat-blocked` occurrence count stops
   climbing under healthy operation.
5. (Manual gate-test) Disconnect network, wait window+strikes,
   confirm emission still fires. *Skip if not safe to test live.*
