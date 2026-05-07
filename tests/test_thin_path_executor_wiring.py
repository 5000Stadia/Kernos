"""Pin tests for INTEGRATION-CAPABILITY-FIRST-V1 Batch 2: live wiring.

Covers:
  - LiveDescriptorLookup reads from the live tool catalog and
    returns None for unknown tools.
  - LiveExecutor enforces dispatch-time gate classification with
    actual call arguments (Fold 3) before executing — refuses
    "unknown" classifications, refuses on classifier errors.
  - LiveExecutor on successful dispatch translates the legacy
    string return into ToolExecutionResult.
  - LiveIntegrationDispatcher (positional shape) does the same
    enforcement and returns the integration-runner-shape dict.
  - RendererToIntegrationAdapter (Fold 1) translates kwarg-style
    PresenceRenderer dispatcher contract → positional integration
    dispatcher contract; preserves tool_use_id on the renderer-side
    while passing through the call.
  - Error paths surface friendly text, never crash.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.kernel.enactment.dispatcher import (
    ToolExecutionInputs,
    ToolExecutionResult,
)
from kernos.kernel.integration.live_wiring import (
    LiveDescriptorLookup,
    LiveExecutor,
    LiveIntegrationDispatcher,
    LivePlannerCatalog,
    build_renderer_to_integration_adapter,
)


def _inputs(tool_id: str = "list-events", args: dict | None = None) -> ToolExecutionInputs:
    return ToolExecutionInputs(
        tool_id=tool_id,
        arguments=args or {},
        operation_name=tool_id,
        instance_id="inst-x",
        member_id="mem-x",
        space_id="space-x",
        turn_id="turn-x",
    )


# ---------------------------------------------------------------------------
# LiveDescriptorLookup
# ---------------------------------------------------------------------------


def test_descriptor_lookup_returns_descriptor_for_known_tool():
    """Pin: catalog hit returns a ToolDescriptor-compatible object
    with ``name`` + ``description`` + ``operations`` + ``safety_for``
    matching what the resolve_operation consumer expects."""
    catalog = MagicMock()
    catalog.get.return_value = MagicMock(
        description="Calendar list events",
        source="mcp",
    )
    lookup = LiveDescriptorLookup(tool_catalog=catalog)
    desc = lookup.descriptor_for("list-events")
    assert desc is not None
    assert desc.name == "list-events"
    assert desc.description == "Calendar list events"
    # Required interface for resolve_operation:
    assert hasattr(desc, "operations")
    assert hasattr(desc, "operation_resolver")
    assert callable(desc.safety_for)
    # Required interface for dispatcher._timeout_ms_for — without
    # this the dispatcher AttributeErrors mid-call (regression seen
    # 2026-05-07 on the live take-a-note path).
    assert callable(desc.operation_for)
    assert desc.operation_for("any-op") is None


def test_descriptor_lookup_returns_none_for_unknown_tool():
    """Pin: when the catalog has no entry, return None — the
    correct signal for the planner to handle tool-not-registered
    gracefully rather than the loud raise the unwired stub did."""
    catalog = MagicMock()
    catalog.get.return_value = None
    lookup = LiveDescriptorLookup(tool_catalog=catalog)
    assert lookup.descriptor_for("mystery-tool") is None


def test_descriptor_lookup_handles_catalog_errors_defensively():
    """Pin: catalog raising never crashes the lookup. None returned."""
    catalog = MagicMock()
    catalog.get.side_effect = RuntimeError("catalog broken")
    lookup = LiveDescriptorLookup(tool_catalog=catalog)
    assert lookup.descriptor_for("anything") is None


def test_descriptor_lookup_handles_none_catalog():
    """Pin: defensive — None catalog → None descriptor, no crash."""
    lookup = LiveDescriptorLookup(tool_catalog=None)
    assert lookup.descriptor_for("anything") is None


# ---------------------------------------------------------------------------
# LiveExecutor — dispatch-time gate enforcement (Fold 3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_executor_dispatches_classified_tool_successfully():
    """Pin: known classified tool dispatches, result is wrapped into
    ToolExecutionResult with output text + is_error=False."""
    gate = MagicMock()
    gate.classify_tool_effect.return_value = "read"
    execute_tool = AsyncMock(return_value="meeting at 2pm")

    executor = LiveExecutor(
        execute_tool=execute_tool,
        gate=gate,
        request_factory=lambda inputs: MagicMock(),
    )
    result = await executor.execute(_inputs())

    assert isinstance(result, ToolExecutionResult)
    assert result.is_error is False
    assert result.output == {"text": "meeting at 2pm"}


@pytest.mark.asyncio
async def test_live_executor_classifies_with_actual_arguments_not_none():
    """Pin (Fold 3 contract): the gate is called with the actual
    call arguments, not None. This is the canonical safety boundary
    for action-dependent tools."""
    gate = MagicMock()
    gate.classify_tool_effect.return_value = "read"
    execute_tool = AsyncMock(return_value="ok")

    executor = LiveExecutor(
        execute_tool=execute_tool,
        gate=gate,
        request_factory=lambda inputs: MagicMock(),
    )
    args = {"action": "list", "id": "x"}
    await executor.execute(_inputs(tool_id="manage_members", args=args))

    # Verify gate was called with the actual args dict, not None
    gate.classify_tool_effect.assert_called_once()
    _, _, classify_args = gate.classify_tool_effect.call_args[0]
    assert classify_args == args, (
        f"Gate classifier must receive actual args; got {classify_args!r}. "
        f"Per Fold 3 'gate at dispatch, hint at surfacing' — surfacing "
        f"hint is approximate; actual-args classification is the safety "
        f"boundary."
    )


@pytest.mark.asyncio
async def test_live_executor_refuses_unknown_classification():
    """Pin: 'unknown' classification → refuse to execute, return
    error result. Closes the action-dependent gap from Batch 1
    Codex review."""
    gate = MagicMock()
    gate.classify_tool_effect.return_value = "unknown"
    execute_tool = AsyncMock()

    executor = LiveExecutor(
        execute_tool=execute_tool,
        gate=gate,
        request_factory=lambda inputs: MagicMock(),
    )
    result = await executor.execute(_inputs(tool_id="manage_members"))

    assert result.is_error is True
    assert "not classified" in result.output.get("error", "").lower()
    execute_tool.assert_not_called()


@pytest.mark.asyncio
async def test_live_executor_refuses_when_classifier_raises():
    """Pin: classifier raising → refuse, never execute. Defensive."""
    gate = MagicMock()
    gate.classify_tool_effect.side_effect = RuntimeError("gate broken")
    execute_tool = AsyncMock()

    executor = LiveExecutor(
        execute_tool=execute_tool,
        gate=gate,
        request_factory=lambda inputs: MagicMock(),
    )
    result = await executor.execute(_inputs())

    assert result.is_error is True
    assert "refused" in result.output.get("error", "").lower()
    execute_tool.assert_not_called()


@pytest.mark.asyncio
async def test_live_executor_returns_error_result_on_dispatch_failure():
    """Pin: when execute_tool raises, the executor returns an error
    ToolExecutionResult rather than re-raising. The turn never tears
    down on a single tool failure."""
    gate = MagicMock()
    gate.classify_tool_effect.return_value = "read"
    execute_tool = AsyncMock(side_effect=RuntimeError("backend exploded"))

    executor = LiveExecutor(
        execute_tool=execute_tool,
        gate=gate,
        request_factory=lambda inputs: MagicMock(),
    )
    result = await executor.execute(_inputs())

    assert result.is_error is True
    assert "backend exploded" in result.output.get("error", "")


# ---------------------------------------------------------------------------
# LiveIntegrationDispatcher — positional shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_dispatcher_positional_call_succeeds_for_classified_tool():
    """Pin: positional (tool_id, args, inputs) signature dispatches
    classified tools and returns the integration-runner shape dict."""
    gate = MagicMock()
    gate.classify_tool_effect.return_value = "read"
    execute_tool = AsyncMock(return_value="result text")

    dispatcher = LiveIntegrationDispatcher(
        execute_tool=execute_tool,
        gate=gate,
        request_factory=lambda tid, args, inp: MagicMock(),
    )
    result = await dispatcher("list-events", {"q": "today"}, MagicMock())
    assert result == {"text": "result text"}


@pytest.mark.asyncio
async def test_integration_dispatcher_refuses_unknown_classification_legacy():
    """Pin (ESCALATE-ON-WRITE-V1): unclassified calls refuse with an
    error dict — refusal is reserved for the gate's 'unknown' verdict
    after the read-only contract was relaxed. Matches LiveExecutor's
    posture at the full-machinery seam."""
    gate = MagicMock()
    gate.classify_tool_effect.return_value = "unknown"
    execute_tool = AsyncMock()

    dispatcher = LiveIntegrationDispatcher(
        execute_tool=execute_tool,
        gate=gate,
        request_factory=lambda tid, args, inp: MagicMock(),
    )
    result = await dispatcher("manage_members", {}, MagicMock())
    assert result.get("is_error") is True
    assert "classif" in result.get("error", "").lower()
    execute_tool.assert_not_called()


@pytest.mark.asyncio
async def test_integration_dispatcher_escalates_soft_write_to_execute_tool():
    """Pin (ESCALATE-ON-WRITE-V1): soft_write classifications dispatch
    through execute_tool rather than refusing. The original strict
    read-only contract stranded the agent when it tried to take a
    note mid-turn (incident 2026-05-07: write_file refused, no
    escalation path existed). Escalations are observable via the
    `live_integration_dispatcher_escalated` seam label and an
    `escalated: True` flag on emitted events/audit entries."""
    events: list[dict] = []
    audits: list[dict] = []

    async def event_emitter(p):
        events.append(p)

    async def audit_emitter(e):
        audits.append(e)

    gate = MagicMock()
    gate.classify_tool_effect.return_value = "soft_write"
    execute_tool = AsyncMock(return_value="note saved")

    dispatcher = LiveIntegrationDispatcher(
        execute_tool=execute_tool,
        gate=gate,
        request_factory=lambda tid, args, inp: MagicMock(),
        event_emitter=event_emitter,
        audit_emitter=audit_emitter,
    )
    result = await dispatcher("write_file", {"name": "n.md"}, MagicMock())

    assert result.get("is_error") is not True
    assert result.get("text") == "note saved"
    execute_tool.assert_called_once()

    seams = {e.get("seam") for e in events}
    assert "live_integration_dispatcher_escalated" in seams
    assert "live_integration_dispatcher" not in seams
    assert all(e.get("escalated") is True for e in events)
    assert any(
        a.get("type") == "tool_call_succeeded" and a.get("escalated") is True
        for a in audits
    )


@pytest.mark.asyncio
async def test_integration_dispatcher_escalates_hard_write_to_execute_tool():
    """Pin (ESCALATE-ON-WRITE-V1): hard_write also escalates rather
    than refusing. The seam-label + escalated flag still mark it as
    an escalation; consumers filtering on those signals see all
    non-read traffic uniformly."""
    gate = MagicMock()
    gate.classify_tool_effect.return_value = "hard_write"
    execute_tool = AsyncMock(return_value="deleted")

    dispatcher = LiveIntegrationDispatcher(
        execute_tool=execute_tool,
        gate=gate,
        request_factory=lambda tid, args, inp: MagicMock(),
    )
    result = await dispatcher("delete-event", {"id": "x"}, MagicMock())
    assert result.get("is_error") is not True
    assert result.get("text") == "deleted"
    execute_tool.assert_called_once()


@pytest.mark.asyncio
async def test_integration_dispatcher_emits_tool_called_and_result_events():
    """Pin (Fold 8): every dispatch emits tool.called before and
    tool.result after. Audit/trace parity with legacy is required
    for Batch 3 equivalence soak."""
    events: list[dict] = []

    async def event_emitter(payload):
        events.append(payload)

    audit_entries: list[dict] = []

    async def audit_emitter(entry):
        audit_entries.append(entry)

    gate = MagicMock()
    gate.classify_tool_effect.return_value = "read"
    execute_tool = AsyncMock(return_value="ok")

    dispatcher = LiveIntegrationDispatcher(
        execute_tool=execute_tool,
        gate=gate,
        request_factory=lambda tid, args, inp: MagicMock(),
        event_emitter=event_emitter,
        audit_emitter=audit_emitter,
    )
    await dispatcher("list-events", {}, MagicMock())

    types = [e.get("type") for e in events]
    assert "tool.called" in types
    assert "tool.result" in types
    assert any(a.get("type") == "tool_call_succeeded" for a in audit_entries)


@pytest.mark.asyncio
async def test_integration_dispatcher_emits_failure_events_on_dispatch_error():
    """Pin (Fold 8): dispatch failures emit tool.result with
    is_error=True and a tool_call_failed audit entry."""
    events: list[dict] = []
    audits: list[dict] = []

    async def event_emitter(p):
        events.append(p)

    async def audit_emitter(e):
        audits.append(e)

    gate = MagicMock()
    gate.classify_tool_effect.return_value = "read"
    execute_tool = AsyncMock(side_effect=RuntimeError("backend fail"))

    dispatcher = LiveIntegrationDispatcher(
        execute_tool=execute_tool,
        gate=gate,
        request_factory=lambda tid, args, inp: MagicMock(),
        event_emitter=event_emitter,
        audit_emitter=audit_emitter,
    )
    await dispatcher("list-events", {}, MagicMock())
    assert any(e.get("type") == "tool.called" for e in events)
    result_events = [e for e in events if e.get("type") == "tool.result"]
    assert len(result_events) == 1
    assert result_events[0].get("is_error") is True
    assert any(a.get("type") == "tool_call_failed" for a in audits)


# ---------------------------------------------------------------------------
# RendererToIntegrationAdapter — Fold 1 shim
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adapter_translates_renderer_kwargs_to_positional_dispatch():
    """Pin (Fold 1): the adapter accepts the renderer's keyword-only
    contract and forwards to the integration dispatcher's positional
    contract. tool_use_id is renderer-side only (correlation in the
    message thread); adapter drops it at the boundary because the
    integration dispatcher doesn't need it."""
    captured: dict = {}

    async def fake_dispatcher(tool_id, args, inputs):
        captured["tool_id"] = tool_id
        captured["args"] = args
        captured["inputs"] = inputs
        return {"text": "result"}

    adapter = build_renderer_to_integration_adapter(
        integration_dispatcher=fake_dispatcher,
        inputs_factory=lambda conv_id: {"conversation_id": conv_id},
    )
    result = await adapter(
        tool_name="list-events",
        tool_input={"q": "today"},
        tool_use_id="tu_123",
        conversation_id="conv-x",
    )

    assert result == "result"
    assert captured["tool_id"] == "list-events"
    assert captured["args"] == {"q": "today"}
    assert captured["inputs"] == {"conversation_id": "conv-x"}


