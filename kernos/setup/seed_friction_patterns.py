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
