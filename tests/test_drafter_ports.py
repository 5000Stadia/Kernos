"""Drafter port structural-absence tests (DRAFTER C1, AC #10 + #11 + #28).

The the design review pin v1→v2 fix: forbidden capabilities are STRUCTURALLY ABSENT
from the port surface — calling them raises :class:`AttributeError`,
never reaches the substrate whitelist. The whitelist is belt-and-
suspenders for any path that escapes the port surface.

Pins:

* ``mark_committed`` is NOT a method on :class:`DrafterDraftPort`.
* Full ``register_workflow`` (with ``dry_run=False``) is NOT a method
  on :class:`DrafterSubstrateToolsPort`. Only ``register_workflow_dry_run``
  is exposed.
* Raw ``emit`` is NOT a method on :class:`DrafterEventPort`. Only
  ``emit_signal`` and ``emit_receipt`` are exposed.
* DrafterEventPort verifies the supplied EventEmitter has
  ``source_module="drafter"``.
"""
from __future__ import annotations

import pytest

from kernos.kernel import event_stream
from kernos.kernel.cohorts._substrate.action_log import ActionLog
from kernos.kernel.cohorts.drafter.ports import (
    DRAFTER_WHITELIST,
    DrafterDraftPort,
    DrafterEventPort,
    DrafterSubstrateToolsPort,
)
from kernos.kernel.drafts.registry import DraftRegistry


# ===========================================================================
# Structural absence — the trust-boundary pins
# ===========================================================================


class TestDrafterDraftPortStructuralAbsence:
    """AC #10 + #28: ``mark_committed`` MUST NOT exist on the port."""

    def test_mark_committed_not_in_class(self):
        assert not hasattr(DrafterDraftPort, "mark_committed"), (
            "AC #10/#28 invariant: mark_committed MUST be structurally "
            "absent from DrafterDraftPort. Production code paths MUST "
            "raise AttributeError before any whitelist check runs."
        )

    def test_mark_committed_not_in_instance_dict(self):
        # Belt-and-suspenders: even if a future refactor monkey-patches
        # the class, the absence must hold at the instance level.
        # Build a minimal port for the check (no DB needed for hasattr).
        class _StubRegistry: ...
        port = DrafterDraftPort.__new__(DrafterDraftPort)
        # Don't __init__; just the class attr lookup.
        assert "mark_committed" not in dir(port)

    def test_only_allowed_methods_present(self):
        """Pin the exact public surface so a future refactor can't add
        a forbidden method without flipping the test."""
        public = {
            name for name in dir(DrafterDraftPort)
            if not name.startswith("_")
        }
        # Allowed public methods + properties.
        expected = {
            "create_draft", "update_draft", "abandon_draft",
            "get_draft", "list_drafts",
            "instance_id",  # property
        }
        assert public == expected, (
            f"DrafterDraftPort public surface drift: "
            f"got {public}, expected {expected}"
        )


class TestDrafterSubstrateToolsPortStructuralAbsence:
    """AC #11 + #28: full ``register_workflow`` MUST NOT exist; only
    ``register_workflow_dry_run`` is exposed."""

    def test_full_register_workflow_not_in_class(self):
        assert not hasattr(DrafterSubstrateToolsPort, "register_workflow"), (
            "AC #11/#28 invariant: full register_workflow MUST be "
            "structurally absent. Activation authority belongs to "
            "user-approved CRB flow."
        )

    def test_register_workflow_dry_run_present(self):
        assert hasattr(DrafterSubstrateToolsPort, "register_workflow_dry_run")

    def test_only_allowed_methods_present(self):
        public = {
            name for name in dir(DrafterSubstrateToolsPort)
            if not name.startswith("_")
        }
        expected = {
            "list_workflows", "list_known_providers", "list_agents",
            "list_drafts", "query_context_brief",
            "register_workflow_dry_run",
            "instance_id",
        }
        assert public == expected, (
            f"DrafterSubstrateToolsPort public surface drift: "
            f"got {public}, expected {expected}"
        )


class TestDrafterEventPortStructuralAbsence:
    """AC #28: raw ``emit`` MUST NOT exist; only ``emit_signal`` and
    ``emit_receipt`` are exposed."""

    def test_raw_emit_not_in_class(self):
        assert not hasattr(DrafterEventPort, "emit"), (
            "DrafterEventPort.emit MUST be structurally absent — "
            "raw emit could carry any event type and bypass the "
            "signal/receipt taxonomy."
        )

    def test_emit_signal_and_receipt_present(self):
        assert hasattr(DrafterEventPort, "emit_signal")
        assert hasattr(DrafterEventPort, "emit_receipt")

    def test_only_allowed_methods_present(self):
        public = {
            name for name in dir(DrafterEventPort)
            if not name.startswith("_")
        }
        expected = {"emit_signal", "emit_receipt", "instance_id"}
        assert public == expected, (
            f"DrafterEventPort public surface drift: "
            f"got {public}, expected {expected}"
        )


