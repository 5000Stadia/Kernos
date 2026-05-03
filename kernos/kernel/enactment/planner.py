"""Concrete Planner implementing PDI's PlannerLike Protocol (IWL C1).

Produces a structured Plan from a Briefing's action_envelope plus
context. Filters the tool catalog to envelope.allowed_operations so
the model only plans against operations integration permitted.

Implements PDI's shipped surface:

    create_plan(inputs: PlanCreationInputs) -> PlanCreationResult

Where PlanCreationResult wraps the structured Plan dataclass per
PDI's shipped contract.

Architectural pins (carried from PDI):
  - Protocol return type has no `streamed` field; streaming is
    structurally unreachable from the planner code path
    (verified by PDI's audit pin).
  - Plan recorded in audit before any step dispatches: enforced by
    EnactmentService, not by the planner; the planner just produces
    the Plan.
  - Locked 7-signal vocabulary: planner's prompt names exactly the
    seven SignalKinds from PDI C5; anything outside falls to prose.
  - Same-model default in v1 (the design review edit, locked): the planner's
    chain_caller is the same callable integration uses by default.
    Per-hook differentiation deferred until soak telemetry justifies.

Tool catalog source: a `ToolCatalogProvider` Protocol the planner
consults at create_plan time. The provider returns the tools
available to the agent; the planner filters by the envelope's
allowed_operations before serializing into the prompt. v1 uses a
simple list-based provider; INTEGRATION-WIRE-LIVE C5 binds the
production source to the workshop registry.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from kernos.kernel.enactment.plan import (
    Plan,
    PlanValidationError,
    SignalKind,
    Step,
    StepExpectation,
    StructuredSignal,
    new_plan_id,
    now_iso,
)
from kernos.kernel.enactment.service import (
    PlanCreationInputs,
    PlanCreationResult,
)
from kernos.kernel.integration.briefing import Briefing
from kernos.providers.base import ContentBlock, ProviderResponse


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PlannerError(RuntimeError):
    """Raised when plan creation fails to produce a well-formed plan."""


# ---------------------------------------------------------------------------
# Tool catalog provider Protocol
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCatalogEntry:
    """A single planner-visible tool entry.

    Fields are deliberately minimal: the planner needs enough to
    construct a step (tool_id, tool_class, operation_name, schema
    for arguments) plus a description for prompt framing. Audit and
    runtime-enforcement detail lives elsewhere.
    """

    tool_id: str
    tool_class: str
    operation_name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ToolCatalogProvider(Protocol):
    """Source of planner-visible tools.

    The planner queries this once per create_plan() and filters the
    result by the envelope's allowed_operations. Implementations bind
    to the workshop registry in production; tests pass simple list
    providers.
    """

    def list_tools_for_planning(self) -> list[ToolCatalogEntry]: ...


@dataclass(frozen=True)
class StaticToolCatalog:
    """Trivial ToolCatalogProvider backed by a fixed list.

    Used by tests and by INTEGRATION-WIRE-LIVE C5 wiring as a
    reasonable default until the workshop-registry binding lands as
    its own concrete provider. The list is captured at construction;
    callers that need dynamic catalogs implement the Protocol
    directly.
    """

    entries: tuple[ToolCatalogEntry, ...] = ()

    def list_tools_for_planning(self) -> list[ToolCatalogEntry]:
        return list(self.entries)


# ---------------------------------------------------------------------------
# ChainCaller — same shape integration runner uses
# ---------------------------------------------------------------------------


ChainCaller = Callable[
    [str | list[dict], list[dict], list[dict], int],
    Awaitable[ProviderResponse],
]
"""(system, messages, tools, max_tokens) → ProviderResponse"""


# ---------------------------------------------------------------------------
# Plan-finalize tool schema
# ---------------------------------------------------------------------------


PLAN_FINALIZE_TOOL_NAME = "__finalize_plan__"


def _plan_finalize_tool_schema() -> dict[str, Any]:
    """Synthetic tool the model fills in to emit a structured Plan.

    The schema enumerates the locked 7-signal vocabulary so the model
    has a closed enum to choose from. Anything outside falls to prose.

    Step shape mirrors PDI's Step dataclass: tool_id, arguments,
    tool_class, operation_name, expectation. The runtime validates
    each step against the envelope downstream.
    """
    return {
        "name": PLAN_FINALIZE_TOOL_NAME,
        "description": (
            "Emit the final plan structure as an ordered list of "
            "steps. Each step is one [action, expectation] pair. "
            "Use only operations from the envelope's allowed_operations. "
            "Use only tool classes from allowed_tool_classes. Express "
            "expectations using the locked 7-signal vocabulary OR prose. "
            "Call this tool when the plan is complete; do not call it "
            "more than once per turn."
        ),
        "input_schema": {
            "type": "object",
            "required": ["steps"],
            "properties": {
                "steps": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "required": [
                            "tool_id",
                            "tool_class",
                            "operation_name",
                            "expectation",
                        ],
                        "properties": {
                            "tool_id": {"type": "string"},
                            "tool_class": {"type": "string"},
                            "operation_name": {"type": "string"},
                            "arguments": {"type": "object"},
                            "expectation": {
                                "type": "object",
                                "required": ["prose"],
                                "properties": {
                                    "prose": {"type": "string"},
                                    "structured": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "required": ["kind"],
                                            "properties": {
                                                "kind": {
                                                    "type": "string",
                                                    "enum": [
                                                        k.value
                                                        for k in SignalKind
                                                    ],
                                                },
                                                "args": {"type": "object"},
                                            },
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
    }


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def _build_planner_system_prompt() -> str:
    """Stable system prompt for plan creation.

    Documents the locked 7-signal vocabulary inline so the model
    knows the closed enum without a separate fetch. Tuning happens
    after soak telemetry; v1 ships the simplest prompt that produces
    correct behavior.
    """
    return _SYSTEM_PROMPT_TEMPLATE


_SYSTEM_PROMPT_TEMPLATE = """\
You are Kernos's plan creator. You sit between integration's decided action
and enactment's step-by-step execution.

