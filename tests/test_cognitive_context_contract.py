"""COGNITIVE-CONTEXT-V1 C2 — 14 contract tests as red bars.

Each test boots a representative turn through MessageHandler and
captures the exact ``system=`` and ``tools=`` arguments passed to the
model provider on the final model call. Tests are parametrized over
``[("legacy", False), ("decoupled", True)]``:

* ``legacy`` — ``KERNOS_USE_DECOUPLED_TURN_RUNNER`` unset; ``assemble.py``
  builds the system prompt; all 14 assertions pass — the legacy
  oracle delivers the substrate.

* ``decoupled`` — ``KERNOS_USE_DECOUPLED_TURN_RUNNER=1`` + server-style
  ``turn_runner_provider`` wired; the decoupled ``TurnRunner`` path
  runs; ``PresenceRenderer.render`` builds the system prompt from
  the typed CognitiveContext packet. As of CCV1 C5, all 14
  assertions pass on the decoupled path too. Pre-C5 history: tests
  flipped progressively at C3a (rules + now + state), C3b (results +
  actions + procedures + canvases), C3c (additive directive), C4
  (memory zone), C5 (always_pinned + request_tool). Test 13's
  legacy variant carried a strict-xfail until C5 — removed when C5
  added request_tool to ALWAYS_PINNED on both paths simultaneously.

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
from kernos.kernel.turn_runner import TurnRunner
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


# A deterministic distinctive string to anchor the additive-directive
# assertion (test 14). The decoupled path puts presence_directive in
# the user-message body today; C3c will combine substrate + directive
# into the system render so test 14 can pin both.
PRESENCE_DIRECTIVE_MARKER = "PD-MARKER-be-warm-and-present"


class _StubIntegrationService:
    """Returns a canned RESPOND_ONLY Briefing with a non-empty
    presence_directive. The contract tests focus on the final
    model-call seam (PresenceRenderer -> chain_caller); the
    integration synthesis is not what's under test here.

    Codex C2-review CONCERN (Q2): a stubbed integration could mask a
    C3b bug where the real ``IntegrationService`` drops
    ``request.cognitive_context`` before ``PresenceRenderer``. C3b is
    expected to add an integration-seam test pinning that
    ``cognitive_context`` flows through the briefing.
    """

    def __init__(self, presence_directive: str = PRESENCE_DIRECTIVE_MARKER):
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
            # COGNITIVE-CONTEXT-V1 C3a: copy the typed packet from
            # IntegrationInputs onto the Briefing so PresenceRenderer
            # can render its substrate. The real IntegrationService
            # does the same at every Briefing construction site
            # (see kernos/kernel/integration/runner.py).
            cognitive_context=getattr(inputs, "cognitive_context", None),
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

    async def shared_chain(system, messages, tools, max_tokens, **_):
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


def _inject_compaction_marker(handler: MessageHandler, marker: str) -> None:
    """Stub ``handler.compaction`` so the legacy assembly's MEMORY
    block carries a deterministic ``marker`` substring sourced from
    compaction's index (the canonical compaction-carry surface).

    Compaction's path into ## MEMORY: ``load_state`` returns a state
    with ``index_tokens > 0``; ``load_index`` returns the rendered
    archive index that legacy embeds verbatim under
    "Archived history (summaries — full archives available on
    request):". We mock both so the marker reaches the system prompt.
    """
    state_obj = MagicMock()
    state_obj.index_tokens = 100
    state_obj.document_tokens = 0
    handler.compaction.load_state = AsyncMock(return_value=state_obj)
    handler.compaction.load_index = AsyncMock(return_value=marker)
    handler.compaction.load_context_document = AsyncMock(return_value="")


def _wire_instance_db(
    handler: MessageHandler,
    *,
    member_profile: dict[str, Any] | None = None,
    relationships: list[dict[str, Any]] | None = None,
) -> None:
    """Wire ``handler._instance_db`` with an AsyncMock whose methods
    return safe defaults — none of the handler's many ``await
    self._instance_db.<method>`` call sites raise. ``member_profile``
    feeds provisioning's ``get_member_profile`` lookup; relationships
    feeds the assembly's ``list_relationships`` lookup.
    """
    db = AsyncMock()
    db.check_sender_blocked.return_value = None
    # Returning None here makes the handler treat the sender as
    # unknown and short-circuit with a "private instance" static
    # response BEFORE reaching reasoning. Returning a known-member
    # dict lets the handler proceed to provisioning + assembly.
    db.get_member_by_channel.return_value = {
        "member_id": "sms:+15555550100",
        "platform": "sms",
        "sender_id": "+15555550100",
    }
    db.get_member_profile.return_value = member_profile
    db.get_member.return_value = None
    db.upsert_member_profile.return_value = None
    db.migrate_soul_to_member_profile.return_value = None
    db.get_instance_stewardship.return_value = ""
    db.list_relationships.return_value = relationships or []
    db.get_permission.return_value = "by-permission"
    db.record_sender_failure.return_value = None
    db.clear_sender_failures.return_value = None
    db.claim_invite_code.return_value = None
    db.set_platform_config.return_value = None
    handler._instance_db = db


def _inject_relationship(
    handler: MessageHandler,
    *,
    other_display_name: str,
    permission: str,
    declarer_member_id: str = "sms:+15555550100",
) -> None:
    """Stub ``handler._instance_db.list_relationships`` so the
    STATE block renders a non-default relationship line.

    Legacy renders relationships only when ``permission != "by-permission"``
    (the implicit default); this helper feeds an explicit declared
    permission so the RELATIONSHIPS line surfaces in ## STATE.
    """
    _wire_instance_db(
        handler,
        relationships=[{
            "other_display_name": other_display_name,
            "permission": permission,
            "declarer_member_id": declarer_member_id,
            "other_member_id": "sms:+19999999999",
        }],
    )


def _inject_space_context(
    handler: MessageHandler,
    *,
    results_prefix: str | None = None,
    memory_prefix: str | None = None,
    procedures_prefix: str | None = None,
    canvases_prefix: str | None = None,
) -> None:
    """Bypass the active-space gating logic by mocking
    ``handler._assemble_space_context`` directly. The legacy assembler
    uses the returned tuple to populate ``ctx.results_prefix``,
    ``ctx.memory_prefix``, and the procedures / canvases prefixes that
    feed the corresponding ``## ...`` blocks. Tests use this seam to
    drop deterministic markers into specific zones without standing
    up a full ContextSpace + file-tree fixture.
    """
    handler._assemble_space_context = AsyncMock(return_value=(
        [],  # space_messages
        results_prefix,
        memory_prefix,
        procedures_prefix,
        canvases_prefix,
    ))


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
    # Post-CCV1-C7-strike (2026-05-03): thin path is the only path.
    _ = decoupled  # parameter retained for compat; legacy path removed

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

    Codex C2-review CONCERN (Q3): "last non-schema" is not a unique
    predicate in the long run. With an unstubbed IntegrationService
    or non-thin-path actions, intermediate Planner /
    DivergenceReasoner calls also use no schema. This helper currently
    runs against thin-path turns + a stubbed IntegrationService, so
    the predicate is unique today; the renderer-shape guard below
    will trip if a future test breaks that assumption.
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
    sys_text = _normalize_system(system)
    # Renderer-shape guard: confirm the captured call is the user-
    # reply reasoning call. Either path yields a recognizable shape.
    # Legacy renders the substrate-bearing system prompt with `## RULES`.
    # Decoupled today renders PresenceRenderer's hardcoded kind prompt
    # ("Kernos's presence renderer", "Generate a conversational reply").
    # If neither marker appears, an unexpected intermediate call slipped
    # through the filter — fail loudly with diagnostic context.
    is_legacy_shape = "## RULES" in sys_text or "## NOW" in sys_text
    is_decoupled_shape = (
        "presence renderer" in sys_text.lower()
        or "Generate a conversational reply" in sys_text
        or "## Directive" in sys_text  # C3c-rendered shape
    )
    assert is_legacy_shape or is_decoupled_shape, (
        f"Captured provider.complete call does not look like the "
        f"user-reply reasoning call. The output_schema filter may "
        f"have admitted an intermediate Planner / Reasoner call. "
        f"system head: {sys_text[:300]!r}"
    )
    return sys_text, tools, messages


def _message_text(messages: list[dict]) -> str:
    """Concatenate text content across the messages array for
    substring assertions (the decoupled path puts presence_directive
    in the user-message position today)."""
    parts: list[str] = []
    for m in messages or []:
        c = m.get("content", "")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for blk in c:
                if isinstance(blk, dict):
                    parts.append(str(blk.get("text", "")))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Parametrize over both paths. Test IDs become ``[legacy]`` / ``[decoupled]``.
# ---------------------------------------------------------------------------


PATHS = [
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
#
# The spec row says "UNIQUE / INHERIT" — both selection modes must
# reach the model. Codex C2-review BLOCKER: original test only
# covered UNIQUE, so C3a could ship that branch and silently leave
# INHERIT broken. This test now parametrizes over both modes.
#   - UNIQUE: agent has no name yet (first conversation, original
#     hatching). Renders ``_UNIQUE_HATCHING_PROMPT``.
#   - INHERIT: a different member is meeting an already-named agent.
#     Renders ``_INHERIT_HATCHING_PROMPT``.
# ---------------------------------------------------------------------------


HATCHING_MODES = [
    pytest.param(
        ("",  # agent_name → empty triggers UNIQUE branch
         "HATCHING. This is your first moment of existence"),
        id="unique",
    ),
    pytest.param(
        ("Echo",  # agent already named → INHERIT branch
         "NEW MEMBER."),
        id="inherit",
    ),
]


@pytest.mark.parametrize("decoupled", PATHS)
@pytest.mark.parametrize("mode", HATCHING_MODES)
async def test_hatching_prompt_present_during_hatching(
    mode, decoupled, monkeypatch
):
    agent_name, excerpt = mode
    handler, mock_provider = _make_handler(
        decoupled=decoupled,
        monkeypatch=monkeypatch,
        bootstrap_graduated=False,
        agent_name=agent_name,
    )
    # The bootstrap-rules code reads ``agent_name`` from
    # ``ctx.member_profile``, not from ``soul`` — INHERIT only fires
    # when the active member profile carries an ``agent_name``. Wire
    # ``_instance_db`` so provisioning can produce a member_profile
    # with the right agent_name in this test fixture.
    _wire_instance_db(handler, member_profile={
        "display_name": "TestUser",
        "agent_name": agent_name,
        "bootstrap_graduated": False,
        "interaction_count": 0,
    })
    system, _tools, _messages = await _run_capture(handler, mock_provider)
    assert excerpt in system, (
        f"Hatching prompt ({excerpt!r}) expected in system. "
        f"system head: {system[:400]!r}"
    )


# ---------------------------------------------------------------------------
# Test 4 — covenants reach the model deterministically (not LLM-synthesized)
# Source: Hunt-list row 4 + the design review covenant verdict. Green at: C3a.
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
#
# Spec row 5 reads "MEMORY zone (compaction carry + knowledge entries
# via remember)". Codex C2-review BLOCKER: original test only seeded
# a knowledge entry which legacy renders under STATE / USER CONTEXT,
# not MEMORY. The test could pass while ``memory.compaction_carry``
# remained dropped on decoupled. This version pins compaction's index
# (the canonical compaction-carry surface) AND keeps the knowledge
# assertion as a secondary check.
# ---------------------------------------------------------------------------


COMPACTION_CARRY_MARKER = "CARRY-MARKER-archive-2026-03"


@pytest.mark.parametrize("decoupled", PATHS)
async def test_memory_zone_reaches_model(decoupled, monkeypatch):
    # Knowledge-entry side: identity archetype lands via always_inject
    # (the message_analysis LLM mock returns non-JSON; identity entries
    # bypass the relevance ranker).
    distinct_subject = "preferred_breakfast"
    distinct_content = "User strongly prefers oatmeal at 7am sharp."
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
    # Compaction-carry side: stub the space-context seam so the legacy
    # MEMORY block carries a deterministic marker. C4 wires
    # ``memory.compaction_carry`` on the decoupled path; the marker
    # is what proves the carry actually reaches the model.
    _inject_space_context(handler, memory_prefix=COMPACTION_CARRY_MARKER)
    system, _tools, _messages = await _run_capture(handler, mock_provider)
    # Pin BOTH surfaces — compaction carry (the load-bearing assertion
    # for C4) and the knowledge content. C4 cannot ship green on this
    # test without restoring both.
    assert COMPACTION_CARRY_MARKER in system, (
        f"Compaction-carry marker expected in MEMORY zone. "
        f"Looked for: {COMPACTION_CARRY_MARKER!r}; "
        f"system head: {system[:500]!r}"
    )
    assert distinct_content in system, (
        f"Knowledge entry content expected reachable to model. "
        f"Looked for: {distinct_content!r}; system head: {system[:500]!r}"
    )


# ---------------------------------------------------------------------------
# Test 6 — awareness whispers reach model (gardener decoupled-only enrich.)
# Source: Hunt-list row 6 + gardener_cohort never registered. Green at: C4.
#
# Per the design review's clarification: "awareness whispers as legacy oracle parity;
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
#
# Codex C2-review BLOCKER: the original disjunction (## NOW OR NOW —
# OR ``sms`` lowercased) admits a false positive — the literal "sms"
# can appear in any block. Now requires BOTH the ``## NOW`` header
# AND a deterministic NOW field (the "Current time:" line legacy's
# ``_build_now_block`` always emits) so the test cannot turn green
# without a real NOW block present.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("decoupled", PATHS)
async def test_now_block_present(decoupled, monkeypatch):
    handler, mock_provider = _make_handler(
        decoupled=decoupled, monkeypatch=monkeypatch,
    )
    system, _tools, _messages = await _run_capture(handler, mock_provider)
    assert "## NOW" in system, (
        f"NOW block header expected in system. "
        f"system head: {system[:500]!r}"
    )
    assert "Current time:" in system or "Time:" in system, (
        f"NOW block must carry a Current time field (the canonical "
        f"NOW deterministic field). system head: {system[:500]!r}"
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
#
# Codex C2-review BLOCKER: original disjunction (## RESULTS OR
# PROCEDURES OR CANVASES) was a false positive — the substring
# "CANVASES" appears in the RULES paragraph too, so the test could
# go green without any of the three blocks actually rendering. This
# version seeds DISTINCT markers in each of the three sources and
# asserts each marker reaches the system. C3b cannot ship green on
# this test without restoring all three substrate sources.
# ---------------------------------------------------------------------------


RESULTS_MARKER = "RESULTS-MARKER-evt-soak-2026"
PROCEDURES_MARKER = "PROCEDURES-MARKER-step-eat-then-log"
CANVASES_MARKER = "CANVASES-MARKER-page-shopping-list"


@pytest.mark.parametrize("decoupled", PATHS)
async def test_results_procedures_canvases_block_present(
    decoupled, monkeypatch
):
    handler, mock_provider = _make_handler(
        decoupled=decoupled, monkeypatch=monkeypatch,
    )
    # Seed deterministic content into each of the three zones via the
    # space-context seam (bypasses active_space gating + file-tree
    # setup).
    _inject_space_context(
        handler,
        results_prefix=RESULTS_MARKER,
        procedures_prefix=PROCEDURES_MARKER,
        canvases_prefix=CANVASES_MARKER,
    )
    system, _tools, _messages = await _run_capture(handler, mock_provider)
    assert RESULTS_MARKER in system, (
        f"RESULTS-block content expected in system. "
        f"Looked for: {RESULTS_MARKER!r}; system head: {system[:500]!r}"
    )
    assert PROCEDURES_MARKER in system, (
        f"PROCEDURES-block content expected in system. "
        f"Looked for: {PROCEDURES_MARKER!r}; system head: {system[:500]!r}"
    )
    assert CANVASES_MARKER in system, (
        f"CANVASES-block content expected in system. "
        f"Looked for: {CANVASES_MARKER!r}; system head: {system[:500]!r}"
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
#
# Codex C2-review BLOCKER: the original test only asserted the
# user_name — that is STATE substrate, not sensitivity/disclosure
# substrate. It would turn green as soon as STATE was wired (C3a)
# even if disclosure constraints were still being dropped. This
# version pins legacy's actual disclosure surface: a non-default
# RELATIONSHIPS line in ## STATE that the member-aware renderer
# emits when the active member has a declared permission toward
# another member (full-access in this fixture). The renderer's
# disclosure/sensitivity work in C3b must preserve this line for
# the test to flip green without false-positive risk.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("decoupled", PATHS)
async def test_sensitivity_gates_present(decoupled, monkeypatch):
    handler, mock_provider = _make_handler(
        decoupled=decoupled, monkeypatch=monkeypatch,
    )
    _inject_relationship(
        handler,
        other_display_name="Sibling",
        permission="full-access",
    )
    system, _tools, _messages = await _run_capture(handler, mock_provider)
    # Legacy renders RELATIONSHIPS line: "Sibling (you → full-access)"
    # (with arrow) when the declarer is the active member. The arrow
    # + permission word together are unique enough to anchor without
    # false positives from other substrate.
    assert "RELATIONSHIPS:" in system, (
        f"RELATIONSHIPS section expected in ## STATE block. "
        f"system head: {system[:500]!r}"
    )
    assert "Sibling" in system and "full-access" in system, (
        f"Specific disclosure declaration (other-member name + "
        f"permission) expected in system. "
        f"system head: {system[:500]!r}"
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
# C5 added ``request_tool`` to ALWAYS_PINNED. Both legacy and
# decoupled paths now surface it on every turn — the strict-xfail
# on the legacy variant (added at C2) was removed when C5 landed.
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
# Asserts both halves:
#   1. the substrate (operating_principles head) reaches ``system``
#   2. on the decoupled path, the presence_directive marker also
#      reaches ``system`` — NOT messages. C3c is the phase that
#      combines substrate + directive into a single deliberate
#      system render; until C3c the directive lives in the user-
#      message body and this half stays red.
# Legacy doesn't produce a presence_directive at all, so the
# directive-side assertion runs only on the decoupled path.
#
# Codex C3a-design CONCERN (Q5): the prior fold accepted directive
# in ``system OR messages`` which would have flipped this test
# green at C3a (renderer adds substrate to system; directive was
# already in messages). Tightening to ``directive in system``
# preserves the C3c flip-green moment as the deliberate combination
# of substrate + directive.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("decoupled", PATHS)
async def test_presence_directive_additive_not_replacing_substrate(
    decoupled, monkeypatch
):
    handler, mock_provider = _make_handler(
        decoupled=decoupled, monkeypatch=monkeypatch,
    )
    system, _tools, _messages = await _run_capture(handler, mock_provider)
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
    if decoupled:
        # Tightened per Codex C3a-design Q5: require the directive in
        # ``system`` specifically. C3a will render substrate to system
        # but leave the directive in messages; C3c is the phase that
        # combines them into the same deliberate system render.
        assert PRESENCE_DIRECTIVE_MARKER in system, (
            f"presence_directive marker expected in system on the "
            f"decoupled path — substrate + directive must be "
            f"combined deliberately at the renderer (C3c). "
            f"Looked for: {PRESENCE_DIRECTIVE_MARKER!r}; "
            f"system head: {system[:300]!r}"
        )
