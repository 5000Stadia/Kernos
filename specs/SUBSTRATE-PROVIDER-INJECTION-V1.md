# SUBSTRATE-PROVIDER-INJECTION-V1

**Date:** 2026-05-21 (revised post-Codex round 1: YELLOW → addressed)
**Status:** Draft for review
**Scope:** `kernos/setup/bring_up_substrate.py` (kill `import kernos.server`
  AND hoist gateway-health bring-up out of self-improvement block),
  `kernos/server.py` (construct + inject gateway-health providers),
  `kernos/kernel/gateway_health.py` (add `GatewayHealthProviders`,
  tighten observer type hint).
**Estimated size:** ~50 LOC source change + ~80 LOC tests.

## Problem (live evidence — Codex round 1)

`bring_up_substrate.py:667` does `import kernos.server as _srv` to read live
state into the `GatewayHealthObserver`. This silently produces **two
separate module objects** for the same `server.py` file:

- `sys.modules["__main__"]` — the actual running script (`python kernos/server.py`).
  This is where the live `client`, `_message_create_counter`,
  `_last_on_message_only_ts`, and `_last_inbound_event_ts` are mutated by
  the @client.event handlers.
- `sys.modules["kernos.server"]` — a parallel copy created when
  `bring_up_substrate.py` does `import kernos.server`. Has the SAME source
  re-executed, but its globals are **separate objects**. Nothing ever
  mutates them.

The observer's lambdas read from the latter (`_srv`). The cross-check
gets `_last_on_message_only_ts == 0.0` and an empty counter forever,
even though Discord traffic is flowing through `__main__`.

Codex's diagnostic reproduction (round 1):
```
$ python -c "
  ... start.sh runs 'python kernos/server.py' → __main__
  Log line: 'INFO __main__: Starting Kernos server' confirms.
  Then bring_up_substrate.py does 'import kernos.server' → new module copy.
"
```

Symptom: the heartbeat false-positive that `HEARTBEAT-DETECTOR-LIVENESS-
CROSSCHECK-V1` (077f8f6) was supposed to suppress is still firing every
60s in production. Suppression count stays 0 because every cross-check
reads inert state. Restart loops at ~3/hour continue.

## Root cause

`bring_up_substrate` is **substrate** code. It should not import its own
caller by name to reach back into the script's globals. Doing so:

1. Creates the dual-module bug above when the caller is `__main__`.
2. Couples substrate code to the specific module path `kernos.server`,
   making it harder to test or invoke from anywhere else (CLI, tests,
   alternate adapters).
3. Hides the dependency. Future readers can't tell what state the
   observer actually reads.

## Fix — provider injection

`bring_up_substrate()` accepts a new optional keyword arg
`gateway_health_providers: GatewayHealthProviders | None`. The caller
constructs it from its own live globals and passes it in. Substrate
uses what it's given and never imports the caller.

### Data class

In `kernos/kernel/gateway_health.py` (the existing module that owns
the observer; same place as `_MessageCreateCounter`):

```python
@dataclass(frozen=True)
class GatewayHealthProviders:
    """Live state sources the observer reads on every tick.

    The caller (typically `server.py`) constructs this from its own
    module globals and passes it to ``bring_up_substrate``. This
    keeps the substrate from importing its caller by name — which
    fails silently when the caller is `__main__` (dual-module bug,
    2026-05-21 RCA).

    All fields are callables so the observer reads fresh values on
    every tick. ``message_create_counter`` is a reference rather
    than a callable because it's a stable object that mutates in
    place.
    """
    latency_provider: Callable[[], float | None]
    inbound_event_ts_provider: Callable[[], float]
    last_on_message_provider: Callable[[], float]
    message_create_counter: "_MessageCreateCounter | None"
```

### Substrate signature

```python
# kernos/setup/bring_up_substrate.py

async def bring_up_substrate(
    *,
    data_dir: str,
    handler: Any,
    agent_registry: "AgentRegistry",
    gateway_health_providers: "GatewayHealthProviders | None" = None,
) -> Substrate:
    ...
```

When `gateway_health_providers is None`, **skip the gateway-health
observer bring-up entirely** with a clear log line. This is the test
mode (no Discord client). Removing the silent reach-back means tests
can no longer rely on it accidentally working.

