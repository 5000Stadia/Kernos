"""Live workshop-binding wiring for the C7 thin path.

INTEGRATION-CAPABILITY-FIRST-V1 Batch 2: replaces the unwired stubs
shipped with the C5c-bringup cutover (``_UnwiredDescriptorLookup``,
``_UnwiredExecutor``, empty ``_integration_dispatcher``,
``StaticToolCatalog()``) with production-wired versions reading
from the live tool catalog and routing through the legacy handler's
existing kernel-tool dispatch path.

Five components in one cohesive module:

1. ``LiveDescriptorLookup`` — reads workshop tool descriptors from
   the live tool catalog. Falls back to a synthesized descriptor
   for kernel tools (which don't have explicit workshop descriptors).
2. ``LiveExecutor`` — accepts ``ToolExecutionInputs`` and dispatches
   through the production reasoning service's ``execute_tool``.
   Enforces dispatch-time gate classification using the actual call
   arguments — the canonical safety boundary per Fold 3 of the
   architect's verdict ("Gate at dispatch, hint at surfacing").
3. ``LiveIntegrationDispatcher`` — positional ``(tool_id, args, inputs)``
   callable that integration's runner uses for read-only dispatch
   during briefing assembly. Routes through reasoning.execute_tool
   with the same dispatch-time gate enforcement.
4. ``build_renderer_to_integration_adapter`` — Fold 1 verdict: the
   adapter shim that bridges PresenceRenderer's keyword-style
   dispatcher contract (``tool_name``, ``tool_input``,
   ``tool_use_id``, ``conversation_id``) to the integration runner's
   positional contract. Both seams stay intact; the adapter
   translates without homogenizing.
5. ``LivePlannerCatalog`` — wraps the handler's live ``ToolCatalog``
   to satisfy the Planner's catalog interface, so the planner sees
   real tool registrations rather than the empty StaticToolCatalog.

Architectural fact (codified by the architect 2026-05-03):
**"Gate at dispatch, hint at surfacing."** Surfacing-time
gate-classification on ``SurfacedTool.gate_classification`` is a
hint that aids tool selection; dispatch-time gate-classification
using actual call arguments is the safety boundary. Every dispatch
path in this module invokes the gate's classifier with the actual
arguments before executing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from kernos.kernel.enactment.dispatcher import (
    ToolExecutionInputs,
    ToolExecutionResult,
)

logger = logging.getLogger(__name__)


# ===========================================================================
# 1. LiveDescriptorLookup
# ===========================================================================


class LiveDescriptorLookup:
    """Descriptor lookup that reads from the live tool catalog.

    Replaces ``_UnwiredDescriptorLookup`` which raised loudly. The
    production version queries the catalog for workshop-registered
    descriptors; for kernel tools (which don't have explicit workshop
    descriptors), it synthesizes a minimal descriptor on the fly so
    the planner's machinery doesn't trip on tool-not-found for the
    kernel surface.

    Returns ``None`` only when the tool is genuinely unknown — that's
    the correct signal for the planner to abort with
    ``StepDispatchResult(reason="tool_not_registered")`` rather than
    a hard exception.
    """

    def __init__(self, *, tool_catalog: Any) -> None:
        self._tool_catalog = tool_catalog

    def descriptor_for(self, tool_id: str) -> Any | None:
        """Return a tool descriptor or None when the tool is unknown.

        The shape returned mirrors the workshop descriptor shape the
        planner expects: an object with ``tool_id``, ``description``,
        and ``parameters`` (or whatever the consumer reads). For
        catalog-registered tools we return the catalog entry directly;
        for kernel tools we synthesize a minimal descriptor.
        """
        if not self._tool_catalog:
            return None
        try:
            entry = self._tool_catalog.get(tool_id)
        except Exception:
            entry = None
        if entry is not None:
            return _SynthesizedDescriptor(
                tool_id=tool_id,
                description=getattr(entry, "description", "") or "",
                source=getattr(entry, "source", "") or "",
            )
        return None


@dataclass(frozen=True)
class _SynthesizedDescriptor:
    """Minimal descriptor shape — enough for the planner's path."""
    tool_id: str
    description: str
    source: str


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
    enforcement (Fold 3): every execute call invokes the gate
    classifier with the actual call arguments before dispatching.

    Safety contract:

    * If the gate returns ``"unknown"`` for the call (with actual
      args), the executor refuses execution and returns an error
      ``ToolExecutionResult`` rather than dispatching. This is the
      conservative fallback that closes the action-dependent gap
      flagged in Batch 1's Codex review.
    * If gate classification raises, refuse execution (defensive).
    * On dispatch failure (exception from execute_tool), return an
      error ``ToolExecutionResult`` rather than re-raising — the
      executor never tears down the turn.

    Construction:
        ``execute_tool``: the ``reasoning.execute_tool`` async method
            (or any compatible ``(tool_name, tool_input, request)``
            callable).
        ``gate``: ``DispatchGate`` for dispatch-time classification.
        ``request_factory``: callable that builds a
            ``ReasoningRequest``-shaped object from
            ``ToolExecutionInputs`` so the underlying execute_tool
            has the context it needs (instance_id, member_id,
            active_space_id, etc.).
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
        # FOLD 3 — gate at dispatch, hint at surfacing.
        # Classify with the ACTUAL call arguments, not the surfacing-
        # time hint. For action-dependent tools (manage_*), the args
        # carry the action that determines the real effect.
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
                        f"(classification={classification!r}). The "
                        f"executor refuses to run unclassified tools "
                        f"for safety. Register the tool effect in "
                        f"capability/known.py or the kernel tool "
                        f"sets in gate.py."
                    ),
                },
                is_error=True,
                corrective_signal="",
            )

        # Dispatch through the production reasoning execute_tool.
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
        # Translate the legacy execute_tool's string return to the
        # ToolExecutionResult shape Batch 2's executor consumers expect.
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
# 3. LiveIntegrationDispatcher — positional shape
# ===========================================================================


