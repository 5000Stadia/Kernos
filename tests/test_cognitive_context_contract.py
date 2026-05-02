"""COGNITIVE-CONTEXT-V1 C2 — 14 contract tests as red bars.

Each test boots a representative turn through MessageHandler and
captures the exact ``system=`` and ``tools=`` arguments passed to the
model provider on the final model call. Tests are parametrized over
``[("legacy", False), ("decoupled", True)]``:

* ``legacy`` — ``KERNOS_USE_DECOUPLED_TURN_RUNNER`` unset; ``assemble.py``
  builds the system prompt; the assertion currently passes — proving
  the assertion is correctly written and the legacy oracle delivers
  the substrate.

* ``decoupled`` — ``KERNOS_USE_DECOUPLED_TURN_RUNNER=1`` + server-style
  ``turn_runner_provider`` wired; the decoupled ``TurnRunner`` path
  runs; ``PresenceRenderer.render`` builds the system prompt; the
  same assertion fails (currently — these are the red bars that
  turn green progressively at C3a, C3b, C3c, C4, C5).

Wiring at a glance::

  Legacy:    handler.process()
               -> reasoning.reason()
               -> _reason_with_chain
               -> mock_provider.complete(system=, tools=, ...)

  Decoupled: handler.process()
               -> reasoning.reason()
               -> _run_via_turn_runner_provider
               -> TurnRunner -> EnactmentService -> PresenceRenderer
               -> shared_chain (proxy)
               -> mock_provider.complete(system=, tools=, ...)

Both paths land on ``mock_provider.complete.call_args_list``, so a
single capture seam covers both. Assertions probe the LAST call's
``system`` and ``tools`` (the final model invocation that produces
the user-visible reply).

Each row of the spec table maps to one test below. The wiring ladder
identifies the phase at which each test is expected to flip from
red to green:

  C3a -> tests 1, 2, 3, 4, 7, 8
  C3b -> tests 9, 10, 11
  C3c -> test 14
  C4  -> tests 5, 6
  C5  -> tests 12, 13
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.capability.client import MCPClientManager
from kernos.capability.registry import (
    CapabilityInfo,
    CapabilityRegistry,
    CapabilityStatus,
)
from kernos.kernel.cohorts.descriptor import CohortFanOutResult
from kernos.kernel.enactment import (
    DivergenceReasoner,
    EnactmentService,
    Planner,
    PresenceRenderer,
    StaticToolCatalog,
    StepDispatcher,
)
from kernos.kernel.enactment.dispatcher import (
    ToolDescriptorLookup,
    ToolExecutor,
)
from kernos.kernel.engine import TaskEngine
from kernos.kernel.events import EventStream
from kernos.kernel.integration.briefing import (
    AuditTrace,
    Briefing,
    RespondOnly,
)
from kernos.kernel.integration.service import IntegrationService
from kernos.kernel.reasoning import (
    ContentBlock,
    Provider,
    ProviderResponse,
    ReasoningService,
)
from kernos.kernel.response_delivery import (
    AggregatedTelemetry,
    ProductionResponseDelivery,
    wrap_chain_caller_with_telemetry,
)
from kernos.kernel.soul import Soul
from kernos.kernel.state import (
    CovenantRule,
    InstanceProfile,
    KnowledgeEntry,
    StateStore,
)
from kernos.kernel.template import PRIMARY_TEMPLATE
from kernos.kernel.turn_runner import FEATURE_FLAG_ENV, TurnRunner
from kernos.messages.handler import (
    _INHERIT_HATCHING_PROMPT,
    _UNIQUE_HATCHING_PROMPT,
    MessageHandler,
)
from kernos.messages.models import AuthLevel, NormalizedMessage
from kernos.persistence import (
    AuditStore,
    ConversationStore,
    InstanceStore,
)


# ---------------------------------------------------------------------------
# Capture helpers
# ---------------------------------------------------------------------------


def _normalize_system(system: Any) -> str:
    """Render the captured ``system`` argument to a plain string for
    substring assertions. Provider accepts ``str | list[dict]`` so
    handle both shapes."""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts: list[str] = []
        for block in system:
            if isinstance(block, dict):
                parts.append(str(block.get("text", "")))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return str(system or "")


def _tool_names(tools: Any) -> list[str]:
    """Extract tool names from the captured ``tools`` argument."""
    if not tools:
        return []
    out: list[str] = []
    for t in tools:
        if isinstance(t, dict):
            name = t.get("name") or t.get("function", {}).get("name", "")
            if name:
                out.append(name)
    return out


# ---------------------------------------------------------------------------
# Provider response fixtures
# ---------------------------------------------------------------------------


def _resp_text(text: str = "ok") -> ProviderResponse:
    return ProviderResponse(
        content=[ContentBlock(type="text", text=text)],
        stop_reason="end_turn",
        input_tokens=10,
        output_tokens=20,
    )


# ---------------------------------------------------------------------------
# Stub IntegrationService — returns canned Briefing so the decoupled
# path reaches PresenceRenderer's model call deterministically.
# ---------------------------------------------------------------------------


class _StubIntegrationService:
    """Returns a canned RESPOND_ONLY Briefing with a non-empty
    presence_directive. The contract tests focus on the final
    model-call seam (PresenceRenderer -> chain_caller); the
    integration synthesis is not what's under test here."""

    def __init__(self, presence_directive: str = "Be present and warm."):
        self._directive = presence_directive

    async def run(self, inputs: Any) -> Briefing:
        return Briefing(
            relevant_context=(),
            filtered_context=(),
            decided_action=RespondOnly(),
            presence_directive=self._directive,
            audit_trace=AuditTrace(),
            turn_id="turn-contract",
            integration_run_id="ir-contract",
        )


