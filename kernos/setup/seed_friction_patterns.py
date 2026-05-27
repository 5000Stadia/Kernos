"""FRICTION-PATTERN-SEED-V1 — seed a starter friction-pattern catalog.

Substrate-tier spec. The FrictionObserver's classifier matches incoming
signals against EXISTING patterns in the catalog. Without seeded
patterns, all signals become "unclassified" reports and the autonomy
loop (FrictionPatternFrequencyEmitter → WTC → workflow execution) has
no fuel — it can never fire from real Kernos usage.

This module seeds 7 starter patterns covering every signal type the
FrictionObserver emits (see kernos/kernel/friction.py), each with a
per-pattern reactivation threshold tuned to its signal class per the
architect's threshold table:

    PROVIDER_ERROR_REPEATED           = 2   # already 'repeated'; low
    MERGED_MESSAGES_DROPPED           = 2   # data loss; low
    EMPTY_RESPONSE                    = 3   # clear failure; modest
    PREFERENCE_STATED_BUT_NOT_CAPTURED= 3   # memory gap; modest
    STALE_DATA_IN_RESPONSE            = 3   # correctness gap; modest
    TOOL_REQUEST_FOR_SURFACED_TOOL    = 3   # surface bug; modest
    TOOL_AVAILABLE_BUT_NOT_USED       = 5   # judgment; higher to limit FP

All patterns enter the catalog in ACTIVE state (substrate-honest:
seed-time facts reflect what actually happened — pattern was
registered, no resolution event occurred). Fast-path autonomy-loop
demonstration is achieved via the low threshold on
PROVIDER_ERROR_REPEATED, not via a fake RESOLVED-at-seed lifecycle
event. (See feedback memory:
[[active-with-threshold-over-resolved-at-seed]].)

Seeding is idempotent — skip-if-present check by
``(instance_id, pattern_id)``. Re-running bring-up doesn't duplicate.

Patterns are substrate-tier / architect-authored / immutable to
Kernos in v1. Operator-mutable surface lands when the agent-side
authoring tools land (Spec 5 deferral).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _SeedPattern:
    """Static descriptor for a starter friction pattern."""

    pattern_id: str
    display_name: str
    description: str
    signal_type_keys: tuple[str, ...]
    reactivation_threshold: int
    # FRICTION-REMEDIATION-V2 (2026-05-20): optional declarative
    # auto-remediation policy. Defaults '' + 0 + 0 = no remediation.
    remediation_action: str = ""
    remediation_threshold_count: int = 0
    remediation_threshold_window_sec: int = 0


# Architect-curated starter catalog (FRICTION-PATTERN-SEED-V1).
# pattern_id naming: lowercase signal_type. Stable, predictable,
# 1:1 mapping with the signal vocabulary emitted by FrictionObserver.
_STARTER_PATTERNS: tuple[_SeedPattern, ...] = (
    _SeedPattern(
        pattern_id="provider-error-repeated",
        display_name="Provider error repeated",
        description=(
            "The LLM provider returned the same error 2+ times in a "
            "single turn. Often signals upstream rate-limit, auth, or "
            "wire-shape regression; recurrence across turns suggests a "
            "stable underlying gap worth a substrate diagnosis."
        ),
        signal_type_keys=("PROVIDER_ERROR_REPEATED",),
        reactivation_threshold=2,
    ),
    _SeedPattern(
        pattern_id="merged-messages-dropped",
        display_name="Merged messages dropped",
        description=(
            "Multi-message merge dropped a request the user actually "
            "made. Data-loss signal — low threshold because each "
            "occurrence represents a user-facing miss."
        ),
        signal_type_keys=("MERGED_MESSAGES_DROPPED",),
        reactivation_threshold=2,
    ),
    _SeedPattern(
        pattern_id="empty-response",
        display_name="Empty response",
        description=(
            "Agent returned no text to a non-empty user message. "
            "Clear failure mode — modest threshold to distinguish "
            "one-off blips from a recurring assembly-path gap."
        ),
        signal_type_keys=("EMPTY_RESPONSE",),
        reactivation_threshold=3,
    ),
    _SeedPattern(
        pattern_id="preference-stated-but-not-captured",
        display_name="Preference stated but not captured",
        description=(
            "User stated a preference but no covenant was created. "
            "Memory-formation gap; modest threshold so recurring "
            "missed preferences surface as a stable pattern."
        ),
        signal_type_keys=("PREFERENCE_STATED_BUT_NOT_CAPTURED",),
        reactivation_threshold=3,
    ),
    _SeedPattern(
        pattern_id="stale-data-in-response",
        display_name="Stale data in response",
        description=(
            "Agent's response referenced data from a prior turn that "
            "was no longer current. Correctness gap; modest threshold."
        ),
        signal_type_keys=("STALE_DATA_IN_RESPONSE",),
        reactivation_threshold=3,
    ),
    _SeedPattern(
        pattern_id="tool-request-for-surfaced-tool",
        display_name="Tool request for surfaced tool",
        description=(
            "Agent requested a tool via request_tool/manage_capabilities "
            "that was already in its surface. Tool-surface bug or "
            "agent confusion about what's already available; modest "
            "threshold."
        ),
        signal_type_keys=("TOOL_REQUEST_FOR_SURFACED_TOOL",),
        reactivation_threshold=3,
    ),
    _SeedPattern(
        pattern_id="tool-available-but-not-used",
        display_name="Tool available but not used",
        description=(
            "A tool that would have fit the user's request was "
            "available in the surface but the agent didn't use it. "
            "Judgment call — higher threshold to limit false "
            "positives from cases where the agent's choice was "
            "actually better."
        ),
        signal_type_keys=("TOOL_AVAILABLE_BUT_NOT_USED",),
        reactivation_threshold=5,
    ),
    # GATEWAY-HEALTH-OBSERVER-V1 (2026-05-19) — gateway/dispatch-layer
    # patterns. Emitted by the new GatewayHealthObserver background
    # task, not by FrictionObserver. Same catalog, same lifecycle,
    # same classifier hook — uniform across both observers.
    _SeedPattern(
        pattern_id="discord-gateway-deaf",
        display_name="Discord gateway dispatching but on_message not firing",
        description=(
            "Discord's gateway is sending MESSAGE_CREATE socket "
            "events (on_socket_event_type counts them) but our "
            "on_message handler hasn't fired within the configured "
            "window. Implies a parser-layer break: events reach the "
            "WebSocket but don't reach the application. Distinct from "
            "discord-heartbeat-blocked which fires when the gateway "
            "itself is dead — this one means the gateway works but "
            "the dispatcher above it is broken. Low threshold (2) "
            "because each occurrence represents lost user messages."
        ),
        signal_type_keys=("DISCORD_GATEWAY_DEAF",),
        reactivation_threshold=2,
    ),
    _SeedPattern(
        pattern_id="space-runner-stuck",
        display_name="on_message fired but no turn produced within window",
        description=(
            "A user message reached on_message (mailbox accepted it) "
            "but no TURN_TIMING event fired for that space within the "
            "configured window. Implies the runner task is blocked — "
            "most likely the sync-I/O-in-async-pipeline class "
            "documented in ASYNC-IO-CONVERSION-V1. Low threshold (2): "
            "stuck runners drop user-facing responses."
        ),
        signal_type_keys=("SPACE_RUNNER_STUCK",),
        reactivation_threshold=2,
    ),
    _SeedPattern(
        pattern_id="discord-heartbeat-blocked",
        display_name="Discord client.latency non-finite or excessive",
        description=(
            "Heartbeat ACK round-trip is reporting inf/NaN/None or "
            "exceeds the configured threshold. Indicates the asyncio "
            "event loop blocked long enough to starve discord.py's "
            "heartbeat task. Once consistent, gateway will close + "
            "discord.py's reconnect coroutine cannot run (same loop) "
            "→ bot goes 'connected but deaf' from the network side. "
            "Moderate threshold (3) — single-tick latency spikes are "
            "common; sustained signals the real failure mode."
        ),
        signal_type_keys=("DISCORD_HEARTBEAT_BLOCKED",),
        reactivation_threshold=3,
        # FRICTION-REMEDIATION-V2: 5 occurrences in 10 min → restart
        # the bot process. Same effective behavior as the standalone
        # watchdog + V1.5 inline strike-counter, but driven by the
        # declarative pattern policy. Cool-off via sentinel file
        # prevents loop-restart if the underlying issue isn't fixed
        # by restart (sentinel survives execv).
        remediation_action="restart_kernos",
        remediation_threshold_count=5,
        remediation_threshold_window_sec=600,
    ),
    _SeedPattern(
        pattern_id="discord-connection-pool-leak",
        display_name="Connection-pool CLOSE_WAIT count above threshold",
        description=(
            "Process has more CLOSE_WAIT sockets than the keepalive "
            "pool size justifies. Typical cause: an httpx.AsyncClient "
            "/ aiohttp ClientSession that's never aclose()d, leaving "
            "remote-closed idle keepalives unreaped until next pool "
            "use. Most commonly observed on the codex_provider's "
            "long-lived OpenAI/Codex client — bounded (pool capped) "
            "but a real lifecycle bug. Higher threshold (5) — pool "
            "naturally floats near its max under load."
        ),
        signal_type_keys=("CONNECTION_POOL_LEAK",),
        reactivation_threshold=5,
    ),
    # SELF-IMPROVEMENT-CLOSURE-V1 (2026-05-26): the seed friction
    # pattern linked to the Tool Availability Honesty invariant.
    # v1 ships the pattern + invariant + probe machinery but does
    # NOT auto-emit CAPABILITY_CATALOG_DISPATCH_DIVERGENCE signals
    # via FrictionObserver — operator manually inserts the
    # friction_pattern_invariant link row and exercises the
    # closure flow. The follow-up CAPABILITY-CATALOG-DISPATCH-
    # DETECTOR-V1 spec adds the auto-detector once operator
    # evidence informs the right shape (post-turn dispatch
    # failure observation vs periodic substrate-state audit).
    _SeedPattern(
        pattern_id="capability-catalog-dispatch-divergence",
        display_name="Capability catalog vs dispatch divergence",
        description=(
            "A tool registered in the catalog is not reachable "
            "via the dispatch path: classify_tool_effect returns "
            "'unknown', or the tool has no handler branch in "
            "execute_tool (kernel source) or no MCP route. Silent "
            "capability-claim vs callability divergence — the "
            "agent sees the tool in its surface but invocation "
            "fails. v1 ships without an auto-detector; closure "
            "exercised manually via operator-inserted link + probe."
        ),
        signal_type_keys=("CAPABILITY_CATALOG_DISPATCH_DIVERGENCE",),
        reactivation_threshold=3,
    ),
)


@dataclass(frozen=True)
class FrictionPatternSeedResult:
    """Returned by ``seed_friction_patterns_on_first_boot``.

    ``seeded`` and ``skipped`` carry pattern_ids; ``warnings`` carries
    free-text non-fatal issues (failed inserts, etc.) so bring-up can
    log them without raising.
    """

    seeded: tuple[str, ...]
    skipped: tuple[str, ...]
    warnings: tuple[str, ...]


async def _maybe_update_remediation_policy(
    *, pattern_store: Any, instance_id: str, seed: "_SeedPattern",
) -> None:
    """FRICTION-REMEDIATION-V2 upgrade path. Existing patterns get
    the seed's remediation policy applied if it differs from what's
    in the DB. Direct SQL update — bypasses ``create_pattern`` since
    the row already exists. Idempotent: a second call where DB
    matches seed is a no-op.
    """
    db = pattern_store._db
    if db is None:
        return
    async with db.execute(
        "SELECT remediation_action, remediation_threshold_count, "
        "       remediation_threshold_window_sec "
        "FROM friction_pattern "
        "WHERE instance_id = ? AND pattern_id = ?",
        (instance_id, seed.pattern_id),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return
    if (
        (row["remediation_action"] or "") == seed.remediation_action
        and int(row["remediation_threshold_count"] or 0)
        == int(seed.remediation_threshold_count)
        and int(row["remediation_threshold_window_sec"] or 0)
        == int(seed.remediation_threshold_window_sec)
    ):
        return  # matches; no-op
    await db.execute(
        "UPDATE friction_pattern SET "
        "remediation_action = ?, "
        "remediation_threshold_count = ?, "
        "remediation_threshold_window_sec = ? "
        "WHERE instance_id = ? AND pattern_id = ?",
        (
            seed.remediation_action,
            int(seed.remediation_threshold_count),
            int(seed.remediation_threshold_window_sec),
            instance_id, seed.pattern_id,
        ),
    )
    logger.info(
        "FRICTION_PATTERN_REMEDIATION_POLICY_UPDATED: "
        "pattern=%s action=%s count=%d window_sec=%d instance=%s",
        seed.pattern_id, seed.remediation_action,
        seed.remediation_threshold_count,
        seed.remediation_threshold_window_sec, instance_id,
    )


async def seed_friction_patterns_on_first_boot(
    instance_id: str,
    pattern_store: Any,
    *,
    data_dir: str,
) -> FrictionPatternSeedResult:
    """Seed the starter friction-pattern catalog for ``instance_id``.

    Idempotent: each starter pattern is created only if its pattern_id
    isn't already in the instance's catalog. Re-running on a bot
    that's been running for a while is a no-op (everything skipped).

    ``pattern_store`` is a started :class:`FrictionPatternStore`;
    ``ensure_schema(data_dir)`` is called defensively so callers
    don't have to thread that ordering. Operationally the store's
    schema is already ensured by bring-up's earlier FrictionObserver
    wiring, but the defensive call keeps this module re-usable from
    other contexts (tests, REPL, etc.).

    Fail-open per substrate convention: individual seed failures are
    logged as warnings and the function continues; only catastrophic
    store-level errors propagate.
    """
    await pattern_store.ensure_schema(data_dir)

    existing_ids: set[str] = set()
    try:
        existing = await pattern_store.list_patterns(instance_id)
        existing_ids = {p.pattern_id for p in existing}
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "FRICTION_PATTERN_SEED: list_patterns failed for instance=%s "
            "error=%s — proceeding with empty existing-id set "
            "(may produce duplicate-id errors below if patterns exist)",
            instance_id, exc,
        )

    seeded: list[str] = []
    skipped: list[str] = []
    warnings: list[str] = []

    for seed in _STARTER_PATTERNS:
        if seed.pattern_id in existing_ids:
            # FRICTION-REMEDIATION-V2: existing patterns get
            # their remediation policy updated from the seed when
            # the policy is non-empty AND differs from what's
            # currently in the DB. This is the upgrade path for
            # bots that were seeded before V2 — they get the
            # restart_kernos policy on discord-heartbeat-blocked
            # without needing a full re-seed.
            if seed.remediation_action:
                try:
                    await _maybe_update_remediation_policy(
                        pattern_store=pattern_store,
                        instance_id=instance_id,
                        seed=seed,
                    )
                except Exception as exc:
                    logger.warning(
                        "FRICTION_PATTERN_REMEDIATION_UPDATE_FAILED "
                        "pattern=%s error=%s",
                        seed.pattern_id, exc,
                    )
            logger.info(
                "FRICTION_PATTERN_SEED_SKIP: pattern=%r already present "
                "instance=%s",
                seed.pattern_id, instance_id,
            )
            skipped.append(seed.pattern_id)
            continue
        try:
            # Use seed_slug to pin pattern_id to the architect-curated
            # value (rather than letting slugify derive it from
            # description). Ensures stable pattern_ids across re-seeds
            # and tests.
            result = await pattern_store.create_pattern(
                instance_id=instance_id,
                description=seed.description,
                signal_type_keys=list(seed.signal_type_keys),
                display_name=seed.display_name,
                seed_slug=seed.pattern_id,
                reactivation_threshold=seed.reactivation_threshold,
                remediation_action=seed.remediation_action,
                remediation_threshold_count=seed.remediation_threshold_count,
                remediation_threshold_window_sec=(
                    seed.remediation_threshold_window_sec
                ),
            )
            logger.info(
                "FRICTION_PATTERN_SEED: pattern_id=%s signal_types=%s "
                "threshold=%d instance=%s",
                result.pattern_id,
                list(seed.signal_type_keys),
                seed.reactivation_threshold,
                instance_id,
            )
            seeded.append(result.pattern_id)
        except Exception as exc:
            msg = (
                f"create_pattern failed for {seed.pattern_id!r}: {exc}"
            )
            logger.warning("FRICTION_PATTERN_SEED_FAILED: %s", msg)
            warnings.append(msg)

    logger.info(
        "FRICTION_PATTERN_SEED_BOOT: instance=%s seeded=%s skipped=%s "
        "warnings=%d",
        instance_id, seeded, skipped, len(warnings),
    )
    return FrictionPatternSeedResult(
        seeded=tuple(seeded),
        skipped=tuple(skipped),
        warnings=tuple(warnings),
    )


__all__ = [
    "FrictionPatternSeedResult",
    "seed_friction_patterns_on_first_boot",
]
