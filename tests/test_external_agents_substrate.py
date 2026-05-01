"""C1 substrate tests for the external_agents module.

Pins:
* Harness protocol round-trip + result types frozen.
* HarnessRegistry registration + mode-aware get + discover.
* SubprocessResult shape + run_subprocess timeout + truncation.
* sanitize_session_id determinism + path-injection safety.
* ConsultationLog mutators round-trip + state transitions
  (pending → succeeded | failed | timed_out).
* Reentrancy ContextVar isolation across concurrent asyncio tasks.

Per-harness implementations + agent tool + reentrancy enforcement
ship in C2-C5 with their own test files. C1 tests cover only the
substrate.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from kernos.kernel.external_agents import (
    BuildResult,
    CallingContext,
    ConsultResult,
    ConsultationFailed,
    ConsultationLog,
    ConsultationTimeout,
    DepthExceeded,
    ExternalAgentError,
    Harness,
    HarnessHealth,
    HarnessRegistrationError,
    HarnessRegistry,
    HarnessUnavailable,
    ReentrancyBlocked,
    SubprocessResult,
    current_calling_context,
    current_consult_depth,
    enter_consult,
    exit_consult,
    reset_calling_context,
    response_truncate,
    run_subprocess,
    sanitize_session_id,
    set_calling_context,
)


# ===========================================================================
# Harness protocol + result types
# ===========================================================================


class TestResultTypes:
    def test_consult_result_frozen(self):
        r = ConsultResult(response="ok", harness="test")
        with pytest.raises(Exception):
            r.response = "changed"  # type: ignore[misc]

    def test_build_result_unified_with_legacy(self):
        # AC9 alignment: external_agents.harness.BuildResult is the
        # same class as builders.base.BuildResult (single source of
        # truth). The legacy class is mutable, so this is too — by
        # design. The frozen-result invariant only applies to
        # ConsultResult / HarnessHealth.
        from kernos.kernel.builders.base import BuildResult as LegacyBR
        assert BuildResult is LegacyBR
        r = BuildResult(success=True)
        r.success = False  # mutable — by design

    def test_harness_health_frozen(self):
        h = HarnessHealth(name="x", installed=True)
        with pytest.raises(Exception):
            h.installed = False  # type: ignore[misc]


# ===========================================================================
# Subprocess substrate
# ===========================================================================


class TestSanitizeSessionId:
    def test_returns_hex_64_chars(self):
        out = sanitize_session_id("kernos-bridge-2026")
        assert len(out) == 64
        assert all(c in "0123456789abcdef" for c in out)

    def test_deterministic(self):
        a = sanitize_session_id("foo")
        b = sanitize_session_id("foo")
        assert a == b

    def test_case_and_whitespace_normalized(self):
        a = sanitize_session_id("  Foo  ")
        b = sanitize_session_id("foo")
        assert a == b

    def test_empty_returns_empty(self):
        assert sanitize_session_id("") == ""
        assert sanitize_session_id("   ") == ""

    def test_path_injection_neutralized(self):
        """AC19: agent-supplied path-injection characters must hash
        to a hex string that's safe as a filesystem-path component."""
        out = sanitize_session_id("../../etc/passwd")
        assert "/" not in out
        assert ".." not in out
        assert len(out) == 64


class TestResponseTruncate:
    def test_short_text_passes_through(self):
        out, truncated = response_truncate("hello", cap_bytes=100)
        assert out == "hello"
        assert truncated is False

    def test_long_text_clipped(self):
        text = "x" * 200
        out, truncated = response_truncate(text, cap_bytes=50)
        assert truncated is True
        assert len(out.encode("utf-8")) <= 50