class _StubCohortRunner:
    """Empty fan-out — no cohorts, but the runner contract is honored
    so TurnRunner's seam doesn't trip on missing fields."""

    async def run(self, ctx: Any) -> CohortFanOutResult:
        return CohortFanOutResult(
            outputs=(),
            fan_out_started_at="2026-05-01T00:00:00+00:00",
            fan_out_completed_at="2026-05-01T00:00:01+00:00",
        )


class _ExploderExecutor:
    """Thin-path turns never dispatch tools; an exploder catches the
    case where wiring goes wrong and dispatch fires unexpectedly."""

    async def execute(self, inputs: Any) -> Any:
        raise RuntimeError(
            "C2 contract tests use thin-path turns; tool dispatch "
            "should not fire."
        )


class _ExploderLookup:
    def descriptor_for(self, tool_id: str) -> Any:
        raise NotImplementedError(
            "C2 contract tests use thin-path turns; descriptor lookup "
            "should not fire."
        )


# ---------------------------------------------------------------------------
# Server-style wiring builder for the decoupled path
# ---------------------------------------------------------------------------


def _build_decoupled_provider(mock_provider: AsyncMock):
    """Construct a turn_runner_provider mirroring server.py's per-turn
    factory, but with shared_chain proxied to ``mock_provider.complete``
    so the same capture seam works for both paths.

    The returned provider takes ``(request, event_emitter)`` and yields
    ``(TurnRunner, ProductionResponseDelivery)`` consistent with the
    production contract.
    """
    catalog = StaticToolCatalog()

    async def shared_chain(system, messages, tools, max_tokens):
        # Proxy to mock_provider.complete so call_args land on a single
        # capture seam shared with the legacy path. The contract tests
        # assert on whatever the renderer chose to send here.
        return await mock_provider.complete(
            model="claude-sonnet-4-6",
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
        )

    async def integration_dispatcher(tool_id, args, inputs):
        return {}

    async def integration_audit_emitter(entry):
        pass

    async def dispatcher_event_emitter(payload):
        pass

    async def dispatcher_audit_emitter(entry):
        pass

    cohort_runner = _StubCohortRunner()

    def turn_runner_provider(request, event_emitter):
        telemetry = AggregatedTelemetry()
        wrapped = wrap_chain_caller_with_telemetry(shared_chain, telemetry)

        planner = Planner(chain_caller=wrapped, tool_catalog=catalog)
        dispatcher = StepDispatcher(
            executor=_ExploderExecutor(),
            descriptor_lookup=_ExploderLookup(),
            trace_sink=[],
            event_emitter=dispatcher_event_emitter,
            audit_emitter=dispatcher_audit_emitter,
            on_dispatch_complete=telemetry.add_tool_iteration,
        )
        reasoner = DivergenceReasoner(chain_caller=wrapped)
        presence = PresenceRenderer(chain_caller=wrapped)
        # The integration service is stubbed — this test surface is
        # specifically about the substrate the model receives on the
        # final PresenceRenderer call; integration synthesis is not
        # the seam under contract here.
        integration = _StubIntegrationService()
        enactment = EnactmentService(
            presence_renderer=presence,
            planner=planner,
            step_dispatcher=dispatcher,
            divergence_reasoner=reasoner,
        )
        delivery = ProductionResponseDelivery(
            request=request,
            telemetry=telemetry,
            event_emitter=event_emitter,
        )
        runner = TurnRunner(
            cohort_runner=cohort_runner,
            integration_service=integration,
            enactment_service=enactment,
            response_delivery=delivery,
        )
        return runner, delivery

    return turn_runner_provider


