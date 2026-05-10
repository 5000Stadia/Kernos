"""FRICTION-PATTERN-STABLE-IDS-V1 implementation tests.

Covers the spec's embedded live tests across all five categories:
catalog round-trip, auto-classify behavior, reactivation,
member-isolation, frontmatter parser, plus the architect-call folds
(FK ON DELETE RESTRICT pin; concurrent transition + occurrence race).
"""
from __future__ import annotations

import asyncio
import os
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
import pytest

from kernos.kernel.friction_patterns import (
    AliasCollision,
    CLASSIFIED_AUTO_SIGNAL_TYPE,
    CLASSIFIED_AUTO_TOKEN_OVERLAP,
    CLASSIFIED_BACKFILL,
    CLASSIFIED_MANUAL,
    FrictionPattern,
    FrictionPatternStore,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_ARCHIVED,
    LIFECYCLE_REACTIVATED,
    LIFECYCLE_RESOLVED,
    PatternArchived,
    SignalTypeKeyCollision,
    StoreContention,
    UnknownPattern,
    classified_by_for_match_path,
    classify_signal,
    parse_spec_pattern_refs,
    slugify,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ago(seconds: int) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(seconds=seconds)
    ).isoformat()


@pytest.fixture
async def store(tmp_path):
    """Fresh store backed by a tmp_path instance.db."""
    s = FrictionPatternStore()
    await s.start(str(tmp_path))
    yield s
    await s.stop()


# ---------------------------------------------------------------------------
# Catalog round-trip
# ---------------------------------------------------------------------------


