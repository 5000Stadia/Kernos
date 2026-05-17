"""FRICTION-PATTERN-SEED-V1 pin tests.

Pins:
  * 7 starter patterns reach the catalog post-seed
  * Each pattern carries its architect-specified threshold
  * Re-seed is idempotent (no duplicates, all skipped)
  * Per-pattern reactivation_threshold is read by record_recurrence
    (not the global env var)
  * PROVIDER_ERROR_REPEATED at threshold=2 fires reactivation on the
    SECOND recurrence — the fast-path autonomy-loop demonstration
    target the architect designed the seed to enable
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from kernos.kernel.friction_patterns import (
    CLASSIFIED_AUTO_SIGNAL_TYPE,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_RESOLVED,
    FrictionPatternStore,
)
from kernos.setup.seed_friction_patterns import (
    _STARTER_PATTERNS,
    seed_friction_patterns_on_first_boot,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Architect-specified threshold table — mirrored in the test as the
# source of truth so any drift between the test's expectations and
# the seed module's table surfaces as a test failure.
_EXPECTED_THRESHOLDS = {
    "provider-error-repeated": 2,
    "merged-messages-dropped": 2,
    "empty-response": 3,
    "preference-stated-but-not-captured": 3,
    "stale-data-in-response": 3,
    "tool-request-for-surfaced-tool": 3,
    "tool-available-but-not-used": 5,
}


@pytest.fixture
async def store(tmp_path):
    """A started FrictionPatternStore against an isolated data_dir."""
    s = FrictionPatternStore()
    await s.start(str(tmp_path))
    yield s
    await s.stop()


# ---------------------------------------------------------------------
# Catalog population pins
# ---------------------------------------------------------------------


async def test_seed_populates_seven_starter_patterns(tmp_path, store):
    """The seed catalog reaches the friction_pattern table."""
    result = await seed_friction_patterns_on_first_boot(
        "inst_a", store, data_dir=str(tmp_path),
    )
    assert len(result.seeded) == 7
    assert len(result.skipped) == 0
    assert result.warnings == ()
    patterns = await store.list_patterns("inst_a")
    assert {p.pattern_id for p in patterns} == set(_EXPECTED_THRESHOLDS)


async def test_each_seeded_pattern_has_architect_specified_threshold(
    tmp_path, store,
):
    """Per-pattern thresholds match the architect's table verbatim."""
    await seed_friction_patterns_on_first_boot(
        "inst_a", store, data_dir=str(tmp_path),
    )
    patterns = await store.list_patterns("inst_a")
    by_id = {p.pattern_id: p for p in patterns}
    for pid, expected_threshold in _EXPECTED_THRESHOLDS.items():
        assert pid in by_id, f"missing seeded pattern: {pid}"
        assert by_id[pid].reactivation_threshold == expected_threshold, (
            f"{pid}: expected threshold {expected_threshold}, "
            f"got {by_id[pid].reactivation_threshold}"
        )


async def test_seeded_patterns_enter_active_state(tmp_path, store):
    """Substrate-honest seeding: patterns enter ACTIVE state at seed
    time. No fabricated resolution events
    ([[active-with-threshold-over-resolved-at-seed]])."""
    await seed_friction_patterns_on_first_boot(
        "inst_a", store, data_dir=str(tmp_path),
    )
    patterns = await store.list_patterns("inst_a")
    for p in patterns:
        assert p.lifecycle_state == LIFECYCLE_ACTIVE, (
            f"{p.pattern_id} entered {p.lifecycle_state} at seed; "
            f"architect's call was ACTIVE for all"
        )


# ---------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------


async def test_reseed_is_idempotent(tmp_path, store):
    """Second seed call skips every pattern; catalog is unchanged."""
    first = await seed_friction_patterns_on_first_boot(
        "inst_a", store, data_dir=str(tmp_path),
    )
    assert len(first.seeded) == 7
    second = await seed_friction_patterns_on_first_boot(
        "inst_a", store, data_dir=str(tmp_path),
    )
    assert len(second.seeded) == 0
    assert len(second.skipped) == 7
    assert second.warnings == ()
    # Catalog total should still be 7 — no duplicates.
    patterns = await store.list_patterns("inst_a")
    assert len(patterns) == 7


async def test_seed_isolates_per_instance(tmp_path, store):
    """Seeding instance A doesn't touch instance B's catalog."""
    await seed_friction_patterns_on_first_boot(
        "inst_a", store, data_dir=str(tmp_path),
    )
    patterns_a = await store.list_patterns("inst_a")
    patterns_b = await store.list_patterns("inst_b")
    assert len(patterns_a) == 7
    assert len(patterns_b) == 0
    # Seeding B then matches.
    await seed_friction_patterns_on_first_boot(
        "inst_b", store, data_dir=str(tmp_path),
    )
    patterns_b = await store.list_patterns("inst_b")
    assert len(patterns_b) == 7


# ---------------------------------------------------------------------
# Per-pattern threshold drives record_recurrence (substrate behavior)
# ---------------------------------------------------------------------