# ---------------------------------------------------------------------------
# Handler builder
# ---------------------------------------------------------------------------


def _make_message(content: str = "Hello") -> NormalizedMessage:
    return NormalizedMessage(
        content=content,
        sender="+15555550100",
        sender_auth_level=AuthLevel.owner_unverified,
        platform="sms",
        platform_capabilities=["text", "mms"],
        conversation_id="+15555550100",
        timestamp=datetime.now(timezone.utc),
        instance_id="sms:+15555550100",
    )


def _make_registry(tools: list[dict] | None = None) -> MagicMock:
    registry = MagicMock(spec=CapabilityRegistry)
    tools_list = tools or []
    registry.get_connected_tools.return_value = tools_list
    registry.get_tools_for_space.return_value = tools_list
    registry.build_capability_prompt.return_value = (
        "CURRENT CAPABILITIES — conversation only."
        if not tools_list
        else "CONNECTED CAPABILITIES — calendar tools available."
    )
    registry.build_tool_directory.return_value = (
        "CAPABILITIES: None connected yet."
        if not tools_list
        else "CONNECTED SERVICES: Test Capability"
    )
    registry.get_preloaded_tools.return_value = tools_list
    registry.get_lazy_tool_stubs.return_value = []
    registry.get_all_tool_names.return_value = {t["name"] for t in tools_list}
    by_name = {t["name"]: t for t in tools_list}
    registry.get_tool_schema.side_effect = lambda n: by_name.get(n)
    registry.get_all.return_value = []
    if tools_list:
        cap = CapabilityInfo(
            name="test-capability",
            display_name="Test Capability",
            description="Test capability",
            category="test",
            status=CapabilityStatus.CONNECTED,
            tools=[t["name"] for t in tools_list],
            server_name="test",
            tool_effects={n: "read" for n in (t["name"] for t in tools_list)},
        )
        registry.get_all.return_value = [cap]
    return registry