class TestCatalogRoundtrip:
    async def test_create_get_pattern(self, store):
        p = await store.create_pattern(
            instance_id="inst-A",
            description="Integration timeout under load",
            signal_type_keys=["INTEGRATION_TIMEOUT"],
        )
        assert p.pattern_id == "integration-timeout-under-load"
        assert p.signal_type_keys == ("INTEGRATION_TIMEOUT",)
        assert p.aliases == ()
        assert p.display_name == ""
        assert p.lifecycle_state == LIFECYCLE_ACTIVE
        assert p.occurrence_count == 0

        loaded = await store.get_pattern("inst-A", p.pattern_id)
        assert loaded is not None
        assert loaded.pattern_id == p.pattern_id
        assert loaded.signal_type_keys == ("INTEGRATION_TIMEOUT",)

    async def test_create_with_parent_grouping(self, store):
        parent = await store.create_pattern(
            instance_id="inst-A",
            description="Integration failure (umbrella)",
        )
        child = await store.create_pattern(
            instance_id="inst-A",
            description="Integration timeout child",
            signal_type_keys=["INTEGRATION_TIMEOUT"],
            parent_pattern_id=parent.pattern_id,
        )
        children = await store.list_patterns(
            "inst-A", parent_pattern_id=parent.pattern_id,
        )
        assert {c.pattern_id for c in children} == {child.pattern_id}

        all_patterns = await store.list_patterns("inst-A")
        assert {p.pattern_id for p in all_patterns} == {parent.pattern_id, child.pattern_id}

    async def test_record_occurrence_increments_counter(self, store):
        p = await store.create_pattern(
            instance_id="inst-A",
            description="Pattern alpha distinctive",
        )
        for i in range(3):
            await store.record_occurrence(
                instance_id="inst-A",
                pattern_id=p.pattern_id,
                observed_at=_now(),
                report_path=f"reports/{i}.md",
            )
        # query_frequency
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        n = await store.query_frequency(
            "inst-A", p.pattern_id,
            window_start=past, window_end=future,
        )
        assert n == 3
        # Outside window.
        far_future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        far_far = (datetime.now(timezone.utc) + timedelta(days=60)).isoformat()
        assert await store.query_frequency(
            "inst-A", p.pattern_id,
            window_start=far_future, window_end=far_far,
        ) == 0

    async def test_query_top_patterns(self, store):
        p1 = await store.create_pattern(
            instance_id="inst-A", description="Top one alpha")
        p2 = await store.create_pattern(
            instance_id="inst-A", description="Top two bravo")
        p3 = await store.create_pattern(
            instance_id="inst-A", description="Top three charlie")
        for n_occurrences, p in [(5, p1), (3, p2), (1, p3)]:
            for i in range(n_occurrences):
                await store.record_occurrence(
                    instance_id="inst-A",
                    pattern_id=p.pattern_id,
                    observed_at=_now(),
                    report_path=f"reports/{p.pattern_id}_{i}.md",
                )
        past = _ago(3600)
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        top = await store.query_top_patterns(
            "inst-A", window_start=past, window_end=future, limit=2,
        )
        assert len(top) == 2
        assert top[0][0].pattern_id == p1.pattern_id
        assert top[0][1] == 5
        assert top[1][0].pattern_id == p2.pattern_id
        assert top[1][1] == 3

    async def test_lifecycle_transitions(self, store):
        p = await store.create_pattern(
            instance_id="inst-A", description="Lifecycle test alpha")
        p2 = await store.transition_lifecycle(
            "inst-A", p.pattern_id, LIFECYCLE_RESOLVED,
            resolved_by_spec="spec-x",
        )
        assert p2.lifecycle_state == LIFECYCLE_RESOLVED
        assert p2.resolved_at
        assert p2.resolved_by_spec == "spec-x"

        # resolved -> archived
        p3 = await store.transition_lifecycle(
            "inst-A", p.pattern_id, LIFECYCLE_ARCHIVED)
        assert p3.lifecycle_state == LIFECYCLE_ARCHIVED

        # archived -> active (operator override path)
        p4 = await store.transition_lifecycle(
            "inst-A", p.pattern_id, LIFECYCLE_ACTIVE)
        assert p4.lifecycle_state == LIFECYCLE_ACTIVE

    async def test_pattern_id_immutable_with_alias_continuity(self, store):
        p = await store.create_pattern(
            instance_id="inst-A",
            description="Immutable test alpha beta",
        )
        # No rename_pattern method.
        assert not hasattr(store, "rename_pattern")

        # Record occurrences.
        for i in range(3):
            await store.record_occurrence(
                instance_id="inst-A",
                pattern_id=p.pattern_id,
                observed_at=_now(),
                report_path=f"reports/imm_{i}.md",
            )
        # Add alias.
        p2 = await store.add_alias("inst-A", p.pattern_id, "old-name")
        assert "old-name" in p2.aliases

        # Old name resolves to same pattern.
        resolved = await store.get_pattern("inst-A", "old-name")
        assert resolved is not None
        assert resolved.pattern_id == p.pattern_id

        # Occurrences still queryable under immutable id.
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        n = await store.query_frequency(
            "inst-A", p.pattern_id, window_start=_ago(3600), window_end=future,
        )
        assert n == 3

    async def test_set_display_name_and_update_description(self, store):
        p = await store.create_pattern(
            instance_id="inst-A", description="Original description for alpha")
        p2 = await store.set_display_name("inst-A", p.pattern_id, "Pretty Name")
        assert p2.display_name == "Pretty Name"
        assert p2.pattern_id == p.pattern_id  # immutable

        p3 = await store.update_description(
            "inst-A", p.pattern_id, "Updated description for the alpha pattern",
        )
        assert p3.description == "Updated description for the alpha pattern"
        assert p3.pattern_id == p.pattern_id  # immutable
        assert p3.display_name == "Pretty Name"

    async def test_create_pattern_signal_type_keys_uniqueness(self, store):
        await store.create_pattern(
            instance_id="inst-A",
            description="First pattern alpha",
            signal_type_keys=["INTEGRATION_TIMEOUT"],
        )
        with pytest.raises(SignalTypeKeyCollision):
            await store.create_pattern(
                instance_id="inst-A",
                description="Second pattern beta gamma",
                signal_type_keys=["INTEGRATION_TIMEOUT", "OTHER"],
            )

        # Archive the first; second can now create.
        pa = await store.list_patterns("inst-A")
        await store.transition_lifecycle(
            "inst-A", pa[0].pattern_id, LIFECYCLE_ARCHIVED,
        )
        p2 = await store.create_pattern(
            instance_id="inst-A",
            description="Second pattern beta gamma",
            signal_type_keys=["INTEGRATION_TIMEOUT"],
        )
        assert p2.signal_type_keys == ("INTEGRATION_TIMEOUT",)

    async def test_slug_collision_appends_numeric_suffix(self, store):
        p1 = await store.create_pattern(
            instance_id="inst-A", description="Compaction fails alpha")
        # Same seed via update_description on a separate pattern won't
        # collide; collision tests use raw seed_slug.
        p2 = await store.create_pattern(
            instance_id="inst-A", description="Compaction fails beta",
            seed_slug=p1.pattern_id,  # force collision
        )
        assert p1.pattern_id == "compaction-fails-alpha"
        assert p2.pattern_id == f"{p1.pattern_id}-2"

    async def test_orphan_insert_rejected_via_fk(self, store):
        """Architect call Q2 round-2 Blocker 3: PRAGMA foreign_keys=ON
        is enforced; orphan inserts trip the FK constraint."""
        with pytest.raises(aiosqlite.IntegrityError):
            await store.db.execute(
                "INSERT INTO friction_pattern_occurrence "
                "(occurrence_id, instance_id, pattern_id, observed_at) "
                "VALUES (?, ?, ?, ?)",
                ("occ-orphan", "inst-A", "ghost-pattern", _now()),
            )

    async def test_single_label_report_uniqueness(self, store):
        """Round-2 Finding 5: same report_path under multiple patterns
        is rejected by the UNIQUE partial index."""
        p1 = await store.create_pattern(
            instance_id="inst-A", description="Pattern uno report-test")
        p2 = await store.create_pattern(
            instance_id="inst-A", description="Pattern dos report-test")
        path = "reports/shared.md"
        await store.record_occurrence(
            instance_id="inst-A",
            pattern_id=p1.pattern_id,
            observed_at=_now(),
            report_path=path,
        )
        # Re-record on p2 — UNIQUE constraint should reject the insert.
        # The store's record_occurrence path catches IntegrityError only
        # on the SAME pattern (idempotent semantics); cross-pattern
        # violations of the (instance_id, report_path) UNIQUE index
        # should surface as an IntegrityError, which we expect here.
        with pytest.raises(aiosqlite.IntegrityError):
            await store.record_occurrence(
                instance_id="inst-A",
                pattern_id=p2.pattern_id,
                observed_at=_now(),
                report_path=path,
            )

    async def test_signal_type_keys_collision_on_transition_to_active(
        self, store,
    ):
        """Round-2 Finding 6: transition_lifecycle into active or
        reactivated re-checks signal_type_keys uniqueness."""
        pa = await store.create_pattern(
            instance_id="inst-A",
            description="Pattern alpha for collision test",
            signal_type_keys=["INTEGRATION_TIMEOUT"],
        )
        # Archive A so B can be created with same keys.
        await store.transition_lifecycle(
            "inst-A", pa.pattern_id, LIFECYCLE_ARCHIVED,
        )
        await store.create_pattern(
            instance_id="inst-A",
            description="Pattern beta for collision test",
            signal_type_keys=["INTEGRATION_TIMEOUT"],
        )
        # Now try to transition A back to active — should collide with B.
        with pytest.raises(SignalTypeKeyCollision):
            await store.transition_lifecycle(
                "inst-A", pa.pattern_id, LIFECYCLE_ACTIVE,
            )

    async def test_alias_collision_against_existing_pattern_id(self, store):
        pa = await store.create_pattern(
            instance_id="inst-A", description="Pattern foo bar baz")
        pb = await store.create_pattern(
            instance_id="inst-A", description="Pattern qux quux corge")
        # Try aliasing B to A's pattern_id.
        with pytest.raises(AliasCollision):
            await store.add_alias(
                "inst-A", pb.pattern_id, pa.pattern_id,
            )

    async def test_alias_collision_against_existing_alias(self, store):
        pa = await store.create_pattern(
            instance_id="inst-A", description="Pattern aa first")
        pb = await store.create_pattern(
            instance_id="inst-A", description="Pattern bb second")
        await store.add_alias("inst-A", pa.pattern_id, "shared-alias")
        with pytest.raises(AliasCollision):
            await store.add_alias("inst-A", pb.pattern_id, "shared-alias")

    async def test_alias_normalized_via_slugify(self, store):
        pa = await store.create_pattern(
            instance_id="inst-A", description="Pattern norm test alpha")
        pa2 = await store.add_alias("inst-A", pa.pattern_id, "Foo Bar!")
        assert "foo-bar" in pa2.aliases
        # Subsequent collision on the normalized form.
        pb = await store.create_pattern(
            instance_id="inst-A", description="Pattern norm test beta")
        with pytest.raises(AliasCollision):
            await store.add_alias("inst-A", pb.pattern_id, "foo-bar")

    async def test_on_delete_restrict_blocks_pattern_delete_with_occurrences(
        self, store,
    ):
        """Architect call Q1 (v3→v4): FK ON DELETE RESTRICT pins."""
        p = await store.create_pattern(
            instance_id="inst-A", description="Delete-restrict test alpha")
        await store.record_occurrence(
            instance_id="inst-A",
            pattern_id=p.pattern_id,
            observed_at=_now(),
            report_path="reports/del.md",
        )
        with pytest.raises(aiosqlite.IntegrityError):
            await store.db.execute(
                "DELETE FROM friction_pattern "
                "WHERE instance_id = ? AND pattern_id = ?",
                ("inst-A", p.pattern_id),
            )

        # Removing the occurrence first lets the pattern delete succeed.
        await store.db.execute(
            "DELETE FROM friction_pattern_occurrence "
            "WHERE instance_id = ? AND pattern_id = ?",
            ("inst-A", p.pattern_id),
        )
        await store.db.execute(
            "DELETE FROM friction_pattern "
            "WHERE instance_id = ? AND pattern_id = ?",
            ("inst-A", p.pattern_id),
        )
        assert await store.get_pattern("inst-A", p.pattern_id) is None


