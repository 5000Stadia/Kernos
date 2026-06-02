"""Integration runner: orchestrates the iterative prep loop.

Consumes the contract defined in `briefing.py` (CohortOutput,
Briefing, AuditTrace, BudgetState, Visibility) and `template.py`
(integration prompt + finalize tool schema). Composes with three
caller-supplied dependencies so the runner is opt-in callable
(spec acceptance criterion #13) — nothing in the existing
reasoning loop calls it yet:

  - `chain_caller`: async callable matching the provider chain
    surface (system, messages, tools, max_tokens) →
    ProviderResponse. The caller picks the chain (cheap-tier
    default per Section 7).

  - `read_only_dispatcher`: async callable that executes a
    read-only retrieval tool the integration model decides to
    call mid-loop. The runner enforces gate_classification: read
    at the dispatch boundary; the dispatcher should also
    re-validate per the runtime-enforcement contract.

  - `audit_emitter`: async callable that the runner invokes once
    per run to log the briefing under audit_category
    `integration.briefing`. Subsequent specs wire this to the
    audit-log substrate; this spec takes the callback and
    exercises it under test.

Loop terminates on any of:
  - model called __finalize_briefing__   → parse + return
  - max_iterations exhausted             → fail-soft fallback (BudgetState.iterations_hit_limit)
  - integration_timeout exceeded         → fail-soft fallback (BudgetState.timeout_hit_limit)
  - any unexpected error                 → fail-soft fallback
  - model produced no tool_use block     → fail-soft fallback
  - integration tried to call non-read   → fail-soft fallback (Section 4b)
  - briefing validation failed           → fail-soft fallback

Per Section 4c the fail-soft path always returns a minimal
respond_only briefing — never raw cohort inputs.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from kernos.kernel.integration.briefing import (
    ActionEnvelope,
    AuditTrace,
    Briefing,
    BriefingValidationError,
    BudgetState,
    CohortOutput,
    ConstrainedResponse,
    ContextItem,
    Defer,
    FilteredItem,
    Restricted,
    RespondOnly,
    action_kind_requires_envelope,
    decided_action_from_dict,
    minimal_fail_soft_briefing,
)
from kernos.kernel.integration.template import (
    FINALIZE_TOOL_NAME,
    FINALIZE_TOOL_SCHEMA,
    build_system_prompt,
)
from kernos.providers.base import ContentBlock, ProviderResponse


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Inputs / config / errors
# ---------------------------------------------------------------------------


# Surfacing rationale tags. Free-form strings are tolerated; the
# canonical set lives here so downstream auditing has stable
# categories. Subsequent specs (cohort fan-out, tool surfacing
# rewire) wire surfacers to emit these.
SURFACING_RATIONALE_CREDENTIAL = "credential_present"
SURFACING_RATIONALE_PINNED = "always_pinned"
SURFACING_RATIONALE_RELEVANCE = "relevance_match"
SURFACING_RATIONALE_GATE_CLASS = "gate_class_match"
SURFACING_RATIONALE_CONTEXT_SPACE_PIN = "context_space_pin"


@dataclass(frozen=True)
class SurfacedTool:
    """A tool the surfacer offered for this turn.

    `gate_classification` is the per-call gate routing token. The
    runner only forwards tools with classification "read" to the
    integration model — soft_write/hard_write/delete tools belong
    to presence's executable surface.

    `surfacing_rationale` tells integration *why* this tool was
    surfaced (credential present, always-pinned, relevance match,
    gate-class match, context-space pin, etc.). Surfaces in the
    model prompt and in the audit trail.
    """

    tool_id: str
    description: str
    input_schema: dict[str, Any]
    gate_classification: str
    surfacing_rationale: str = ""


@dataclass(frozen=True)
class IntegrationInputs:
    """Everything the runner needs to produce a briefing for this turn.

    The conversation_thread is in API-message format (role/content
    dicts). cohort_outputs, surfaced_tools, and active_context_spaces
    arrive structured so the prompt rendering is straightforward and
    the audit trail can pick them up cleanly.
    """

    user_message: str
    conversation_thread: tuple[dict[str, Any], ...]
    cohort_outputs: tuple[CohortOutput, ...]
    surfaced_tools: tuple[SurfacedTool, ...]
    active_context_spaces: tuple[dict[str, Any], ...]
    member_id: str
    instance_id: str
    space_id: str
    turn_id: str
    integration_run_id: str = ""
    # COHORT-ADAPT-COVENANT V1 schema extension. Cohort_ids of
    # required + safety_class cohorts whose fan-out outcome was
    # not SUCCESS. Non-empty means safety is degraded and
    # integration's filter phase must produce defer or
    # constrained_response — not respond_only / execute_tool /
    # propose_tool. Per spec Section 2c + the design review's load-bearing
    # input ("safety-degraded fail-soft must never be respond_only").
    # Backwards-compatible: defaults to empty tuple; pre-CAC
    # callers see no behavior change.
    required_safety_cohort_failures: tuple[str, ...] = ()
    # COGNITIVE-CONTEXT-V1 C3a: typed cognitive substrate produced
    # by assembly. None when assembly hasn't constructed the packet
    # (legacy paths or callers that pre-date C3a). The integration
    # runner copies this through onto Briefing so the renderer at
    # C3c can consume the canonical primitive. Optional + default-
    # None preserves backward compat for the many existing
    # IntegrationInputs construction sites.
    cognitive_context: Any = None


@dataclass(frozen=True)
class IntegrationConfig:
    """Depth guardrails and behavioural knobs.

    Defaults match Section 4c of the spec. max_iterations=5 per the
    spec's literal text. integration_timeout_seconds is wall-clock;
    max_integration_tokens is the model's max_tokens parameter for
    each call (per-call, not cumulative — the BudgetState's
    tokens_hit_limit flag is reserved for cumulative tracking when
    that lands).

    Retry semantics (PHASE-1-WIPE-VERIFICATION fix, 2026-05-07):
    Section 4c originally returned a generic "minimal fail-soft"
    briefing on the first iteration/timeout/validation failure.
    That softened internal errors into "respond conservatively"
    apologies, hiding real failures and producing degenerate agent
    responses (audit traces showed this directly: timeout fires
    mid-synthesis → fail-soft engages → presence directive becomes
    "integration prep was incomplete" → agent says "I'm here, send
    me what you want" even though the original brief was concrete
    and tool results had been gathered successfully).

    Architectural correction: synthesis attempts now retry up to
    ``max_retries`` times before surfacing a hard system error. Each
    retry resets the iteration loop's chain state so the model gets
    a clean shot. After exhaustion, the runner emits a hard-error
    briefing whose directive instructs presence to surface the
    failure to the user transparently rather than apologize for
    "limited context". Loud, identifiable, attributable.
    """

    # Natural checkpoint cadence — high enough to absorb routine
    # multi-step work in one shot, low enough that long-running tasks
    # surface a check-in to the user partway through rather than
    # silently consuming budget. ITERATION-CAP-PROMPT (2026-05-07)
    # replaced the prior cap=5 (sized for legacy briefing-assembly)
    # after CCV1 C7 strike turned the integration runner into the
    # primary tool-dispatch seam. The wall-clock timeout
    # (integration_timeout_seconds) remains the absolute safety; the
    # iteration cap is a check-in cadence: on exhaustion the runner
    # surfaces a three-option choice (continue / always continue /
    # terminate) so the user can keep the work going, raise the cap
    # permanently, or stop.
    max_iterations: int = 50
    max_integration_tokens: int = 2048
    # Generous default: meaningful work (multi-step navigation, slow
    # tool dispatches) is allowed to take its time. The retry harness
    # already ensures a stuck attempt eventually surfaces a hard
    # error; the absolute wall-clock ceiling exists only to prevent a
    # truly hung attempt from running forever. Set to 0 to disable
    # the wall-clock check entirely (env: KERNOS_INTEGRATION_TIMEOUT_SECONDS=0).
    integration_timeout_seconds: float = 600.0
    max_summarized_cohort_entries: int = 20
    max_filtered_entries: int = 50
    chain_name: str = "lightweight"
    max_retries: int = 3
    retry_backoff_seconds: float = 0.0
    # When set, the runner writes a friction-style markdown report to
    # ``{data_dir}/diagnostics/friction/`` on retry exhaustion so the
    # operator sees the timeout/exhaustion at session start (mirrors
    # FrictionObserver's surfacing convention). When None, friction
    # writing is skipped — appropriate for tests and library-only use.
    data_dir: str | None = None

    def __post_init__(self) -> None:
        # max_retries < 1 would skip the retry loop entirely and trip
        # the run() exhaustion-path assertion; reject it loudly at
        # construction time instead. Same posture for negative
        # backoffs, which would imply zero-or-negative sleep durations.
        if self.max_retries < 1:
            raise ValueError(
                f"IntegrationConfig.max_retries must be >= 1, "
                f"got {self.max_retries}"
            )
        if self.retry_backoff_seconds < 0:
            raise ValueError(
                f"IntegrationConfig.retry_backoff_seconds must be >= 0, "
                f"got {self.retry_backoff_seconds}"
            )
        # NaN/inf would silently disable the timeout guardrail because
        # `current_clock - start > timeout` is False for non-finite
        # comparisons — that's a footgun, not an opt-in. Reject those.
        # 0 is the explicit opt-in sentinel for "no wall-clock ceiling"
        # (the runtime check skips the comparison when timeout == 0)
        # so that operators who want truly unbounded synthesis time
        # can ask for it without us silently allowing it via NaN.
        # Negative values are nonsense.
        import math
        if (
            not math.isfinite(self.integration_timeout_seconds)
            or self.integration_timeout_seconds < 0
        ):
            raise ValueError(
                f"IntegrationConfig.integration_timeout_seconds must be "
                f"a finite non-negative number (0 disables the wall-"
                f"clock ceiling), got {self.integration_timeout_seconds}"
            )
        if self.max_iterations < 1:
            raise ValueError(
                f"IntegrationConfig.max_iterations must be >= 1, "
                f"got {self.max_iterations}"
            )

    @classmethod
    def from_env(cls, **overrides: Any) -> "IntegrationConfig":
        """Build a config with env-var overrides applied on top of the
        defaults. Recognised env vars (all optional):

          - ``KERNOS_INTEGRATION_TIMEOUT_SECONDS`` — extend the wall-
            clock budget for one synthesis attempt. Slow-but-correct
            cohort/docs questions need a roomier budget than the 30s
            default.
          - ``KERNOS_INTEGRATION_MAX_RETRIES`` — number of attempts
            before the runner surfaces a hard system-error.
          - ``KERNOS_INTEGRATION_MAX_ITERATIONS`` — per-attempt
            iteration cap.
          - ``KERNOS_DATA_DIR`` — root for the friction report drop;
            mirrors REPL/server convention.

        Programmatic ``**overrides`` win over env vars (lets callers
        pin a specific value while still picking up the data_dir).
        """
        env_values: dict[str, Any] = {}
        timeout = os.getenv("KERNOS_INTEGRATION_TIMEOUT_SECONDS")
        if timeout:
            try:
                env_values["integration_timeout_seconds"] = float(timeout)
            except ValueError:
                logger.warning(
                    "KERNOS_INTEGRATION_TIMEOUT_SECONDS=%r is not a "
                    "float; ignoring", timeout,
                )
        retries = os.getenv("KERNOS_INTEGRATION_MAX_RETRIES")
        if retries:
            try:
                env_values["max_retries"] = int(retries)
            except ValueError:
                logger.warning(
                    "KERNOS_INTEGRATION_MAX_RETRIES=%r is not an int; "
                    "ignoring", retries,
                )
        max_iters = os.getenv("KERNOS_INTEGRATION_MAX_ITERATIONS")
        if max_iters:
            try:
                env_values["max_iterations"] = int(max_iters)
            except ValueError:
                logger.warning(
                    "KERNOS_INTEGRATION_MAX_ITERATIONS=%r is not an "
                    "int; ignoring", max_iters,
                )
        # Mirror server.py / FrictionObserver convention: when
        # KERNOS_DATA_DIR is unset, default to "./data" so the
        # integration runner's friction reports land in the same
        # directory the architect and operator already check.
        # Programmatic callers can still pass `data_dir=None` to
        # opt out (used in tests).
        env_values["data_dir"] = os.getenv("KERNOS_DATA_DIR", "./data")
        env_values.update(overrides)
        return cls(**env_values)


class ReadOnlyToolViolation(Exception):
    """Integration tried to call a tool whose gate classification is not read."""


class IntegrationAttemptFailed(Exception):
    """Raised by ``_attempt_synthesis`` when one attempt of the
    integration loop fails. Caught by the public ``run()``'s retry
    harness, which either retries or surfaces a hard system-error
    briefing after exhaustion.

    Carries the attempt-local audit state so the retry harness can
    fold it into either the next attempt's diagnostics or the final
    system-error briefing.
    """

    def __init__(
        self,
        *,
        component: str,
        reason: str,
        iterations: int,
        phase_durations_ms: dict[str, int],
        tools_called: list[str],
        budget_state: "BudgetState",
        chained_error: Exception | None = None,
        iteration_metrics: list[dict] | None = None,
        tool_results: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(f"{component}: {reason}")
        self.component = component
        self.reason = reason
        self.iterations = iterations
        self.phase_durations_ms = dict(phase_durations_ms)
        self.tools_called = list(tools_called)
        self.budget_state = budget_state
        self.chained_error = chained_error
        # Per-iteration breakdown — used by the friction report writer
        # to attribute time across model latency vs tool dispatch vs
        # tool result size. Each entry:
        #   {"iter": N, "model_ms": int, "dispatch_ms": int|None,
        #    "tool_name": str, "tool_result_chars": int|None,
        #    "input_tokens": int, "output_tokens": int}
        self.iteration_metrics = list(iteration_metrics or [])
        # tool_name + serialized result per successful dispatch — used
        # by the system-error / iteration-cap briefings to render a
        # receipt block in the user-facing reply (substrate-grounded
        # next-turn context).
        self.tool_results = list(tool_results or [])


# Callback protocols. Defined as Callable aliases rather than
# typing.Protocol so existing async lambdas plug in cleanly under tests.
ChainCaller = Callable[..., Awaitable[ProviderResponse]]
"""(system, messages, tools, max_tokens, *, tool_choice="auto", ...) → ProviderResponse"""

ReadOnlyToolDispatcher = Callable[
    [str, dict[str, Any], IntegrationInputs],
    Awaitable[dict[str, Any]],
]
"""(tool_id, arguments, inputs) → tool_result_dict"""

AuditEmitter = Callable[[dict[str, Any]], Awaitable[None]]
"""(audit_entry) → None. Subsequent specs wire this to tool_audit."""


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class IntegrationRunner:
    """Iterative prep loop. Produces one Briefing per `run()` call.

    Opt-in callable per spec acceptance criterion #13: nothing in
    the existing reasoning loop invokes this today. Subsequent
    specs (cohort fan-out runner, presence decoupling, integration
    wiring) compose this with the live system.
    """

    def __init__(
        self,
        *,
        chain_caller: ChainCaller,
        read_only_dispatcher: ReadOnlyToolDispatcher,
        audit_emitter: AuditEmitter,
        config: IntegrationConfig | None = None,
        clock: Callable[[], float] = time.monotonic,
        action_record_drainer: Callable[[], list] | None = None,
    ) -> None:
        self._chain_caller = chain_caller
        self._dispatcher = read_only_dispatcher
        self._audit_emitter = audit_emitter
        self._config = config or IntegrationConfig()
        self._clock = clock
        # RESPONSE-FIDELITY-V1 Batch 1.3 (2026-05-08): callable that
        # returns the per-turn ActionStateRecords accumulated by tool
        # handlers (currently note_this; existing surfaces migrate in
        # Batch 2 onward). Drained once at finalize time and stored on
        # Briefing.audit_trace.action_state_records so the renderer can
        # consume the structured envelope. None when no drainer is
        # wired (tests / library-only use); production wiring threads
        # ReasoningService.drain_action_records here.
        self._action_record_drainer = action_record_drainer

    async def run(self, inputs: IntegrationInputs) -> Briefing:
        """Public entry. Retries synthesis up to ``max_retries`` times;
        on exhaustion, surfaces a hard system-error briefing whose
        directive instructs presence to report the failure transparently
        rather than apologize for "limited context".

        Safety-degraded turns (any required safety cohort failed) skip
        the retry harness entirely and Defer immediately — that's a
        real safety failure, not a synthesis hiccup; retrying would be
        wrong.
        """
        run_id = inputs.integration_run_id or _new_run_id()
        inputs = _with_run_id(inputs, run_id)
        cohort_refs = tuple(co.cohort_run_id for co in inputs.cohort_outputs)

        # Safety-degraded turns: the iteration loop still runs (the
        # system prompt carries safety-preamble guidance and the model
        # is expected to produce a Defer or ConstrainedResponse). Only
        # if synthesis fails entirely do we route to the safety
        # _safety_degraded_defer fallback. This preserves the prior
        # contract (tested in test_integration_safety_policy.py) where
        # the prompt guides the model on safety-degraded turns.
        last_failure: IntegrationAttemptFailed | None = None
        # Full attempt history — passed to the friction report writer so
        # operators can see whether attempts got progressively faster/
        # slower, or hit the same bottleneck repeatedly.
        attempts_history: list[IntegrationAttemptFailed] = []
        for attempt in range(1, self._config.max_retries + 1):
            try:
                return await self._attempt_synthesis(
                    inputs=inputs,
                    cohort_refs=cohort_refs,
                    attempt=attempt,
                    # INTEGRATION-RETRY-WITH-FEEDBACK-V1: pass all
                    # prior failures so the next attempt's prompt
                    # tells the model what to address. Empty tuple
                    # on attempt 1.
                    prior_attempt_failures=tuple(attempts_history),
                )
            except IntegrationAttemptFailed as exc:
                last_failure = exc
                attempts_history.append(exc)
                logger.warning(
                    "INTEGRATION_ATTEMPT_FAILED: attempt=%d/%d "
                    "component=%s reason=%s iterations=%d "
                    "tools_called=%s",
                    attempt, self._config.max_retries,
                    exc.component, exc.reason,
                    exc.iterations, exc.tools_called,
                )
                await self._emit_attempt_audit(
                    inputs=inputs,
                    cohort_refs=cohort_refs,
                    attempt=attempt,
                    failure=exc,
                )
                if attempt < self._config.max_retries:
                    if self._config.retry_backoff_seconds > 0:
                        import asyncio as _asyncio
                        await _asyncio.sleep(
                            self._config.retry_backoff_seconds,
                        )
                    continue

        # All attempts exhausted.
        assert last_failure is not None  # max_retries >= 1 by contract

        # Safety-degraded route: if the safety cohorts failed, the
        # post-exhaustion fallback is a Defer briefing rather than a
        # generic system-error. Real safety failures must Defer, not
        # respond_only.
        if inputs.required_safety_cohort_failures:
            logger.error(
                "INTEGRATION_SYNTHESIS_FAILED_SAFETY_DEGRADED: "
                "%d attempts exhausted with required_safety failures=%s",
                self._config.max_retries,
                inputs.required_safety_cohort_failures,
            )
            return await self._safety_degraded_defer(
                inputs=inputs,
                cohort_refs=cohort_refs,
                tools_called=last_failure.tools_called,
                iterations=last_failure.iterations,
                phase_durations_ms=last_failure.phase_durations_ms,
                budget_state=last_failure.budget_state,
                notes=(
                    f"safety-degraded after {self._config.max_retries} "
                    f"attempts; final-component={last_failure.component}; "
                    f"reason={last_failure.reason}"
                ),
                error=last_failure.reason,
            )

        logger.error(
            "INTEGRATION_SYNTHESIS_FAILED: %d attempts exhausted, "
            "final_component=%s final_reason=%s",
            self._config.max_retries,
            last_failure.component,
            last_failure.reason,
        )
        # Friction report — operator surfacing per founder directive
        # 2026-05-07: timeouts/exhaustion drop a self-contained markdown
        # report into data/diagnostics/friction/, mirroring the existing
        # FrictionObserver pattern, so the architect sees the failure
        # at session start.
        try:
            self._write_integration_friction_report(
                inputs=inputs,
                attempts_history=attempts_history,
                last_failure=last_failure,
            )
        except Exception:
            # Friction-report writing is best-effort — never let it
            # mask the actual system-error briefing the user needs.
            logger.exception("integration-friction report write failed")

        return await self._emit_system_error(
            inputs=inputs,
            cohort_refs=cohort_refs,
            attempts=self._config.max_retries,
            last_failure=last_failure,
        )

    async def _attempt_synthesis(
        self,
        *,
        inputs: IntegrationInputs,
        cohort_refs: tuple[str, ...],
        attempt: int,
        prior_attempt_failures: tuple["IntegrationAttemptFailed", ...] = (),
    ) -> Briefing:
        """One synthesis attempt. Returns a successful Briefing or
        raises :class:`IntegrationAttemptFailed`. Each attempt builds
        fresh chain state so the model gets a clean shot.

        INTEGRATION-RETRY-WITH-FEEDBACK-V1 (2026-05-25):
        ``prior_attempt_failures`` carries the failure summaries from
        all previously-failed attempts in the same synthesis. The
        prompt assembly weaves those into a "your prior attempts
        failed because X; address this now" block so the model has
        actionable feedback. Without this, retries were wasting
        budget by replaying identical prompts and hitting identical
        validation errors (3x ProposeTool.reason failures observed
        in production today). Empty tuple == first attempt; the
        block is suppressed.
        """
        start = self._clock()
        tools_called: list[str] = []
        # INTEGRATION-RENDERER-RESULT-FORWARD-V1: capture per-call
        # tool_name + serialized result alongside tools_called so the
        # renderer can consume the integration model's tool results
        # without re-dispatching the same reads. Empty if no successful
        # dispatches happened this attempt.
        tool_results: list[dict[str, str]] = []
        phase_durations_ms: dict[str, int] = {}
        # Per-iteration metric records — one entry per iteration, even
        # if the iteration didn't reach the dispatch phase. Used to
        # attribute time across model latency, tool dispatch, and tool
        # result size in the post-exhaustion friction report.
        iteration_metrics: list[dict] = []

        # Section 4a: Collect phase.
        collect_started = self._clock()
        system_prompt = build_system_prompt()
        chain_messages = self._build_initial_messages(
            inputs, prior_attempt_failures=prior_attempt_failures,
        )
        integration_tools = self._build_integration_tools(inputs.surfaced_tools)
        phase_durations_ms["collect"] = _ms_since(collect_started, self._clock)

        iterations = 0
        try:
            while True:
                iterations += 1

                # Section 4c: max_iterations guardrail.
                if iterations > self._config.max_iterations:
                    raise IntegrationAttemptFailed(
                        component="max_iterations",
                        reason=(
                            f"max_iterations exhausted "
                            f"({self._config.max_iterations})"
                        ),
                        iterations=iterations - 1,
                        phase_durations_ms=phase_durations_ms,
                        tools_called=tools_called,
                        budget_state=BudgetState(iterations_hit_limit=True),
                        iteration_metrics=iteration_metrics,
                        tool_results=tool_results,
                    )

                # Section 4c: integration_timeout guardrail.
                # `integration_timeout_seconds == 0` is the explicit
                # opt-in disable sentinel; skip the wall-clock check
                # so meaningful work can run as long as it needs.
                if (
                    self._config.integration_timeout_seconds > 0
                    and self._clock() - start
                    > self._config.integration_timeout_seconds
                ):
                    raise IntegrationAttemptFailed(
                        component="integration_timeout",
                        reason=(
                            f"integration_timeout exceeded "
                            f"({self._config.integration_timeout_seconds}s)"
                        ),
                        iterations=iterations - 1,
                        phase_durations_ms=phase_durations_ms,
                        tools_called=tools_called,
                        budget_state=BudgetState(timeout_hit_limit=True),
                        iteration_metrics=iteration_metrics,
                        tool_results=tool_results,
                    )

                # Sections 4a (Integrate / Decide).
                iter_started = self._clock()
                response = await self._chain_caller(
                    system_prompt,
                    chain_messages,
                    integration_tools,
                    self._config.max_integration_tokens,
                    tool_choice="required",
                )
                model_ms = _ms_since(iter_started, self._clock)
                phase_durations_ms[f"integrate_iter_{iterations}"] = model_ms

                # Capture token counts where the provider response
                # exposes them — used by the friction report to compare
                # input-token bloat vs model latency.
                input_tokens = getattr(response, "input_tokens", 0) or 0
                output_tokens = getattr(response, "output_tokens", 0) or 0

                iter_record: dict = {
                    "iter": iterations,
                    "model_ms": model_ms,
                    "dispatch_ms": None,
                    "tool_name": "",
                    "tool_result_chars": None,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                }
                iteration_metrics.append(iter_record)

                tool_uses = [
                    b for b in response.content if b.type == "tool_use"
                ]
                if not tool_uses:
                    # NO_TOOL_USE DIAGNOSTIC (2026-06-02): capture exactly what
                    # the model returned so this failure is root-caused, not
                    # guessed — block types, the text it said instead of
                    # calling a tool, the stop reason, and which tools were
                    # even offered (an empty list = a synthesis-config bug, not
                    # model behavior).
                    try:
                        _content = response.content or []
                        _block_types = [getattr(b, "type", "?") for b in _content]
                        _text_preview = next(
                            (
                                (getattr(b, "text", "") or "")[:400]
                                for b in _content
                                if getattr(b, "type", "") == "text"
                            ),
                            "",
                        )
                        _offered = []
                        for t in (integration_tools or []):
                            _n = (
                                t.get("name") if isinstance(t, dict)
                                else getattr(t, "name", "?")
                            )
                            _offered.append(_n)
                        logger.error(
                            "NO_TOOL_USE_DIAG: iter=%d n_blocks=%d block_types=%s "
                            "stop_reason=%s tools_offered=%s text_preview=%r",
                            iterations, len(_content), _block_types,
                            getattr(response, "stop_reason", None)
                            or getattr(response, "finish_reason", None),
                            _offered, _text_preview,
                        )
                    except Exception as _diag_exc:
                        logger.error("NO_TOOL_USE_DIAG_FAILED: %s", _diag_exc)
                    raise IntegrationAttemptFailed(
                        component="no_tool_use",
                        reason=(
                            "model produced no tool_use block; cannot "
                            "finalize"
                        ),
                        iterations=iterations,
                        phase_durations_ms=phase_durations_ms,
                        tools_called=tools_called,
                        budget_state=BudgetState(),
                        iteration_metrics=iteration_metrics,
                        tool_results=tool_results,
                    )

                tool_use = tool_uses[0]

                if tool_use.name == FINALIZE_TOOL_NAME:
                    # Section 4a: Brief phase.
                    finalize_started = self._clock()
                    cohort_entries_capped = (
                        len(inputs.cohort_outputs)
                        > self._config.max_summarized_cohort_entries
                    )
                    briefing = self._finalize(
                        inputs=inputs,
                        tool_input=dict(tool_use.input or {}),
                        cohort_refs=cohort_refs,
                        tools_called=tools_called,
                        tool_results=tool_results,
                        iterations=iterations,
                        phase_durations_ms=phase_durations_ms,
                        cohort_entries_capped=cohort_entries_capped,
                    )
                    phase_durations_ms["brief"] = _ms_since(
                        finalize_started, self._clock
                    )
                    await self._emit_audit(briefing, success=True, error="")
                    return briefing

                # Section 4b: read-only enforcement.
                self._enforce_read_only(tool_use.name, inputs.surfaced_tools)
                dispatch_started = self._clock()
                tool_result = await self._dispatcher(
                    tool_use.name, dict(tool_use.input or {}), inputs
                )
                dispatch_ms = _ms_since(dispatch_started, self._clock)
                phase_durations_ms[f"dispatch_iter_{iterations}"] = dispatch_ms

                # Stamp the per-iteration record now that dispatch has
                # landed. tool_result_chars measures the serialised
                # payload size that flows back into the next iteration's
                # context — useful for spotting payload-bloat-driven
                # timeouts.
                serialised_result = _serialise_tool_result(tool_result)
                iter_record["dispatch_ms"] = dispatch_ms
                iter_record["tool_name"] = str(tool_use.name)
                iter_record["tool_result_chars"] = len(serialised_result)

                invocation_ref = (
                    tool_result.get("invocation_id")
                    if isinstance(tool_result, dict)
                    else None
                ) or f"{tool_use.name}:iter{iterations}"
                tools_called.append(str(invocation_ref))
                # INTEGRATION-RENDERER-RESULT-FORWARD-V1: capture for
                # forwarding to the renderer. Tool name + serialized
                # result; renderer consumes via AuditTrace.tool_results
                # _during_prep so the model already has the content
                # the integration model fetched and doesn't re-call
                # the read tool just to render it back to the user.
                tool_results.append({
                    "tool_name": str(tool_use.name),
                    "result": serialised_result,
                })

                # See full original comment above; orphan tool_use
                # filter prevents Codex's Responses API from rejecting
                # the next call with "No tool output found for function
                # call <id>" when the model emits multiple tool_use
                # blocks but the runner only dispatches the first.
                assistant_content: list[dict] = []
                for b in response.content:
                    btype = getattr(b, "type", None)
                    if btype == "tool_use" and getattr(b, "id", "") != tool_use.id:
                        continue
                    assistant_content.append(_block_to_api_dict(b))
                chain_messages.append(
                    {
                        "role": "assistant",
                        "content": assistant_content,
                    }
                )
                chain_messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_use.id or "",
                                "content": serialised_result,
                            }
                        ],
                    }
                )
        except IntegrationAttemptFailed:
            raise
        except ReadOnlyToolViolation as exc:
            raise IntegrationAttemptFailed(
                component="read_only_violation",
                reason=str(exc),
                iterations=iterations,
                phase_durations_ms=phase_durations_ms,
                tools_called=tools_called,
                budget_state=BudgetState(),
                chained_error=exc,
                iteration_metrics=iteration_metrics,
                tool_results=tool_results,
            )
        except BriefingValidationError as exc:
            raise IntegrationAttemptFailed(
                component="briefing_validation",
                reason=str(exc),
                iterations=iterations,
                phase_durations_ms=phase_durations_ms,
                tools_called=tools_called,
                budget_state=BudgetState(),
                chained_error=exc,
                iteration_metrics=iteration_metrics,
                tool_results=tool_results,
            )
        except Exception as exc:  # pragma: no cover - guard rail
            logger.exception("Integration runner unexpected error")
            raise IntegrationAttemptFailed(
                component="unexpected_error",
                reason=f"{type(exc).__name__}: {exc}",
                iterations=iterations,
                phase_durations_ms=phase_durations_ms,
                tools_called=tools_called,
                budget_state=BudgetState(),
                chained_error=exc,
                iteration_metrics=iteration_metrics,
                tool_results=tool_results,
            )

    # ----- prompt + tool list assembly -----

    def _build_initial_messages(
        self,
        inputs: IntegrationInputs,
        *,
        prior_attempt_failures: tuple["IntegrationAttemptFailed", ...] = (),
    ) -> list[dict[str, Any]]:
        thread_text = _render_conversation_thread(inputs.conversation_thread)
        cohort_block = _render_cohort_outputs(
            inputs.cohort_outputs,
            cap=self._config.max_summarized_cohort_entries,
        )
        surfaced_block = _render_surfaced_tools(inputs.surfaced_tools)
        spaces_block = _render_context_spaces(inputs.active_context_spaces)

        # INTEGRATION-RETRY-WITH-FEEDBACK-V1 (2026-05-25): when this
        # is a retry attempt, prepend a block summarizing why prior
        # attempts failed so the model has actionable feedback rather
        # than replaying the same prompt blind. Without this, retries
        # observed in production wasted budget on identical 3x
        # failures (e.g. all 3 attempts hitting the same
        # ProposeTool.reason validation error).
        retry_block = ""
        if prior_attempt_failures:
            lines: list[str] = [
                "<prior_attempt_failures>",
                (
                    f"You have already made {len(prior_attempt_failures)} "
                    "failed attempt(s) at this synthesis. Each failure "
                    "is listed below with the substrate's reason. "
                    "Address these specifically in your next briefing — "
                    "do not repeat the same mistake."
                ),
                "",
            ]
            for i, failure in enumerate(prior_attempt_failures, start=1):
                lines.append(
                    f"Attempt {i} failed at component "
                    f"'{failure.component}': {failure.reason}"
                )
            lines.append("</prior_attempt_failures>\n")
            retry_block = "\n".join(lines) + "\n"

        body = (
            f"{retry_block}"
            "<conversation_thread>\n"
            f"{thread_text}\n"
            "</conversation_thread>\n\n"
            "<cohort_outputs>\n"
            f"{cohort_block}\n"
            "</cohort_outputs>\n\n"
            "<surfaced_tools>\n"
            f"{surfaced_block}\n"
            "</surfaced_tools>\n\n"
            "<active_context_spaces>\n"
            f"{spaces_block}\n"
            "</active_context_spaces>\n\n"
            f"<user_message>\n{inputs.user_message}\n</user_message>\n\n"
            f"{_render_safety_degradation(inputs.required_safety_cohort_failures)}"
            "Run the integration loop. Call read-only tools if you "
            "need more information. When ready, call "
            f"{FINALIZE_TOOL_NAME} with the structured briefing."
        )
        return [{"role": "user", "content": body}]

    def _build_integration_tools(
        self, surfaced: tuple[SurfacedTool, ...]
    ) -> list[dict[str, Any]]:
        """Tools exposed to the integration model: read-only retrievals
        plus the synthetic finalize tool. Non-read tools are filtered
        out at this surface so the model never sees them — defence in
        depth alongside the dispatch-time enforcement (Section 4b)."""
        tools: list[dict[str, Any]] = []
        for st in surfaced:
            if st.gate_classification != "read":
                continue
            tools.append(
                {
                    "name": st.tool_id,
                    "description": (
                        f"{st.description}\n[surfaced because: "
                        f"{st.surfacing_rationale or 'unspecified'}]"
                    ),
                    "input_schema": dict(st.input_schema),
                }
            )
        tools.append(dict(FINALIZE_TOOL_SCHEMA))
        return tools

    def _enforce_read_only(
        self, tool_name: str, surfaced: tuple[SurfacedTool, ...]
    ) -> None:
        for st in surfaced:
            if st.tool_id == tool_name:
                if st.gate_classification != "read":
                    raise ReadOnlyToolViolation(
                        f"integration attempted to call non-read tool "
                        f"{tool_name!r} (gate_classification="
                        f"{st.gate_classification!r}); only read-only tools "
                        f"are allowed in the integration prep loop"
                    )
                return
        raise ReadOnlyToolViolation(
            f"integration attempted to call tool {tool_name!r} which was "
            f"not surfaced this turn"
        )

    # ----- finalize / fail-soft -----

    def _finalize(
        self,
        *,
        inputs: IntegrationInputs,
        tool_input: dict[str, Any],
        cohort_refs: tuple[str, ...],
        tools_called: list[str],
        tool_results: list[dict[str, str]],
        iterations: int,
        phase_durations_ms: dict[str, int],
        cohort_entries_capped: bool,
    ) -> Briefing:
        relevant = tuple(
            ContextItem.from_dict(item)
            for item in (tool_input.get("relevant_context") or [])
        )
        filtered_raw = tuple(
            FilteredItem.from_dict(item)
            for item in (tool_input.get("filtered_context") or [])
        )
        filtered_capped = (
            len(filtered_raw) > self._config.max_filtered_entries
        )
        filtered = filtered_raw[: self._config.max_filtered_entries]

        decided = decided_action_from_dict(tool_input.get("decided_action") or {})

        # PDI the design review edit: action-shape decided_actions REQUIRE an
        # explicit ActionEnvelope on the briefing. Parse from
        # tool_input when the kind warrants it; absent envelope on an
        # action-shape decision is a structural violation surfaced
        # via the same fail-soft path as malformed presence_directive.
        envelope: ActionEnvelope | None = None
        envelope_raw = tool_input.get("action_envelope")
        if action_kind_requires_envelope(decided.kind):
            if not isinstance(envelope_raw, dict):
                raise BriefingValidationError(
                    f"action_envelope is required when decided_action.kind "
                    f"is {decided.kind.value!r}; integration must construct "
                    f"a well-formed envelope or fall back to "
                    f"clarification_needed"
                )
            envelope = ActionEnvelope.from_dict(envelope_raw)
        elif isinstance(envelope_raw, dict):
            # Non-action kinds must NOT carry an envelope; the briefing
            # validator will reject. Surface clearly here so the
            # mismatch is obvious in the audit trail.
            raise BriefingValidationError(
                f"action_envelope must be omitted when decided_action.kind "
                f"is {decided.kind.value!r} (no dispatch to constrain)"
            )

        directive = str(tool_input.get("presence_directive") or "").strip()
        if not directive:
            raise BriefingValidationError(
                "model emitted briefing with empty presence_directive"
            )

        # Section 3: redaction post-check. The runner refuses to ship a
        # briefing whose text fields contain content from a Restricted
        # CohortOutput. Integration is supposed to translate restricted
        # material into behavioral instruction; if it didn't, fail
        # rather than leak.
        #
        # NOTE — earlier attempt (fd1d725 / f4671e6) extended this scan
        # to forwarded tool_results. Reverted 2026-05-08 after repeated
        # field false-positives: tool results come from the agent's own
        # dispatched tools, scoped to the agent's permissions, and are
        # not borrowed from a Restricted cohort. The substring guard
        # firing on overlap between (e.g.) inspect_state output and a
        # Restricted cohort's payload was reading textual coincidence
        # as a leak. The right defense for "tools shouldn't return
        # cross-member restricted content" is at the tool's dispatch
        # boundary (member-scoping in the tool itself), not finalize-
        # time substring scan.
        self._check_redaction_invariant(
            relevant=relevant,
            filtered=filtered,
            directive=directive,
            cohort_outputs=inputs.cohort_outputs,
        )

        # COHORT-ADAPT-COVENANT safety policy: when one or more
        # required + safety_class cohorts failed, the briefing's
        # decided_action MUST be defer or constrained_response.
        # respond_only / execute_tool / propose_tool are forbidden
        # (the design review's load-bearing input: safety-degraded fail-soft
        # must never be respond_only — and the same constraint
        # applies on the success path post-finalize). If the model
        # disobeyed, raise so the outer try/except routes to the
        # safety-degraded fail-soft path.
        if inputs.required_safety_cohort_failures:
            if not isinstance(decided, (Defer, ConstrainedResponse)):
                raise BriefingValidationError(
                    "safety policy violation: required+safety_class cohort "
                    f"failed ({list(inputs.required_safety_cohort_failures)}); "
                    f"decided_action must be defer or constrained_response, "
                    f"got {type(decided).__name__}"
                )

        audit_trace = AuditTrace(
            cohort_outputs=cohort_refs,
            tools_called_during_prep=tuple(tools_called),
            tool_results_during_prep=tuple(tool_results),
            action_state_records=tuple(self._drain_action_records()),
            iterations_used=iterations,
            budget_state=BudgetState(
                cohort_entries_hit_limit=cohort_entries_capped,
                filtered_entries_hit_limit=filtered_capped,
            ),
            fail_soft_engaged=False,
            phase_durations_ms=dict(phase_durations_ms),
            notes="",
        )

        return Briefing(
            relevant_context=relevant,
            filtered_context=tuple(filtered),
            decided_action=decided,
            presence_directive=directive,
            audit_trace=audit_trace,
            turn_id=inputs.turn_id,
            integration_run_id=inputs.integration_run_id,
            action_envelope=envelope,
            cognitive_context=inputs.cognitive_context,
            # INTEGRATION-CAPABILITY-FIRST-V1 Batch 2 Fold 6:
            # turn-context identifiers thread through to step
            # dispatcher's ToolExecutionInputs construction.
            instance_id=inputs.instance_id,
            member_id=inputs.member_id,
            space_id=inputs.space_id,
            user_message=inputs.user_message,
            recent_messages=tuple(inputs.conversation_thread),
        )

    def _drain_action_records(self) -> list:
        """Drain accumulated ActionStateRecords from the configured
        drainer. Returns empty list when no drainer wired or when the
        drainer raises (defensive — drainer failure shouldn't fail
        the whole briefing).
        """
        if self._action_record_drainer is None:
            return []
        try:
            return list(self._action_record_drainer())
        except Exception as exc:
            logger.warning(
                "INTEGRATION_ACTION_RECORD_DRAIN_FAILED: %s", exc,
            )
            return []

    def _check_redaction_invariant(
        self,
        *,
        relevant: tuple[ContextItem, ...],
        filtered: tuple[FilteredItem, ...],
        directive: str,
        cohort_outputs: tuple[CohortOutput, ...],
    ) -> None:
        """Refuse a briefing whose text quotes Restricted output content.

        The check is a substring scan — coarse but explicit. It guards
        against the most direct leak path (integration model
        accidentally copying a restricted cohort's payload string into
        a summary or directive). Integration is the policy layer; the
        runner is the enforcement layer of last resort.

        Scope: only model-authored text fields (relevant.summary,
        filtered.reason_filtered, directive). Forwarded tool_results
        are deliberately NOT scanned — they come from the agent's own
        dispatched tools running in the agent's own scope, not from
        cohort content the integration model could choose to quote.
        Cross-member tool-result content protection is the tool's own
        dispatch-time responsibility (member-scoping at the tool
        boundary), not a finalize-time substring scan.
        """
        restricted_payloads: list[str] = []
        for co in cohort_outputs:
            if not isinstance(co.visibility, Restricted):
                continue
            for value in _flatten_strings(co.output):
                stripped = value.strip()
                # Skip very short tokens (false positive risk on
                # common words). Restricted leak typically is a
                # phrase from the secret payload, not a 4-letter word.
                if len(stripped) >= 12:
                    restricted_payloads.append(stripped)

        if not restricted_payloads:
            return

        combined_text = " ".join(
            [item.summary for item in relevant]
            + [item.reason_filtered for item in filtered]
            + [directive]
        )
        for snippet in restricted_payloads:
            if snippet in combined_text:
                raise BriefingValidationError(
                    "redaction invariant violated: briefing text contains "
                    "content from a Restricted CohortOutput. Integration "
                    "must translate restricted material into behavioral "
                    "instruction before populating briefing fields."
                )

    async def _safety_degraded_defer(
        self,
        *,
        inputs: IntegrationInputs,
        cohort_refs: tuple[str, ...],
        tools_called: list[str],
        iterations: int,
        phase_durations_ms: dict[str, int],
        budget_state: BudgetState,
        notes: str,
        error: str = "",
    ) -> Briefing:
        """Safety-degraded path. Required + safety-class cohorts failed,
        so the turn defers immediately rather than retrying. Per the
        design review's COHORT-ADAPT-COVENANT input: safety-degraded
        fallback MUST be a Defer, never a respond_only.
        """
        decided_action = Defer(
            reason=(
                "required safety cohorts failed and integration could "
                "not produce a valid briefing; cannot proceed at full "
                "strength without safety verification"
            ),
            follow_up_signal=(
                f"will retry once required safety cohorts recover "
                f"({', '.join(inputs.required_safety_cohort_failures)})"
            ),
        )
        presence_directive = (
            "acknowledge the user briefly; signal that this turn must "
            "be deferred until safety verification is possible. Do not "
            "execute or propose tool calls."
        )
        safety_budget = BudgetState(
            iterations_hit_limit=budget_state.iterations_hit_limit,
            timeout_hit_limit=budget_state.timeout_hit_limit,
            cohort_entries_hit_limit=budget_state.cohort_entries_hit_limit,
            filtered_entries_hit_limit=budget_state.filtered_entries_hit_limit,
            tokens_hit_limit=budget_state.tokens_hit_limit,
            required_cohort_failed=True,
            required_safety_cohort_failed=True,
            cohort_fan_out_global_timeout=budget_state.cohort_fan_out_global_timeout,
        )
        briefing = Briefing(
            relevant_context=(),
            filtered_context=(),
            decided_action=decided_action,
            presence_directive=presence_directive,
            audit_trace=AuditTrace(
                cohort_outputs=cohort_refs,
                tools_called_during_prep=tuple(tools_called),
                iterations_used=iterations,
                budget_state=safety_budget,
                fail_soft_engaged=True,
                phase_durations_ms=dict(phase_durations_ms),
                notes=f"safety-degraded defer: {notes}",
            ),
            turn_id=inputs.turn_id,
            integration_run_id=inputs.integration_run_id,
            cognitive_context=inputs.cognitive_context,
            instance_id=inputs.instance_id,
            member_id=inputs.member_id,
            space_id=inputs.space_id,
        )
        await self._emit_audit(
            briefing, success=False, error=error or notes,
        )
        return briefing

    async def _emit_attempt_audit(
        self,
        *,
        inputs: IntegrationInputs,
        cohort_refs: tuple[str, ...],
        attempt: int,
        failure: IntegrationAttemptFailed,
    ) -> None:
        """Emit an ``integration.retry`` audit record per failed attempt.

        Distinguished from ``integration.briefing`` so operators can
        scan the audit trail and immediately see retry events. Each
        retry's component, reason, and per-iteration durations are
        captured for post-mortem.
        """
        try:
            await self._audit_emitter(
                {
                    "audit_category": "integration.retry",
                    "turn_id": inputs.turn_id,
                    "integration_run_id": inputs.integration_run_id,
                    "instance_id": inputs.instance_id,
                    "member_id": inputs.member_id,
                    "space_id": inputs.space_id,
                    "attempt": attempt,
                    "max_retries": self._config.max_retries,
                    "component": failure.component,
                    "reason": failure.reason,
                    "iterations": failure.iterations,
                    "tools_called": list(failure.tools_called),
                    "phase_durations_ms": dict(failure.phase_durations_ms),
                    "cohort_outputs": list(cohort_refs),
                    "success": False,
                    "error": (
                        f"{failure.component}: {failure.reason}"
                    ),
                }
            )
        except Exception:  # pragma: no cover
            logger.exception("integration.retry audit emit failed")

    async def _emit_system_error(
        self,
        *,
        inputs: IntegrationInputs,
        cohort_refs: tuple[str, ...],
        attempts: int,
        last_failure: IntegrationAttemptFailed,
    ) -> Briefing:
        """All retry attempts exhausted. Emit a hard system-error
        briefing whose directive instructs presence to surface the
        failure transparently — no apology fallback, no pretending to
        answer the user's question.

        Replaces the prior ``minimal_fail_soft_briefing`` codepath.
        Architectural shift (PHASE-1-WIPE-VERIFICATION 2026-05-07,
        founder direction): "I want to fix the failure, not just
        improve the shape of the failing." Loud, attributable,
        actionable for the operator.

        Special case (ITERATION-CAP-PROMPT 2026-05-07): when the
        final-attempt component is ``max_iterations``, dispatch to
        :func:`iteration_cap_briefing` instead — that failure mode is
        recoverable by user choice (continue / always continue /
        terminate) rather than purely operator-investigable.
        """
        from kernos.kernel.integration.briefing import (
            iteration_cap_briefing,
            system_error_briefing,
        )

        # RESPONSE-FIDELITY-V1 Batch 1.3: drain ActionStateRecords on
        # failure paths too. Writes that succeeded before exhaustion
        # should still surface in the briefing so the next-turn agent
        # sees what actually happened, even when the integration loop
        # itself failed.
        action_records = tuple(self._drain_action_records())

        if last_failure.component == "max_iterations":
            briefing = iteration_cap_briefing(
                turn_id=inputs.turn_id,
                integration_run_id=inputs.integration_run_id,
                attempts=attempts,
                cap=self._config.max_iterations,
                cohort_refs=cohort_refs,
                tools_called=last_failure.tools_called,
                tool_results=last_failure.tool_results,
                action_state_records=action_records,
                iterations=last_failure.iterations,
                phase_durations_ms=last_failure.phase_durations_ms,
                budget_state=last_failure.budget_state,
                cognitive_context=inputs.cognitive_context,
                instance_id=inputs.instance_id,
                member_id=inputs.member_id,
                space_id=inputs.space_id,
            )
            audit_error = (
                f"iteration-cap-prompt after {attempts} attempts; "
                f"cap={self._config.max_iterations}"
            )
        else:
            briefing = system_error_briefing(
                turn_id=inputs.turn_id,
                integration_run_id=inputs.integration_run_id,
                attempts=attempts,
                component=last_failure.component,
                reason=last_failure.reason,
                cohort_refs=cohort_refs,
                tools_called=last_failure.tools_called,
                tool_results=last_failure.tool_results,
                action_state_records=action_records,
                iterations=last_failure.iterations,
                phase_durations_ms=last_failure.phase_durations_ms,
                budget_state=last_failure.budget_state,
                cognitive_context=inputs.cognitive_context,
                instance_id=inputs.instance_id,
                member_id=inputs.member_id,
                space_id=inputs.space_id,
            )
            audit_error = (
                f"system-error after {attempts} attempts; "
                f"{last_failure.component}: {last_failure.reason}"
            )

        await self._emit_audit(
            briefing,
            success=False,
            error=audit_error,
        )
        return briefing

    async def _emit_audit(
        self, briefing: Briefing, *, success: bool, error: str
    ) -> None:
        # Section 6: integration.briefing audit category. Member-
        # scoped, ephemeral, references-not-dumps. Subsequent specs
        # wire this into the existing tool_audit substrate; this
        # spec emits a forward-compatible record for testability.
        try:
            await self._audit_emitter(
                {
                    "audit_category": "integration.briefing",
                    "briefing": briefing.to_dict(),
                    "success": success,
                    "error": error,
                }
            )
        except Exception:  # pragma: no cover
            # Audit emission is best-effort, in line with the kernel
            # convention. Never fail the user's turn on an audit
            # write.
            logger.exception("integration.briefing audit emit failed")

    def _write_integration_friction_report(
        self,
        *,
        inputs: IntegrationInputs,
        attempts_history: list[IntegrationAttemptFailed],
        last_failure: IntegrationAttemptFailed,
    ) -> None:
        """Drop a markdown friction report when retries exhaust.

        Mirrors the FrictionObserver convention (filename pattern,
        directory, structure) so existing surfacing tooling (the
        ``/debug friction`` slash command, session-start friction
        scan) picks the report up automatically. No-op when no
        ``data_dir`` is configured.
        """
        if not self._config.data_dir:
            return
        from datetime import datetime, timezone
        from pathlib import Path

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        # Short uniqueness suffix prevents filename collisions when two
        # exhausted runs land in the same second with the same
        # component (which can happen during incident bursts and is
        # exactly when the diagnostic value matters most).
        unique = uuid.uuid4().hex[:8]
        friction_dir = Path(self._config.data_dir) / "diagnostics" / "friction"
        friction_dir.mkdir(parents=True, exist_ok=True)

        # Component label is constrained — same reasoning as
        # _safe_component in briefing.py: keep raw exception text out
        # of file paths (which may end up in shared environments).
        from kernos.kernel.integration.briefing import _safe_component
        safe_component = _safe_component(last_failure.component)
        filename = (
            f"FRICTION_{ts}_{unique}_INTEGRATION_"
            f"{safe_component.upper()}.md"
        )
        filepath = friction_dir / filename

        # Truncate user message to a sane preview; full text lives in
        # the conversation log if needed for forensics.
        user_msg = (inputs.user_message or "")[:500]
        if len(inputs.user_message or "") > 500:
            user_msg += "…"

        lines: list[str] = []
        lines.append(f"# Friction Report: INTEGRATION_{safe_component.upper()}")
        lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
        lines.append("")
        lines.append("## Description")
        lines.append(
            f"Integration synthesis failed after "
            f"{len(attempts_history)} retry attempts. Final attempt's "
            f"component was `{safe_component}`. The retry harness "
            f"surfaced a hard system-error to the user; this report "
            f"captures per-iteration timing so the bottleneck "
            f"(model latency / tool dispatch / payload size) can be "
            f"attributed."
        )
        lines.append("")
        lines.append("## Recommendation: INVESTIGATE")
        lines.append(
            "Review per-iteration metrics below. Likely levers, in "
            "rough order of cost: (1) extend "
            "`KERNOS_INTEGRATION_TIMEOUT_SECONDS` if model+dispatch "
            "wall-time consistently overruns the budget by a small "
            "margin; (2) shrink tool-result payloads if "
            "`tool_result_chars` dominates; (3) parallelize multi-"
            "target retrieval if the model issues several "
            "`request_reference` calls sequentially; (4) swap the "
            "`lightweight` chain to a faster provider if model_ms "
            "dominates and payloads are small."
        )
        lines.append("")
        lines.append("## Context")
        lines.append(f"- instance_id: `{inputs.instance_id}`")
        lines.append(f"- member_id: `{inputs.member_id}`")
        lines.append(f"- space_id: `{inputs.space_id}`")
        lines.append(f"- turn_id: `{inputs.turn_id}`")
        lines.append(f"- integration_run_id: `{inputs.integration_run_id}`")
        lines.append(
            f"- integration_timeout_seconds: "
            f"`{self._config.integration_timeout_seconds}`"
        )
        lines.append(f"- max_iterations: `{self._config.max_iterations}`")
        lines.append(f"- max_retries: `{self._config.max_retries}`")
        lines.append("")
        lines.append("## User message (preview)")
        lines.append("")
        lines.append("```")
        lines.append(user_msg or "(empty)")
        lines.append("```")
        lines.append("")
        lines.append("## Per-attempt breakdown")
        lines.append("")
        for idx, failure in enumerate(attempts_history, start=1):
            lines.append(
                f"### Attempt {idx}/{len(attempts_history)} — "
                f"`{failure.component}`"
            )
            lines.append(f"- iterations: {failure.iterations}")
            lines.append(f"- tools_called: {failure.tools_called}")
            lines.append(
                f"- phase_durations_ms: {failure.phase_durations_ms}"
            )
            if failure.iteration_metrics:
                lines.append("- per-iteration metrics:")
                for m in failure.iteration_metrics:
                    lines.append(
                        f"  - iter {m.get('iter')}: "
                        f"model_ms={m.get('model_ms')}, "
                        f"dispatch_ms={m.get('dispatch_ms')}, "
                        f"tool=`{m.get('tool_name', '')}`, "
                        f"tool_result_chars={m.get('tool_result_chars')}, "
                        f"input_tokens={m.get('input_tokens')}, "
                        f"output_tokens={m.get('output_tokens')}"
                    )
            else:
                lines.append("- per-iteration metrics: (none captured)")
            lines.append("")
        # Aggregate diagnostic — the question the operator needs
        # answered first.
        total_attempts = len(attempts_history)
        all_metrics = [
            m for f in attempts_history for m in f.iteration_metrics
        ]
        if all_metrics:
            sum_model = sum((m.get("model_ms") or 0) for m in all_metrics)
            sum_dispatch = sum(
                (m.get("dispatch_ms") or 0) for m in all_metrics
            )
            max_payload = max(
                (m.get("tool_result_chars") or 0) for m in all_metrics
            )
            tools_observed = sorted({
                m.get("tool_name", "") for m in all_metrics
                if m.get("tool_name")
            })
            lines.append("## Aggregate signals")
            lines.append("")
            lines.append(
                f"- total iterations across all attempts: "
                f"{len(all_metrics)}"
            )
            lines.append(f"- sum model_ms: {sum_model}")
            lines.append(f"- sum dispatch_ms: {sum_dispatch}")
            lines.append(f"- max tool_result_chars: {max_payload}")
            lines.append(f"- tools observed: {tools_observed}")
            if sum_model > sum_dispatch * 2:
                lines.append(
                    "- **Hypothesis:** model latency dominates "
                    "(sum_model >> sum_dispatch). Lever: faster chain "
                    "or smaller input."
                )
            elif sum_dispatch > sum_model * 2:
                lines.append(
                    "- **Hypothesis:** tool dispatch dominates "
                    "(sum_dispatch >> sum_model). Lever: shrink "
                    "results or parallelize."
                )
            else:
                lines.append(
                    "- **Hypothesis:** model and dispatch contribute "
                    "comparably. Lever: extend timeout if both are "
                    "necessary, or bound iteration count."
                )

        try:
            filepath.write_text("\n".join(lines), encoding="utf-8")
            logger.info(
                "INTEGRATION_FRICTION_REPORT: written %s "
                "(component=%s attempts=%d)",
                filepath, safe_component, total_attempts,
            )
        except OSError:
            logger.exception(
                "INTEGRATION_FRICTION_REPORT: write failed for %s",
                filepath,
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_run_id() -> str:
    return f"int-{uuid.uuid4().hex[:12]}"


def _with_run_id(inputs: IntegrationInputs, run_id: str) -> IntegrationInputs:
    if inputs.integration_run_id == run_id:
        return inputs
    return IntegrationInputs(
        user_message=inputs.user_message,
        conversation_thread=inputs.conversation_thread,
        cohort_outputs=inputs.cohort_outputs,
        surfaced_tools=inputs.surfaced_tools,
        active_context_spaces=inputs.active_context_spaces,
        member_id=inputs.member_id,
        instance_id=inputs.instance_id,
        space_id=inputs.space_id,
        turn_id=inputs.turn_id,
        integration_run_id=run_id,
        required_safety_cohort_failures=inputs.required_safety_cohort_failures,
        # COGNITIVE-CONTEXT-V1 C3a: preserve the typed packet across
        # the run-id rebuild. The seam test
        # ``test_real_integration_service_carries_packet_onto_briefing``
        # caught this drop site (Codex C3a-design "not asked, but
        # flagging" pin). Without this line the packet would be
        # silently dropped on every IntegrationRunner.run call —
        # exactly the bug class CCV1 was created to prevent.
        cognitive_context=inputs.cognitive_context,
    )


def _render_safety_degradation(failed_safety_cohorts: tuple[str, ...]) -> str:
    """Render the safety-policy preamble injected into the prompt body.

    Per COHORT-ADAPT-COVENANT Section 2c: when one or more
    required + safety_class cohorts failed, the integration model
    must be told that respond_only / execute_tool / propose_tool
    are forbidden for this turn. The safety constraint is enforced
    structurally on the post-finalize path too — this preamble is
    cooperative guidance so the model doesn't waste tokens
    proposing forbidden actions.
    """
    if not failed_safety_cohorts:
        return ""
    return (
        "<safety_policy>\n"
        f"Required safety_class cohorts failed this turn: "
        f"{', '.join(failed_safety_cohorts)}.\n"
        "Without these cohorts' signals, Kernos cannot verify safety\n"
        "constraints. You MUST produce a briefing whose decided_action\n"
        "is `defer` (preferred) or `constrained_response`. The actions\n"
        "respond_only, execute_tool, and propose_tool are forbidden\n"
        "for this turn. Encode the missing safety signal as the\n"
        "presence_directive's behavioral instruction.\n"
        "</safety_policy>\n\n"
    )


def _ms_since(start: float, clock: Callable[[], float]) -> int:
    return max(0, int((clock() - start) * 1000))


def _block_to_api_dict(block: ContentBlock) -> dict[str, Any]:
    if block.type == "text":
        return {"type": "text", "text": block.text or ""}
    if block.type == "tool_use":
        return {
            "type": "tool_use",
            "id": block.id or "",
            "name": block.name or "",
            "input": block.input or {},
        }
    return {"type": block.type}


def _serialise_tool_result(result: Any) -> str:
    try:
        return json.dumps(result, default=str)
    except Exception:
        return str(result)


def _flatten_strings(value: Any) -> list[str]:
    """Yield string values recursively from a nested structure.

    Used by the redaction-invariant check to scan a Restricted
    CohortOutput's payload for substrings that must not appear
    in briefing text. Numbers, bools, and Nones are skipped.
    """
    out: list[str] = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            out.extend(_flatten_strings(v))
    elif isinstance(value, (list, tuple)):
        for v in value:
            out.extend(_flatten_strings(v))
    return out


def _render_conversation_thread(thread: tuple[dict[str, Any], ...]) -> str:
    if not thread:
        return "(no recent turns)"
    lines = []
    for turn in thread:
        role = turn.get("role", "?")
        content = turn.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        lines.append(f"[{role}] {content}")
    return "\n".join(lines)


def _render_cohort_outputs(
    cohorts: tuple[CohortOutput, ...], *, cap: int
) -> str:
    if not cohorts:
        return "(no cohort outputs this turn)"
    rendered = []
    for co in cohorts[:cap]:
        marker = ""
        if isinstance(co.visibility, Restricted):
            # Restricted cohorts are surfaced to integration so they
            # can shape the decision, but the marker signals to the
            # model that the content must NOT be quoted in the
            # briefing.
            marker = f" (RESTRICTED: {co.visibility.reason})"
        try:
            payload_text = json.dumps(co.output, ensure_ascii=False)
        except Exception:
            payload_text = repr(co.output)
        rendered.append(
            f"- {co.cohort_id}{marker} [run={co.cohort_run_id}]: {payload_text}"
        )
    if len(cohorts) > cap:
        rendered.append(f"... and {len(cohorts) - cap} more (capped)")
    return "\n".join(rendered)


def _render_surfaced_tools(tools: tuple[SurfacedTool, ...]) -> str:
    if not tools:
        return "(no tools surfaced this turn)"
    rendered = []
    for st in tools:
        rationale = st.surfacing_rationale or "unspecified"
        rendered.append(
            f"- {st.tool_id} [{st.gate_classification}] "
            f"(surfaced: {rationale}): {st.description}"
        )
    return "\n".join(rendered)


def _render_context_spaces(spaces: tuple[dict[str, Any], ...]) -> str:
    if not spaces:
        return "(no active context spaces)"
    return "\n".join(f"- {json.dumps(s, ensure_ascii=False)}" for s in spaces)


__all__ = [
    "AuditEmitter",
    "ChainCaller",
    "IntegrationConfig",
    "IntegrationInputs",
    "IntegrationRunner",
    "ReadOnlyToolDispatcher",
    "ReadOnlyToolViolation",
    "SurfacedTool",
    "SURFACING_RATIONALE_CONTEXT_SPACE_PIN",
    "SURFACING_RATIONALE_CREDENTIAL",
    "SURFACING_RATIONALE_GATE_CLASS",
    "SURFACING_RATIONALE_PINNED",
    "SURFACING_RATIONALE_RELEVANCE",
]