@pytest.mark.asyncio
async def test_adapter_renders_error_dict_as_text_for_renderer_loop():
    """Pin: when the integration dispatcher returns an error dict,
    the adapter renders friendly error text. Renderer's tool-use loop
    appends this verbatim into a tool_result block, giving the model
    a chance to recover."""

    async def fake_dispatcher(tool_id, args, inputs):
        return {"is_error": True, "error": "tool not found"}

    adapter = build_renderer_to_integration_adapter(
        integration_dispatcher=fake_dispatcher,
    )
    result = await adapter(
        tool_name="missing-tool",
        tool_input={},
        tool_use_id="tu",
        conversation_id="c",
    )
    assert "tool not found" in result


@pytest.mark.asyncio
async def test_adapter_passes_text_result_through_unchanged():
    """Pin: text result dict → text return; preserves the model's
    tool_result content shape."""

    async def fake_dispatcher(tool_id, args, inputs):
        return {"text": "calendar event at 2pm"}

    adapter = build_renderer_to_integration_adapter(
        integration_dispatcher=fake_dispatcher,
    )
    result = await adapter(
        tool_name="list-events",
        tool_input={},
        tool_use_id="tu",
        conversation_id="c",
    )
    assert result == "calendar event at 2pm"


# ---------------------------------------------------------------------------
# LivePlannerCatalog — wraps live tool catalog
# ---------------------------------------------------------------------------