async def test_record_recurrence_uses_per_pattern_threshold(
    tmp_path, store, monkeypatch,
):
    """The reactivation threshold check at recurrence time reads from
    the pattern's stored value, NOT the global env var. This is the
    substrate change that makes the seed's per-pattern thresholds
    meaningful — without it, the seed values would persist but the
    check would still consult the env."""
    # Set env to a value DIFFERENT from any seeded pattern's threshold
    # so we can tell which side drives the reactivation.
    monkeypatch.setenv("KERNOS_FRICTION_REACTIVATION_THRESHOLD", "10")
    monkeypatch.setenv(
        "KERNOS_FRICTION_REACTIVATION_WINDOW_DAYS", "365",
    )
    await seed_friction_patterns_on_first_boot(
        "inst_a", store, data_dir=str(tmp_path),
    )
    # Resolve PROVIDER_ERROR_REPEATED so subsequent occurrences route
    # through record_recurrence (the reactivation-check path).
    await store.transition_lifecycle(
        "inst_a", "provider-error-repeated", LIFECYCLE_RESOLVED,
    )
    # First recurrence: shouldn't trigger (threshold=2, count=1).
    triggered_1 = await store.record_recurrence(
        instance_id="inst_a",
        pattern_id="provider-error-repeated",
        observed_at=_now(),
        report_path="seed-pin-1.md",
        classified_by=CLASSIFIED_AUTO_SIGNAL_TYPE,
    )
    assert triggered_1 is False, (
        "first recurrence triggered prematurely — per-pattern "
        "threshold not being read (env=10 would make this false too "
        "but seeded threshold=2 should also not trigger on count=1)"
    )
    # Second recurrence: SHOULD trigger (threshold=2, count=2).
    # If env-derived threshold (=10) were being used, this would NOT
    # trigger. The fact that it does triggers proves per-pattern wins.
    triggered_2 = await store.record_recurrence(
        instance_id="inst_a",
        pattern_id="provider-error-repeated",
        observed_at=_now(),
        report_path="seed-pin-2.md",
        classified_by=CLASSIFIED_AUTO_SIGNAL_TYPE,
    )
    assert triggered_2 is True, (
        "second recurrence didn't trigger reactivation; per-pattern "
        "threshold=2 was not honored (env=10 may be incorrectly "
        "overriding the per-pattern value)"
    )


async def test_higher_threshold_pattern_resists_early_reactivation(
    tmp_path, store, monkeypatch,
):
    """TOOL_AVAILABLE_BUT_NOT_USED (threshold=5) should NOT reactivate
    on the 2nd recurrence even when env says threshold=1. Inverse
    pin to the previous test — proves the per-pattern read is faithful
    in both directions."""
    monkeypatch.setenv("KERNOS_FRICTION_REACTIVATION_THRESHOLD", "1")
    monkeypatch.setenv(
        "KERNOS_FRICTION_REACTIVATION_WINDOW_DAYS", "365",
    )
    await seed_friction_patterns_on_first_boot(
        "inst_a", store, data_dir=str(tmp_path),
    )
    await store.transition_lifecycle(
        "inst_a", "tool-available-but-not-used", LIFECYCLE_RESOLVED,
    )
    # Drive 4 recurrences — under the seeded threshold of 5.
    for i in range(4):
        triggered = await store.record_recurrence(
            instance_id="inst_a",
            pattern_id="tool-available-but-not-used",
            observed_at=_now(),
            report_path=f"tabnu-{i}.md",
            classified_by=CLASSIFIED_AUTO_SIGNAL_TYPE,
        )
        assert triggered is False, (
            f"recurrence #{i+1} triggered reactivation; "
            f"per-pattern threshold=5 should have held it back "
            f"even though env=1"
        )
    # 5th recurrence: should now trigger.
    triggered_5 = await store.record_recurrence(
        instance_id="inst_a",
        pattern_id="tool-available-but-not-used",
        observed_at=_now(),
        report_path="tabnu-5.md",
        classified_by=CLASSIFIED_AUTO_SIGNAL_TYPE,
    )
    assert triggered_5 is True, (
        "5th recurrence should trigger reactivation per the seeded "
        "threshold=5; per-pattern read is not firing at the threshold"
    )


# ---------------------------------------------------------------------
# Module-level invariants
# ---------------------------------------------------------------------


def test_starter_patterns_cover_all_seven_signal_types():
    """The seed module's _STARTER_PATTERNS must cover every signal
    type the FrictionObserver emits. Test pins both completeness
    (no signal type missed) and the architect's threshold values."""
    starter_by_signal = {
        sig: p
        for p in _STARTER_PATTERNS
        for sig in p.signal_type_keys
    }
    for signal, expected_threshold in {
        "PROVIDER_ERROR_REPEATED": 2,
        "MERGED_MESSAGES_DROPPED": 2,
        "EMPTY_RESPONSE": 3,
        "PREFERENCE_STATED_BUT_NOT_CAPTURED": 3,
        "STALE_DATA_IN_RESPONSE": 3,
        "TOOL_REQUEST_FOR_SURFACED_TOOL": 3,
        "TOOL_AVAILABLE_BUT_NOT_USED": 5,
    }.items():
        assert signal in starter_by_signal, (
            f"signal type {signal!r} not covered by any starter "
            f"pattern in _STARTER_PATTERNS — FrictionObserver emits "
            f"it but the seed catalog won't classify it"
        )
        p = starter_by_signal[signal]
        assert p.reactivation_threshold == expected_threshold, (
            f"{signal}: expected threshold {expected_threshold}, "
            f"got {p.reactivation_threshold}"
        )