# ---------------------------------------------------------------------------
# Auto-classify behavior
# ---------------------------------------------------------------------------


class TestAutoClassify:
    async def test_signal_type_path_a_match_scores_one_zero(self, store):
        p = await store.create_pattern(
            instance_id="inst-A",
            description="Integration timeout under load",
            signal_type_keys=["INTEGRATION_TIMEOUT"],
        )
        candidates = await store.list_patterns("inst-A")
        result = classify_signal(
            signal_type="INTEGRATION_TIMEOUT",
            signal_description="some unrelated text",
            candidates=candidates,
        )
        assert result is not None
        pattern, score, match_path = result
        assert pattern.pattern_id == p.pattern_id
        assert score == 1.0
        assert match_path == "signal-type"
        assert classified_by_for_match_path(match_path) == CLASSIFIED_AUTO_SIGNAL_TYPE

    async def test_token_overlap_path_b_uses_own_threshold(
        self, store, monkeypatch,
    ):
        monkeypatch.setenv("KERNOS_FRICTION_TOKEN_OVERLAP_THRESHOLD", "0.1")
        p = await store.create_pattern(
            instance_id="inst-A",
            description="agent called request_tool when target tool already surfaced",
        )
        candidates = await store.list_patterns("inst-A")
        result = classify_signal(
            signal_type="UNKNOWN_TYPE",  # no Path A match
            signal_description=(
                "request_tool was called even though the target tool was surfaced"
            ),
            candidates=candidates,
        )
        assert result is not None
        pattern, score, match_path = result
        assert pattern.pattern_id == p.pattern_id
        assert match_path == "token-overlap"
        assert 0.0 < score < 1.0
        assert classified_by_for_match_path(match_path) == CLASSIFIED_AUTO_TOKEN_OVERLAP

    async def test_path_a_wins_over_path_b(self, store, monkeypatch):
        monkeypatch.setenv("KERNOS_FRICTION_TOKEN_OVERLAP_THRESHOLD", "0.1")
        # Pattern A has Path A match via signal_type.
        pa = await store.create_pattern(
            instance_id="inst-A",
            description="Distinctive alpha pattern",
            signal_type_keys=["MERGED_MESSAGES_DROPPED"],
        )
        # Pattern B has Path B match via token overlap with the message.
        pb = await store.create_pattern(
            instance_id="inst-A",
            description="merged dropped messages content units",
        )
        candidates = await store.list_patterns("inst-A")
        result = classify_signal(
            signal_type="MERGED_MESSAGES_DROPPED",
            signal_description="merged dropped messages content units only one",
            candidates=candidates,
        )
        assert result is not None
        pattern, score, match_path = result
        assert pattern.pattern_id == pa.pattern_id
        assert match_path == "signal-type"

    async def test_low_confidence_returns_no_match(self, store, monkeypatch):
        monkeypatch.setenv("KERNOS_FRICTION_TOKEN_OVERLAP_THRESHOLD", "0.6")
        await store.create_pattern(
            instance_id="inst-A",
            description="completely unrelated content domain alpha",
        )
        candidates = await store.list_patterns("inst-A")
        result = classify_signal(
            signal_type="UNKNOWN_TYPE",
            signal_description="bravo charlie delta echo foxtrot golf",
            candidates=candidates,
        )
        assert result is None

    async def test_token_overlap_threshold_env_var_tunable(
        self, store, monkeypatch,
    ):
        # At 0.1 the match passes
        monkeypatch.setenv("KERNOS_FRICTION_TOKEN_OVERLAP_THRESHOLD", "0.1")
        await store.create_pattern(
            instance_id="inst-A",
            description="some moderately matching content tokens here",
        )
        candidates = await store.list_patterns("inst-A")
        result_low = classify_signal(
            signal_type="UNKNOWN_TYPE",
            signal_description="content tokens here moderate matching",
            candidates=candidates,
        )
        assert result_low is not None
        # At 0.95 the same signal doesn't match
        monkeypatch.setenv("KERNOS_FRICTION_TOKEN_OVERLAP_THRESHOLD", "0.95")
        result_high = classify_signal(
            signal_type="UNKNOWN_TYPE",
            signal_description="content tokens here moderate matching",
            candidates=candidates,
        )
        assert result_high is None

    async def test_idempotent_on_report_path_unique_index(self, store):
        p = await store.create_pattern(
            instance_id="inst-A", description="Idempotency test alpha")
        await store.record_occurrence(
            instance_id="inst-A",
            pattern_id=p.pattern_id,
            observed_at=_now(),
            report_path="reports/once.md",
        )
        # Re-record same report_path same pattern — should be idempotent
        # (silent skip).
        await store.record_occurrence(
            instance_id="inst-A",
            pattern_id=p.pattern_id,
            observed_at=_now(),
            report_path="reports/once.md",
        )
        reloaded = await store.get_pattern("inst-A", p.pattern_id)
        assert reloaded.occurrence_count == 1

    async def test_path_b_short_description_returns_zero(self, store):
        """Architect call Q3: short-description guard returns 0.0."""
        # Pattern with only 2 cleaned tokens.
        await store.create_pattern(
            instance_id="inst-A",
            description="short text",  # only "short", "text" pass clean
        )
        candidates = await store.list_patterns("inst-A")
        result = classify_signal(
            signal_type="UNKNOWN_TYPE",
            signal_description="short text indeed many tokens here alpha",
            candidates=candidates,
        )
        # Pattern's cleaned token count is below the floor; Path B returns 0.
        assert result is None

    async def test_path_b_stopwords_dropped(self, store, monkeypatch):
        monkeypatch.setenv("KERNOS_FRICTION_TOKEN_OVERLAP_THRESHOLD", "0.6")
        # Description with stopwords + meaningful tokens.
        p = await store.create_pattern(
            instance_id="inst-A",
            description="the tool was used for the request alpha",
        )
        candidates = await store.list_patterns("inst-A")
        # Signal description shares only stopwords with pattern.
        result = classify_signal(
            signal_type="UNKNOWN_TYPE",
            signal_description="the canvas was used for the page beta",
            candidates=candidates,
        )
        # Stopwords "the", "was", "for", "used" are dropped (the latter
        # two are len>=3 but not stopwords — used IS not in stopwords).
        # Cleaned overlap is {used, for}∩{used, for} — but "for" is in
        # stopwords. So cleaned overlap is {used}. Below 0.6.
        assert result is None