**Hoist the observer bring-up out of the self-improvement block**
(Codex round 1 finding 1). Today the gateway-health observer is
nested inside the self-improvement autonomy block, so it's
silently skipped when self-improvement env vars are missing or
its bring-up fails. Gateway health is a safety monitor — it must
run independently of self-improvement gating. The new layout:
own try block, sources `_si_instance_id` from
`KERNOS_INSTANCE_ID` or the handler's instance identifier, and
gets `pattern_store` from `handler._friction_pattern_store`
(already constructed during handler init at `handler.py:962`)
falling back to a fresh `FrictionPatternStore` if absent.

When provided, use the fields directly:

```python
if gateway_health_providers is not None:
    try:
        _gw_observer = GatewayHealthObserver(
            instance_id=_si_instance_id,
            data_dir=data_dir,
            pattern_store=_si_pattern_store,
            latency_provider=gateway_health_providers.latency_provider,
            inbound_event_ts_provider=gateway_health_providers.inbound_event_ts_provider,
            message_create_counter=gateway_health_providers.message_create_counter,
            last_on_message_provider=gateway_health_providers.last_on_message_provider,
            runner_inspector=None,
        )
        await _gw_observer.start()
        execution_engine.register_emitter("gateway_health", _gw_observer)
    except Exception as _exc_gw:
        logger.warning(
            "GATEWAY_HEALTH_OBSERVER_BRINGUP_FAILED error=%s — "
            "continuing without gateway-health observer", _exc_gw,
        )
else:
    logger.info(
        "GATEWAY_HEALTH_OBSERVER_SKIPPED: no providers injected "
        "(test or headless mode)",
    )
```

**No `import kernos.server` anywhere in `bring_up_substrate.py`.**

### Caller side (server.py)

At the bring-up call site:

```python
from kernos.kernel.gateway_health import GatewayHealthProviders
from kernos.setup.bring_up_substrate import bring_up_substrate

_gw_providers = GatewayHealthProviders(
    latency_provider=lambda: (
        getattr(client, "latency", None) if client is not None else None
    ),
    inbound_event_ts_provider=lambda: _last_inbound_event_ts,
    last_on_message_provider=lambda: _last_on_message_only_ts,
    message_create_counter=_message_create_counter,
)
_substrate = await bring_up_substrate(
    data_dir=data_dir,
    handler=handler,
    agent_registry=_agent_registry,
    gateway_health_providers=_gw_providers,
)
```

The lambdas close over `server.py`'s LOCAL `__main__` globals. No
`import kernos.server`. Live mutations are visible immediately.

## What does NOT change

- The observer's internal API (`__init__` signature, `_tick`,
  `_detect_*`) is unchanged.
- Other substrate components (`FrictionPatternStore`, runtime
  emitters, action library, etc.) are untouched.
- The `_MessageCreateCounter`, `_last_inbound_event_ts`,
  `_last_on_message_only_ts`, `_mark_inbound_event`,
  `_bump_on_message_only_ts` definitions in `server.py` are
  untouched — they keep working as `__main__` globals.
- The runner_inspector seam (None today) is unchanged.

## Acceptance criteria