class TestRunSubprocess:
    async def test_captures_stdout(self):
        result = await run_subprocess(
            [sys.executable, "-c", "print('hello world')"],
            timeout_seconds=10,
        )
        assert isinstance(result, SubprocessResult)
        assert result.exit_code == 0
        assert "hello world" in result.stdout
        assert result.timed_out is False
        assert result.duration_seconds >= 0

    async def test_captures_stderr(self):
        result = await run_subprocess(
            [
                sys.executable, "-c",
                "import sys; sys.stderr.write('boom\\n'); sys.exit(2)",
            ],
            timeout_seconds=10,
        )
        assert result.exit_code == 2
        assert "boom" in result.stderr

    async def test_timeout_kills_subprocess(self):
        result = await run_subprocess(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout_seconds=1,
        )
        assert result.timed_out is True

    async def test_truncation_flag_set_when_output_exceeds_cap(self):
        result = await run_subprocess(
            [
                sys.executable, "-c",
                "import sys; sys.stdout.write('y' * 5000)",
            ],
            timeout_seconds=10,
            response_cap_bytes=1000,
        )
        assert result.truncated is True
        assert len(result.stdout.encode("utf-8")) <= 1000


# ===========================================================================
# HarnessRegistry
# ===========================================================================


class _StubHarness:
    """Minimal Harness for registry tests; raises HarnessUnavailable
    for both consult and build."""

    def __init__(self, name: str = "stub"):
        self.name = name

    def health_check(self) -> HarnessHealth:
        return HarnessHealth(name=self.name, installed=True, version="test")

    async def consult(self, **_) -> ConsultResult:
        raise HarnessUnavailable("stub")

    async def build(self, **_) -> BuildResult:
        raise HarnessUnavailable("stub")


class TestRegistry:
    def test_register_and_get_consult(self):
        reg = HarnessRegistry()
        h = _StubHarness("foo")
        reg.register(h, consult_supported=True, build_supported=False)
        assert reg.get("foo", mode="consult") is h

    def test_register_and_get_build(self):
        reg = HarnessRegistry()
        h = _StubHarness("foo")
        reg.register(h, consult_supported=False, build_supported=True)
        assert reg.get("foo", mode="build") is h

    def test_get_unknown_raises(self):
        reg = HarnessRegistry()
        with pytest.raises(HarnessUnavailable):
            reg.get("nope")

    def test_get_wrong_mode_raises(self):
        reg = HarnessRegistry()
        reg.register(
            _StubHarness("build_only"),
            consult_supported=False, build_supported=True,
        )
        with pytest.raises(HarnessUnavailable, match="consult"):
            reg.get("build_only", mode="consult")

    def test_register_neither_mode_rejected(self):
        reg = HarnessRegistry()
        with pytest.raises(HarnessRegistrationError):
            reg.register(
                _StubHarness("none"),
                consult_supported=False, build_supported=False,
            )

    def test_duplicate_registration_rejected(self):
        reg = HarnessRegistry()
        reg.register(_StubHarness("dup"), consult_supported=True)
        with pytest.raises(HarnessRegistrationError, match="already"):
            reg.register(_StubHarness("dup"), consult_supported=True)

    def test_list_consult_harnesses(self):
        reg = HarnessRegistry()
        reg.register(
            _StubHarness("c"), consult_supported=True, build_supported=False,
        )
        reg.register(
            _StubHarness("b"), consult_supported=False, build_supported=True,
        )
        reg.register(
            _StubHarness("both"), consult_supported=True, build_supported=True,
        )
        assert reg.list_consult_harnesses() == ["both", "c"]
        assert reg.list_build_harnesses() == ["b", "both"]

    def test_discover_returns_health_per_harness(self):
        reg = HarnessRegistry()
        reg.register(_StubHarness("h1"), consult_supported=True)
        reg.register(_StubHarness("h2"), consult_supported=True)
        out = reg.discover()
        assert set(out) == {"h1", "h2"}
        assert out["h1"].installed is True


# ===========================================================================
# ConsultationLog
# ===========================================================================


@pytest.fixture
async def log(tmp_path):
    cl = ConsultationLog()
    await cl.start(str(tmp_path))
    yield cl
    await cl.stop()