# ---------------------------------------------------------------------------
# Reactivation
# ---------------------------------------------------------------------------


class TestReactivation:
    async def test_record_occurrence_rejects_on_resolved(self, store):
        p = await store.create_pattern(
            instance_id="inst-A", description="Reject test alpha")
        await store.transition_lifecycle(
            "inst-A", p.pattern_id, LIFECYCLE_RESOLVED,
        )
        with pytest.raises(ValueError):
            await store.record_occurrence(
                instance_id="inst-A",
                pattern_id=p.pattern_id,
                observed_at=_now(),
                report_path="reports/rej.md",
            )

    async def test_record_recurrence_emits_event_without_incrementing(
        self, store,
    ):
        p = await store.create_pattern(
            instance_id="inst-A", description="Recur emit test alpha")
        await store.transition_lifecycle(
            "inst-A", p.pattern_id, LIFECYCLE_RESOLVED,
        )

        emitted: list[tuple[str, dict]] = []

        async def _emit(event_type: str, payload: dict) -> None:
            emitted.append((event_type, payload))

        reactivated = await store.record_recurrence(
            instance_id="inst-A",
            pattern_id=p.pattern_id,
            observed_at=_now(),
            report_path="reports/recur.md",
            emit_event=_emit,
        )
        assert reactivated is False
        assert any(e[0] == "friction.pattern_recurrence" for e in emitted)

        reloaded = await store.get_pattern("inst-A", p.pattern_id)
        assert reloaded.occurrence_count == 0  # not incremented

    async def test_threshold_recurrences_trigger_reactivation(self, store):
        p = await store.create_pattern(
            instance_id="inst-A", description="Threshold reactivation test alpha")
        await store.transition_lifecycle(
            "inst-A", p.pattern_id, LIFECYCLE_RESOLVED,
        )
        emitted: list[tuple[str, dict]] = []

        async def _emit(event_type: str, payload: dict) -> None:
            emitted.append((event_type, payload))

        for i in range(3):
            triggered = await store.record_recurrence(
                instance_id="inst-A",
                pattern_id=p.pattern_id,
                observed_at=_now(),
                report_path=f"reports/recur_{i}.md",
                emit_event=_emit,
            )
        assert triggered is True
        assert any(e[0] == "friction.pattern_reactivated" for e in emitted)
        reloaded = await store.get_pattern("inst-A", p.pattern_id)
        assert reloaded.lifecycle_state == LIFECYCLE_REACTIVATED

    async def test_below_threshold_recurrences_do_not_reactivate(self, store):
        p = await store.create_pattern(
            instance_id="inst-A", description="Below threshold test alpha")
        await store.transition_lifecycle(
            "inst-A", p.pattern_id, LIFECYCLE_RESOLVED,
        )
        for i in range(2):  # below default threshold of 3
            await store.record_recurrence(
                instance_id="inst-A",
                pattern_id=p.pattern_id,
                observed_at=_now(),
                report_path=f"reports/under_{i}.md",
            )
        reloaded = await store.get_pattern("inst-A", p.pattern_id)
        assert reloaded.lifecycle_state == LIFECYCLE_RESOLVED

    async def test_backfill_recurrences_excluded_from_reactivation(
        self, store,
    ):
        p = await store.create_pattern(
            instance_id="inst-A", description="Backfill excluded test alpha")
        await store.transition_lifecycle(
            "inst-A", p.pattern_id, LIFECYCLE_RESOLVED,
        )
        # 5 backfill recurrences — should NOT reactivate.
        for i in range(5):
            await store.record_recurrence(
                instance_id="inst-A",
                pattern_id=p.pattern_id,
                observed_at=_now(),
                report_path=f"reports/bf_{i}.md",
                classified_by=CLASSIFIED_BACKFILL,
            )
        reloaded = await store.get_pattern("inst-A", p.pattern_id)
        assert reloaded.lifecycle_state == LIFECYCLE_RESOLVED

    async def test_reactivated_pattern_resumes_counting(self, store):
        p = await store.create_pattern(
            instance_id="inst-A", description="Reactivated resume test alpha")
        await store.transition_lifecycle(
            "inst-A", p.pattern_id, LIFECYCLE_RESOLVED,
        )
        for i in range(3):
            await store.record_recurrence(
                instance_id="inst-A",
                pattern_id=p.pattern_id,
                observed_at=_now(),
                report_path=f"reports/r_{i}.md",
            )
        # Now reactivated; record_occurrence should work.
        await store.record_occurrence(
            instance_id="inst-A",
            pattern_id=p.pattern_id,
            observed_at=_now(),
            report_path="reports/after.md",
        )
        reloaded = await store.get_pattern("inst-A", p.pattern_id)
        assert reloaded.occurrence_count == 1
        # record_recurrence rejects on reactivated.
        with pytest.raises(ValueError):
            await store.record_recurrence(
                instance_id="inst-A",
                pattern_id=p.pattern_id,
                observed_at=_now(),
                report_path="reports/after_recur.md",
            )

    async def test_record_recurrence_rejects_on_active_or_archived(
        self, store,
    ):
        p = await store.create_pattern(
            instance_id="inst-A", description="Reject active or archived alpha")
        # active
        with pytest.raises(ValueError):
            await store.record_recurrence(
                instance_id="inst-A",
                pattern_id=p.pattern_id,
                observed_at=_now(),
                report_path="reports/a.md",
            )
        await store.transition_lifecycle(
            "inst-A", p.pattern_id, LIFECYCLE_ARCHIVED,
        )
        with pytest.raises(PatternArchived):
            await store.record_occurrence(
                instance_id="inst-A",
                pattern_id=p.pattern_id,
                observed_at=_now(),
                report_path="reports/b.md",
            )
        with pytest.raises(PatternArchived):
            await store.record_recurrence(
                instance_id="inst-A",
                pattern_id=p.pattern_id,
                observed_at=_now(),
                report_path="reports/c.md",
            )

    async def test_backfill_counts_in_normal_frequency_query(self, store):
        """Round-2 Finding 8: backfill rows count in normal queries;
        excluded only from reactivation threshold."""
        p = await store.create_pattern(
            instance_id="inst-A", description="Backfill freq test alpha")
        for i in range(5):
            await store.record_occurrence(
                instance_id="inst-A",
                pattern_id=p.pattern_id,
                observed_at=_now(),
                report_path=f"reports/bf_{i}.md",
                classified_by=CLASSIFIED_BACKFILL,
            )
        for i in range(3):
            await store.record_occurrence(
                instance_id="inst-A",
                pattern_id=p.pattern_id,
                observed_at=_now(),
                report_path=f"reports/auto_{i}.md",
                classified_by=CLASSIFIED_AUTO_SIGNAL_TYPE,
            )
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        past = _ago(3600)
        # default exclude_backfill=False
        n_all = await store.query_frequency(
            "inst-A", p.pattern_id, window_start=past, window_end=future,
        )
        assert n_all == 8
        # exclude_backfill=True
        n_non_backfill = await store.query_frequency(
            "inst-A", p.pattern_id, window_start=past, window_end=future,
            exclude_backfill=True,
        )
        assert n_non_backfill == 3


