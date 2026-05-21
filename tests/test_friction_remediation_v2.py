"""Tests for FRICTION-REMEDIATION-V2 — declarative auto-remediation
policy on FrictionPattern records.

Spec context: founder accepted full V2 batch 2026-05-20 after live
failure where the standalone watchdog stopped firing and the bot
sat in 'connected but deaf' state for 2 hours. V2 makes the
gateway-health observer's signal a load-bearing trigger for
remediation via the catalog, removing the parallel watchdog
strike counter eventually (V3).

Pins:
* FrictionPattern dataclass + DDL migration carry the three new
  fields (remediation_action, remediation_threshold_count,
  remediation_threshold_window_sec)
* create_pattern accepts + persists them
* record_occurrence fires the handler when threshold + window
  conditions are met
* Cool-off via sentinel file prevents loop-firing (critical:
  restart_kernos handler is os.execv and the underlying issue
  might not be fixed by restart)
* Sentinel survives "restart" (we just simulate it by writing
  then re-creating the store)
* No-op when pattern has no remediation policy (backward-compat)
* No-op when no handler is registered for the action name
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kernos.kernel.friction_patterns import (
    FrictionPatternStore,
)


@pytest.fixture
async def store(tmp_path):
    s = FrictionPatternStore()
    await s.start(str(tmp_path))
    yield s
    await s.stop()


# ===========================================================================
# Field + DDL pins
# ===========================================================================


class TestSchemaCarriesNewFields:
    async def test_create_pattern_accepts_remediation_args(self, store):
        p = await store.create_pattern(
            instance_id="i",
            description="x" * 20,
            seed_slug="test-pat",
            remediation_action="restart_kernos",
            remediation_threshold_count=5,
            remediation_threshold_window_sec=600,
        )
        assert p.remediation_action == "restart_kernos"
        assert p.remediation_threshold_count == 5
        assert p.remediation_threshold_window_sec == 600

    async def test_round_trip_via_list_patterns(self, store):
        await store.create_pattern(
            instance_id="i", description="x" * 20,
            seed_slug="test-pat",
            remediation_action="restart_kernos",
            remediation_threshold_count=3,
            remediation_threshold_window_sec=120,
        )
        patterns = await store.list_patterns("i")
        assert len(patterns) == 1
        p = patterns[0]
        assert p.remediation_action == "restart_kernos"
        assert p.remediation_threshold_count == 3
        assert p.remediation_threshold_window_sec == 120

    async def test_default_no_remediation_policy(self, store):
        """Backward-compat: patterns created without the new params
        have empty/zero defaults (= no remediation)."""
        p = await store.create_pattern(
            instance_id="i", description="x" * 20, seed_slug="legacy",
        )
        assert p.remediation_action == ""
        assert p.remediation_threshold_count == 0
        assert p.remediation_threshold_window_sec == 0


# ===========================================================================
# Trigger logic — record_occurrence fires handler when threshold crossed
# ===========================================================================


class TestRemediationTriggering:
    async def test_no_handler_no_remediation_logs_warning(
        self, store, caplog,
    ):
        """Pattern declares remediation but no handler is registered:
        logs warning, no exception, normal occurrence recorded."""
        await store.create_pattern(
            instance_id="i", description="x" * 20, seed_slug="test",
            remediation_action="unregistered_action",
            remediation_threshold_count=1,
            remediation_threshold_window_sec=600,
        )
        import logging
        with caplog.at_level(logging.WARNING):
            await store.record_occurrence(
                instance_id="i", pattern_id="test",
                observed_at=datetime.now(timezone.utc).isoformat(),
                report_path="/tmp/r1.md",
            )
        assert any(
            "NO_HANDLER" in r.message
            for r in caplog.records
        )

    async def test_handler_fires_when_threshold_crossed(self, store):
        """Threshold=3 in 600s window. Record 3 occurrences, expect
        handler called exactly once on the 3rd."""
        fires: list[dict] = []

        async def handler(*, instance_id, pattern_id, occurrence_count):
            fires.append({
                "instance_id": instance_id,
                "pattern_id": pattern_id,
                "count": occurrence_count,
            })

        store.register_remediation_handler("test_action", handler)
        await store.create_pattern(
            instance_id="i", description="x" * 20, seed_slug="test",
            remediation_action="test_action",
            remediation_threshold_count=3,
            remediation_threshold_window_sec=600,
        )
        now = datetime.now(timezone.utc)
        for i in range(3):
            await store.record_occurrence(
                instance_id="i", pattern_id="test",
                observed_at=now.isoformat(),
                report_path=f"/tmp/r{i}.md",
            )
        assert len(fires) == 1
        assert fires[0]["pattern_id"] == "test"
        assert fires[0]["count"] == 3

    async def test_handler_does_not_fire_below_threshold(self, store):
        fires = []

        async def handler(**kw):
            fires.append(kw)

        store.register_remediation_handler("test", handler)
        await store.create_pattern(
            instance_id="i", description="x" * 20, seed_slug="t",
            remediation_action="test",
            remediation_threshold_count=5,
            remediation_threshold_window_sec=600,
        )
        now = datetime.now(timezone.utc)
        for i in range(4):  # below threshold
            await store.record_occurrence(
                instance_id="i", pattern_id="t",
                observed_at=now.isoformat(),
                report_path=f"/tmp/r{i}.md",
            )
        assert fires == []

    async def test_no_policy_no_fire(self, store):
        """Backward-compat pin: patterns without remediation_action
        are not affected by V2."""
        fires = []

        async def handler(**kw):
            fires.append(kw)

        store.register_remediation_handler("anything", handler)
        await store.create_pattern(
            instance_id="i", description="x" * 20, seed_slug="legacy",
        )
        for i in range(20):
            await store.record_occurrence(
                instance_id="i", pattern_id="legacy",
                observed_at=datetime.now(timezone.utc).isoformat(),
                report_path=f"/tmp/r{i}.md",
            )
        assert fires == []


# ===========================================================================
# Cool-off via sentinel file — prevents loop-restart
# ===========================================================================


class TestCoolOffSentinel:
    async def test_second_fire_within_window_skipped(self, store, caplog):
        """The critical safety pin: after a fire, the handler does
        NOT re-fire within window_sec — even if occurrence count is
        still way above threshold. This is what prevents loop-restart
        when restart_kernos is the action and the underlying issue
        isn't fixed by restart."""
        fires = []

        async def handler(**kw):
            fires.append(kw)

        store.register_remediation_handler("test", handler)
        await store.create_pattern(
            instance_id="i", description="x" * 20, seed_slug="t",
            remediation_action="test",
            remediation_threshold_count=2,
            remediation_threshold_window_sec=600,
        )
        now = datetime.now(timezone.utc)
        # Fire 1: hit threshold (2 occurrences), handler fires
        await store.record_occurrence(
            instance_id="i", pattern_id="t",
            observed_at=now.isoformat(), report_path="/tmp/r1.md",
        )
        await store.record_occurrence(
            instance_id="i", pattern_id="t",
            observed_at=now.isoformat(), report_path="/tmp/r2.md",
        )
        assert len(fires) == 1

        # More occurrences within the same window — handler must NOT
        # re-fire because the sentinel says we just fired.
        import logging
        with caplog.at_level(logging.INFO):
            for i in range(3, 10):
                await store.record_occurrence(
                    instance_id="i", pattern_id="t",
                    observed_at=now.isoformat(),
                    report_path=f"/tmp/r{i}.md",
                )
        assert len(fires) == 1, (
            "handler must not re-fire while within cool-off window"
        )
        assert any(
            "SKIPPED_COOL_OFF" in r.message
            for r in caplog.records
        )

    async def test_sentinel_persists_to_disk(self, store, tmp_path):
        """The sentinel file must land on disk (so it survives bot
        restart). Without disk-persistence, restart_kernos would
        loop-restart immediately on next startup."""
        fires = []

        async def handler(**kw):
            fires.append(kw)

        store.register_remediation_handler("test", handler)
        await store.create_pattern(
            instance_id="i", description="x" * 20, seed_slug="t",
            remediation_action="test",
            remediation_threshold_count=1,
            remediation_threshold_window_sec=600,
        )
        await store.record_occurrence(
            instance_id="i", pattern_id="t",
            observed_at=datetime.now(timezone.utc).isoformat(),
            report_path="/tmp/r.md",
        )
        assert len(fires) == 1
        sentinel_path = (
            tmp_path / "diagnostics" / "friction" / "remediation"
            / "i__t.last_fired"
        )
        assert sentinel_path.exists(), (
            f"sentinel file must land at {sentinel_path}"
        )
        # File contains an ISO-parseable timestamp
        ts_str = sentinel_path.read_text(encoding="utf-8").strip()
        ts = datetime.fromisoformat(ts_str)
        assert (
            datetime.now(timezone.utc) - ts
        ).total_seconds() < 10

    async def test_sentinel_simulated_restart_still_dedupes(
        self, store, tmp_path,
    ):
        """End-to-end: fire once, simulate a 'restart' by creating a
        fresh store pointed at the same data_dir. The cool-off
        sentinel should be read from disk and the new store's
        handler should NOT re-fire."""
        fires_first = []

        async def handler1(**kw):
            fires_first.append(kw)

        store.register_remediation_handler("test", handler1)
        await store.create_pattern(
            instance_id="i", description="x" * 20, seed_slug="t",
            remediation_action="test",
            remediation_threshold_count=1,
            remediation_threshold_window_sec=600,
        )
        await store.record_occurrence(
            instance_id="i", pattern_id="t",
            observed_at=datetime.now(timezone.utc).isoformat(),
            report_path="/tmp/r1.md",
        )
        assert len(fires_first) == 1

        # Simulate a restart: stop the first store, create a fresh one
        await store.stop()
        fires_second = []

        async def handler2(**kw):
            fires_second.append(kw)

        store2 = FrictionPatternStore()
        await store2.start(str(tmp_path))
        store2.register_remediation_handler("test", handler2)
        # Record another occurrence; sentinel still in window → no fire
        await store2.record_occurrence(
            instance_id="i", pattern_id="t",
            observed_at=datetime.now(timezone.utc).isoformat(),
            report_path="/tmp/r2.md",
        )
        await store2.stop()
        assert fires_second == [], (
            "post-restart handler must respect the on-disk sentinel; "
            "without this, restart_kernos would loop-restart"
        )

    async def test_cool_off_expires_allows_refire(self, store, tmp_path):
        """After window_sec elapses, the sentinel is treated as stale
        and the handler CAN fire again. We simulate elapsed time by
        backdating the sentinel file."""
        fires = []

        async def handler(**kw):
            fires.append(kw)

        store.register_remediation_handler("test", handler)
        await store.create_pattern(
            instance_id="i", description="x" * 20, seed_slug="t",
            remediation_action="test",
            remediation_threshold_count=1,
            remediation_threshold_window_sec=60,  # short window
        )
        await store.record_occurrence(
            instance_id="i", pattern_id="t",
            observed_at=datetime.now(timezone.utc).isoformat(),
            report_path="/tmp/r1.md",
        )
        assert len(fires) == 1

        # Backdate the sentinel to 90s ago — beyond the 60s window
        sentinel = (
            tmp_path / "diagnostics" / "friction" / "remediation"
            / "i__t.last_fired"
        )
        backdated = datetime.now(timezone.utc) - timedelta(seconds=90)
        sentinel.write_text(backdated.isoformat(), encoding="utf-8")

        # Next occurrence should fire again (cool-off elapsed)
        await store.record_occurrence(
            instance_id="i", pattern_id="t",
            observed_at=datetime.now(timezone.utc).isoformat(),
            report_path="/tmp/r2.md",
        )
        assert len(fires) == 2