## What you do

Given an envelope (the structural constraints integration produced) and the
current context, you produce an ordered plan. Each step is one [action,
expectation] pair:

  - action: a tool call with arguments
  - expectation: what completing the action should look like

The plan is the contract enactment validates against at every plan-changing
step. Stay inside the envelope.

## Envelope respect (load-bearing)

- Use only operations listed in allowed_operations.
- Use only tool classes listed in allowed_tool_classes.
- Do not propose any move that matches a forbidden_moves entry.
- If a step's operation is in confirmation_requirements, that step requires
  user confirmation across a turn boundary — do NOT chain it after a draft
  step in the same plan; the user must confirm first.

## Expectation vocabulary (LOCKED — exactly seven structured signal kinds)

  count_at_least   — collection at named path has length ≥ value
  count_at_most    — collection at named path has length ≤ value
  contains_field   — dict at named path has key set
  returns_truthy   — result is truthy (non-empty, non-zero, etc.)
  success_status   — success-status field equals value (default ok=True)
  value_equality   — value at named path equals expected
  value_in_set     — value at named path is in allowed set

If your expectation cannot be expressed using these seven, write a prose
expectation. Do not invent additional signal kinds; the runtime falls back
to model-judged prose comparison for anything outside the seven.

## Output format

Call __finalize_plan__ exactly once with the structured plan. That ends
your work. Do not narrate or comment outside the tool call.
"""


def _build_planner_user_message(
    inputs: PlanCreationInputs,
    filtered_catalog: list[ToolCatalogEntry],
) -> str:
    """Build the user-message payload the planner prompt consumes."""
    briefing = inputs.briefing
    envelope = briefing.action_envelope
    parts: list[str] = []

    parts.append("## Envelope")
    if envelope is not None:
        parts.append(f"intended_outcome: {envelope.intended_outcome}")
        if envelope.allowed_tool_classes:
            parts.append(
                f"allowed_tool_classes: {list(envelope.allowed_tool_classes)}"
            )
        if envelope.allowed_operations:
            parts.append(
                f"allowed_operations: {list(envelope.allowed_operations)}"
            )
        if envelope.constraints:
            parts.append(f"constraints: {list(envelope.constraints)}")
        if envelope.confirmation_requirements:
            parts.append(
                f"confirmation_requirements: "
                f"{list(envelope.confirmation_requirements)}"
            )
        if envelope.forbidden_moves:
            parts.append(f"forbidden_moves: {list(envelope.forbidden_moves)}")
    else:
        # Defensive — execute_tool briefings always carry an envelope
        # per PDI C1's structural rule. Reaching here means an upstream
        # contract violation; surface clearly.
        parts.append("(envelope missing — contract violation)")

    if briefing.presence_directive:
        parts.append("\n## Briefing presence_directive")
        parts.append(briefing.presence_directive)

    if briefing.relevant_context:
        parts.append("\n## Relevant context")
        for item in briefing.relevant_context:
            parts.append(
                f"- [{item.source_type}] {item.summary} "
                f"(confidence: {item.confidence:.2f})"
            )

    parts.append("\n## Filtered tool catalog")
    if filtered_catalog:
        for entry in filtered_catalog:
            parts.append(
                f"- tool_id={entry.tool_id} "
                f"tool_class={entry.tool_class} "
                f"operation={entry.operation_name}: {entry.description}"
            )
    else:
        parts.append("(no tools match the envelope's allowed_operations)")

    if inputs.prior_plan_id:
        parts.append("\n## Reassembly context")
        parts.append(f"prior_plan_id: {inputs.prior_plan_id}")
        if inputs.triggering_context_summary:
            parts.append(
                f"triggering_context_summary: "
                f"{inputs.triggering_context_summary}"
            )

    parts.append("\nProduce the plan now via __finalize_plan__.")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Plan parsing
# ---------------------------------------------------------------------------


def _parse_finalize_block(
    response: ProviderResponse,
) -> dict[str, Any]:
    """Locate the __finalize_plan__ tool_use block in a model response
    and return its input dict. Raises PlannerError if absent."""
    for block in response.content:
        if block.type == "tool_use" and block.name == PLAN_FINALIZE_TOOL_NAME:
            payload = block.input or {}
            if not isinstance(payload, dict):
                raise PlannerError(
                    f"__finalize_plan__ payload must be a dict; got "
                    f"{type(payload).__name__}"
                )
            return payload
    raise PlannerError(
        "model did not call __finalize_plan__ with a structured plan"
    )


def _parse_step(raw: dict[str, Any], step_index: int) -> Step:
    """Parse one step from the model's __finalize_plan__ payload."""
    if not isinstance(raw, dict):
        raise PlannerError(
            f"step {step_index} must be a dict; got {type(raw).__name__}"
        )
    step_id = raw.get("step_id") or f"s{step_index + 1}"
    expectation_raw = raw.get("expectation") or {"prose": " "}
    try:
        expectation = StepExpectation.from_dict(expectation_raw)
    except PlanValidationError as exc:
        raise PlannerError(
            f"step {step_id} expectation invalid: {exc}"
        ) from exc

    try:
        return Step(
            step_id=str(step_id),
            tool_id=str(raw.get("tool_id", "")),
            arguments=dict(raw.get("arguments", {}) or {}),
            tool_class=str(raw.get("tool_class", "")),
            operation_name=str(raw.get("operation_name", "")),
            expectation=expectation,
        )
    except PlanValidationError as exc:
        raise PlannerError(
            f"step {step_id} construction failed: {exc}"
        ) from exc