# ---------------------------------------------------------------------------
# Member isolation
# ---------------------------------------------------------------------------


class TestMemberIsolation:
    async def test_patterns_scoped_per_instance(self, store):
        await store.create_pattern(
            instance_id="inst-A", description="Same slug alpha")
        await store.create_pattern(
            instance_id="inst-B", description="Same slug alpha")
        a_list = await store.list_patterns("inst-A")
        b_list = await store.list_patterns("inst-B")
        assert len(a_list) == 1
        assert len(b_list) == 1
        # Same slug across instances; both independent rows.
        assert a_list[0].pattern_id == b_list[0].pattern_id == "same-slug-alpha"

    async def test_occurrences_scoped_per_instance(self, store):
        pa = await store.create_pattern(
            instance_id="inst-A", description="Occ scoped alpha")
        pb = await store.create_pattern(
            instance_id="inst-B", description="Occ scoped alpha")
        await store.record_occurrence(
            instance_id="inst-A",
            pattern_id=pa.pattern_id,
            observed_at=_now(),
            report_path="reports/oa.md",
        )
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        past = _ago(3600)
        n_a = await store.query_frequency(
            "inst-A", pa.pattern_id, window_start=past, window_end=future)
        n_b = await store.query_frequency(
            "inst-B", pb.pattern_id, window_start=past, window_end=future)
        assert n_a == 1
        assert n_b == 0