# ===========================================================================
# Seed integration — discord-heartbeat-blocked wires restart_kernos
# ===========================================================================


class TestEscalationGuard:
    """2026-05-20 live-failure pin: the bot was restart-cycling
    every 10 min for hours because heartbeat-NaN persisted across
    restart (the metric was effectively false-positive — bot was
    functionally alive but metric kept reading NaN). V2 sentinel
    cool-off correctly prevented WITHIN-window loops, but CROSS-
    window the cycle continued indefinitely. Escalation guard
    caps total fires per longer rolling window (1 hour default,
    3 fires max). When the cap trips, V2 stops firing and logs
    loud — operator decides next move."""

    async def test_under_max_fires_remediation_fires(
        self, store, monkeypatch,
    ):
        monkeypatch.setenv(
            "KERNOS_FRICTION_REMEDIATION_ESCALATION_MAX_FIRES", "5",
        )
        fires = []

        async def handler(**kw):
            fires.append(kw)

        store.register_remediation_handler("test", handler)
        await store.create_pattern(
            instance_id="i", description="x" * 20, seed_slug="t",
            remediation_action="test",
            remediation_threshold_count=1,
            remediation_threshold_window_sec=1,  # short so cool-off ages out
        )
        for i in range(3):  # 3 fires
            await store.record_occurrence(
                instance_id="i", pattern_id="t",
                observed_at=datetime.now(timezone.utc).isoformat(),
                report_path=f"/tmp/r{i}.md",
            )
            await asyncio.sleep(1.1)  # let cool-off expire
        assert len(fires) == 3

    async def test_over_max_fires_remediation_stops(
        self, store, monkeypatch,
    ):
        """Critical: after the escalation cap is reached, further
        record_occurrence calls do NOT trigger the handler. Even
        if cool-off has expired. The cycle is broken."""
        monkeypatch.setenv(
            "KERNOS_FRICTION_REMEDIATION_ESCALATION_MAX_FIRES", "3",
        )
        fires = []

        async def handler(**kw):
            fires.append(kw)

        store.register_remediation_handler("test", handler)
        await store.create_pattern(
            instance_id="i", description="x" * 20, seed_slug="t",
            remediation_action="test",
            remediation_threshold_count=1,
            remediation_threshold_window_sec=1,
        )
        # Fire 3 times — should all succeed
        for i in range(3):
            await store.record_occurrence(
                instance_id="i", pattern_id="t",
                observed_at=datetime.now(timezone.utc).isoformat(),
                report_path=f"/tmp/r{i}.md",
            )
            await asyncio.sleep(1.1)
        assert len(fires) == 3

        # Fire 5 more — escalation guard should block ALL of them
        for i in range(3, 8):
            await store.record_occurrence(
                instance_id="i", pattern_id="t",
                observed_at=datetime.now(timezone.utc).isoformat(),
                report_path=f"/tmp/r{i}.md",
            )
            await asyncio.sleep(1.1)
        assert len(fires) == 3, (
            f"escalation guard must block fires beyond max; "
            f"got {len(fires)} fires when max is 3"
        )

    async def test_history_file_persists_across_store_restart(
        self, tmp_path, monkeypatch,
    ):
        """The escalation guard reads from a file, so escalation
        decisions survive bot restart (just like the cool-off
        sentinel). Without this, every restart resets the counter
        and the cycle would continue."""
        from kernos.kernel.friction_patterns import FrictionPatternStore
        monkeypatch.setenv(
            "KERNOS_FRICTION_REMEDIATION_ESCALATION_MAX_FIRES", "3",
        )
        # Pre-fire and stop
        store = FrictionPatternStore()
        await store.start(str(tmp_path))
        fires_first = []

        async def h1(**kw):
            fires_first.append(kw)

        store.register_remediation_handler("test", h1)
        await store.create_pattern(
            instance_id="i", description="x" * 20, seed_slug="t",
            remediation_action="test",
            remediation_threshold_count=1,
            remediation_threshold_window_sec=1,
        )
        for i in range(3):
            await store.record_occurrence(
                instance_id="i", pattern_id="t",
                observed_at=datetime.now(timezone.utc).isoformat(),
                report_path=f"/tmp/r{i}.md",
            )
            await asyncio.sleep(1.1)
        assert len(fires_first) == 3
        await store.stop()

        # Fresh store, same data_dir — escalation history persists
        store2 = FrictionPatternStore()
        await store2.start(str(tmp_path))
        fires_second = []

        async def h2(**kw):
            fires_second.append(kw)

        store2.register_remediation_handler("test", h2)
        # One more occurrence — should be blocked by escalation
        await store2.record_occurrence(
            instance_id="i", pattern_id="t",
            observed_at=datetime.now(timezone.utc).isoformat(),
            report_path="/tmp/r_post_restart.md",
        )
        await store2.stop()
        assert fires_second == [], (
            "escalation history must persist across restart; "
            "without this, restart resets the counter and the cycle continues"
        )