def test_planner_catalog_lookup_returns_catalog_entry():
    """Pin: planner catalog wrapper returns the live catalog's entry
    rather than the empty StaticToolCatalog default. Without this
    the planner sees no tools and produces empty plans."""
    catalog = MagicMock()
    catalog.get.return_value = MagicMock(name="list-events")
    planner_catalog = LivePlannerCatalog(tool_catalog=catalog)
    assert planner_catalog.lookup("list-events") is not None


def test_planner_catalog_list_tools_for_planning_returns_all_registered():
    """Pin: list_tools_for_planning() surfaces all live
    registrations as ToolCatalogEntry shape (the planner protocol
    method, NOT a generic list_tools accessor). Pre-fix the wrapper
    only exposed lookup() and list_tools(); planner crashed with
    AttributeError."""
    from kernos.kernel.enactment.planner import ToolCatalogEntry
    catalog = MagicMock()
    entry1 = MagicMock()
    entry1.name = "list-events"
    entry1.description = "List calendar events"
    entry1.source = "mcp"
    entry2 = MagicMock()
    entry2.name = "remember"
    entry2.description = "Remember a fact"
    entry2.source = "kernel"
    catalog.get_all.return_value = [entry1, entry2]
    planner_catalog = LivePlannerCatalog(tool_catalog=catalog)
    tools = planner_catalog.list_tools_for_planning()
    assert len(tools) == 2
    assert all(isinstance(t, ToolCatalogEntry) for t in tools)
    by_id = {t.tool_id: t for t in tools}
    assert by_id["list-events"].tool_class == "mcp"
    assert by_id["remember"].tool_class == "kernel"


def test_planner_catalog_handles_none_catalog_defensively():
    """Pin: defensive — None catalog returns empty for both
    accessors rather than crashing planner construction."""
    pc = LivePlannerCatalog(tool_catalog=None)
    assert pc.lookup("anything") is None
    assert pc.list_tools_for_planning() == []