# ---------------------------------------------------------------------------
# Frontmatter parser
# ---------------------------------------------------------------------------


class TestFrontmatterParser:
    def test_parse_addresses_field_list_form(self, tmp_path):
        spec = tmp_path / "spec.md"
        spec.write_text(textwrap.dedent("""\
            ---
            name: my-spec
            addresses_friction_patterns:
              - tool-request-for-surfaced-tool
              - merged-messages-dropped
            ---
            # Body
        """), encoding="utf-8")
        refs = parse_spec_pattern_refs(spec)
        assert refs == [
            "tool-request-for-surfaced-tool",
            "merged-messages-dropped",
        ]

    def test_parse_addresses_field_inline_form(self, tmp_path):
        spec = tmp_path / "spec.md"
        spec.write_text(textwrap.dedent("""\
            ---
            addresses_friction_patterns: [pattern-a, pattern-b]
            ---
            # Body
        """), encoding="utf-8")
        refs = parse_spec_pattern_refs(spec)
        assert refs == ["pattern-a", "pattern-b"]

    def test_parse_no_frontmatter_returns_empty(self, tmp_path):
        spec = tmp_path / "spec.md"
        spec.write_text("# No frontmatter here\n\nBody.", encoding="utf-8")
        assert parse_spec_pattern_refs(spec) == []

    def test_parse_no_field_returns_empty(self, tmp_path):
        spec = tmp_path / "spec.md"
        spec.write_text(textwrap.dedent("""\
            ---
            name: my-spec
            other_field: foo
            ---
            # Body
        """), encoding="utf-8")
        assert parse_spec_pattern_refs(spec) == []

    def test_parse_unclosed_frontmatter_returns_empty(self, tmp_path):
        spec = tmp_path / "spec.md"
        spec.write_text("---\nname: x\n# unterminated", encoding="utf-8")
        assert parse_spec_pattern_refs(spec) == []

    def test_parse_missing_file_returns_empty(self, tmp_path):
        assert parse_spec_pattern_refs(tmp_path / "nonexistent.md") == []