def _make_state(
    *,
    bootstrap_graduated: bool = True,
    agent_name: str = "TestAgent",
    user_name: str = "TestUser",
    covenants: list[CovenantRule] | None = None,
    knowledge: list[KnowledgeEntry] | None = None,
) -> AsyncMock:
    state = AsyncMock(spec=StateStore)
    state.get_instance_profile.return_value = InstanceProfile(
        instance_id="sms:+15555550100",
        status="active",
        created_at="2026-03-01T00:00:00Z",
    )
    state.get_conversation_summary.return_value = None
    state.save_conversation_summary.return_value = None
    state.save_instance_profile.return_value = None
    soul = Soul(
        instance_id="sms:+15555550100",
        user_name=user_name,
        hatched=not (not bootstrap_graduated),  # hatched mirrors graduation
        bootstrap_graduated=bootstrap_graduated,
        agent_name=agent_name,
        interaction_count=5 if bootstrap_graduated else 0,
    )
    state.get_soul.return_value = soul
    state.save_soul.return_value = None
    state.get_contract_rules.return_value = covenants or []
    state.query_covenant_rules.return_value = covenants or []
    state.list_context_spaces.return_value = []
    state.get_context_space.return_value = None
    state.get_knowledge_hashes.return_value = set()
    state.query_knowledge.return_value = knowledge or []
    return state


def _make_handler(
    *,
    decoupled: bool,
    monkeypatch,
    tools: list[dict] | None = None,
    bootstrap_graduated: bool = True,
    agent_name: str = "TestAgent",
    user_name: str = "TestUser",
    covenants: list[CovenantRule] | None = None,
    knowledge: list[KnowledgeEntry] | None = None,
) -> tuple[MessageHandler, AsyncMock]:
    """Build a handler wired for either legacy or decoupled path.

    Both paths share the same ``mock_provider`` capture seam.
    """
    if decoupled:
        monkeypatch.setenv(FEATURE_FLAG_ENV, "1")
    else:
        monkeypatch.delenv(FEATURE_FLAG_ENV, raising=False)

    mcp = MagicMock(spec=MCPClientManager)
    mcp.get_tools.return_value = tools or []
    conversations = AsyncMock(spec=ConversationStore)
    conversations.get_recent.return_value = []
    conversations.get_recent_full.return_value = []
    conversations.get_space_thread.return_value = []
    conversations.get_cross_domain_messages.return_value = []
    conversations.append.return_value = None
    tenants = AsyncMock(spec=InstanceStore)
    tenants.get_or_create.return_value = {
        "instance_id": "sms:+15555550100",
        "status": "active",
        "created_at": "2026-03-01T00:00:00Z",
        "capabilities": {},
    }
    audit = AsyncMock(spec=AuditStore)
    audit.log.return_value = None
    events = AsyncMock(spec=EventStream)
    events.emit.return_value = None
    state = _make_state(
        bootstrap_graduated=bootstrap_graduated,
        agent_name=agent_name,
        user_name=user_name,
        covenants=covenants,
        knowledge=knowledge,
    )
    mock_provider = AsyncMock(spec=Provider)
    mock_provider.complete.return_value = _resp_text("hi")
    registry = _make_registry(tools)

    if decoupled:
        provider_factory = _build_decoupled_provider(mock_provider)
        reasoning = ReasoningService(
            mock_provider,
            events,
            mcp,
            audit,
            turn_runner_provider=provider_factory,
        )
    else:
        reasoning = ReasoningService(mock_provider, events, mcp, audit)
    engine = TaskEngine(reasoning=reasoning, events=events)
    handler = MessageHandler(
        mcp, conversations, tenants, audit, events, state,
        reasoning, registry, engine,
    )
    handler.preference_parsing_enabled = False
    return handler, mock_provider


