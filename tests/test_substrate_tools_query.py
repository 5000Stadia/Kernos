"""SubstrateTools (STS) C1 query-surface tests.

Spec reference: SPEC-STS-v2.

Pins:

* AC #1  — STS module exists with the cohort-facing facade.
* AC #2  — query surfaces deterministic, instance-scoped, no LLM, no
           cross-instance leakage.
* AC #14 — capability tags namespaced (``domain.action``).
* AC #15 — ProviderRegistry aggregates across registered provider types.
* AC #16 — ContextBriefRegistry dispatches by ref type.
* AC #22 — future-composition invariant inline in __init__ + facade.
"""
from __future__ import annotations

import pytest

from kernos.kernel import event_stream
from kernos.kernel.agents.providers import ProviderRegistry as DARProviderRegistry
from kernos.kernel.agents.registry import AgentRecord, AgentRegistry
from kernos.kernel.drafts.registry import DraftRegistry
from kernos.kernel.substrate_tools import (
    ContextBrief,
    ContextBriefRegistry,
    ContextRef,
    InvalidCapabilityTagFormat,
    ProviderRecord,
    ProviderRegistry,
    SubstrateTools,
    validate_capability_tag,
)
from kernos.kernel.workflows.agent_inbox import InMemoryAgentInbox
from kernos.kernel.workflows.trigger_registry import (
    TriggerRegistry,
    _reset_for_tests as _reset_trigger_registry,
)
from kernos.kernel.workflows.workflow_registry import (
    ActionDescriptor,
    Bounds,
    ContinuationRules,
    Verifier,
    Workflow,
    WorkflowRegistry,
)


# ===========================================================================
# Stack helpers
# ===========================================================================


@pytest.fixture
async def stack(tmp_path):
    """Full STS stack with DAR + WLP + WDP + provider/context registries."""
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path))
    dar_pr = DARProviderRegistry()
    dar_pr.register("inmemory", lambda ref: InMemoryAgentInbox())
    agents = AgentRegistry(provider_registry=dar_pr)
    await agents.start(str(tmp_path))
    trig = TriggerRegistry()
    await trig.start(str(tmp_path))
    wfr = WorkflowRegistry()
    await wfr.start(str(tmp_path), trig)
    wfr.wire_agent_registry(agents)
    drafts = DraftRegistry()
    await drafts.start(str(tmp_path))
    sts_pr = ProviderRegistry()
    cbr = ContextBriefRegistry()
    sts = SubstrateTools(
        agent_registry=agents,
        workflow_registry=wfr,
        draft_registry=drafts,
        provider_registry=sts_pr,
        context_brief_registry=cbr,
    )
    yield {
        "agents": agents, "wfr": wfr, "drafts": drafts,
        "sts_pr": sts_pr, "cbr": cbr, "sts": sts,
    }
    await drafts.stop()
    await wfr.stop()
    await _reset_trigger_registry(trig)
    await agents.stop()
    await event_stream._reset_for_tests()


def _agent_record(**overrides) -> AgentRecord:
    base = dict(
        agent_id="spec-agent",
        instance_id="inst_a",
        display_name="Spec drafter",
        aliases=[],
        provider_key="inmemory",
        provider_config_ref="default",
        domain_summary="",
        capabilities_summary="",
        status="active",
    )
    base.update(overrides)
    return AgentRecord(**base)


def _make_workflow(*, instance_id="inst_a", workflow_id="wf-1", metadata=None) -> Workflow:
    return Workflow(
        workflow_id=workflow_id,
        instance_id=instance_id,
        name="test-workflow",
        description="",
        owner="owner",
        version="1",
        bounds=Bounds(iteration_count=1),
        verifier=Verifier(flavor="deterministic", check="x == y"),
        action_sequence=[
            ActionDescriptor(
                action_type="mark_state",
                parameters={"key": "k", "value": "v", "scope": "ledger"},
                continuation_rules=ContinuationRules(),
            ),
        ],
        metadata=metadata or {},
    )


# ===========================================================================
# AC #1 — module exists with facade and 5 query surfaces
# ===========================================================================


class TestModuleShape:
    def test_facade_exposes_query_methods(self, stack):
        sts = stack["sts"]
        for name in (
            "list_known_providers",
            "list_agents",
            "list_workflows",
            "list_drafts",
            "query_context_brief",
        ):
            assert callable(getattr(sts, name)), f"missing query surface: {name}"

    def test_facade_exposes_register_workflow(self, stack):
        # C2 added the approval-bound register_workflow gate.
        sts = stack["sts"]
        assert callable(getattr(sts, "register_workflow", None)), (
            "AC #1: SubstrateTools must expose register_workflow"
        )