| # | Scenario | Expected behavior |
|---|---|---|
| 1 | `bring_up_substrate(..., gateway_health_providers=None)` | Observer bring-up skipped (NOT a no-op observer); INFO `GATEWAY_HEALTH_OBSERVER_SKIPPED` log; substrate otherwise normal |
| 2 | `bring_up_substrate(..., gateway_health_providers=<populated>)`, **with self-improvement bring-up disabled or failing** | Observer STILL constructs (independent of self-improvement block). Codex round 1 finding: gateway-health is a safety monitor and must not be self-improvement-gated. |
| 3 | `bring_up_substrate.py` AST import-guard test: no `Import("kernos.server")` or `ImportFrom("kernos.server", ...)` node anywhere in the module | Catches future re-add at the import-statement level, not just substring presence |
| 4 | Runtime test: after calling `bring_up_substrate(...)` from a clean Python process, `sys.modules.get("kernos.server")` is `None` (or equal to `sys.modules[__name__]` of the caller). Substrate must never have caused a second module-load. | Confirms the dual-module bug cannot recur from substrate code |
| 5 | After server.py boot in production, observer's `last_on_message_provider()` returns the value set by the on_message bump (not 0.0) | Live verification: send a Discord message, observer's provider returns the bump timestamp |
| 6 | Existing `GatewayHealthObserver` unit tests still pass unchanged | Constructor signature unchanged; only the caller pattern changes |
| 7 | New test: `bring_up_substrate` called with `gateway_health_providers=None` logs the SKIPPED line and produces no observer in `execution_engine` emitters | Verify via caplog + emitter introspection |
| 8 | New test: `bring_up_substrate` with populated providers calls `GatewayHealthObserver(...)` with the **exact identical** callable objects + counter object from the dataclass (assert via `is` identity, not equality). Then asserts `start()` called once and `register_emitter("gateway_health", obs)` called once. | Codex round 1 finding: AC must assert constructor-arg identity, not just "start called" |
| 9 | New mutation test: caller constructs `GatewayHealthProviders` closing over a local mutable state. Mutate the state, then call provider lambdas, assert provider call returns new value. Pins that the lambda captures-by-reference, not by-value-at-construction. | Catches a future regression where a lambda freezes the value at construction time |
| 10 | New test: `server.py` source has `gateway_health_providers=` keyword in the actual `bring_up_substrate(` call. AST-level check. | Catches a future removal of the wiring at the call site |
| 11 | New test: `bring_up_substrate.py` source has no `Import`/`ImportFrom` node referencing `kernos.server` (same as AC3) AND no string literal `"kernos.server"` outside of comments/docstrings | Belt-and-suspenders against future re-introduction by any mechanism |

## Out of scope

- Other places in the codebase that import `kernos.server` by name.
  Quick grep:
  ```
  $ grep -rn "kernos.server" kernos/ --include='*.py'
  ```
  If any production code path does this, it should also be fixed —
  but that's a separate audit. This spec is the heartbeat-detector
  hotfix only.
- Renaming `server.py` to allow `python -m kernos.server` invocation
  (which would resolve the `__main__` vs `kernos.server` split at
  the source). That's a deployment-shape decision, not this spec.
- Refactoring all of `bring_up_substrate.py` to take dependencies via
  injection. Only the gateway-health observer is broken by the dual
  module; other substrate components don't reach back into `server.py`.

## Implementation surface

- `kernos/kernel/gateway_health.py` — new `GatewayHealthProviders`
  dataclass (frozen, 4 fields). Exported via `__all__`. Also
  tighten `GatewayHealthObserver.__init__` `message_create_counter`
  type hint to `_MessageCreateCounter | None` (existing code at
  lines 563 + 609 already handles None; the type hint just hasn't
  caught up).
- `kernos/setup/bring_up_substrate.py` — new optional kwarg, remove
  `import kernos.server`, branch on providers-None.
- `kernos/server.py` — construct `GatewayHealthProviders` locally,
  pass to bring-up.
- `tests/test_substrate_bringup_providers.py` (new) — acceptance tests
  6, 7, 8.
- `tests/test_gateway_health_observer.py` — unchanged (constructor
  unchanged).

## Risk

- **Headless callers** — if anything other than `server.py` was
  relying on the silent reach-back (tests, alt adapters), they will
  now skip the observer bring-up. Mitigation: the skip logs an INFO
  line so it's visible. Test suite passes prove no surprise reliance.
- **Breaking change to bring_up_substrate signature** — new kwarg is
  optional with `None` default. Existing callers continue to work
  (just get the SKIPPED log).

## Roll-out

Single batch. Manual verification on live bot:
1. Restart bot (auto-update pulls the new commit). Optional: set
   `KERNOS_HEARTBEAT_SUPPRESSION_LOG_EVERY_N=1` for the verification
   window so the nominal log fires every tick instead of every 10
   (default 10 × 60s poll = first nominal at ~10 min, may exceed
   patience).
2. Send a Discord message.
3. Watch `data/discord_*/diagnostics/server.log` for the next 10 min.
4. Expected: `GATEWAY_HEALTH_HEARTBEAT_SUPPRESSED_NOMINAL` INFO log
   appears (frequency depends on the env override above); no
   `GATEWAY_HEALTH_SIGNAL_EMITTED` lines for `discord-heartbeat-blocked`
   while traffic is flowing.
5. Confirm: `FRICTION_REMEDIATION_RESTART_KERNOS` no longer fires
   for this pattern. The fire-history file stops growing.

If post-merge the observer doesn't construct or the providers don't
read live, the bring-up log surfaces it loudly — no silent fall-back.
