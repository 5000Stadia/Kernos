"""Probe 5 — retry_with_feedback_invariant (SUBSTRATE-SELF-TEST-V1).

Asserts that when an integration synthesis attempt fails
validation, attempt N+1's prompt includes a
<prior_attempt_failures> block naming the specific component
+ reason that failed in attempt N. Three blind identical
failures must not be possible.

Regression bug: 521c7f5. Retry loop replayed identical prompts;
3x identical ProposeTool.reason failures observed in production
because the model never saw what it had done wrong.
"""
from __future__ import annotations

import time

from kernos.kernel.self_test_gate import ProbeResult


REQUIRED_BEHAVIORAL_KEYS = frozenset({
    "attempt_1_outcome",
    "attempt_2_outcome",
    "final_briefing_received",
})

REQUIRED_SUBSTRATE_KEYS = frozenset({
    "attempt_2_prompt_contains_block",
    "attempt_2_prompt_failure_reason_text",
    "prior_attempt_failures_count",
})


async def run_probe() -> ProbeResult:
    start = time.monotonic()

    # Lazy imports to avoid pulling integration machinery at
    # module load time.
    from kernos.kernel.integration.runner import (
        IntegrationRunner,
        IntegrationConfig,
    )

    # Build a minimal IntegrationInputs without the full
    # cohort/handler stack. The retry-feedback invariant lives
    # in _build_initial_messages — we can exercise it directly
    # with a synthetic input rather than running the full pipeline.
    # This keeps the probe deterministic + fast.

    # _build_initial_messages constructs the user-message body
    # from inputs + prior_attempt_failures. Test that path
    # directly: with prior_attempt_failures populated, the body
    # must include <prior_attempt_failures> block + the reason
    # text.

    from kernos.kernel.integration.runner import (
        IntegrationAttemptFailed,
        BudgetState,
        IntegrationInputs,
    )

    # Construct minimal inputs.
    inputs = IntegrationInputs(
        user_message="trivial user message",
        conversation_thread=(),
        cohort_outputs=(),
        surfaced_tools=(),
        active_context_spaces=(),
        member_id="probe5_member",
        instance_id="probe5_test",
        space_id="probe5_space",
        turn_id="probe5_turn",
        required_safety_cohort_failures=(),
    )

    # Construct a runner. Use defaults; the path we exercise
    # doesn't need a real chain caller.
    async def _stub_chain(*_a, **_kw):  # pragma: no cover
        raise NotImplementedError("not invoked by this probe")

    async def _stub_dispatcher(*_a, **_kw):  # pragma: no cover
        raise NotImplementedError("not invoked by this probe")

    async def _stub_emit(entry: dict) -> None:
        pass

    runner = IntegrationRunner(
        chain_caller=_stub_chain,
        read_only_dispatcher=_stub_dispatcher,
        audit_emitter=_stub_emit,
        config=IntegrationConfig(max_retries=1),
    )

    # Synthetic prior failure to thread.
    prior_failure_reason = (
        "ProposeTool.reason must be a non-empty string"
    )
    prior_failure = IntegrationAttemptFailed(
        component="briefing_validation",
        reason=prior_failure_reason,
        iterations=1,
        phase_durations_ms={},
        tools_called=[],
        budget_state=BudgetState(),
    )

    # First attempt: no prior failures — the body should NOT
    # contain the retry block.
    attempt_1_messages = runner._build_initial_messages(inputs)
    attempt_1_body = attempt_1_messages[0]["content"]
    attempt_1_outcome = (
        "no_retry_block_present"
        if "<prior_attempt_failures>" not in attempt_1_body
        else "FAIL_unexpected_retry_block"
    )

    # Second attempt: one prior failure threaded. Body must
    # include the retry block + the failure reason text.
    attempt_2_messages = runner._build_initial_messages(
        inputs, prior_attempt_failures=(prior_failure,),
    )
    attempt_2_body = attempt_2_messages[0]["content"]

    block_present = "<prior_attempt_failures>" in attempt_2_body
    reason_in_block = prior_failure_reason in attempt_2_body
    address_directive = "Address these specifically" in attempt_2_body
    component_named = "briefing_validation" in attempt_2_body

    attempt_2_outcome = (
        "retry_block_present_with_reason"
        if (block_present and reason_in_block)
        else "FAIL_missing_retry_block_or_reason"
    )

    duration_ms = int((time.monotonic() - start) * 1000)

    all_passed = (
        attempt_1_outcome == "no_retry_block_present"
        and block_present
        and reason_in_block
        and address_directive
        and component_named
    )

    failure_reason = ""
    if not all_passed:
        failed = []
        if attempt_1_outcome != "no_retry_block_present":
            failed.append("attempt_1_has_unexpected_retry_block")
        if not block_present:
            failed.append("attempt_2_missing_prior_attempt_failures_block")
        if not reason_in_block:
            failed.append(
                f"attempt_2_missing_reason_text "
                f"({prior_failure_reason!r})"
            )
        if not address_directive:
            failed.append(
                "attempt_2_missing_address_specifically_directive"
            )
        if not component_named:
            failed.append("attempt_2_missing_component_name")
        failure_reason = (
            f"retry-with-feedback invariant violated: "
            f"{', '.join(failed)}. Likely regression of 521c7f5 "
            f"(INTEGRATION-RETRY-WITH-FEEDBACK-V1) — the "
            f"_build_initial_messages signature or "
            f"prior_attempt_failures rendering reverted."
        )

    # Count of prior_attempt_failures detected in attempt 2's
    # body — should be 1 to match what we threaded.
    prior_failures_count_seen = attempt_2_body.count(
        "Attempt 1 failed at component"
    )

    return ProbeResult(
        probe_name="retry_with_feedback_invariant",
        passed=all_passed,
        behavioral_evidence={
            "attempt_1_outcome": attempt_1_outcome,
            "attempt_2_outcome": attempt_2_outcome,
            "final_briefing_received": (
                "synthetic_probe_does_not_run_full_synthesis"
            ),
        },
        substrate_evidence={
            "attempt_2_prompt_contains_block": block_present,
            "attempt_2_prompt_failure_reason_text": (
                prior_failure_reason if reason_in_block
                else f"NOT_FOUND in body of length {len(attempt_2_body)}"
            ),
            "prior_attempt_failures_count": prior_failures_count_seen,
        },
        duration_ms=duration_ms,
        failure_reason=failure_reason,
    )