# ===========================================================================
# AC #22 — future-composition invariant docstring
# ===========================================================================


class TestFutureCompositionInvariant:
    def test_package_docstring_carries_invariant(self):
        import kernos.kernel.substrate_tools as pkg
        doc = pkg.__doc__ or ""
        assert "future-composition" in doc.lower()
        assert "without depending directly" in doc.lower()
        assert "canvas" in doc.lower()

    def test_facade_docstring_carries_invariant(self):
        from kernos.kernel.substrate_tools import facade as facade_mod
        doc = facade_mod.__doc__ or ""
        assert "future-composition" in doc.lower()
        assert "without depending directly" in doc.lower()


# ===========================================================================
# AC #14 — capability tags namespaced (domain.action)
# ===========================================================================


class TestCapabilityTagFormat:
    @pytest.mark.parametrize("tag", [
        "email.send",
        "calendar.read",
        "agent_inbox.notion",
        "tool.execute_code",
        "domain.action_name",
    ])
    def test_valid_tags_accepted(self, tag):
        validate_capability_tag(tag)  # no raise

    @pytest.mark.parametrize("tag", [
        "Email.send",        # uppercase domain
        "email.Send",        # uppercase action
        "email",             # no dot
        "email.",            # trailing dot
        ".send",             # leading dot
        "email.send.extra",  # two dots
        "1email.send",       # leading digit
        "email send",        # space
        "email-send",        # dash
        "",                  # empty
        "email.send\n",      # trailing newline (re.match quirk)
        "email.send\r",      # carriage return
        "\nemail.send",      # leading newline
        "email.send ",       # trailing space
        "email​.send",  # zero-width space (unicode)
    ])
    def test_invalid_tags_rejected(self, tag):
        with pytest.raises(InvalidCapabilityTagFormat):
            validate_capability_tag(tag)

    def test_provider_record_validates_tags_at_construction(self):
        with pytest.raises(InvalidCapabilityTagFormat):
            ProviderRecord(
                provider_id="x",
                provider_type="agent_inbox",
                capability_tags=["Email.send"],
            )

    def test_provider_record_accepts_list_or_tuple(self):
        rec = ProviderRecord(
            provider_id="x",
            provider_type="agent_inbox",
            capability_tags=["email.send", "email.read"],
        )
        assert rec.capability_tags == ("email.send", "email.read")

    def test_provider_record_requires_id_and_type(self):
        with pytest.raises(ValueError):
            ProviderRecord(provider_id="", provider_type="agent_inbox")
        with pytest.raises(ValueError):
            ProviderRecord(provider_id="x", provider_type="")


# ===========================================================================
# AC #15 — ProviderRegistry aggregates across types
# ===========================================================================


class TestProviderRegistryAggregation:
    async def test_register_and_list_single_type(self, stack):
        pr = stack["sts_pr"]

        def list_inboxes(instance_id: str) -> list[ProviderRecord]:
            return [
                ProviderRecord(
                    provider_id="agent-1",
                    provider_type="agent_inbox",
                    capability_tags=["email.send"],
                ),
            ]

        pr.register_provider_type("agent_inbox", list_inboxes)
        records = await pr.list_all(instance_id="inst_a")
        assert len(records) == 1
        assert records[0].provider_type == "agent_inbox"

    async def test_aggregate_multiple_types(self, stack):
        pr = stack["sts_pr"]

        def list_inboxes(instance_id: str) -> list[ProviderRecord]:
            return [
                ProviderRecord(
                    provider_id="agent-1",
                    provider_type="agent_inbox",
                    capability_tags=["email.send"],
                ),
            ]

        def list_canvases(instance_id: str) -> list[ProviderRecord]:
            return [
                ProviderRecord(
                    provider_id="canvas-1",
                    provider_type="canvas",
                    capability_tags=["canvas.read", "canvas.write"],
                ),
            ]

        pr.register_provider_type("agent_inbox", list_inboxes)
        pr.register_provider_type("canvas", list_canvases)
        records = await pr.list_all(instance_id="inst_a")
        types = sorted(r.provider_type for r in records)
        assert types == ["agent_inbox", "canvas"]

    async def test_async_lister_is_awaited(self, stack):
        pr = stack["sts_pr"]

        async def async_lister(instance_id: str) -> list[ProviderRecord]:
            return [
                ProviderRecord(
                    provider_id="async-agent",
                    provider_type="agent_inbox",
                    capability_tags=[],
                ),
            ]

        pr.register_provider_type("agent_inbox", async_lister)
        records = await pr.list_all(instance_id="inst_a")
        assert len(records) == 1
        assert records[0].provider_id == "async-agent"

    async def test_lister_misregistration_surfaces(self, stack):
        """A lister registered under provider_type X that returns a
        record claiming provider_type Y must surface as an error so
        misregistrations don't propagate silently."""
        pr = stack["sts_pr"]

        def bad_lister(instance_id: str) -> list[ProviderRecord]:
            return [
                ProviderRecord(
                    provider_id="x",
                    provider_type="canvas",  # wrong!
                    capability_tags=[],
                ),
            ]

        pr.register_provider_type("agent_inbox", bad_lister)
        with pytest.raises(ValueError, match="provider_type"):
            await pr.list_all(instance_id="inst_a")

    async def test_duplicate_registration_rejected(self, stack):
        pr = stack["sts_pr"]
        pr.register_provider_type("agent_inbox", lambda i: [])
        with pytest.raises(ValueError):
            pr.register_provider_type("agent_inbox", lambda i: [])

    async def test_known_provider_types(self, stack):
        pr = stack["sts_pr"]
        pr.register_provider_type("agent_inbox", lambda i: [])
        pr.register_provider_type("canvas", lambda i: [])
        assert set(pr.known_provider_types()) == {"agent_inbox", "canvas"}

    async def test_list_all_requires_instance_id(self, stack):
        pr = stack["sts_pr"]
        with pytest.raises(ValueError):
            await pr.list_all(instance_id="")