# ---------------------------------------------------------------------------
# Concurrent transition + occurrence race (architect call Q2)
# ---------------------------------------------------------------------------


class TestConcurrentTransitionAndOccurrenceRace:
    async def test_concurrent_transition_and_occurrence_race(self, store):
        """Two awaitables race on the same pattern: one transitions
        active → resolved; the other records_occurrence. With BEGIN
        IMMEDIATE serializing them through the store's write lock,
        exactly one outcome is consistent (the second observes the
        post-transition state and dispatches/rejects correctly per
        Decision 6's table)."""
        p = await store.create_pattern(
            instance_id="inst-A", description="Race test alpha")
        # Launch both coroutines.
        results: dict[str, object] = {}

        async def _transition():
            try:
                pp = await store.transition_lifecycle(
                    "inst-A", p.pattern_id, LIFECYCLE_RESOLVED,
                )
                results["transition"] = pp.lifecycle_state
            except Exception as exc:
                results["transition_err"] = exc

        async def _occurrence():
            try:
                await store.record_occurrence(
                    instance_id="inst-A",
                    pattern_id=p.pattern_id,
                    observed_at=_now(),
                    report_path="reports/race.md",
                )
                results["occurrence"] = "ok"
            except ValueError as exc:
                # Pattern resolved-state rejection (caller should
                # retry via record_recurrence).
                results["occurrence_err"] = exc
            except Exception as exc:
                results["occurrence_err"] = exc

        await asyncio.gather(_transition(), _occurrence())
        reloaded = await store.get_pattern("inst-A", p.pattern_id)
        # Transition always succeeds (it's a simple UPDATE on the
        # pattern row, doesn't depend on FK to occurrences).
        assert reloaded.lifecycle_state == LIFECYCLE_RESOLVED
        # Occurrence either landed before transition (counter==1) or
        # rejected after transition (counter==0). Either outcome is
        # consistent with serializable ordering.
        if "occurrence" in results:
            assert reloaded.occurrence_count == 1
        else:
            assert "occurrence_err" in results
            assert reloaded.occurrence_count == 0

    async def test_store_contention_on_exhausted_retries(
        self, store, monkeypatch,
    ):
        """Force retry budget to 0 and synthesize a BUSY scenario by
        holding a BEGIN IMMEDIATE transaction on another connection.

        Implementation note: BEGIN IMMEDIATE on a fresh connection
        against the same DB while the store's connection has its
        own BEGIN IMMEDIATE active raises SQLITE_BUSY. We approximate
        the contention by opening a second connection, taking the
        write lock, then asking the store to do a mutating op with
        retry_limit=0.
        """
        monkeypatch.setenv("KERNOS_FRICTION_TXN_RETRY_LIMIT", "0")
        # Open a second connection and take BEGIN IMMEDIATE on it.
        # busy_timeout=0 on this conn so we don't accidentally wait.
        other = await aiosqlite.connect(
            str(store._db_path), isolation_level=None,
        )
        try:
            await other.execute("PRAGMA busy_timeout=0")
            await other.execute("BEGIN IMMEDIATE")
            # Force the store's busy_timeout to 0 too so it doesn't
            # block waiting; that lets us see the immediate
            # StoreContention.
            await store.db.execute("PRAGMA busy_timeout=0")
            with pytest.raises(StoreContention):
                await store.create_pattern(
                    instance_id="inst-A",
                    description="Contention test alpha beta",
                )
        finally:
            await other.execute("ROLLBACK")
            await other.close()
            # Restore busy_timeout on store conn for any subsequent
            # tests sharing the fixture (we're function-scoped so
            # this is paranoia).
            await store.db.execute("PRAGMA busy_timeout=5000")


# ---------------------------------------------------------------------------
# Slugify helper
# ---------------------------------------------------------------------------


class TestSlugify:
    def test_basic(self):
        assert slugify("Integration Timeout Under Load") == "integration-timeout-under-load"

    def test_punctuation_collapsed(self):
        assert slugify("Foo! Bar?  Baz...") == "foo-bar-baz"

    def test_unicode_dropped(self):
        # ASCII-only per spec; non-ASCII drops to hyphens then collapse.
        assert slugify("café") == "caf"

    def test_empty_returns_empty(self):
        assert slugify("") == ""
        assert slugify("!!!") == ""
