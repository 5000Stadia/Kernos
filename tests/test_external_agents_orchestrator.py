"""C4 orchestrator tests — agent-facing consult lifecycle.

The orchestrator wires together: reentrancy gate → workspace
resolution → session-id sanitization → native-ref lookup → log
begin → harness invoke → log mark.

Tests use a mock harness so no real CLI runs. Reentrancy
enforcement, log lifecycle, workspace resolution, prior-native-ref
lookup, and timeout clamping all pinned here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kernos.kernel.external_agents import (
    CallingContext,
    ConsultationFailed,
    ConsultationLog,
    ConsultationOrchestrator,
    ConsultationTimeout,
    ConsultResult,
    DepthExceeded,
    HarnessHealth,
    HarnessRegistry,
    HarnessUnavailable,
    ReentrancyBlocked,
    WorkspaceNotAllowed,
    WorkspacePolicy,
    reset_calling_context,
    sanitize_session_id,
    set_calling_context,
)


# ===========================================================================
# Mock harness — captures inputs, returns canned response or raises
# ===========================================================================


class _RecordingHarness:
    name = "claude_code"  # CHECK constraint requires one of the four valid harness names
    consult_supported = True

    def __init__(
        self,
        *,
        canned_response: str = "ok",
        canned_native_ref: str = "",
        raise_exc: Exception | None = None,
        truncated: bool = False,
    ):
        self.canned_response = canned_response
        self.canned_native_ref = canned_native_ref
        self.raise_exc = raise_exc
        self.truncated = truncated
        self.calls: list[dict] = []

    def health_check(self) -> HarnessHealth:
        return HarnessHealth(
            name=self.name, installed=True, authenticated=True,
        )

    async def consult(
        self,
        *,
        question: str,
        context: dict | str,
        session_id: str,
        workspace_dir: Path,
        timeout_seconds: int,
        harness_options: dict[str, Any],
    ) -> ConsultResult:
        self.calls.append({
            "question": question,
            "context": context,
            "session_id": session_id,
            "workspace_dir": workspace_dir,
            "timeout_seconds": timeout_seconds,
            "harness_options": dict(harness_options),
        })
        if self.raise_exc is not None:
            raise self.raise_exc
        return ConsultResult(
            response=self.canned_response,
            harness=self.name,
            session_id=session_id,
            native_session_ref=self.canned_native_ref,
            truncated=self.truncated,
            metadata={"recorder": True},
        )

    async def build(self, **_):
        raise HarnessUnavailable("recorder is consult-only")


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
async def log(tmp_path):
    cl = ConsultationLog()
    await cl.start(str(tmp_path))
    yield cl
    await cl.stop()


@pytest.fixture
def conversational_context():
    """Set CONVERSATIONAL calling context for the duration of a
    test, then reset. Most orchestrator tests need this — only the
    reentrancy-block tests deliberately don't set it (or set a
    blocked context)."""
    token = set_calling_context(CallingContext.CONVERSATIONAL)
    yield
    reset_calling_context(token)


def _make_orchestrator(log, harness, *, policy=None):
    reg = HarnessRegistry()
    reg.register(harness, consult_supported=True, build_supported=False)
    return ConsultationOrchestrator(
        registry=reg, log=log,
        workspace_policy=policy or WorkspacePolicy(),
    )


# ===========================================================================
# Happy path
# ===========================================================================


class TestHappyPath:
    async def test_consult_returns_response(
        self, log, conversational_context, tmp_path,
    ):
        h = _RecordingHarness(canned_response="hello back")
        orch = _make_orchestrator(log, h)
        result = await orch.consult(
            instance_id="i", member_id="m",
            harness="claude_code", question="hi",
            workspace_dir=tmp_path,
            timeout_seconds=300,
        )
        assert result.response == "hello back"
        assert len(h.calls) == 1
        assert h.calls[0]["question"] == "hi"

    async def test_log_row_marked_succeeded(
        self, log, conversational_context, tmp_path,
    ):
        h = _RecordingHarness(
            canned_response="answer", canned_native_ref="native-1",
        )
        orch = _make_orchestrator(log, h)
        await orch.consult(
            instance_id="i", member_id="m",
            harness="claude_code", question="q",
            workspace_dir=tmp_path,
            timeout_seconds=300,
        )
        rows = await log.find_pending(instance_id="i")
        assert rows == []  # no pending rows; succeeded immediately

    async def test_session_id_sanitized_before_harness(
        self, log, conversational_context, tmp_path,
    ):
        """Codex spec-review fold #7 + AC19: agent-supplied raw
        session_id is hashed to safe hex BEFORE the harness sees it."""
        h = _RecordingHarness()
        orch = _make_orchestrator(log, h)
        await orch.consult(
            instance_id="i", member_id="m",
            harness="claude_code", question="x",
            session_id_raw="../../etc/passwd",  # path-injection attempt
            workspace_dir=tmp_path,
        )
        # Harness saw the sanitized hex, not the raw input.
        passed_session = h.calls[0]["session_id"]
        assert "/" not in passed_session
        assert ".." not in passed_session
        assert len(passed_session) == 64
        assert passed_session == sanitize_session_id("../../etc/passwd")


# ===========================================================================
# Reentrancy enforcement
# ===========================================================================


class TestReentrancyEnforcement:
    async def test_blocked_context_raises_before_harness_invoked(
        self, log, tmp_path,
    ):
        h = _RecordingHarness()
        orch = _make_orchestrator(log, h)
        token = set_calling_context(CallingContext.CRB_DISPATCH)
        try:
            with pytest.raises(ReentrancyBlocked, match="crb_dispatch"):
                await orch.consult(
                    instance_id="i", member_id="m",
                    harness="claude_code", question="q",
                    workspace_dir=tmp_path,
                )
        finally:
            reset_calling_context(token)
        # Harness was NOT invoked
        assert h.calls == []
        # No log row created either (reject before begin)
        assert await log.find_pending(instance_id="i") == []

    async def test_unknown_context_blocked_by_default(
        self, log, tmp_path,
    ):
        h = _RecordingHarness()
        orch = _make_orchestrator(log, h)
        # No context set — defaults to UNKNOWN, blocked.
        with pytest.raises(ReentrancyBlocked):
            await orch.consult(
                instance_id="i", member_id="m",
                harness="claude_code", question="q",
                workspace_dir=tmp_path,
            )

    async def test_depth_exceeded_after_two_nested_consults(
        self, log, conversational_context, tmp_path,
    ):
        """CONVERSATIONAL depth limit is 2. The orchestrator's
        enter_consult / exit_consult manages it; nested consults
        beyond limit raise."""
        from kernos.kernel.external_agents.reentrancy import (
            enter_consult, exit_consult,
        )
        h = _RecordingHarness()
        orch = _make_orchestrator(log, h)
        # Manually push two depth levels (simulating that two
        # consultations are already in flight on this task).
        t1 = enter_consult()
        t2 = enter_consult()
        try:
            with pytest.raises(DepthExceeded):
                await orch.consult(
                    instance_id="i", member_id="m",
                    harness="claude_code", question="q",
                    workspace_dir=tmp_path,
                )
        finally:
            exit_consult(t2)
            exit_consult(t1)


# ===========================================================================
# Failure capture in consultation_log
# ===========================================================================


class TestFailureCapture:
    async def test_consultation_failed_logged_then_raised(
        self, log, conversational_context, tmp_path,
    ):
        h = _RecordingHarness(
            raise_exc=ConsultationFailed("CLI exited 7"),
        )
        orch = _make_orchestrator(log, h)
        with pytest.raises(ConsultationFailed, match="exited 7"):
            await orch.consult(
                instance_id="i", member_id="m",
                harness="claude_code", question="q",
                workspace_dir=tmp_path,
            )
        # Verify a row exists with status='failed'
        from kernos.kernel.external_agents.consultation_log import (
            ConsultationLog,
        )
        # Re-query via direct sql since find_pending won't find failed
        rows = await log._db.execute_fetchall(
            "SELECT status, error FROM consultation_log "
            "WHERE instance_id = ?",
            ("i",),
        )
        rows = list(rows)
        assert len(rows) == 1
        assert rows[0][0] == "failed"
        assert "exited 7" in rows[0][1]

    async def test_consultation_timeout_logged_as_timed_out(
        self, log, conversational_context, tmp_path,
    ):
        h = _RecordingHarness(
            raise_exc=ConsultationTimeout("timed out after 10s"),
        )
        orch = _make_orchestrator(log, h)
        with pytest.raises(ConsultationTimeout):
            await orch.consult(
                instance_id="i", member_id="m",
                harness="claude_code", question="q",
                workspace_dir=tmp_path,
            )
        rows = await log._db.execute_fetchall(
            "SELECT status FROM consultation_log "
            "WHERE instance_id = ?",
            ("i",),
        )
        assert list(rows)[0][0] == "timed_out"

    async def test_harness_unavailable_logged_as_failed(
        self, log, conversational_context, tmp_path,
    ):
        h = _RecordingHarness(
            raise_exc=HarnessUnavailable("missing"),
        )
        orch = _make_orchestrator(log, h)
        with pytest.raises(HarnessUnavailable):
            await orch.consult(
                instance_id="i", member_id="m",
                harness="claude_code", question="q",
                workspace_dir=tmp_path,
            )
        rows = await log._db.execute_fetchall(
            "SELECT status, error FROM consultation_log "
            "WHERE instance_id = ?",
            ("i",),
        )
        rows = list(rows)
        assert rows[0][0] == "failed"
        assert "HarnessUnavailable" in rows[0][1]


# ===========================================================================
# Workspace resolution + allowlist
# ===========================================================================


class TestWorkspaceResolution:
    async def test_per_call_override_wins(
        self, log, conversational_context, tmp_path,
    ):
        h = _RecordingHarness()
        policy = WorkspacePolicy(default_dir=tmp_path / "default")
        orch = _make_orchestrator(log, h, policy=policy)
        override = tmp_path / "override"
        override.mkdir()
        await orch.consult(
            instance_id="i", member_id="m",
            harness="claude_code", question="q",
            workspace_dir=override,
        )
        assert h.calls[0]["workspace_dir"] == override.resolve()

    async def test_default_dir_used_when_no_override(
        self, log, conversational_context, tmp_path,
    ):
        h = _RecordingHarness()
        default = tmp_path / "default"
        default.mkdir()
        policy = WorkspacePolicy(default_dir=default)
        orch = _make_orchestrator(log, h, policy=policy)
        await orch.consult(
            instance_id="i", member_id="m",
            harness="claude_code", question="q",
        )
        assert h.calls[0]["workspace_dir"] == default.resolve()

    async def test_allowlist_blocks_outside_paths(
        self, log, conversational_context, tmp_path,
    ):
        h = _RecordingHarness()
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        policy = WorkspacePolicy(allowlist=(allowed,))
        orch = _make_orchestrator(log, h, policy=policy)
        outside = tmp_path / "outside"
        outside.mkdir()
        with pytest.raises(WorkspaceNotAllowed):
            await orch.consult(
                instance_id="i", member_id="m",
                harness="claude_code", question="q",
                workspace_dir=outside,
            )

    async def test_allowlist_permits_under_prefix(
        self, log, conversational_context, tmp_path,
    ):
        h = _RecordingHarness()
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        nested = allowed / "nested"
        nested.mkdir()
        policy = WorkspacePolicy(allowlist=(allowed,))
        orch = _make_orchestrator(log, h, policy=policy)
        result = await orch.consult(
            instance_id="i", member_id="m",
            harness="claude_code", question="q",
            workspace_dir=nested,
        )
        assert result.response == "ok"


# ===========================================================================
# Native-session-ref lookup for codex resume
# ===========================================================================


class TestNativeRefLookup:
    async def test_codex_resume_passes_prior_native_ref(
        self, log, conversational_context, tmp_path,
    ):
        """When a prior consultation in the same session_id captured
        a Codex thread_id, the next codex consult passes it via
        harness_options['prior_native_session_ref']."""
        # First call captures native ref "thread-abc"
        first = _RecordingHarness(
            canned_native_ref="thread-abc",
        )
        first.name = "codex"
        reg = HarnessRegistry()
        reg.register(first, consult_supported=True, build_supported=False)
        orch = ConsultationOrchestrator(registry=reg, log=log)
        await orch.consult(
            instance_id="i", member_id="m",
            harness="codex", question="first",
            session_id_raw="threaded-session",
            workspace_dir=tmp_path,
        )

        # Second call: orchestrator looks up the prior native_ref
        # and passes it via harness_options.
        second = _RecordingHarness(canned_native_ref="thread-abc")
        second.name = "codex"
        reg2 = HarnessRegistry()
        reg2.register(second, consult_supported=True, build_supported=False)
        orch2 = ConsultationOrchestrator(registry=reg2, log=log)
        await orch2.consult(
            instance_id="i", member_id="m",
            harness="codex", question="follow-up",
            session_id_raw="threaded-session",
            workspace_dir=tmp_path,
        )
        opts = second.calls[0]["harness_options"]
        assert opts.get("prior_native_session_ref") == "thread-abc"

    async def test_non_codex_harness_does_not_get_prior_ref(
        self, log, conversational_context, tmp_path,
    ):
        """The orchestrator only looks up prior_native_session_ref
        for harnesses that need it (codex). Other harnesses get
        their harness_options unmodified."""
        h = _RecordingHarness(canned_native_ref="some-uuid")
        h.name = "claude_code"
        reg = HarnessRegistry()
        reg.register(h, consult_supported=True, build_supported=False)
        orch = ConsultationOrchestrator(registry=reg, log=log)
        await orch.consult(
            instance_id="i", member_id="m",
            harness="claude_code", question="hi",
            session_id_raw="threaded-session",
            workspace_dir=tmp_path,
        )
        opts = h.calls[0]["harness_options"]
        assert "prior_native_session_ref" not in opts


# ===========================================================================
# Timeout clamping
# ===========================================================================


class TestTimeoutClamping:
    async def test_timeout_clamped_to_max(
        self, log, conversational_context, tmp_path,
    ):
        h = _RecordingHarness()
        orch = _make_orchestrator(log, h)
        await orch.consult(
            instance_id="i", member_id="m",
            harness="claude_code", question="q",
            workspace_dir=tmp_path,
            timeout_seconds=999_999,  # absurd
        )
        assert h.calls[0]["timeout_seconds"] == 1800  # max

    async def test_timeout_default_when_none(
        self, log, conversational_context, tmp_path,
    ):
        h = _RecordingHarness()
        orch = _make_orchestrator(log, h)
        await orch.consult(
            instance_id="i", member_id="m",
            harness="claude_code", question="q",
            workspace_dir=tmp_path,
        )
        assert h.calls[0]["timeout_seconds"] == 600  # default

    async def test_timeout_clamped_above_one(
        self, log, conversational_context, tmp_path,
    ):
        h = _RecordingHarness()
        orch = _make_orchestrator(log, h)
        await orch.consult(
            instance_id="i", member_id="m",
            harness="claude_code", question="q",
            workspace_dir=tmp_path,
            timeout_seconds=0,  # below floor
        )
        assert h.calls[0]["timeout_seconds"] == 1


# ===========================================================================
# GateClassification enum extension (Codex spec-review fold #2)
# ===========================================================================


class TestGateClassificationExtension:
    def test_external_agent_read_added(self):
        from kernos.kernel.tool_descriptor import GateClassification
        assert GateClassification.EXTERNAL_AGENT_READ.value == "external_agent_read"

    def test_existing_four_values_preserved(self):
        """AC backward-compat: the four existing GateClassification
        values are still present after the additive extension."""
        from kernos.kernel.tool_descriptor import GateClassification
        names = {v.name for v in GateClassification}
        assert "READ" in names
        assert "SOFT_WRITE" in names
        assert "HARD_WRITE" in names
        assert "DELETE" in names

    def test_safety_for_gate_includes_external_agent_read(self):
        """Catalog-filter safety derivation: external_agent_read
        derives read_only safety so integration's catalog continues
        surfacing the tool with read-class semantics."""
        from kernos.kernel.tool_descriptor import (
            GateClassification, OperationSafety, SAFETY_FOR_GATE,
        )
        assert (
            SAFETY_FOR_GATE[GateClassification.EXTERNAL_AGENT_READ]
            == OperationSafety.READ_ONLY
        )