class TestConsultationLog:
    async def test_begin_creates_pending_row(self, log):
        cid = await log.begin(
            instance_id="i", member_id="m", harness="claude_code",
            question="test?", timeout_seconds=600,
        )
        rec = await log.get(cid)
        assert rec is not None
        assert rec.status == "pending"
        assert rec.harness == "claude_code"
        assert rec.question == "test?"
        assert rec.response == ""

    async def test_mark_succeeded_transitions_state(self, log):
        cid = await log.begin(
            instance_id="i", member_id="m", harness="codex",
            question="x", timeout_seconds=300,
        )
        await log.mark_succeeded(
            consultation_id=cid, response="hi back",
            native_session_ref="codex-thread-7",
        )
        rec = await log.get(cid)
        assert rec.status == "succeeded"
        assert rec.response == "hi back"
        assert rec.native_session_ref == "codex-thread-7"
        assert rec.ended_at

    async def test_mark_failed_records_error_and_status(self, log):
        cid = await log.begin(
            instance_id="i", member_id="m", harness="gemini",
            question="x", timeout_seconds=300,
        )
        await log.mark_failed(
            consultation_id=cid, error="exit 2: bad input",
            exit_status=2,
        )
        rec = await log.get(cid)
        assert rec.status == "failed"
        assert rec.error == "exit 2: bad input"
        assert rec.exit_status == 2

    async def test_mark_timed_out_distinct_from_failed(self, log):
        cid = await log.begin(
            instance_id="i", member_id="m", harness="claude_code",
            question="x", timeout_seconds=600,
        )
        await log.mark_timed_out(
            consultation_id=cid, timeout_seconds=600,
        )
        rec = await log.get(cid)
        assert rec.status == "timed_out"
        assert "600" in rec.error

    async def test_check_constraint_rejects_invalid_status(self, log):
        """v1 schema enforces status enum at the SQL layer; mutator
        methods cover the legal transitions but a direct SQL UPDATE
        with an invalid status must be rejected."""
        import aiosqlite
        cid = await log.begin(
            instance_id="i", member_id="m", harness="codex",
            question="x", timeout_seconds=600,
        )
        with pytest.raises(aiosqlite.IntegrityError):
            await log._db.execute(
                "UPDATE consultation_log SET status = 'bogus' "
                "WHERE consultation_id = ?",
                (cid,),
            )

    async def test_check_constraint_rejects_invalid_harness(self, log):
        import aiosqlite
        with pytest.raises(aiosqlite.IntegrityError):
            await log._db.execute(
                "INSERT INTO consultation_log "
                "(consultation_id, instance_id, member_id, harness, "
                " question, timeout_seconds, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "x", "i", "m", "imaginary_harness",
                    "q", 60, "2026-04-30T00:00:00+00:00",
                ),
            )

    async def test_find_pending_returns_only_pending(self, log):
        cid_pending = await log.begin(
            instance_id="i", member_id="m", harness="codex",
            question="x", timeout_seconds=300,
        )
        cid_done = await log.begin(
            instance_id="i", member_id="m", harness="codex",
            question="y", timeout_seconds=300,
        )
        await log.mark_succeeded(consultation_id=cid_done, response="z")
        pending = await log.find_pending(instance_id="i")
        assert {r.consultation_id for r in pending} == {cid_pending}

    async def test_find_by_session(self, log):
        sid = "session-abc"
        c1 = await log.begin(
            instance_id="i", member_id="m", harness="codex",
            session_id=sid, question="q1", timeout_seconds=300,
        )
        c2 = await log.begin(
            instance_id="i", member_id="m", harness="codex",
            session_id=sid, question="q2", timeout_seconds=300,
        )
        c3 = await log.begin(
            instance_id="i", member_id="m", harness="codex",
            session_id="other", question="q3", timeout_seconds=300,
        )
        rows = await log.find_by_session(session_id=sid)
        assert {r.consultation_id for r in rows} == {c1, c2}


