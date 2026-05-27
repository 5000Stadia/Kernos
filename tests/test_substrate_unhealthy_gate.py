"""SUBSTRATE-SELF-TEST-V1 AC9 — autonomous-mutation gate.

When the most recent substrate-soak result is failed, autonomous
git_commit + git_push calls refuse with SubstrateUnhealthyError.
Operator-initiated paths (manual git CLI) bypass the gate.
"""
from __future__ import annotations

import pytest

from kernos.kernel.self_test_gate import (
    SubstrateUnhealthyError,
    check_substrate_healthy_or_raise,
    is_substrate_healthy,
    mark_substrate_health,
)


class TestSubstrateHealthFlag:
    def test_default_state_is_healthy(self):
        """Module imports with passed=True so unit tests that
        don't bring up the substrate don't get spurious refusals."""
        # Reset to default in case prior tests mutated it.
        mark_substrate_health(passed=True, failing_probes=())
        healthy, failing = is_substrate_healthy()
        assert healthy is True
        assert failing == ()

    def test_mark_unhealthy_persists(self):
        mark_substrate_health(
            passed=False,
            failing_probes=("agent_round_trip_soak",),
        )
        try:
            healthy, failing = is_substrate_healthy()
            assert healthy is False
            assert failing == ("agent_round_trip_soak",)
        finally:
            # Reset so other tests aren't poisoned.
            mark_substrate_health(passed=True, failing_probes=())

    def test_mark_back_to_healthy_clears(self):
        mark_substrate_health(passed=False, failing_probes=("x",))
        mark_substrate_health(passed=True, failing_probes=())
        healthy, failing = is_substrate_healthy()
        assert healthy is True
        assert failing == ()


class TestCheckSubstrateHealthyOrRaise:
    def test_healthy_state_no_raise(self):
        mark_substrate_health(passed=True, failing_probes=())
        # Should not raise.
        check_substrate_healthy_or_raise(autonomous_path="git_commit")

    def test_unhealthy_state_raises_substrate_unhealthy_error(self):
        mark_substrate_health(
            passed=False,
            failing_probes=("consult_drain_invariant",),
        )
        try:
            with pytest.raises(SubstrateUnhealthyError) as excinfo:
                check_substrate_healthy_or_raise(
                    autonomous_path="git_commit",
                )
            msg = str(excinfo.value)
            assert "git_commit refused" in msg
            assert "consult_drain_invariant" in msg
            assert "AC9" in msg
        finally:
            mark_substrate_health(passed=True, failing_probes=())

    def test_autonomous_path_name_in_error(self):
        mark_substrate_health(
            passed=False, failing_probes=("x_probe",),
        )
        try:
            with pytest.raises(SubstrateUnhealthyError) as excinfo:
                check_substrate_healthy_or_raise(
                    autonomous_path="git_push",
                )
            assert "git_push refused" in str(excinfo.value)
        finally:
            mark_substrate_health(passed=True, failing_probes=())


class TestGitCommitHandlerHonorsGate:
    @pytest.mark.asyncio
    async def test_git_commit_refuses_when_substrate_unhealthy(self):
        from kernos.kernel.git_operations import handle_git_commit
        mark_substrate_health(
            passed=False,
            failing_probes=("gateway_deafness_invariant",),
        )
        try:
            result = await handle_git_commit(
                tool_input={
                    "workspace_dir": "irrelevant",
                    "message": "test",
                    "approval_id": "irrelevant",
                    "files": ["x"],
                },
                instance_id="test",
                data_dir="/tmp",
            )
            # The handler returns the SubstrateUnhealthyError
            # message as prose (per the wired pattern).
            assert "git_commit refused" in result
            assert "gateway_deafness_invariant" in result
        finally:
            mark_substrate_health(passed=True, failing_probes=())

    @pytest.mark.asyncio
    async def test_git_push_refuses_when_substrate_unhealthy(self):
        from kernos.kernel.git_operations import handle_git_push
        mark_substrate_health(
            passed=False, failing_probes=("retry_with_feedback_invariant",),
        )
        try:
            result = await handle_git_push(
                tool_input={
                    "workspace_dir": "irrelevant",
                    "target_branch": "main",
                    "approval_id": "irrelevant",
                },
                instance_id="test",
                data_dir="/tmp",
            )
            assert "git_push refused" in result
            assert "retry_with_feedback_invariant" in result
        finally:
            mark_substrate_health(passed=True, failing_probes=())
