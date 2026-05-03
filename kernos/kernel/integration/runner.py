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
    """

    max_iterations: int = 5
    max_integration_tokens: int = 2048
    integration_timeout_seconds: float = 30.0
    max_summarized_cohort_entries: int = 20
    max_filtered_entries: int = 50
    chain_name: str = "lightweight"


class ReadOnlyToolViolation(Exception):
    """Integration tried to call a tool whose gate classification is not read."""


# Callback protocols. Defined as Callable aliases rather than
# typing.Protocol so existing async lambdas plug in cleanly under tests.
ChainCaller = Callable[
    [str | list[dict], list[dict], list[dict], int],
    Awaitable[ProviderResponse],
]
"""(system, messages, tools, max_tokens) → ProviderResponse"""

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
    ) -> None:
        self._chain_caller = chain_caller
        self._dispatcher = read_only_dispatcher
        self._audit_emitter = audit_emitter
        self._config = config or IntegrationConfig()
        self._clock = clock

    async def run(self, inputs: IntegrationInputs) -> Briefing:
        run_id = inputs.integration_run_id or _new_run_id()
        inputs = _with_run_id(inputs, run_id)
        start = self._clock()
        cohort_refs = tuple(co.cohort_run_id for co in inputs.cohort_outputs)
        tools_called: list[str] = []
        phase_durations_ms: dict[str, int] = {}

        # Section 4a: Collect phase. System prompt is static (per-install
        # configurable). User message bundles the inputs.
        collect_started = self._clock()
        system_prompt = build_system_prompt()
        chain_messages = self._build_initial_messages(inputs)
        integration_tools = self._build_integration_tools(inputs.surfaced_tools)
        phase_durations_ms["collect"] = _ms_since(collect_started, self._clock)

        iterations = 0
        try:
            while True:
                iterations += 1

                # Section 4c: max_iterations guardrail.
                if iterations > self._config.max_iterations:
                    return await self._fail_soft(
                        inputs=inputs,
                        cohort_refs=cohort_refs,
                        tools_called=tools_called,
                        iterations=iterations - 1,
                        phase_durations_ms=phase_durations_ms,
                        budget_state=BudgetState(iterations_hit_limit=True),
                        notes=(
                            f"max_iterations exhausted "
                            f"({self._config.max_iterations})"
                        ),
                    )

                # Section 4c: integration_timeout guardrail.
                if (
                    self._clock() - start
                    > self._config.integration_timeout_seconds
                ):
                    return await self._fail_soft(
                        inputs=inputs,
                        cohort_refs=cohort_refs,
                        tools_called=tools_called,
                        iterations=iterations - 1,
                        phase_durations_ms=phase_durations_ms,
                        budget_state=BudgetState(timeout_hit_limit=True),
                        notes=(
                            f"integration_timeout exceeded "
                            f"({self._config.integration_timeout_seconds}s)"
                        ),
                    )

                # Sections 4a (Integrate / Decide) — one chain call per
                # iteration. The model either calls a read-only tool
                # (continue) or __finalize_briefing__ (terminate).
                iter_started = self._clock()
                response = await self._chain_caller(
                    system_prompt,
                    chain_messages,
                    integration_tools,
                    self._config.max_integration_tokens,
                )
                phase_durations_ms[f"integrate_iter_{iterations}"] = (
                    _ms_since(iter_started, self._clock)
                )

                tool_uses = [
                    b for b in response.content if b.type == "tool_use"
                ]
                if not tool_uses:
                    return await self._fail_soft(
                        inputs=inputs,
                        cohort_refs=cohort_refs,
                        tools_called=tools_called,
                        iterations=iterations,
                        phase_durations_ms=phase_durations_ms,
                        budget_state=BudgetState(),
                        notes=(
                            "model produced no tool_use block; cannot finalize"
                        ),
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
                phase_durations_ms[f"dispatch_iter_{iterations}"] = (
                    _ms_since(dispatch_started, self._clock)
                )

                invocation_ref = (
                    tool_result.get("invocation_id")
                    if isinstance(tool_result, dict)
                    else None
                ) or f"{tool_use.name}:iter{iterations}"
                tools_called.append(str(invocation_ref))

                chain_messages.append(
                    {
                        "role": "assistant",
                        "content": [
                            _block_to_api_dict(b) for b in response.content
                        ],
                    }
                )
                chain_messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_use.id or "",
                                "content": _serialise_tool_result(tool_result),
                            }
                        ],
                    }
                )
        except ReadOnlyToolViolation as exc:
            return await self._fail_soft(
                inputs=inputs,
                cohort_refs=cohort_refs,
                tools_called=tools_called,
                iterations=iterations,
                phase_durations_ms=phase_durations_ms,
                budget_state=BudgetState(),
                notes=f"read-only violation: {exc}",
                error=str(exc),
            )
        except BriefingValidationError as exc:
            return await self._fail_soft(
                inputs=inputs,
                cohort_refs=cohort_refs,
                tools_called=tools_called,
                iterations=iterations,
                phase_durations_ms=phase_durations_ms,
                budget_state=BudgetState(),
                notes=f"briefing validation failed: {exc}",
                error=str(exc),
            )
        except Exception as exc:  # pragma: no cover - guard rail
            logger.exception("Integration runner unexpected error")
            return await self._fail_soft(
                inputs=inputs,
                cohort_refs=cohort_refs,
                tools_called=tools_called,
                iterations=iterations,
                phase_durations_ms=phase_durations_ms,
                budget_state=BudgetState(),
                notes=f"unexpected error: {type(exc).__name__}: {exc}",
                error=str(exc),
            )

    # ----- prompt + tool list assembly -----

    def _build_initial_messages(
        self, inputs: IntegrationInputs
    ) -> list[dict[str, Any]]:
        thread_text = _render_conversation_thread(inputs.conversation_thread)
        cohort_block = _render_cohort_outputs(
            inputs.cohort_outputs,
            cap=self._config.max_summarized_cohort_entries,
        )
        surfaced_block = _render_surfaced_tools(inputs.surfaced_tools)
        spaces_block = _render_context_spaces(inputs.active_context_spaces)

        body = (
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
        )

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
        against the most direct leak path (model accidentally copying
        a restricted cohort's payload string into a summary or
        directive). Integration is the policy layer; the runner is
        the enforcement layer of last resort.
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

    async def _fail_soft(
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
        # Per the design review's load-bearing input on COHORT-ADAPT-COVENANT:
        # safety-degraded fail-soft must NEVER be respond_only.
        # When required + safety_class cohorts failed, the fallback
        # is a Defer briefing rather than the standard minimal
        # respond_only fallback. This makes safety_class load-bearing
        # even on the fail-soft path.
        if inputs.required_safety_cohort_failures:
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
                    notes=f"safety-degraded fail-soft: {notes}",
                ),
                turn_id=inputs.turn_id,
                integration_run_id=inputs.integration_run_id,
                cognitive_context=inputs.cognitive_context,
            )
            await self._emit_audit(
                briefing, success=False, error=error or notes,
            )
            return briefing

        briefing = minimal_fail_soft_briefing(
            turn_id=inputs.turn_id,
            integration_run_id=inputs.integration_run_id,
            notes=notes,
            budget_state=budget_state,
        )
        # Carry through whatever we did manage to gather so the audit
        # trail isn't blank — references only, never raw content.
        briefing = Briefing(
            relevant_context=briefing.relevant_context,
            filtered_context=briefing.filtered_context,
            decided_action=briefing.decided_action,
            presence_directive=briefing.presence_directive,
            audit_trace=AuditTrace(
                cohort_outputs=cohort_refs,
                tools_called_during_prep=tuple(tools_called),
                iterations_used=iterations,
                budget_state=budget_state,
                fail_soft_engaged=True,
                phase_durations_ms=dict(phase_durations_ms),
                notes=notes,
            ),
            turn_id=briefing.turn_id,
            integration_run_id=briefing.integration_run_id,
            cognitive_context=inputs.cognitive_context,
        )
        await self._emit_audit(briefing, success=False, error=error or notes)
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