# ===========================================================================
# Reentrancy guard
# ===========================================================================


class TestReentrancy:
    async def test_blocked_context_raises(self):
        token = set_calling_context(CallingContext.CRB_DISPATCH)
        try:
            with pytest.raises(ReentrancyBlocked, match="crb_dispatch"):
                enter_consult()
        finally:
            reset_calling_context(token)

    async def test_allowed_context_succeeds(self):
        token = set_calling_context(CallingContext.CONVERSATIONAL)
        try:
            consult_token = enter_consult()
            try:
                assert current_consult_depth() == 1
            finally:
                exit_consult(consult_token)
            assert current_consult_depth() == 0
        finally:
            reset_calling_context(token)

    async def test_unknown_context_blocked_by_default(self):
        # No calling context set — defaults to UNKNOWN which is blocked.
        with pytest.raises(ReentrancyBlocked):
            enter_consult()

    async def test_depth_limit_enforced(self):
        # CONVERSATIONAL depth limit is 2; third nested call rejected.
        token = set_calling_context(CallingContext.CONVERSATIONAL)
        try:
            t1 = enter_consult()
            t2 = enter_consult()
            with pytest.raises(DepthExceeded, match="2"):
                enter_consult()
            exit_consult(t2)
            exit_consult(t1)
        finally:
            reset_calling_context(token)

    async def test_drafter_depth_one(self):
        token = set_calling_context(CallingContext.DRAFTER)
        try:
            t1 = enter_consult()
            with pytest.raises(DepthExceeded, match="1"):
                enter_consult()
            exit_consult(t1)
        finally:
            reset_calling_context(token)

    async def test_concurrent_async_isolation(self):
        """AC17 pin: two tasks running through different paths via
        asyncio.gather see independent ContextVar state. Allowlisted
        path succeeds; blocked path raises; neither leaks to the
        other."""

        results: dict[str, str | Exception] = {}

        async def conversational_task():
            tok = set_calling_context(CallingContext.CONVERSATIONAL)
            try:
                # Yield to let the other task interleave its set().
                await asyncio.sleep(0)
                ctok = enter_consult()
                try:
                    # Confirm isolation: this task sees its own
                    # context, not the sibling's CRB_DISPATCH.
                    results["conv_ctx"] = current_calling_context().value
                    assert current_consult_depth() == 1
                    await asyncio.sleep(0)
                finally:
                    exit_consult(ctok)
            finally:
                reset_calling_context(tok)

        async def crb_task():
            tok = set_calling_context(CallingContext.CRB_DISPATCH)
            try:
                await asyncio.sleep(0)
                try:
                    enter_consult()
                    results["crb"] = "should_have_raised"
                except ReentrancyBlocked:
                    results["crb"] = "blocked_correctly"
                results["crb_ctx"] = current_calling_context().value
            finally:
                reset_calling_context(tok)

        await asyncio.gather(conversational_task(), crb_task())
        assert results["conv_ctx"] == "conversational"
        assert results["crb_ctx"] == "crb_dispatch"
        assert results["crb"] == "blocked_correctly"

    async def test_token_reset_restores_prior_context(self):
        # Outer conversational; inner switches to drafter; reset
        # restores conversational.
        outer = set_calling_context(CallingContext.CONVERSATIONAL)
        try:
            inner = set_calling_context(CallingContext.DRAFTER)
            try:
                assert current_calling_context() == CallingContext.DRAFTER
            finally:
                reset_calling_context(inner)
            assert current_calling_context() == CallingContext.CONVERSATIONAL
        finally:
            reset_calling_context(outer)


# ===========================================================================
# Module surface
# ===========================================================================


class TestModuleSurface:
    def test_all_public_names_importable(self):
        # Smoke test: every name in __all__ resolves.
        from kernos.kernel import external_agents
        for name in external_agents.__all__:
            assert hasattr(external_agents, name), name