def _parse_plan_from_response(
    response: ProviderResponse, *, turn_id: str, created_via: str = "initial"
) -> Plan:
    """Locate the finalize block and produce a Plan dataclass.

    Raises PlannerError on any structural problem so the
    EnactmentService routes through fail-soft / B1 termination
    rather than producing a malformed plan.
    """
    payload = _parse_finalize_block(response)
    raw_steps = payload.get("steps", [])
    if not isinstance(raw_steps, list) or not raw_steps:
        raise PlannerError(
            "__finalize_plan__ requires a non-empty steps array"
        )
    steps = tuple(
        _parse_step(raw, idx) for idx, raw in enumerate(raw_steps)
    )
    try:
        return Plan(
            plan_id=new_plan_id(),
            turn_id=turn_id,
            steps=steps,
            created_at=now_iso(),
            created_via=created_via,
        )
    except PlanValidationError as exc:
        raise PlannerError(f"plan construction failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Tool catalog filtering
# ---------------------------------------------------------------------------


def _filter_catalog_to_envelope(
    catalog: list[ToolCatalogEntry], briefing: Briefing
) -> list[ToolCatalogEntry]:
    """Filter the tool catalog to entries whose operation_name is in
    envelope.allowed_operations.

    Per the design review edit + spec acceptance criterion #3: the access path is
    inputs.briefing.action_envelope.allowed_operations.

    When allowed_operations is empty, the envelope is treated as
    unconstrained (matching the existing
    validate_step_against_envelope semantics from PDI C5). The
    planner sees the full catalog in that case; envelope validation
    downstream would still reject specific steps if the envelope
    forbids them.

    When allowed_tool_classes is non-empty, the catalog is also
    filtered to those classes — a defense-in-depth pin on top of the
    operation-level filter.
    """
    envelope = briefing.action_envelope
    if envelope is None:
        # Defensive — reaching here means the planner was called
        # outside an action-shape briefing (which shouldn't happen).
        return list(catalog)
    out = list(catalog)
    if envelope.allowed_operations:
        out = [
            e for e in out
            if e.operation_name in envelope.allowed_operations
        ]
    if envelope.allowed_tool_classes:
        out = [
            e for e in out
            if e.tool_class in envelope.allowed_tool_classes
        ]
    if envelope.forbidden_moves:
        out = [
            e for e in out
            if e.operation_name not in envelope.forbidden_moves
            and e.tool_class not in envelope.forbidden_moves
        ]
    return out


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


# v1 default max_tokens. Tunable per soak; not yet load-bearing.
DEFAULT_PLANNER_MAX_TOKENS = 2048


class Planner:
    """Concrete Planner conforming to PDI's PlannerLike Protocol.

    Responsibilities:
      - Build the prompt from briefing + envelope + filtered tool
        catalog.
      - Call the chain (same shape integration uses) once.
      - Parse the model's __finalize_plan__ output into a Plan.
      - Wrap the Plan in PlanCreationResult per PDI's shipped shape.

    The Planner does NOT validate the plan against the envelope;
    that's EnactmentService's responsibility (validate_plan_against_envelope
    runs after plan_created emits and before any step dispatches).
    The Planner just produces a candidate plan.
    """

    def __init__(
        self,
        *,
        chain_caller: ChainCaller,
        tool_catalog: ToolCatalogProvider,
        max_tokens: int = DEFAULT_PLANNER_MAX_TOKENS,
    ) -> None:
        self._chain_caller = chain_caller
        self._catalog = tool_catalog
        self._max_tokens = max_tokens

    async def create_plan(
        self, inputs: PlanCreationInputs
    ) -> PlanCreationResult:
        """Produce a Plan from the briefing + envelope.

        Raises PlannerError if the model fails to call
        __finalize_plan__ or produces a structurally invalid plan.
        EnactmentService catches and routes through B1.
        """
        catalog_all = self._catalog.list_tools_for_planning()
        filtered = _filter_catalog_to_envelope(catalog_all, inputs.briefing)

        system = _build_planner_system_prompt()
        user_message = _build_planner_user_message(inputs, filtered)
        messages: list[dict] = [{"role": "user", "content": user_message}]
        tools = [_plan_finalize_tool_schema()]

        response = await self._chain_caller(
            system, messages, tools, self._max_tokens
        )

        created_via = (
            "tier_4_reassemble" if inputs.prior_plan_id else "initial"
        )
        plan = _parse_plan_from_response(
            response,
            turn_id=inputs.briefing.turn_id,
            created_via=created_via,
        )
        return PlanCreationResult(plan=plan)


def build_planner(
    *,
    chain_caller: ChainCaller,
    tool_catalog: ToolCatalogProvider,
    max_tokens: int = DEFAULT_PLANNER_MAX_TOKENS,
) -> Planner:
    """Convenience factory mirroring the constructor."""
    return Planner(
        chain_caller=chain_caller,
        tool_catalog=tool_catalog,
        max_tokens=max_tokens,
    )


__all__ = [
    "ChainCaller",
    "DEFAULT_PLANNER_MAX_TOKENS",
    "PLAN_FINALIZE_TOOL_NAME",
    "Planner",
    "PlannerError",
    "StaticToolCatalog",
    "ToolCatalogEntry",
    "ToolCatalogProvider",
    "build_planner",
]
