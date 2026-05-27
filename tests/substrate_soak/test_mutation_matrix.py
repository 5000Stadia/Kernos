"""SUBSTRATE-SELF-TEST-V1 AC6 — mutation matrix.

For each known regression that would have broken the 2026-05-25
session, apply a targeted mutation via monkeypatch, run the full
soak suite, assert the mapped probe DOES fail. This proves the
probe is sensitive to the mutation (would catch the regression
if it happened) — not just informal "we wrote a test."

v1 strictness: each mutation must fail AT LEAST the mapped probe.
Some mutations affect overlapping substrate (canonicalize_tool_name
touches both Probes 1 and 4); v1 accepts that as legitimate
cross-coverage rather than fighting it. Strict "exactly one"
attribution is a follow-up if v1 telemetry shows we need it.

Codex round-2 + round-3 explicit: AC6 must be executable, not
informal. This file IS the proof.
"""
from __future__ import annotations

import asyncio
import pytest

from kernos.kernel.self_test_gate import SubstrateSoakRunner


# (mutation_name, monkeypatch_setup_fn, expected_failing_probe)
# monkeypatch_setup_fn takes the pytest monkeypatch fixture and
# applies the mutation. Must restore state on teardown — pytest's
# monkeypatch does this automatically for setattr/setitem/setenv.

def _mutation_read_source_kernos_only(monkeypatch):
    """Probe 2: revert read_source to reject specs/ + docs/."""
    from kernos.kernel.tools import schemas as _schemas
    _original = _schemas.read_source

    def _patched(path: str, section: str = "") -> str:
        if path.startswith(("specs/", "docs/")):
            return (
                "Error: Path resolves outside the allowed roots "
                "(simulated regression — kernos/ only)."
            )
        return _original(path, section=section)

    monkeypatch.setattr(_schemas, "read_source", _patched)


def _mutation_drain_limited_line_reads(monkeypatch):
    """Probe 3: replace _read_lines_unbounded with a wrapper that
    raises on lines >64KB (simulating the pre-fix behavior)."""
    from kernos.kernel.external_agents import acpx_adapter as _acpx

    async def _patched_unbounded(reader, chunk_size: int = 65536):
        # Use the standard async-for which crashes on >64KB.
        if reader is None:
            return
        async for line in reader:
            yield line

    monkeypatch.setattr(
        _acpx, "_read_lines_unbounded", _patched_unbounded,
    )


def _mutation_canonicalize_identity(monkeypatch):
    """Probes 1 + 4: canonicalize_tool_name becomes a no-op
    identity function — aliases never repair."""
    from kernos.kernel import tool_aliases as _ta

    def _patched(name: str) -> tuple[str, bool]:
        return (name, False)

    monkeypatch.setattr(_ta, "canonicalize_tool_name", _patched)


def _mutation_retry_ignores_prior_failures(monkeypatch):
    """Probe 5: _build_initial_messages ignores
    prior_attempt_failures — retry replays the same prompt."""
    from kernos.kernel.integration import runner as _runner

    _original = _runner.IntegrationRunner._build_initial_messages

    def _patched(self, inputs, *, prior_attempt_failures=()):
        # Always pass empty tuple to original — simulates the
        # pre-fix behavior of ignoring failure context.
        return _original(self, inputs, prior_attempt_failures=())

    monkeypatch.setattr(
        _runner.IntegrationRunner,
        "_build_initial_messages",
        _patched,
    )


def _mutation_watchdog_skip_silence_check(monkeypatch):
    """Probe 6: _is_gateway_heartbeat_unhealthy reverts to only
    checking latency, ignoring socket silence."""
    from kernos import server as _server
    import math

    def _patched():
        try:
            latency = _server.client.latency
        except Exception as exc:
            return True, f"client.latency raised: {exc}"
        if latency is None:
            return True, "None latency"
        if not math.isfinite(latency):
            return True, "non-finite latency"
        if latency <= 0:
            return True, "non-positive latency"
        if latency > _server._DISCORD_WATCHDOG_LATENCY_THRESHOLD_SEC:
            return True, f"latency too high"
        return False, "latency OK (silence check skipped)"

    monkeypatch.setattr(
        _server, "_is_gateway_heartbeat_unhealthy", _patched,
    )