# ===========================================================================
# AC #16 — ContextBriefRegistry dispatches by ref type
# ===========================================================================


class TestContextBriefRegistry:
    async def test_resolve_by_registered_type(self, stack):
        cbr = stack["cbr"]

        def space_resolver(instance_id: str, ref_id: str) -> ContextBrief | None:
            return ContextBrief(
                ref=ContextRef(type="space", id=ref_id),
                summary=f"space {ref_id}",
                capability_hints=("memory.read",),
            )

        cbr.register_resolver("space", space_resolver)
        brief = await cbr.resolve(
            instance_id="inst_a",
            ref=ContextRef(type="space", id="spc_general"),
        )
        assert brief is not None
        assert brief.summary == "space spc_general"
        assert "memory.read" in brief.capability_hints

    async def test_unknown_ref_type_returns_none(self, stack):
        cbr = stack["cbr"]
        brief = await cbr.resolve(
            instance_id="inst_a",
            ref=ContextRef(type="canvas", id="cvs-1"),
        )
        assert brief is None

    async def test_async_resolver_is_awaited(self, stack):
        cbr = stack["cbr"]

        async def async_space(instance_id: str, ref_id: str) -> ContextBrief:
            return ContextBrief(
                ref=ContextRef(type="space", id=ref_id),
                summary=f"async space {ref_id}",
            )

        cbr.register_resolver("space", async_space)
        brief = await cbr.resolve(
            instance_id="inst_a",
            ref=ContextRef(type="space", id="spc_x"),
        )
        assert brief is not None
        assert brief.summary == "async space spc_x"

    async def test_duplicate_resolver_rejected(self, stack):
        cbr = stack["cbr"]
        cbr.register_resolver("space", lambda i, r: None)
        with pytest.raises(ValueError):
            cbr.register_resolver("space", lambda i, r: None)

    async def test_resolver_returning_none_when_ref_missing(self, stack):
        cbr = stack["cbr"]

        def space_resolver(instance_id: str, ref_id: str) -> ContextBrief | None:
            return None

        cbr.register_resolver("space", space_resolver)
        brief = await cbr.resolve(
            instance_id="inst_a",
            ref=ContextRef(type="space", id="missing"),
        )
        assert brief is None

    def test_context_ref_requires_type_and_id(self):
        with pytest.raises(ValueError):
            ContextRef(type="", id="x")
        with pytest.raises(ValueError):
            ContextRef(type="space", id="")


# ===========================================================================
# AC #2 — query surfaces deterministic + instance-scoped + no leakage
# ===========================================================================


