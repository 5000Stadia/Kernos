"""WTC v1 C5c-bringup-crb — production CRB substrate wiring smoke test.

Pins:

* ``bring_up_substrate`` constructs and starts ``InstallProposalStore``,
  ``CRBProposalAuthor``, and ``CRBApprovalFlow`` — the trio that was
  out of scope for the original C5c-bringup commit.
* The bring-up adapters structurally satisfy the typed Protocols
  shipped with ``CRBApprovalFlow`` (``DraftReadPort``,
  ``STSRegistrationPort``, ``CRBEventPort``).
* The ``"crb"`` ``source_module`` is registered exactly once via the
  EmitterRegistry; a second bring-up call within the same process
  finds the already-registered emitter and reuses it.
* ``ReasoningLLMAdapter`` forwards prompts to
  ``ReasoningService.complete_simple`` and returns its raw text.
* ``tear_down_substrate`` stops ``InstallProposalStore`` cleanly.
"""
from __future__ import annotations

from typing import Any

import pytest

from kernos.kernel import event_stream
from kernos.kernel.crb.approval.flow import CRBApprovalFlow
from kernos.kernel.crb.approval.ports import (
    CRBEventPort,
    DraftReadPort,
    STSRegistrationPort,
)
from kernos.kernel.crb.bringup_adapters import (
    DraftRegistryReadAdapter,
    ReasoningLLMAdapter,
    SubstrateToolsSTSAdapter,
)
from kernos.kernel.crb.events import CRB_SOURCE_MODULE, CRBEventEmitter
from kernos.kernel.crb.proposal.author import (
    MAX_TEMPERATURE,
    CRBProposalAuthor,
)
from kernos.kernel.crb.proposal.install_proposal_store import (
    InstallProposalStore,
)
from kernos.setup.bring_up_substrate import (
    Substrate,
    bring_up_substrate,
    tear_down_substrate,
)


# ---------------------------------------------------------------------------
# Handler stub — adds complete_simple to the C5c-bringup _StubReasoning
# so ReasoningLLMAdapter can be constructed against handler.reasoning.
# ---------------------------------------------------------------------------


class _StubReasoning:
    def __init__(self) -> None:
        self.complete_simple_calls: list[dict] = []

    async def execute_tool(self, **kwargs: Any) -> Any:
        return {"executed": True, "kwargs": kwargs}

    async def complete_simple(
        self,
        system_prompt: str,
        user_content: str,
        max_tokens: int = 1024,
        prefer_cheap: bool = False,
        output_schema: dict | None = None,
        chain: str | None = None,
    ) -> str:
        self.complete_simple_calls.append({
            "system_prompt": system_prompt,
            "user_content": user_content,
            "max_tokens": max_tokens,
        })
        return "stub-completion"


class _StubState:
    async def set_preference(self, **kwargs: Any) -> Any:
        return None

    async def get_preference(self, **kwargs: Any) -> Any:
        return None


class _StubHandler:
    def __init__(self) -> None:
        self.reasoning = _StubReasoning()
        self.state = _StubState()
        self.send_outbound_calls: list[dict] = []

    async def send_outbound(
        self,
        instance_id: str,
        member_id: str,
        channel_name: str | None,
        message: str,
    ) -> int:
        self.send_outbound_calls.append({
            "instance_id": instance_id,
            "member_id": member_id,
            "channel_name": channel_name,
            "message": message,
        })
        return 1

    def _get_canvas_service(self) -> None:
        return None


@pytest.fixture
async def event_stream_started(tmp_path):
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path), flush_interval_s=0.05)
    yield
    await event_stream.stop_writer()
    await event_stream._reset_for_tests()


@pytest.fixture
async def agent_registry(tmp_path):
    from kernos.kernel.agents.providers import (
        ProviderRegistry as DARProviderRegistry,
    )
    from kernos.kernel.agents.registry import AgentRegistry
    from kernos.kernel.workflows.agent_inbox import InMemoryAgentInbox
    dar_pr = DARProviderRegistry()
    dar_pr.register("inmemory", lambda ref: InMemoryAgentInbox())
    agents = AgentRegistry(provider_registry=dar_pr)
    await agents.start(str(tmp_path))
    yield agents
    await agents.stop()


# ---------------------------------------------------------------------------
# Construction smoke
# ---------------------------------------------------------------------------


async def test_bring_up_constructs_crb_components(
    tmp_path, event_stream_started, agent_registry,
):
    handler = _StubHandler()
    sub = await bring_up_substrate(
        data_dir=str(tmp_path),
        handler=handler,
        agent_registry=agent_registry,
    )
    try:
        assert isinstance(sub, Substrate)
        assert isinstance(sub.install_proposal_store, InstallProposalStore)
        assert isinstance(sub.crb_event_emitter, CRBEventEmitter)
        assert isinstance(sub.crb_proposal_author, CRBProposalAuthor)
        assert isinstance(sub.crb_approval_flow, CRBApprovalFlow)
    finally:
        await tear_down_substrate(sub)