async def _run_capture(
    handler: MessageHandler,
    mock_provider: AsyncMock,
    *,
    content: str = "Hello",
) -> tuple[str, list[dict], list[dict]]:
    """Run a turn through the handler and return the captured
    ``(system, tools, messages)`` from the FINAL reasoning call.

    The handler issues multiple provider calls per turn:

    * MESSAGE_ANALYSIS (uses ``output_schema=`` for structured output)
    * the user-reply reasoning call (no output_schema; produces the
      conversational response)

    The contract tests target the user-reply reasoning call. We filter
    to the LAST call without an ``output_schema`` argument — which is
    PresenceRenderer's render on decoupled and ``_reason_with_chain``
    on legacy.
    """
    await handler.process(_make_message(content))
    assert mock_provider.complete.called, (
        "Expected provider.complete to be called at least once "
        "during the turn."
    )
    reasoning_calls = [
        c for c in mock_provider.complete.call_args_list
        if c.kwargs.get("output_schema") is None
    ]
    assert reasoning_calls, (
        "Expected at least one non-schema reasoning call to provider. "
        f"All calls had output_schema set; total calls: "
        f"{len(mock_provider.complete.call_args_list)}"
    )
    last = reasoning_calls[-1]
    system = last.kwargs.get("system", "")
    tools = last.kwargs.get("tools", []) or []
    messages = last.kwargs.get("messages", []) or []
    return _normalize_system(system), tools, messages


# ---------------------------------------------------------------------------
# Parametrize over both paths. Test IDs become ``[legacy]`` / ``[decoupled]``.
# ---------------------------------------------------------------------------


PATHS = [
    pytest.param(False, id="legacy"),
    pytest.param(True, id="decoupled"),
]


# ---------------------------------------------------------------------------
# Test 1 — bootstrap_prompt verbatim when bootstrap_graduated == False
# Source: Hunt-list bootstrap injection drop. Green at: C3a.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("decoupled", PATHS)
async def test_bootstrap_prompt_present_when_not_graduated(
    decoupled, monkeypatch
):
    handler, mock_provider = _make_handler(
        decoupled=decoupled,
        monkeypatch=monkeypatch,
        bootstrap_graduated=False,
        agent_name="",  # hatching not yet named
    )
    system, _tools, _messages = await _run_capture(handler, mock_provider)
    # A canonical excerpt from PRIMARY_TEMPLATE.bootstrap_prompt that
    # is unique enough to anchor the assertion.
    excerpt = "FIRST CONVERSATION"
    assert excerpt in system, (
        f"Bootstrap prompt expected in system but not found. "
        f"system head: {system[:300]!r}"
    )