class TestSeedRemediationPolicy:
    def test_discord_heartbeat_blocked_seed_declares_restart(self):
        from kernos.setup.seed_friction_patterns import _STARTER_PATTERNS
        hb = next(
            p for p in _STARTER_PATTERNS
            if p.pattern_id == "discord-heartbeat-blocked"
        )
        assert hb.remediation_action == "restart_kernos"
        assert hb.remediation_threshold_count == 5
        assert hb.remediation_threshold_window_sec == 600

    def test_other_seeds_have_no_remediation_policy(self):
        """Conservative default: only discord-heartbeat-blocked has
        a remediation policy in V2. The other patterns are
        observation-only (V2 will add more as we learn which
        warrant auto-action)."""
        from kernos.setup.seed_friction_patterns import _STARTER_PATTERNS
        for p in _STARTER_PATTERNS:
            if p.pattern_id == "discord-heartbeat-blocked":
                continue
            assert p.remediation_action == "", (
                f"{p.pattern_id} unexpectedly has remediation_action "
                f"{p.remediation_action!r}; V2 only seeds policy on "
                f"discord-heartbeat-blocked"
            )

    async def test_seed_upgrade_path_applies_policy_to_existing_pattern(
        self, tmp_path,
    ):
        """The critical V2 deployment scenario: bot was seeded
        before V2 ships (so existing pattern has no remediation
        policy in DB). New code runs seed → existing pattern gets
        policy updated from seed without needing full re-seed.
        Without this upgrade path, live bots would never get the
        remediation policy applied."""
        from kernos.kernel.friction_patterns import FrictionPatternStore
        from kernos.setup.seed_friction_patterns import (
            _STARTER_PATTERNS,
            _maybe_update_remediation_policy,
        )

        store = FrictionPatternStore()
        await store.start(str(tmp_path))
        # Pre-seed the pattern WITHOUT remediation (legacy shape)
        await store.create_pattern(
            instance_id="i",
            description="legacy description",
            signal_type_keys=["DISCORD_HEARTBEAT_BLOCKED"],
            display_name="legacy",
            seed_slug="discord-heartbeat-blocked",
        )
        before = (await store.list_patterns("i"))[0]
        assert before.remediation_action == ""

        # Run the upgrade
        hb_seed = next(
            p for p in _STARTER_PATTERNS
            if p.pattern_id == "discord-heartbeat-blocked"
        )
        await _maybe_update_remediation_policy(
            pattern_store=store, instance_id="i", seed=hb_seed,
        )

        # Now the pattern carries the seed's remediation policy
        after = (await store.list_patterns("i"))[0]
        assert after.remediation_action == "restart_kernos"
        assert after.remediation_threshold_count == 5
        assert after.remediation_threshold_window_sec == 600
        await store.stop()