async def test_install_proposal_store_started_after_bringup(
    tmp_path, event_stream_started, agent_registry,
):
    """The store's ``start`` must be called so its sqlite connection is
    open by the time the flow tries to persist a proposal."""
    handler = _StubHandler()
    sub = await bring_up_substrate(
        data_dir=str(tmp_path),
        handler=handler,
        agent_registry=agent_registry,
    )
    try:
        # The store opens an aiosqlite connection in ``start`` and
        # asserts ``_db is not None`` on first use; if ``start`` was
        # skipped, ``_db`` would still be ``None``.
        assert sub.install_proposal_store._db is not None
    finally:
        await tear_down_substrate(sub)


async def test_crb_emitter_registered_with_correct_source_module(
    tmp_path, event_stream_started, agent_registry,
):
    handler = _StubHandler()
    sub = await bring_up_substrate(
        data_dir=str(tmp_path),
        handler=handler,
        agent_registry=agent_registry,
    )
    try:
        assert sub.crb_event_emitter.source_module == CRB_SOURCE_MODULE
        # The substrate-side EmitterRegistry should hold the same
        # underlying emitter — i.e. the bring-up registered it.
        registry = event_stream.emitter_registry()
        assert registry.is_registered(CRB_SOURCE_MODULE)
    finally:
        await tear_down_substrate(sub)


# ---------------------------------------------------------------------------
# Port protocol satisfaction (runtime_checkable)
# ---------------------------------------------------------------------------


async def test_draft_port_adapter_satisfies_protocol(
    tmp_path, event_stream_started, agent_registry,
):
    handler = _StubHandler()
    sub = await bring_up_substrate(
        data_dir=str(tmp_path),
        handler=handler,
        agent_registry=agent_registry,
    )
    try:
        adapter = DraftRegistryReadAdapter(
            draft_registry=sub.draft_registry,
        )
        assert isinstance(adapter, DraftReadPort)
    finally:
        await tear_down_substrate(sub)


async def test_sts_port_adapter_satisfies_protocol(
    tmp_path, event_stream_started, agent_registry,
):
    handler = _StubHandler()
    sub = await bring_up_substrate(
        data_dir=str(tmp_path),
        handler=handler,
        agent_registry=agent_registry,
    )
    try:
        adapter = SubstrateToolsSTSAdapter(
            substrate_tools=sub.substrate_tools,
        )
        assert isinstance(adapter, STSRegistrationPort)
    finally:
        await tear_down_substrate(sub)


async def test_crb_event_emitter_satisfies_event_port(
    tmp_path, event_stream_started, agent_registry,
):
    handler = _StubHandler()
    sub = await bring_up_substrate(
        data_dir=str(tmp_path),
        handler=handler,
        agent_registry=agent_registry,
    )
    try:
        assert isinstance(sub.crb_event_emitter, CRBEventPort)
    finally:
        await tear_down_substrate(sub)


# ---------------------------------------------------------------------------
# LLMClient adapter behavior
# ---------------------------------------------------------------------------


async def test_reasoning_llm_adapter_temperature_below_max(
    tmp_path, event_stream_started, agent_registry,
):
    """ProposalAuthor refuses a client whose temperature exceeds
    MAX_TEMPERATURE; the bring-up adapter must satisfy that pin."""
    handler = _StubHandler()
    adapter = ReasoningLLMAdapter(reasoning=handler.reasoning)
    assert adapter.temperature <= MAX_TEMPERATURE


async def test_reasoning_llm_adapter_forwards_complete(
    tmp_path, event_stream_started, agent_registry,
):
    handler = _StubHandler()
    adapter = ReasoningLLMAdapter(reasoning=handler.reasoning, max_tokens=512)
    result = await adapter.complete("hello world")
    assert result == "stub-completion"
    assert len(handler.reasoning.complete_simple_calls) == 1
    call = handler.reasoning.complete_simple_calls[0]
    assert call["system_prompt"] == ""
    assert call["user_content"] == "hello world"
    assert call["max_tokens"] == 512


# ---------------------------------------------------------------------------
# Tear-down
# ---------------------------------------------------------------------------


async def test_tear_down_stops_install_proposal_store(
    tmp_path, event_stream_started, agent_registry,
):
    handler = _StubHandler()
    sub = await bring_up_substrate(
        data_dir=str(tmp_path),
        handler=handler,
        agent_registry=agent_registry,
    )
    await tear_down_substrate(sub)
    # Stop closes the aiosqlite connection and clears the handle.
    assert sub.install_proposal_store._db is None


# ---------------------------------------------------------------------------
# Idempotent re-bring-up (process-level singleton concern)
# ---------------------------------------------------------------------------


async def test_second_bringup_reuses_registered_crb_emitter(
    tmp_path, event_stream_started, agent_registry,
):
    """EmitterRegistry.register raises EmitterAlreadyRegistered on a
    duplicate. The bring-up uses get-or-register so a re-entry within
    one process (rare in production, common in tests that share an
    event-stream fixture) does not crash."""
    handler1 = _StubHandler()
    sub1 = await bring_up_substrate(
        data_dir=str(tmp_path),
        handler=handler1,
        agent_registry=agent_registry,
    )
    try:
        handler2 = _StubHandler()
        sub2 = await bring_up_substrate(
            data_dir=str(tmp_path),
            handler=handler2,
            agent_registry=agent_registry,
        )
        try:
            assert (
                sub1.crb_event_emitter.source_module
                == sub2.crb_event_emitter.source_module
                == CRB_SOURCE_MODULE
            )
        finally:
            await tear_down_substrate(sub2)
    finally:
        await tear_down_substrate(sub1)
