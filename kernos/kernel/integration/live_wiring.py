"""Live workshop-binding wiring for the C7 thin path.

INTEGRATION-CAPABILITY-FIRST-V1 Batch 2: replaces the unwired stubs
shipped with the C5c-bringup cutover (``_UnwiredDescriptorLookup``,
``_UnwiredExecutor``, empty ``_integration_dispatcher``,
``StaticToolCatalog()``) with production-wired versions reading
from the live tool catalog and routing through the legacy handler's
existing kernel-tool dispatch path.

Components:

1. ``LiveDescriptorLookup`` — reads workshop tool descriptors from
   the live tool catalog. Returns a duck-typed ``ToolDescriptor``-
   compatible shape with the minimum interface ``resolve_operation``
   consumes. Full-machinery semantic correctness for non-trivial
   operation resolvers is a Batch 3 equivalence-soak follow-up — for
   now the shape is correct enough not to crash the planner, and
   dispatch-time gate enforcement (Fold 3) is the safety boundary.
2. ``LiveExecutor`` — accepts ``ToolExecutionInputs`` and dispatches
   through the production reasoning service's ``execute_tool``.
   Enforces dispatch-time gate classification using the actual call
   arguments — the canonical safety boundary per Fold 3
   ("Gate at dispatch, hint at surfacing").
3. ``LiveIntegrationDispatcher`` — positional ``(tool_id, args, inputs)``
   callable that integration's runner uses during briefing assembly.
   Per ESCALATE-ON-WRITE-V1 (2026-05-07): writes are NOT refused
   outright — non-read classifications dispatch through the same
   ``execute_tool`` path full-machinery's ``LiveExecutor`` uses, with
   the seam label switched to ``live_integration_dispatcher_escalated``
   so audit/event consumers can distinguish escalations from native
   read traffic. Refusal is reserved for ``unknown`` / unclassified
   calls (matching ``LiveExecutor`` posture). Covenant/permission
   policy (full ``DispatchGate.evaluate``) is a Batch 3 follow-up at
   both seams; until that lands, both seams enforce only the gate's
   effect classifier. Per Fold 8: emits ``tool.called`` and
   ``tool.result`` events on every dispatch + logs an audit entry,
   matching the per-call event shape the legacy path used to emit via
   the legacy ``_execute_single_tool`` wrapper (removed in CCV1 C7
   strike 2026-05-03).
4. ``build_renderer_to_integration_adapter`` — Fold 1 verdict: the
   adapter shim that bridges PresenceRenderer's keyword-style
   dispatcher contract to the integration runner's positional
   contract. Both seams stay intact; the adapter translates without
   homogenizing.
5. ``LivePlannerCatalog`` — wraps the handler's live ``ToolCatalog``
   to satisfy the planner's ``ToolCatalogProvider`` protocol via
   ``list_tools_for_planning``. Maps catalog entries to
   ``ToolCatalogEntry`` shape using the registered ``source`` as
   tool_class.

Architectural facts (codified by the architect 2026-05-03):

* **"Gate at dispatch, hint at surfacing."** Surfacing-time
  ``SurfacedTool.gate_classification`` is a hint that aids tool
  selection; dispatch-time gate-classification using actual call
  arguments is the safety boundary.
* **"Two seams, different roles."** Integration runner's read-only
  dispatcher and presence renderer's tool-use loop dispatcher serve
  different architectural roles. Read-only dispatcher gets strict
  read enforcement; full-machinery executor gets full policy gate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from kernos.kernel.enactment.dispatcher import (
    ToolExecutionInputs,
    ToolExecutionResult,
)
from kernos.kernel.enactment.planner import ToolCatalogEntry

logger = logging.getLogger(__name__)


# Conservative classification at surfacing time. Per
# INTEGRATION-CAPABILITY-FIRST-V1 §"Conservative classification fallback":
# missing/unknown classification defaults to propose/blocked rather than
# silently read-safe.
_FALLBACK_CLASSIFICATION = "unknown"


def _gate_refusal_prose(gate_result: Any) -> str:
    """Compose natural-prose refusal text from a gate result.

    Per [[agent-facing-natural-simplicity]]: the agent reads
    English, not status codes. The structured reason field stays
    in the event log + audit for operator inspection; this layer
    just composes the sentence the agent receives in its tool
    error output.

    LIVE-DISPATCH-UNBLOCKER-V1 (2026-05-22) Phase A.
    """
    reason = getattr(gate_result, "reason", "") or ""
    proposed = getattr(gate_result, "proposed_action", "") or ""
    conflict = getattr(gate_result, "conflicting_rule", "") or ""
    if reason == "covenant_conflict" and conflict:
        return (
            f"A standing rule prevents that action: {conflict}"
        )
    if reason == "clarify":
        action = proposed or "what was requested"
        return (
            f"The action is ambiguous — clarify the user's intent "
            f"before retrying ({action})."
        )
    if reason == "confirm":
        action = proposed or "this action"
        return (
            f"Operator confirmation needed before {action}. Ask "
            f"the user to confirm, then retry."
        )
    if reason == "refused_by_mode":
        return (
            "Operator's strict posture is blocking this action "
            "outright. Try a clearer formulation or skip the step."
        )
    if reason == "denial_limit":
        return (
            "Too many consecutive blocks for this tool this turn. "
            "Step back and reconsider the approach."
        )
    return "Action blocked by the dispatch gate."


# ===========================================================================
# 1. LiveDescriptorLookup
# ===========================================================================


@dataclass
class _LiveToolDescriptor:
    """Minimal duck-type descriptor satisfying the
    ``resolve_operation`` consumer's required interface.

    The full ``ToolDescriptor`` (kernos/kernel/tool_descriptor.py)
    carries operation classifications, safety mappings, and an
    operation resolver — those fields require per-tool authoring
    that doesn't yet live anywhere centralized for kernel + MCP +
    workspace tools. This duck-type satisfies the interface for
    the planner and ``resolve_operation`` paths to not crash;
    full-machinery semantic correctness lands in a Batch 3
    follow-up if equivalence soak surfaces specific gaps.

    Dispatch-time gate enforcement (Fold 3) is the safety boundary
    in the meantime — every dispatch invokes the gate's classifier
    with actual call arguments before executing, so the
    descriptor's operation classification is advisory.
    """

    name: str
    description: str = ""
    input_schema: dict = field(default_factory=dict)
    operations: tuple = ()
    operation_resolver: Any = None

    def safety_for(self, operation_name: str) -> Any:
        """Return a default ambiguous safety classification.

        Tools with explicit operations would shadow this; tools
        without rely on dispatch-time gate enforcement to catch
        unsafe calls. Returning a placeholder rather than raising
        keeps the planner's operation-resolution path stable.
        """
        from kernos.kernel.tool_descriptor import (
            DEFAULT_AMBIGUOUS_SAFETY,
        )
        return DEFAULT_AMBIGUOUS_SAFETY

    def operation_for(self, name: str) -> Any:
        """Return the OperationClassification for ``name`` if declared,
        else None. Mirrors ``ToolDescriptor.operation_for`` so dispatcher
        consumers (e.g. ``_timeout_ms_for``) can call uniformly without
        type-checking. Live duck-types carry empty ``operations`` by
        design, so this returns None and the dispatcher falls back to
        the default per-call timeout."""
        for op in self.operations:
            if getattr(op, "operation", None) == name:
                return op
        return None


class LiveDescriptorLookup:
    """Descriptor lookup that reads from the live tool catalog.

    Replaces ``_UnwiredDescriptorLookup`` which raised loudly. Returns
    a ``_LiveToolDescriptor`` duck-type when the tool is registered;
    returns ``None`` when the tool is genuinely unknown (correct
    signal for the planner to abort with
    ``StepDispatchResult(reason="tool_not_registered")``).
    """

    def __init__(self, *, tool_catalog: Any) -> None:
        self._tool_catalog = tool_catalog

    def descriptor_for(self, tool_id: str) -> Any | None:
        if not self._tool_catalog:
            return None
        try:
            entry = self._tool_catalog.get(tool_id)
        except Exception:
            entry = None
        if entry is None:
            return None
        return _LiveToolDescriptor(
            name=tool_id,
            description=getattr(entry, "description", "") or "",
        )

    def known_tool_ids(self) -> set[str]:
        """CORRECTIVE-SIGNAL-CLOSEST-MATCH (2026-05-17): expose the
        currently-registered tool names so the dispatcher's
        tool_not_registered path can suggest closest matches when
        the model emits an unrecognized name (e.g. hallucinated
        namespaced variants like ``code_execution.execute_python``
        instead of ``execute_code``).

        Empty set when the catalog isn't wired yet — dispatcher's
        suggestion logic is best-effort and handles empty input.
        """
        if not self._tool_catalog:
            return set()
        try:
            get_names = getattr(self._tool_catalog, "get_names", None)
            if callable(get_names):
                return set(get_names() or [])
        except Exception:
            return set()
        return set()


# ===========================================================================
# 2. LiveExecutor — dispatch-time gate enforcement (Fold 3)
# ===========================================================================


# The reasoning-service tool-execution callable signature, factored
# out so tests can stub it without importing reasoning.
ReasoningExecuteTool = Callable[
    ...,  # (tool_name, tool_input, request) → str
    Awaitable[str],
]


class LiveExecutor:
    """Production tool executor for the C7 thin path.

    Replaces ``_UnwiredExecutor`` which raised loudly. Dispatches
    through ``reasoning.execute_tool`` with dispatch-time gate
    enforcement: every execute call invokes the gate classifier with
    the actual call arguments before dispatching.

    Safety contract:

    * If the gate returns ``"unknown"`` for the call (with actual
      args), the executor refuses execution and returns an error
      ``ToolExecutionResult`` rather than dispatching.
    * If gate classification raises, refuse execution (defensive).
    * On dispatch failure (exception from execute_tool), return an
      error ``ToolExecutionResult`` rather than re-raising — the
      executor never tears down the turn.

    Note: per Fold 4 ("two seams, different roles"), the live
    executor seam handles full-machinery dispatch where covenants
    and per-instance permission overrides apply. The current
    implementation classifies via the gate's effect classifier;
    follow-up work could route through the full
    ``DispatchGate.evaluate`` method to honor covenants and
    permission policies. For Batch 2 the effect classifier is
    sufficient; covenant and permission policy at this seam is a
    Batch 3 equivalence-soak follow-up if regressions surface.
    """

    def __init__(
        self,
        *,
        execute_tool: ReasoningExecuteTool,
        gate: Any,
        request_factory: Callable[[ToolExecutionInputs], Any],
    ) -> None:
        self._execute_tool = execute_tool
        self._gate = gate
        self._request_factory = request_factory

    async def execute(
        self, inputs: ToolExecutionInputs,
    ) -> ToolExecutionResult:
        try:
            classification = self._gate.classify_tool_effect(
                inputs.tool_id, None, inputs.arguments,
            )
        except Exception as exc:
            logger.warning(
                "EXECUTOR_GATE_CLASSIFY_FAILED: tool=%s err=%s",
                inputs.tool_id, exc,
            )
            return ToolExecutionResult(
                output={
                    "error": (
                        f"Dispatch refused: gate classifier raised "
                        f"for tool {inputs.tool_id!r}. {exc}"
                    ),
                },
                is_error=True,
                corrective_signal="",
            )
        if not classification or classification == "unknown":
            return ToolExecutionResult(
                output={
                    "error": (
                        f"Dispatch refused: tool {inputs.tool_id!r} "
                        f"not classified by the dispatch gate "
                        f"(classification={classification!r})."
                    ),
                },
                is_error=True,
                corrective_signal="",
            )

        # LIVE-DISPATCH-UNBLOCKER-V1 (2026-05-22) Phase A: full
        # policy gate now runs on the live path. Mode policy
        # modulates bias (permissive / balanced / strict);
        # amortization layer (Phase B) collapses repeated-call
        # interaction cost; binding-failure diagnostics (Phase C)
        # shape failed-binding output.
        try:
            gate_result = await self._gate.evaluate(
                tool_name=inputs.tool_id,
                tool_input=dict(inputs.arguments or {}),
                effect=classification,
                agent_reasoning=inputs.agent_reasoning,
                instance_id=inputs.instance_id,
                active_space_id=inputs.space_id,
                is_reactive=inputs.is_reactive,
                approval_token_id=inputs.approval_token_id,
                messages=list(inputs.recent_messages),
                user_message=inputs.user_message,
                member_id=inputs.member_id,
            )
        except Exception as exc:
            logger.warning(
                "EXECUTOR_GATE_EVALUATE_FAILED: tool=%s err=%s",
                inputs.tool_id, exc,
            )
            return ToolExecutionResult(
                output={
                    "error": (
                        f"Dispatch refused: gate evaluation raised "
                        f"for tool {inputs.tool_id!r}. {exc}"
                    ),
                },
                is_error=True,
                corrective_signal="",
            )
        if not gate_result.allowed:
            # Compose a natural-prose error per
            # [[agent-facing-natural-simplicity]] — the agent reads
            # English, not a status code. The structured reason
            # lives in the gate event log for operator inspection.
            agent_text = _gate_refusal_prose(gate_result)
            return ToolExecutionResult(
                output={"error": agent_text},
                is_error=True,
                corrective_signal="",
            )

        request = self._request_factory(inputs)
        try:
            result_text = await self._execute_tool(
                inputs.tool_id, dict(inputs.arguments or {}), request,
            )
        except Exception as exc:
            logger.warning(
                "EXECUTOR_DISPATCH_FAILED: tool=%s err=%s",
                inputs.tool_id, exc,
            )
            return ToolExecutionResult(
                output={"error": str(exc)},
                is_error=True,
                corrective_signal="",
            )
        if isinstance(result_text, str):
            output: dict[str, Any] = {"text": result_text}
        elif isinstance(result_text, dict):
            output = dict(result_text)
        else:
            output = {"text": str(result_text)}
        return ToolExecutionResult(
            output=output,
            is_error=False,
            corrective_signal="",
        )


# ===========================================================================
# 3. LiveIntegrationDispatcher — strict read (Fold 4) + audit/event (Fold 8)
# ===========================================================================


class LiveIntegrationDispatcher:
    """Production dispatcher for integration's briefing assembly.
    Positional ``(tool_id, args, inputs)`` shape that the integration
    runner expects.

    ESCALATE-ON-WRITE-V1 (2026-05-07) — non-read classifications
    escalate through the same ``execute_tool`` path full-machinery's
    ``LiveExecutor`` uses rather than refusing. Background: the
    original Fold 4 contract was strict read-only on the assumption
    that writes would always be routed through full-machinery's
    EXECUTE_TOOL kind. In practice the agent reaches the integration
    seam mid-turn and tries to call write tools (e.g. ``write_file``
    to take a note); refusing those calls stranded the agent because
    no escalation path existed — the model just got back an error
    string saying "writes route through full-machinery" with no way
    to actually trigger that route. Now the dispatcher escalates such
    calls itself.

    Audit/event seam labels:
      * ``live_integration_dispatcher`` — native read traffic.
      * ``live_integration_dispatcher_escalated`` — write classifications
        dispatched through this seam. The ``escalated: True`` field on
        audit entries is the same signal in structured form so consumers
        can filter without label parsing.

    Refusal is now reserved for ``unknown`` / unclassified calls,
    matching the posture ``LiveExecutor`` takes at the full-machinery
    seam.

    Fold 8 — emits ``tool.called`` and ``tool.result`` events plus
    an audit entry on every dispatch, matching the per-call event
    shape the legacy path used to emit via ``_execute_single_tool``
    (removed in CCV1 C7 strike 2026-05-03). Audit and trace parity is
    required for Batch 3 equivalence soak.
    """

    def __init__(
        self,
        *,
        execute_tool: ReasoningExecuteTool,
        gate: Any,
        request_factory: Callable[[str, dict, Any], Any],
        event_emitter: Callable[[dict], Awaitable[None]] | None = None,
        audit_emitter: Callable[[dict], Awaitable[None]] | None = None,
    ) -> None:
        self._execute_tool = execute_tool
        self._gate = gate
        self._request_factory = request_factory
        self._event_emitter = event_emitter
        self._audit_emitter = audit_emitter

    async def __call__(
        self, tool_id: str, args: dict, inputs: Any,
    ) -> dict:
        try:
            classification = self._gate.classify_tool_effect(
                tool_id, None, args,
            )
        except Exception as exc:
            logger.warning(
                "DISPATCHER_GATE_CLASSIFY_FAILED: tool=%s err=%s",
                tool_id, exc,
            )
            return {
                "error": (
                    f"Dispatch refused: gate classifier raised for "
                    f"tool {tool_id!r}. {exc}"
                ),
                "is_error": True,
            }
        # ESCALATE-ON-WRITE-V1 — refusal is reserved for unclassified
        # calls. Writes dispatch through the same execute_tool path as
        # reads, with seam label flipped so audits can tell escalations
        # apart from native read traffic.
        if not classification or classification == "unknown":
            return {
                "error": (
                    f"Dispatch refused: tool {tool_id!r} not classified "
                    f"by the dispatch gate "
                    f"(classification={classification!r})."
                ),
                "is_error": True,
            }

        is_escalated = classification != "read"
        seam_label = (
            "live_integration_dispatcher_escalated"
            if is_escalated
            else "live_integration_dispatcher"
        )
        if is_escalated:
            logger.info(
                "DISPATCHER_ESCALATE_WRITE: tool=%s classification=%s",
                tool_id, classification,
            )

        instance_id = (
            getattr(inputs, "instance_id", "")
            if inputs is not None
            else ""
        )

        # LIVE-DISPATCH-UNBLOCKER-V1 (2026-05-22) Phase A: full
        # policy gate now runs on this seam too. Same shape as
        # LiveExecutor — fields pulled from inputs (with sensible
        # defaults for legacy callers that haven't been threaded
        # through yet).
        try:
            gate_result = await self._gate.evaluate(
                tool_name=tool_id,
                tool_input=dict(args or {}),
                effect=classification,
                agent_reasoning=getattr(inputs, "agent_reasoning", "") or "",
                instance_id=instance_id,
                active_space_id=getattr(inputs, "space_id", "") or "",
                is_reactive=getattr(inputs, "is_reactive", True),
                approval_token_id=getattr(inputs, "approval_token_id", "") or "",
                messages=list(getattr(inputs, "recent_messages", ()) or ()),
                user_message=getattr(inputs, "user_message", "") or "",
                member_id=getattr(inputs, "member_id", "") or "",
            )
        except Exception as exc:
            logger.warning(
                "DISPATCHER_GATE_EVALUATE_FAILED: tool=%s err=%s",
                tool_id, exc,
            )
            return {
                "error": (
                    f"Dispatch refused: gate evaluation raised for "
                    f"tool {tool_id!r}. {exc}"
                ),
                "is_error": True,
            }
        if not gate_result.allowed:
            return {
                "error": _gate_refusal_prose(gate_result),
                "is_error": True,
            }

        # FOLD 8 — emit tool.called before dispatch.
        await self._emit_event({
            "type": "tool.called",
            "instance_id": instance_id,
            "tool_id": tool_id,
            "tool_input": dict(args or {}),
            "classification": classification,
            "seam": seam_label,
            "escalated": is_escalated,
        })

        request = self._request_factory(tool_id, args, inputs)
        try:
            result_text = await self._execute_tool(
                tool_id, dict(args or {}), request,
            )
        except Exception as exc:
            logger.warning(
                "DISPATCHER_TOOL_FAILED: tool=%s err=%s", tool_id, exc,
            )
            await self._emit_event({
                "type": "tool.result",
                "instance_id": instance_id,
                "tool_id": tool_id,
                "is_error": True,
                "error": str(exc),
                "seam": seam_label,
                "escalated": is_escalated,
            })
            await self._emit_audit({
                "type": "tool_call_failed",
                "instance_id": instance_id,
                "tool_id": tool_id,
                "error": str(exc),
                "escalated": is_escalated,
            })
            return {"error": str(exc), "is_error": True}

        if isinstance(result_text, str):
            result_dict = {"text": result_text}
        elif isinstance(result_text, dict):
            result_dict = dict(result_text)
        else:
            result_dict = {"text": str(result_text)}

        # FOLD 8 — emit tool.result + audit after successful dispatch.
        await self._emit_event({
            "type": "tool.result",
            "instance_id": instance_id,
            "tool_id": tool_id,
            "is_error": False,
            "seam": seam_label,
            "escalated": is_escalated,
        })
        await self._emit_audit({
            "type": "tool_call_succeeded",
            "instance_id": instance_id,
            "tool_id": tool_id,
            "classification": classification,
            "escalated": is_escalated,
        })
        return result_dict

    async def _emit_event(self, payload: dict) -> None:
        if self._event_emitter is None:
            return
        try:
            await self._event_emitter(payload)
        except Exception:
            logger.exception("DISPATCHER_EVENT_EMIT_FAILED")

    async def _emit_audit(self, entry: dict) -> None:
        if self._audit_emitter is None:
            return
        try:
            await self._audit_emitter(entry)
        except Exception:
            logger.exception("DISPATCHER_AUDIT_EMIT_FAILED")


# ===========================================================================
# 4. RendererToIntegrationAdapter — Fold 1 shim
# ===========================================================================


def build_renderer_to_integration_adapter(
    *,
    integration_dispatcher: Any,
    inputs_factory: Callable[[str], Any] = lambda _conversation_id: None,
) -> Callable[..., Awaitable[str]]:
    """Adapter shim: PresenceRenderer's kwarg-style dispatcher →
    integration runner's positional ``(tool_id, args, inputs)`` shape.

    Per Fold 1: two seams serve different architectural roles
    (integration LLM observing tool effects during briefing assembly
    vs. principal model executing tools mid-render). Preserve both
    via adapter, don't refactor toward unity. The renderer's kwargs
    include ``tool_use_id`` (provider correlation) which the
    integration dispatcher doesn't consume — adapter drops it on the
    floor at the boundary because the renderer's loop preserves it
    on the message thread independently.

    ``inputs_factory`` builds the ``inputs`` argument the integration
    dispatcher receives. Production wiring threads turn context (e.g.,
    instance_id, member_id, space_id) so the dispatcher's
    request_factory can populate the ReasoningRequest used downstream.
    Default is a no-op (returns None) which yields empty identifiers
    — safe for tests but operators should wire a real factory.
    """

    async def _adapter(
        *,
        tool_name: str,
        tool_input: dict,
        tool_use_id: str,
        conversation_id: str,
    ) -> str:
        positional_inputs = inputs_factory(conversation_id)
        result = await integration_dispatcher(
            tool_name, dict(tool_input or {}), positional_inputs,
        )
        if isinstance(result, dict):
            if result.get("is_error"):
                return result.get("error") or f"[tool error] {tool_name} failed"
            text = result.get("text")
            if text is not None:
                return str(text)
            return str(result)
        return str(result)

    return _adapter


# ===========================================================================
# 5. LivePlannerCatalog — wraps live tool catalog
# ===========================================================================


class LivePlannerCatalog:
    """Wraps the handler's live ``ToolCatalog`` to satisfy the
    planner's ``ToolCatalogProvider`` protocol.

    The planner reads ``list_tools_for_planning()`` once per
    create_plan() to know what tools exist. Pre-Batch-2 the planner
    saw an empty ``StaticToolCatalog()``; this wrapper exposes the
    full live registry (kernel + workspace + MCP) mapped to the
    ``ToolCatalogEntry`` shape the planner expects.

    Mapping notes:

    * ``tool_id`` ← catalog entry's ``name``
    * ``tool_class`` ← catalog entry's ``source`` (kernel / workspace
      / mcp). Treat the source as the tool class since the planner's
      step construction routes by class.
    * ``operation_name`` ← defaults to ``tool_id`` (matches
      ``OperationResolver``'s single-operation fallback).
    * ``description`` ← catalog entry's ``description``.
    * ``input_schema`` ← empty dict for now. Catalog entries don't
      store the schema directly; if the planner's prompt needs the
      schema for framing, future work can resolve it from the
      handler's ``_tool_descriptors`` registry. Empty schema doesn't
      crash the planner; it just gives the model less framing detail.
    """

    def __init__(self, *, tool_catalog: Any) -> None:
        self._tool_catalog = tool_catalog

    def lookup(self, tool_id: str) -> Any | None:
        if not self._tool_catalog:
            return None
        try:
            return self._tool_catalog.get(tool_id)
        except Exception:
            return None

    def list_tools_for_planning(self) -> list[ToolCatalogEntry]:
        if not self._tool_catalog:
            return []
        try:
            entries = list(self._tool_catalog.get_all())
        except Exception:
            return []
        out: list[ToolCatalogEntry] = []
        for entry in entries:
            try:
                out.append(ToolCatalogEntry(
                    tool_id=entry.name,
                    tool_class=getattr(entry, "source", "") or "kernel",
                    operation_name=entry.name,
                    description=getattr(entry, "description", "") or "",
                    input_schema={},
                ))
            except Exception as exc:
                logger.warning(
                    "PLANNER_CATALOG_MAP_FAILED: entry=%s err=%s",
                    getattr(entry, "name", "?"), exc,
                )
        return out


__all__ = [
    "LiveDescriptorLookup",
    "LiveExecutor",
    "LiveIntegrationDispatcher",
    "LivePlannerCatalog",
    "build_renderer_to_integration_adapter",
]