class LiveIntegrationDispatcher:
    """Production read-only dispatcher for integration's briefing
    assembly. Positional ``(tool_id, args, inputs)`` shape that the
    integration runner expects.

    Routes through the same ``reasoning.execute_tool`` path as
    ``LiveExecutor`` but with the integration runner's positional
    arity. Returns a dict (the integration runner converts to a
    tool_result block).

    Dispatch-time gate enforcement (Fold 3) applies here too — the
    dispatcher refuses ``unknown``-classified tools.
    """

    def __init__(
        self,
        *,
        execute_tool: ReasoningExecuteTool,
        gate: Any,
        request_factory: Callable[[str, dict, Any], Any],
    ) -> None:
        self._execute_tool = execute_tool
        self._gate = gate
        self._request_factory = request_factory

    async def __call__(
        self, tool_id: str, args: dict, inputs: Any,
    ) -> dict:
        # FOLD 3 — dispatch-time gate enforcement.
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
        if not classification or classification == "unknown":
            return {
                "error": (
                    f"Dispatch refused: tool {tool_id!r} not "
                    f"classified by the dispatch gate "
                    f"(classification={classification!r})."
                ),
                "is_error": True,
            }

        request = self._request_factory(tool_id, args, inputs)
        try:
            result_text = await self._execute_tool(
                tool_id, dict(args or {}), request,
            )
        except Exception as exc:
            logger.warning(
                "DISPATCHER_TOOL_FAILED: tool=%s err=%s", tool_id, exc,
            )
            return {"error": str(exc), "is_error": True}
        if isinstance(result_text, str):
            return {"text": result_text}
        if isinstance(result_text, dict):
            return dict(result_text)
        return {"text": str(result_text)}


# ===========================================================================
# 4. RendererToIntegrationAdapter — Fold 1 shim
# ===========================================================================


def build_renderer_to_integration_adapter(
    *,
    integration_dispatcher: LiveIntegrationDispatcher,
    inputs_factory: Callable[[str], Any] = lambda _conversation_id: None,
) -> Callable[..., Awaitable[str]]:
    """Adapter shim: PresenceRenderer's kwarg-style dispatcher →
    integration runner's positional ``(tool_id, args, inputs)`` shape.

    The architect's Fold 1 verdict 2026-05-03: two seams serve
    different architectural roles (integration LLM observing tool
    effects during briefing assembly vs. principal model executing
    tools mid-render). Preserve both via adapter, don't refactor
    toward unity. The renderer's kwargs include ``tool_use_id``
    (provider correlation) which the integration dispatcher doesn't
    consume — adapter drops it on the floor at the boundary, that's
    fine because the renderer's loop preserves it on the message
    thread independently.

    The returned callable matches PresenceRenderer's ``ToolDispatcher``
    contract: keyword-only ``tool_name``, ``tool_input``,
    ``tool_use_id``, ``conversation_id`` and returns the textual
    tool result content.

    ``inputs_factory`` builds the ``inputs`` argument the integration
    dispatcher receives. Default is a no-op (returns None) — wiring
    code can override to thread per-turn integration inputs context.
    """

    async def _adapter(
        *,
        tool_name: str,
        tool_input: dict,
        tool_use_id: str,
        conversation_id: str,
    ) -> str:
        # Build the positional inputs the integration dispatcher
        # expects. The conversation_id is the obvious turn-correlation
        # handle; richer per-turn context can be threaded via
        # inputs_factory.
        positional_inputs = inputs_factory(conversation_id)
        result = await integration_dispatcher(
            tool_name, dict(tool_input or {}), positional_inputs,
        )
        # Translate the integration dispatcher's dict result to the
        # renderer's text contract. Both error and success cases map
        # cleanly to text the model can read in the next iteration.
        if isinstance(result, dict):
            if result.get("is_error"):
                return result.get("error") or f"[tool error] {tool_name} failed"
            text = result.get("text")
            if text is not None:
                return str(text)
            # Render the dict as a structured text payload for the
            # model to interpret (matches what tool_result blocks
            # carry on the legacy path).
            return str(result)
        return str(result)

    return _adapter


# ===========================================================================
# 5. LivePlannerCatalog — wraps live tool catalog
# ===========================================================================


class LivePlannerCatalog:
    """Wraps the handler's live ``ToolCatalog`` to satisfy the
    planner's catalog interface.

    The planner reads the catalog to know what tools exist when
    constructing plans. Pre-Batch-2 the planner saw an empty
    ``StaticToolCatalog()``; with this wrapper it sees the full
    live registry (kernel + workspace + MCP).
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

    def list_tools(self) -> list[Any]:
        if not self._tool_catalog:
            return []
        try:
            return list(self._tool_catalog.get_all())
        except Exception:
            return []


__all__ = [
    "LiveDescriptorLookup",
    "LiveExecutor",
    "LiveIntegrationDispatcher",
    "LivePlannerCatalog",
    "build_renderer_to_integration_adapter",
]