class TestQuerySurfaceIsolation:
    async def test_list_agents_scoped_to_instance(self, stack):
        agents = stack["agents"]
        sts = stack["sts"]
        await agents.register_agent(_agent_record(
            agent_id="a-inst-a", instance_id="inst_a",
        ))
        await agents.register_agent(_agent_record(
            agent_id="a-inst-b", instance_id="inst_b",
        ))
        a_only = await sts.list_agents(instance_id="inst_a")
        b_only = await sts.list_agents(instance_id="inst_b")
        assert {r.agent_id for r in a_only} == {"a-inst-a"}
        assert {r.agent_id for r in b_only} == {"a-inst-b"}

    async def test_list_workflows_scoped_to_instance(self, stack):
        wfr = stack["wfr"]
        sts = stack["sts"]
        await wfr._register_workflow_unbound(_make_workflow(
            workflow_id="wf-inst-a", instance_id="inst_a",
        ))
        await wfr._register_workflow_unbound(_make_workflow(
            workflow_id="wf-inst-b", instance_id="inst_b",
        ))
        a_only = await sts.list_workflows(instance_id="inst_a")
        b_only = await sts.list_workflows(instance_id="inst_b")
        assert {w.workflow_id for w in a_only} == {"wf-inst-a"}
        assert {w.workflow_id for w in b_only} == {"wf-inst-b"}

    async def test_list_workflows_home_space_id_filter(self, stack):
        wfr = stack["wfr"]
        sts = stack["sts"]
        await wfr._register_workflow_unbound(_make_workflow(
            workflow_id="wf-spc-1", instance_id="inst_a",
            metadata={"home_space_id": "spc_general"},
        ))
        await wfr._register_workflow_unbound(_make_workflow(
            workflow_id="wf-spc-2", instance_id="inst_a",
            metadata={"home_space_id": "spc_work"},
        ))
        await wfr._register_workflow_unbound(_make_workflow(
            workflow_id="wf-no-space", instance_id="inst_a",
            metadata={},
        ))
        general = await sts.list_workflows(
            instance_id="inst_a", home_space_id="spc_general",
        )
        assert {w.workflow_id for w in general} == {"wf-spc-1"}
        all_for_inst = await sts.list_workflows(instance_id="inst_a")
        assert {w.workflow_id for w in all_for_inst} == {
            "wf-spc-1", "wf-spc-2", "wf-no-space",
        }

    async def test_list_drafts_scoped_to_instance(self, stack):
        # WDP create_draft proxy
        drafts = stack["drafts"]
        sts = stack["sts"]
        d_a = await drafts.create_draft(
            instance_id="inst_a",
            intent_summary="A draft",
            home_space_id="spc_general",
        )
        d_b = await drafts.create_draft(
            instance_id="inst_b",
            intent_summary="B draft",
            home_space_id="spc_general",
        )
        a_only = await sts.list_drafts(instance_id="inst_a")
        b_only = await sts.list_drafts(instance_id="inst_b")
        assert {d.draft_id for d in a_only} == {d_a.draft_id}
        assert {d.draft_id for d in b_only} == {d_b.draft_id}

    async def test_list_known_providers_scoped_to_instance(self, stack):
        pr = stack["sts_pr"]
        sts = stack["sts"]
        per_instance = {
            "inst_a": [ProviderRecord(
                provider_id="agent-a", provider_type="agent_inbox",
                capability_tags=["email.send"],
            )],
            "inst_b": [ProviderRecord(
                provider_id="agent-b", provider_type="agent_inbox",
                capability_tags=["sms.send"],
            )],
        }
        pr.register_provider_type(
            "agent_inbox", lambda inst: per_instance.get(inst, []),
        )
        a = await sts.list_known_providers(instance_id="inst_a")
        b = await sts.list_known_providers(instance_id="inst_b")
        assert {r.provider_id for r in a} == {"agent-a"}
        assert {r.provider_id for r in b} == {"agent-b"}

    async def test_query_context_brief_scoped_to_instance(self, stack):
        cbr = stack["cbr"]
        sts = stack["sts"]
        # Resolver that produces different briefs per instance.
        per_instance = {
            "inst_a": "alpha space",
            "inst_b": "beta space",
        }

        def space_resolver(instance_id: str, ref_id: str) -> ContextBrief | None:
            return ContextBrief(
                ref=ContextRef(type="space", id=ref_id),
                summary=per_instance.get(instance_id, "?"),
            )

        cbr.register_resolver("space", space_resolver)
        a = await sts.query_context_brief(
            instance_id="inst_a", ref=ContextRef(type="space", id="x"),
        )
        b = await sts.query_context_brief(
            instance_id="inst_b", ref=ContextRef(type="space", id="x"),
        )
        assert a is not None and a.summary == "alpha space"
        assert b is not None and b.summary == "beta space"

    async def test_query_methods_keyword_only(self, stack):
        """Defensive shape: every query takes instance_id keyword-only.
        Positional is rejected at the type level."""
        sts = stack["sts"]
        with pytest.raises(TypeError):
            await sts.list_agents("inst_a")  # type: ignore[misc]
        with pytest.raises(TypeError):
            await sts.list_workflows("inst_a")  # type: ignore[misc]
        with pytest.raises(TypeError):
            await sts.list_drafts("inst_a")  # type: ignore[misc]
        with pytest.raises(TypeError):
            await sts.list_known_providers("inst_a")  # type: ignore[misc]
        with pytest.raises(TypeError):
            await sts.query_context_brief(
                "inst_a", ContextRef(type="space", id="x"),  # type: ignore[misc]
            )