# ---------------------------------------------------------------------------
# Test 2 — operating_principles present
# Source: Hunt-list row 2. Green at: C3a.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("decoupled", PATHS)
async def test_operating_principles_present(decoupled, monkeypatch):
    handler, mock_provider = _make_handler(
        decoupled=decoupled, monkeypatch=monkeypatch,
    )
    system, _tools, _messages = await _run_capture(handler, mock_provider)
    # Anchor on a unique-to-operating_principles phrase. The principles
    # block opens with content distinct from any other substrate.
    sample = PRIMARY_TEMPLATE.operating_principles[:80]
    # First non-trivial sentence; trimmed to avoid trailing-newline
    # mismatches across renderers.
    head = sample.strip().splitlines()[0].strip()
    assert head and head in system, (
        f"Operating principles expected in system. "
        f"Looked for: {head!r}; system head: {system[:300]!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 — hatching prompt (UNIQUE / INHERIT) present during hatching
# Source: Hunt-list row 3. Green at: C3a.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("decoupled", PATHS)
async def test_hatching_prompt_present_during_hatching(
    decoupled, monkeypatch
):
    handler, mock_provider = _make_handler(
        decoupled=decoupled,
        monkeypatch=monkeypatch,
        bootstrap_graduated=False,
        agent_name="",  # UNIQUE hatching path (no inherited identity)
    )
    system, _tools, _messages = await _run_capture(handler, mock_provider)
    # The UNIQUE hatching prompt has a load-bearing line that must
    # reach the model on a hatching turn.
    excerpt = "HATCHING. This is your first moment of existence"
    assert excerpt in system, (
        f"UNIQUE hatching prompt expected in system. "
        f"system head: {system[:400]!r}"
    )


# ---------------------------------------------------------------------------
# Test 4 — covenants reach the model deterministically (not LLM-synthesized)
# Source: Hunt-list row 4 + Kit covenant verdict. Green at: C3a.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("decoupled", PATHS)
async def test_active_covenants_reach_model_deterministically(
    decoupled, monkeypatch
):
    distinct = "do not send replies before 7am"
    covenant = CovenantRule(
        id="rule_test01",
        instance_id="sms:+15555550100",
        capability="general",
        rule_type="must_not",
        description=distinct,
        active=True,
        source="user_stated",
        layer="practice",
    )
    handler, mock_provider = _make_handler(
        decoupled=decoupled,
        monkeypatch=monkeypatch,
        covenants=[covenant],
    )
    system, _tools, _messages = await _run_capture(handler, mock_provider)
    assert distinct in system, (
        f"Active covenant text expected in system verbatim "
        f"(deterministic, not summarized). Looked for: {distinct!r}; "
        f"system head: {system[:400]!r}"
    )


# ---------------------------------------------------------------------------
# Test 5 — MEMORY zone reaches model (compaction carry + knowledge entries)
# Source: Hunt-list row 5 + memory_cohort never registered. Green at: C4.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("decoupled", PATHS)
async def test_memory_zone_reaches_model(decoupled, monkeypatch):
    distinct_subject = "preferred_breakfast"
    distinct_content = "User strongly prefers oatmeal at 7am sharp."
    # ``lifecycle_archetype="identity"`` puts the entry into the
    # always_inject set so it lands in user_knowledge_entries
    # regardless of the message_analysis LLM's relevance ranking
    # (which defaults to empty when the analysis call returns
    # non-JSON, as in our mocked fixture).
    entry = KnowledgeEntry(
        id="know_test01",
        instance_id="sms:+15555550100",
        category="preference",
        subject=distinct_subject,
        content=distinct_content,
        confidence="stated",
        source_event_id="evt_test",
        source_description="explicitly stated 2026-04-15",
        created_at="2026-04-15T00:00:00Z",
        last_referenced="2026-04-30T00:00:00Z",
        tags=["food", "morning"],
        lifecycle_archetype="identity",
    )
    handler, mock_provider = _make_handler(
        decoupled=decoupled, monkeypatch=monkeypatch, knowledge=[entry],
    )
    system, _tools, _messages = await _run_capture(handler, mock_provider)
    assert distinct_content in system, (
        f"Knowledge entry content expected in MEMORY zone. "
        f"Looked for: {distinct_content!r}; system head: {system[:500]!r}"
    )


# ---------------------------------------------------------------------------
# Test 6 — awareness whispers reach model (gardener decoupled-only enrich.)
# Source: Hunt-list row 6 + gardener_cohort never registered. Green at: C4.
#
# Per Kit's clarification: "awareness whispers as legacy oracle parity;
# gardener observations as decoupled-only enrichment, not legacy-oracle
# equivalence assertion." This test pins awareness whispers reaching
# the model on both paths.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("decoupled", PATHS)
async def test_awareness_whispers_reach_model(decoupled, monkeypatch):
    handler, mock_provider = _make_handler(
        decoupled=decoupled, monkeypatch=monkeypatch,
    )
    # Wire a pending whisper via state.get_pending_whispers — the
    # state-store seam the handler reads for awareness rendering.
    # Legacy renders pending whispers into the RESULTS zone of the
    # system prompt.
    from kernos.kernel.awareness import Whisper
    distinct = "remind user about water cup"
    whisper = Whisper(
        whisper_id="wsp_test01",
        insight_text=distinct,
        delivery_class="ambient",
        source_space_id="",
        target_space_id="",
        supporting_evidence=[],
        reasoning_trace="test fixture",
        knowledge_entry_id="",
        foresight_signal="",
        created_at="2026-04-30T00:00:00Z",
        owner_member_id="",  # instance-wide — visible regardless of member
    )
    handler.state.get_pending_whispers = AsyncMock(return_value=[whisper])
    handler.state.mark_whisper_surfaced = AsyncMock(return_value=None)
    system, _tools, _messages = await _run_capture(handler, mock_provider)
    assert distinct in system, (
        f"Awareness whisper text expected in system. "
        f"Looked for: {distinct!r}; system head: {system[:500]!r}"
    )


# ---------------------------------------------------------------------------
# Test 7 — NOW block content reaches model
# Source: Hunt-list row 7. Green at: C3a.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("decoupled", PATHS)
async def test_now_block_present(decoupled, monkeypatch):
    handler, mock_provider = _make_handler(
        decoupled=decoupled, monkeypatch=monkeypatch,
    )
    system, _tools, _messages = await _run_capture(handler, mock_provider)
    # The NOW block carries platform + member identifiers as a
    # distinctive pair only the NOW block produces.
    assert "## NOW" in system or "NOW —" in system or "sms" in system.lower(), (
        f"NOW block expected in system (looking for header or "
        f"platform marker). system head: {system[:500]!r}"
    )


# ---------------------------------------------------------------------------
# Test 8 — STATE block content reaches model
# Source: Hunt-list row 8. Green at: C3a.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("decoupled", PATHS)
async def test_state_block_present(decoupled, monkeypatch):
    handler, mock_provider = _make_handler(
        decoupled=decoupled,
        monkeypatch=monkeypatch,
        user_name="DistinctNameForState42",
    )
    system, _tools, _messages = await _run_capture(handler, mock_provider)
    # The STATE block surfaces user_name so the agent has identity
    # context for the turn. Distinctive name anchors the assertion.
    assert "DistinctNameForState42" in system, (
        f"STATE-block content (user_name) expected in system. "
        f"system head: {system[:500]!r}"
    )


# ---------------------------------------------------------------------------
# Test 9 — RESULTS / PROCEDURES / canvases blocks
# Source: Hunt-list rows 9-12. Green at: C3b.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("decoupled", PATHS)
async def test_results_procedures_canvases_block_present(
    decoupled, monkeypatch
):
    handler, mock_provider = _make_handler(
        decoupled=decoupled, monkeypatch=monkeypatch,
    )
    system, _tools, _messages = await _run_capture(handler, mock_provider)
    # Legacy assemble.py renders headers for these zones. C3b adds
    # them to the decoupled packet path.
    found_marker = any(
        m in system for m in ("## RESULTS", "PROCEDURES", "CANVASES")
    )
    assert found_marker, (
        f"RESULTS / PROCEDURES / CANVASES headers expected in system. "
        f"system head: {system[:500]!r}"
    )


# ---------------------------------------------------------------------------
# Test 10 — ACTIONS block reaches model
# Source: Hunt-list row 13. Green at: C3b.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("decoupled", PATHS)
async def test_actions_block_present(decoupled, monkeypatch):
    handler, mock_provider = _make_handler(
        decoupled=decoupled, monkeypatch=monkeypatch,
    )
    system, _tools, _messages = await _run_capture(handler, mock_provider)
    assert "## ACTIONS" in system or "CAPABILITIES" in system, (
        f"ACTIONS block / capabilities prompt expected in system. "
        f"system head: {system[:500]!r}"
    )


# ---------------------------------------------------------------------------
# Test 11 — sensitivity gates / multi-member disclosure
# Source: Hunt-list rows 14-15. Green at: C3b.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("decoupled", PATHS)
async def test_sensitivity_gates_present(decoupled, monkeypatch):
    """Multi-member disclosure / sensitivity gates reach the model.

    Legacy renders a member-identity line and (when in a multi-member
    context) a disclosure-layer block. The marker we assert on is the
    member-identity surface text — distinctive enough that absence
    indicates the safety substrate was dropped.
    """
    handler, mock_provider = _make_handler(
        decoupled=decoupled,
        monkeypatch=monkeypatch,
        user_name="MultiMemberSafetyMarker",
    )
    system, _tools, _messages = await _run_capture(handler, mock_provider)
    # The disclosure / sensitivity surface manifests as the
    # member-identity line ("you are talking with X") plus any
    # cross-member rules. On single-member testing fixtures the
    # cross-member block is empty; we anchor on the member identity
    # marker reaching the system prompt deterministically.
    assert "MultiMemberSafetyMarker" in system, (
        f"Sensitivity / disclosure substrate (member identity) "
        f"expected in system. system head: {system[:500]!r}"
    )


# ---------------------------------------------------------------------------
# Test 12 — ALWAYS_PINNED tools reach model
# Source: Hunt-list row 19. Green at: C5.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("decoupled", PATHS)
async def test_always_pinned_tools_present(decoupled, monkeypatch):
    handler, mock_provider = _make_handler(
        decoupled=decoupled, monkeypatch=monkeypatch,
    )
    _system, tools, _messages = await _run_capture(handler, mock_provider)
    names = _tool_names(tools)
    # ``remember_details`` is the canonical-named ALWAYS_PINNED entry
    # (deep memory retrieval). Legacy surfaces every ALWAYS_PINNED
    # tool on every turn; the decoupled thin-path currently sends
    # an empty tools list. ``remember`` (the alias) is gated behind
    # a wired RetrievalService — out of scope for this assertion.
    assert "remember_details" in names, (
        f"ALWAYS_PINNED tools (e.g., 'remember_details') expected "
        f"in tools. Got: {names!r}"
    )


# ---------------------------------------------------------------------------
# Test 13 — request_tool reaches model
# Source: Hunt-list row 18. Green at: C5.
#
# Documented deviation from the C2 acceptance "all 14 pass legacy"
# framing: request_tool is NOT in ``ALWAYS_PINNED`` today on either
# path (the legacy assemble surfacer adds it conditionally only when
# the analyzer requests it). The C5 work explicitly ships it INTO
# ``ALWAYS_PINNED`` per the spec — at which point both legacy AND
# decoupled paths surface it on every turn. So this test currently
# fails BOTH paths and turns green simultaneously on both at C5.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("decoupled", PATHS)
async def test_request_tool_present(decoupled, monkeypatch):
    handler, mock_provider = _make_handler(
        decoupled=decoupled, monkeypatch=monkeypatch,
    )
    _system, tools, _messages = await _run_capture(handler, mock_provider)
    names = _tool_names(tools)
    assert "request_tool" in names, (
        f"request_tool meta-recovery tool expected in tools. "
        f"Got: {names!r}"
    )


# ---------------------------------------------------------------------------
# Test 14 — presence_directive ADDITIVELY (not replacing substrate)
# Source: Architectural mental-model mismatch reconciliation. Green at: C3c.
#
# The substrate (operating_principles canonical excerpt) MUST reach
# the model even when a non-empty presence_directive is set. Legacy
# does not produce a presence_directive but trivially carries the
# substrate. Decoupled today produces a presence_directive that
# replaces the substrate; C3c fixes the renderer to combine both.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("decoupled", PATHS)
async def test_presence_directive_additive_not_replacing_substrate(
    decoupled, monkeypatch
):
    handler, mock_provider = _make_handler(
        decoupled=decoupled, monkeypatch=monkeypatch,
    )
    system, _tools, _messages = await _run_capture(handler, mock_provider)
    # Substrate-canonical anchor — the operating_principles head must
    # reach the model. Decoupled currently sends only the
    # presence_directive (or hardcoded kind-prompt) and drops this.
    head = (
        PRIMARY_TEMPLATE.operating_principles
        .strip()
        .splitlines()[0]
        .strip()
    )
    assert head and head in system, (
        f"Substrate (operating_principles head) expected in system "
        f"alongside any presence_directive. Looked for: {head!r}; "
        f"system head: {system[:400]!r}"
    )