def _mutation_observer_skip_pattern_b(monkeypatch):
    """Probe 6: _detect_gateway_deaf reverts to pattern-A only."""
    from kernos.kernel import gateway_health as _gh

    _original = _gh.GatewayHealthObserver._detect_gateway_deaf

    def _patched(self, now):
        # Skip the pattern-B (total silence) branch entirely.
        # Pattern A: only fires if MESSAGE_CREATE counter has
        # activity, which the probe deliberately leaves empty.
        if self._message_create_counter is None:
            return None
        mc_count = self._message_create_counter.count_in_window(now)
        if mc_count == 0:
            return None
        return _original(self, now)

    monkeypatch.setattr(
        _gh.GatewayHealthObserver,
        "_detect_gateway_deaf",
        _patched,
    )


def _mutation_approval_receipt_strips_binding(monkeypatch):
    """Probe 7: request_approval drops the binding payload."""
    from kernos.kernel import approval_receipts as _ar

    _original = _ar.request_approval

    async def _patched(**kwargs):
        # Drop the binding_payload — return an approval_id but
        # the receipt would be missing the bound payload.
        kwargs["binding_payload"] = {}
        return await _original(**kwargs)

    monkeypatch.setattr(_ar, "request_approval", _patched)


def _mutation_emit_boot_probe_noop(monkeypatch):
    """Probe 8: emit_boot_probe becomes a no-op — boot event
    never reaches the event stream."""
    from kernos.kernel.workflows import loop_health_helper as _lh

    async def _patched(**kwargs):
        # Return a boot_id but don't emit anything.
        return "noop_boot_id"

    monkeypatch.setattr(_lh, "emit_boot_probe", _patched)


MUTATION_MATRIX: list[tuple[str, callable, str]] = [
    (
        "read_source_kernos_only",
        _mutation_read_source_kernos_only,
        "self_knowledge_invariant",
    ),
    (
        "drain_limited_line_reads",
        _mutation_drain_limited_line_reads,
        "consult_drain_invariant",
    ),
    (
        "canonicalize_identity",
        _mutation_canonicalize_identity,
        "dispatch_canonicalization_invariant",
    ),
    (
        "retry_ignores_prior_failures",
        _mutation_retry_ignores_prior_failures,
        "retry_with_feedback_invariant",
    ),
    (
        "watchdog_skip_silence_check",
        _mutation_watchdog_skip_silence_check,
        "gateway_deafness_invariant",
    ),
    (
        "observer_skip_pattern_b",
        _mutation_observer_skip_pattern_b,
        "gateway_deafness_invariant",
    ),
    (
        "approval_receipt_strips_binding",
        _mutation_approval_receipt_strips_binding,
        "approval_loop_invariant",
    ),
    (
        "emit_boot_probe_noop",
        _mutation_emit_boot_probe_noop,
        "loop_health_completion_invariant",
    ),
]


@pytest.mark.parametrize(
    "mutation_name,mutation_fn,expected_failing_probe",
    MUTATION_MATRIX,
    ids=[m[0] for m in MUTATION_MATRIX],
)
def test_mutation_matrix_attribution(
    monkeypatch, mutation_name, mutation_fn, expected_failing_probe,
):
    """Each mutation MUST cause at least the mapped probe to fail.
    v1 strictness: at-least (some mutations affect overlapping
    substrate — that's accepted cross-coverage, not a defect)."""
    mutation_fn(monkeypatch)

    runner = SubstrateSoakRunner()
    result = asyncio.run(runner.run_all())

    failing = result.failing_probe_names()
    assert expected_failing_probe in failing, (
        f"Mutation {mutation_name!r} should have failed "
        f"{expected_failing_probe!r} but failing set was "
        f"{failing}. Either the probe is not sensitive to this "
        f"mutation (would not catch the regression) or the "
        f"mutation isn't actually applying. Investigate."
    )