# ===========================================================================
# Source-module verification at construction
# ===========================================================================


class TestEventPortSourceModule:
    """The port verifies the supplied emitter is registered with
    ``source_module='drafter'``. Catches misconfiguration where the
    cohort gets handed e.g. a ``"crb"`` emitter."""

    async def test_rejects_emitter_with_wrong_source_module(self, tmp_path):
        await event_stream._reset_for_tests()
        await event_stream.start_writer(str(tmp_path))
        try:
            # Register a NON-drafter emitter.
            other = event_stream.emitter_registry().register("not_drafter")
            log = ActionLog(cohort_id="drafter")
            await log.start(str(tmp_path))
            try:
                with pytest.raises(ValueError, match="source_module"):
                    DrafterEventPort(
                        emitter=other, action_log=log,
                        instance_id="inst_a",
                    )
            finally:
                await log.stop()
        finally:
            await event_stream._reset_for_tests()

    async def test_accepts_drafter_emitter(self, tmp_path):
        await event_stream._reset_for_tests()
        await event_stream.start_writer(str(tmp_path))
        try:
            drafter = event_stream.emitter_registry().register("drafter")
            log = ActionLog(cohort_id="drafter")
            await log.start(str(tmp_path))
            try:
                port = DrafterEventPort(
                    emitter=drafter, action_log=log,
                    instance_id="inst_a",
                )
                assert port.instance_id == "inst_a"
            finally:
                await log.stop()
        finally:
            await event_stream._reset_for_tests()


# ===========================================================================
# Whitelist completeness (AC #9)
# ===========================================================================


class TestDrafterWhitelist:
    def test_whitelist_includes_allowed_writes(self):
        # WDP writes that Drafter is permitted.
        for tool in (
            "DraftRegistry.create_draft",
            "DraftRegistry.update_draft",
            "DraftRegistry.abandon_draft",
        ):
            assert tool in DRAFTER_WHITELIST

    def test_whitelist_excludes_mark_committed(self):
        assert "DraftRegistry.mark_committed" not in DRAFTER_WHITELIST, (
            "mark_committed is the spec's load-bearing forbidden "
            "capability — MUST NOT appear in DRAFTER_WHITELIST."
        )

    def test_whitelist_excludes_full_register_workflow(self):
        assert "SubstrateTools.register_workflow" not in DRAFTER_WHITELIST, (
            "Full register_workflow (dry_run=False) is the activation-"
            "authority pin — MUST NOT appear in DRAFTER_WHITELIST."
        )

    def test_whitelist_includes_dry_run_alias(self):
        assert "SubstrateTools.register_workflow_dry_run" in DRAFTER_WHITELIST


# ===========================================================================
# Port write-path: action_log idempotency
# ===========================================================================


@pytest.fixture
async def draft_port_stack(tmp_path):
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path))
    drafts = DraftRegistry()
    await drafts.start(str(tmp_path))
    log = ActionLog(cohort_id="drafter")
    await log.start(str(tmp_path))
    port = DrafterDraftPort(
        registry=drafts, action_log=log, instance_id="inst_a",
    )
    yield {"port": port, "drafts": drafts, "log": log}
    await log.stop()
    await drafts.stop()
    await event_stream._reset_for_tests()


class TestDraftPortIdempotency:
    async def test_create_draft_records_action_log_entry(
        self, draft_port_stack,
    ):
        port = draft_port_stack["port"]
        await port.create_draft(
            source_event_id="evt_1",
            intent_summary="test intent",
            target_draft_id="deterministic-target-1",
        )
        # Action log should have a record under the deterministic target.
        record = await draft_port_stack["log"].is_already_done(
            instance_id="inst_a",
            source_event_id="evt_1",
            action_type="create_draft",
            target_id="deterministic-target-1",
        )
        assert record is not None
        assert "draft_id" in record.result_summary

    async def test_create_draft_replay_does_not_double_invoke(
        self, draft_port_stack,
    ):
        port = draft_port_stack["port"]
        first = await port.create_draft(
            source_event_id="evt_1",
            intent_summary="test intent",
            target_draft_id="det-1",
        )
        # Second call with same source_event_id + target — replay path.
        second = await port.create_draft(
            source_event_id="evt_1",
            intent_summary="test intent",
            target_draft_id="det-1",
        )
        # Both return the same draft_id (replay returns prior summary).
        assert first["draft_id"] == second["draft_id"]
        # Only one WDP draft exists.
        all_drafts = await draft_port_stack["drafts"].list_drafts(
            instance_id="inst_a",
        )
        assert len(all_drafts) == 1