# ===========================================================================
# Shared try_claim_remediation_fire helper (Codex audit 2026-05-20 round 2)
# ===========================================================================


class TestTryClaimRemediationFire:
    """Behavior tests for the shared claim helper. V1.5 and V2 both
    call this; tests live here because the helper is in the same
    module."""

    def test_first_call_claims(self, tmp_path):
        from kernos.kernel.friction_patterns import (
            try_claim_remediation_fire,
        )
        claimed, reason = try_claim_remediation_fire(
            data_dir=str(tmp_path), instance_id="i",
            pattern_id="discord-heartbeat-blocked", window_sec=600,
        )
        assert claimed is True
        assert reason == "claimed"

    def test_second_call_blocked_by_cool_off(self, tmp_path):
        from kernos.kernel.friction_patterns import (
            try_claim_remediation_fire,
        )
        try_claim_remediation_fire(
            data_dir=str(tmp_path), instance_id="i",
            pattern_id="discord-heartbeat-blocked", window_sec=600,
        )
        claimed, reason = try_claim_remediation_fire(
            data_dir=str(tmp_path), instance_id="i",
            pattern_id="discord-heartbeat-blocked", window_sec=600,
        )
        assert claimed is False
        assert reason == "cool_off"

    def test_sentinel_write_failure_refuses_claim(self, tmp_path, monkeypatch):
        """Codex audit round 2: if the cool-off sentinel can't be
        persisted, the helper MUST refuse the claim rather than
        return True. Otherwise os.execv proceeds with no cool-off
        and the next process boot loops immediately."""
        from kernos.kernel import friction_patterns as fp_mod

        def boom(*a, **kw):
            raise OSError("simulated write failure")
        monkeypatch.setattr(
            fp_mod.Path, "write_text", boom, raising=False,
        )
        # Use a separate module-level monkeypatch on pathlib.Path so
        # the .write_text on the tmp sentinel hits our boom.
        import pathlib
        monkeypatch.setattr(pathlib.Path, "write_text", boom)

        claimed, reason = fp_mod.try_claim_remediation_fire(
            data_dir=str(tmp_path), instance_id="i",
            pattern_id="discord-heartbeat-blocked", window_sec=600,
        )
        assert claimed is False
        assert reason == "sentinel_write_failed"

    def test_escalation_max_fires_blocks_after_threshold(self, tmp_path):
        """Three fires already in the rolling window → fourth refuses."""
        from datetime import datetime, timezone
        from kernos.kernel.friction_patterns import (
            FrictionPatternStore, try_claim_remediation_fire,
        )
        history = FrictionPatternStore.remediation_history_path(
            str(tmp_path), "i", "discord-heartbeat-blocked",
        )
        history.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        history.write_text(f"{now}\n{now}\n{now}\n", encoding="utf-8")

        claimed, reason = try_claim_remediation_fire(
            data_dir=str(tmp_path), instance_id="i",
            pattern_id="discord-heartbeat-blocked", window_sec=0,
            max_fires_per_window=3, escalation_window_sec=3600,
        )
        assert claimed is False
        assert reason == "escalation_max_fires_reached"

    def test_successful_claim_writes_sentinel_and_history(self, tmp_path):
        from kernos.kernel.friction_patterns import (
            FrictionPatternStore, try_claim_remediation_fire,
        )
        claimed, _ = try_claim_remediation_fire(
            data_dir=str(tmp_path), instance_id="i",
            pattern_id="discord-heartbeat-blocked", window_sec=600,
        )
        assert claimed is True
        sentinel = FrictionPatternStore.remediation_sentinel_path(
            str(tmp_path), "i", "discord-heartbeat-blocked",
        )
        history = FrictionPatternStore.remediation_history_path(
            str(tmp_path), "i", "discord-heartbeat-blocked",
        )
        assert sentinel.exists()
        assert history.exists()
        assert history.read_text(encoding="utf-8").strip() != ""

    def test_per_pid_tmp_path_avoids_collision(self, tmp_path):
        """Codex audit round 2: prior implementation used a fixed
        '.tmp' suffix that two concurrent processes would trample.
        Per-PID suffix means each attempt has its own tmp file."""
        import os
        from kernos.kernel.friction_patterns import (
            FrictionPatternStore, try_claim_remediation_fire,
        )
        sentinel = FrictionPatternStore.remediation_sentinel_path(
            str(tmp_path), "i", "discord-heartbeat-blocked",
        )
        # Pre-create a stale .tmp file (no PID suffix) — old code
        # would have trampled this; new code ignores it.
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        stale = sentinel.with_suffix(".tmp")
        stale.write_text("stale", encoding="utf-8")

        claimed, _ = try_claim_remediation_fire(
            data_dir=str(tmp_path), instance_id="i",
            pattern_id="discord-heartbeat-blocked", window_sec=600,
        )
        assert claimed is True
        # Per-PID tmp was used, the stale fixed-suffix tmp untouched
        assert stale.exists()
        assert stale.read_text(encoding="utf-8") == "stale"
        # Our PID's tmp got renamed to the sentinel, not left behind
        pid_tmp = sentinel.with_suffix(f".tmp.{os.getpid()}")
        assert not pid_tmp.exists()
        assert sentinel.exists()
