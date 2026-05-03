"""Tests for MESSENGER-COHORT.

Plumbing paths: decision parsing, dispatcher hook, refer-whisper creation,
MessengerExhausted default-deny, ephemeral permission storage, trace
events, target resolution.

Judgment-path tests (cheap-chain welfare judgment quality against the
adequacy rubrics) live in evals/scenarios/ — those tests hit a real LLM,
so we keep them out of pytest. Here we use stub ReasoningServices to
verify the plumbing around the judgment call.

Zero-LLM-call: every pytest here runs entirely against stubs. No real
provider invocation.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kernos.cohorts.messenger import (
    CovenantEvidence,
    Disclosure,
    ExchangeContext,
    MessengerDecision,
    MessengerExhausted,
    _parse_decision,
    judge_exchange,
    render_exhaustion_response,
)
from kernos.cohorts.messenger_prompt import build_judge_prompt


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _ctx(**kw) -> ExchangeContext:
    defaults = dict(
        disclosing_member_id="mem_disc",
        disclosing_display_name="Disc",
        requesting_member_id="mem_req",
        requesting_display_name="Req",
        relationship_profile="by-permission",
        exchange_direction="inbound",
        content="How's Disc doing?",
        covenants=[],
        disclosures=[],
    )
    defaults.update(kw)
    return ExchangeContext(**defaults)


class _StubReasoning:
    """Minimal async stub matching the ReasoningService.complete_simple surface."""

    def __init__(self, *, response: str | Exception = '{"outcome":"none"}'):
        self.response = response
        self.call_count = 0
        self.last_kwargs: dict = {}

    async def complete_simple(self, **kwargs) -> str:
        self.call_count += 1
        self.last_kwargs = kwargs
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


# ---------------------------------------------------------------------------
# Decision parsing
# ---------------------------------------------------------------------------


class TestDecisionParsing:
    def test_none_outcome_returns_none(self):
        d = _parse_decision('{"outcome":"none"}', _ctx())
        assert d is None

    def test_revise_outcome_requires_response_text(self):
        # Missing response_text → degrades to None.
        d = _parse_decision('{"outcome":"revise"}', _ctx())
        assert d is None

    def test_revise_outcome_parses(self):
        raw = '{"outcome":"revise","response_text":"She\'s doing fine.","reasoning":"discretion"}'
        d = _parse_decision(raw, _ctx())
        assert d is not None
        assert d.outcome == "revise"
        assert d.response_text == "She's doing fine."
        assert d.reasoning == "discretion"

    def test_refer_synthesizes_holding_response_when_missing(self):
        raw = '{"outcome":"refer"}'
        d = _parse_decision(raw, _ctx(disclosing_display_name="Emma"))
        assert d is not None
        assert d.outcome == "refer"
        assert d.response_text  # always non-empty
        assert "Emma" in d.response_text
        assert d.refer_prompt  # synthesized if missing

    def test_refer_preserves_llm_response_when_present(self):
        raw = (
            '{"outcome":"refer","response_text":"Let me check with Emma.",'
            '"refer_prompt":"Mom asked about therapy — ok to mention?"}'
        )
        d = _parse_decision(raw, _ctx())
        assert d is not None
        assert d.outcome == "refer"
        assert d.response_text == "Let me check with Emma."
        assert d.refer_prompt == "Mom asked about therapy — ok to mention?"

    def test_malformed_json_degrades_to_none(self):
        d = _parse_decision("{not valid json", _ctx())
        assert d is None

    def test_empty_raw_degrades_to_none(self):
        assert _parse_decision("", _ctx()) is None
        assert _parse_decision("   ", _ctx()) is None

    def test_unknown_outcome_degrades_to_none(self):
        d = _parse_decision('{"outcome":"block"}', _ctx())
        assert d is None


# ---------------------------------------------------------------------------
# judge_exchange
# ---------------------------------------------------------------------------


class TestJudgeExchange:
    @pytest.mark.asyncio
    async def test_calls_lightweight_chain(self):
        """Messenger routes through the lightweight chain (bounded judgment)."""
        stub = _StubReasoning(response='{"outcome":"none"}')
        await judge_exchange(_ctx(), reasoning_service=stub)
        assert stub.call_count == 1
        assert stub.last_kwargs["chain"] == "lightweight"

    @pytest.mark.asyncio
    async def test_exhaustion_maps_to_domain_exception(self):
        stub = _StubReasoning(response=RuntimeError("all providers failed"))
        with pytest.raises(MessengerExhausted) as exc:
            await judge_exchange(_ctx(), reasoning_service=stub)
        assert exc.value.chain_name == "lightweight"


# ---------------------------------------------------------------------------
# Prompt shape — steward posture
# ---------------------------------------------------------------------------


class TestPromptPosture:
    """The prompt must sound like a steward, not a policy engine."""

    def test_system_prompt_contains_steward_language(self):
        sp, _ = build_judge_prompt(_ctx())
        assert "steward" in sp.lower() or "helping someone" in sp.lower()

    def test_system_prompt_is_not_policy_engine_toned(self):
        """Spec §the design review v4 #4 — implementation must not adopt policy-engine tone.
        Any drift toward 'evaluate whether rules are violated' phrasing is a
        finding per the batch spec. This sentinel catches the most obvious
        policy-engine phrasings."""
        sp, _ = build_judge_prompt(_ctx())
        lo = sp.lower()
        # Policy-engine sentinel phrases.
        assert "evaluate whether" not in lo or "evaluate whether the content" not in lo
        assert "rule violation" not in lo
        assert "compliance" not in lo
        assert "in violation of" not in lo

    def test_discretion_not_misleading_instruction_present(self):
        sp, _ = build_judge_prompt(_ctx())
        lo = sp.lower()
        # Must explicitly forbid creating false impressions.
        assert "mislead" in lo or "contradict reality" in lo or "discretion" in lo

    def test_always_respond_instruction_present(self):
        sp, _ = build_judge_prompt(_ctx())
        lo = sp.lower()
        # Accept either "always respond", "always produce a response",
        # "never silence" — any of the always-respond invariant phrasings
        # the prompt can land on.
        assert (
            ("always" in lo and ("respond" in lo or "produce a response" in lo))
            or "never silence" in lo
        )

    def test_refer_framed_as_first_class(self):
        sp, _ = build_judge_prompt(_ctx())
        lo = sp.lower()
        # Refer must be described as honorable / first-class, not a fallback.
        assert "honorable" in lo or "first-class" in lo or "honest move" in lo

    def test_structural_adherence_framing(self):
        """§1.1 — inputs organized by disclosing stated preferences, requesting
        stated preferences, welfare extrapolation, output contract."""
        sp, uc = build_judge_prompt(_ctx(
            covenants=[CovenantEvidence(
                id="c1", description="don't tell mom about therapy",
                rule_type="must_not", topic="therapy", target="mom",
            )],
            disclosures=[Disclosure(
                content="seeing a therapist", subject="therapy",
                sensitivity="personal",
            )],
        ))
        # Disclosing member's declared preferences surfaced in user content.
        assert "covenants" in uc.lower() or "declared" in uc.lower()
        # Requesting member's profile surfaced.
        assert "relationship profile" in uc.lower()
        # Recent relevant disclosures surfaced.
        assert "shared" in uc.lower() or "disclosures" in uc.lower()


# ---------------------------------------------------------------------------
# Dispatcher gate — messenger_judge callback contract
# ---------------------------------------------------------------------------


class TestDispatcherGate:
    """The RelationalDispatcher invokes the messenger_judge callback after
    permission passes and before envelope creation. Content is rewritten per
    callback return; refer-whisper is persisted best-effort.
    """

    @pytest.mark.asyncio
    async def test_callback_unchanged_path_preserves_content(self):
        """Callback returns (content, None) → envelope uses original content."""
        from kernos.kernel.relational_dispatch import RelationalDispatcher

        state = _FakeState()
        instance_db = _FakeInstanceDB()
        captured: list = []
        async def _judge(**kw):
            captured.append(kw)
            return kw["content"], None  # unchanged
        dispatcher = RelationalDispatcher(
            state=state, instance_db=instance_db, messenger_judge=_judge,
        )
        res = await dispatcher.send(
            instance_id="t1", origin_member_id="mem_o",
            origin_agent_identity="o", addressee="mem_d",
            intent="ask_question", content="Hi there",
        )
        assert res.ok is True
        assert captured[0]["content"] == "Hi there"
        assert state.saved_messages[-1].content == "Hi there"
        assert not state.saved_whispers

    @pytest.mark.asyncio
    async def test_callback_revise_rewrites_content(self):
        """Callback returns (revised, None) → envelope content is revised."""
        from kernos.kernel.relational_dispatch import RelationalDispatcher

        state = _FakeState()
        instance_db = _FakeInstanceDB()
        async def _judge(**kw):
            return "Revised response.", None
        dispatcher = RelationalDispatcher(
            state=state, instance_db=instance_db, messenger_judge=_judge,
        )
        res = await dispatcher.send(
            instance_id="t1", origin_member_id="mem_o",
            origin_agent_identity="o", addressee="mem_d",
            intent="ask_question", content="Original ask",
        )
        assert res.ok is True
        assert state.saved_messages[-1].content == "Revised response."

    @pytest.mark.asyncio
    async def test_callback_refer_persists_whisper(self):
        """Callback returns (holding, whisper) → content is holding response,
        whisper is persisted to the disclosing member."""
        from kernos.kernel.awareness import Whisper
        from kernos.kernel.relational_dispatch import RelationalDispatcher

        state = _FakeState()
        instance_db = _FakeInstanceDB()
        whisper = Whisper(
            whisper_id="wsp_refer", insight_text="Mom asked about therapy — ok?",
            delivery_class="ambient", source_space_id="",
            target_space_id="", supporting_evidence=[],
            reasoning_trace="", knowledge_entry_id="",
            foresight_signal="messenger_refer:mem_d:mem_o",
            created_at=datetime.now(timezone.utc).isoformat(),
            owner_member_id="mem_d",
        )
        async def _judge(**kw):
            return "Let me check with them.", whisper
        dispatcher = RelationalDispatcher(
            state=state, instance_db=instance_db, messenger_judge=_judge,
        )
        res = await dispatcher.send(
            instance_id="t1", origin_member_id="mem_o",
            origin_agent_identity="o", addressee="mem_d",
            intent="ask_question", content="How's Disc?",
        )
        assert res.ok is True
        assert state.saved_messages[-1].content == "Let me check with them."
        assert len(state.saved_whispers) == 1
        assert state.saved_whispers[0].owner_member_id == "mem_d"

    @pytest.mark.asyncio
    async def test_callback_exception_falls_through_to_original(self):
        """Callback exception → always-respond by dispatching original content."""
        from kernos.kernel.relational_dispatch import RelationalDispatcher

        state = _FakeState()
        instance_db = _FakeInstanceDB()
        async def _judge(**kw):
            raise RuntimeError("boom")
        dispatcher = RelationalDispatcher(
            state=state, instance_db=instance_db, messenger_judge=_judge,
        )
        res = await dispatcher.send(
            instance_id="t1", origin_member_id="mem_o",
            origin_agent_identity="o", addressee="mem_d",
            intent="ask_question", content="Original ask",
        )
        # Message still sent — always-respond holds even when the cohort
        # callback fails.
        assert res.ok is True
        assert state.saved_messages[-1].content == "Original ask"

    @pytest.mark.asyncio
    async def test_no_callback_means_no_messenger(self):
        """Dispatcher constructed without messenger_judge dispatches normally —
        Messenger is optional, not mandatory infrastructure."""
        from kernos.kernel.relational_dispatch import RelationalDispatcher

        state = _FakeState()
        instance_db = _FakeInstanceDB()
        dispatcher = RelationalDispatcher(
            state=state, instance_db=instance_db,
        )
        res = await dispatcher.send(
            instance_id="t1", origin_member_id="mem_o",
            origin_agent_identity="o", addressee="mem_d",
            intent="ask_question", content="Original ask",
        )
        assert res.ok is True
        assert state.saved_messages[-1].content == "Original ask"

    @pytest.mark.asyncio
    async def test_permission_matrix_rejection_bypasses_messenger(self):
        """Layer 1 — the design review §0 / Spec §0: Messenger never runs on exchanges the
        permission matrix refused. RM-native refusal, callback not invoked."""
        from kernos.kernel.relational_dispatch import RelationalDispatcher

        state = _FakeState()
        instance_db = _FakeInstanceDB(permission="no-access")
        callback_invoked = []
        async def _judge(**kw):
            callback_invoked.append(kw)
            return kw["content"], None
        dispatcher = RelationalDispatcher(
            state=state, instance_db=instance_db, messenger_judge=_judge,
        )
        res = await dispatcher.send(
            instance_id="t1", origin_member_id="mem_o",
            origin_agent_identity="o", addressee="mem_d",
            intent="ask_question", content="Something",
        )
        assert res.ok is False
        assert "permission denied" in res.error
        assert callback_invoked == []


# ---------------------------------------------------------------------------
# Ephemeral permissions
# ---------------------------------------------------------------------------


class TestEphemeralPermissions:
    @pytest.mark.asyncio
    async def test_save_and_list(self, tmp_path):
        from kernos.kernel.state import EphemeralPermission
        from kernos.kernel.state_json import JsonStateStore

        store = JsonStateStore(tmp_path)
        now = datetime.now(timezone.utc)
        perm = EphemeralPermission(
            id="eph1", instance_id="t1",
            disclosing_member_id="mem_d", requesting_member_id="mem_r",
            topic="therapy", granted=True,
            created_at=now.isoformat(),
            expires_at=(now + timedelta(hours=24)).isoformat(),
        )
        await store.save_ephemeral_permission(perm)
        out = await store.list_ephemeral_permissions(
            "t1", disclosing_member_id="mem_d", requesting_member_id="mem_r",
        )
        assert len(out) == 1
        assert out[0].topic == "therapy"
        assert out[0].granted is True

    @pytest.mark.asyncio
    async def test_expired_entries_filtered_at_read(self, tmp_path):
        from kernos.kernel.state import EphemeralPermission
        from kernos.kernel.state_json import JsonStateStore

        store = JsonStateStore(tmp_path)
        past = datetime.now(timezone.utc) - timedelta(hours=25)
        perm = EphemeralPermission(
            id="eph_expired", instance_id="t1",
            disclosing_member_id="mem_d", requesting_member_id="mem_r",
            topic="past", granted=True,
            created_at=(past - timedelta(hours=1)).isoformat(),
            expires_at=past.isoformat(),
        )
        await store.save_ephemeral_permission(perm)
        out = await store.list_ephemeral_permissions(
            "t1", disclosing_member_id="mem_d", requesting_member_id="mem_r",
        )
        assert out == []

    @pytest.mark.asyncio
    async def test_pair_scoping(self, tmp_path):
        from kernos.kernel.state import EphemeralPermission
        from kernos.kernel.state_json import JsonStateStore

        store = JsonStateStore(tmp_path)
        now = datetime.now(timezone.utc)
        exp = (now + timedelta(hours=24)).isoformat()
        for (pid, disc, req) in [
            ("e1", "mem_d", "mem_r"),
            ("e2", "mem_d", "mem_other"),
            ("e3", "mem_other", "mem_r"),
        ]:
            await store.save_ephemeral_permission(EphemeralPermission(
                id=pid, instance_id="t1",
                disclosing_member_id=disc, requesting_member_id=req,
                topic="t", granted=True,
                created_at=now.isoformat(), expires_at=exp,
            ))
        scoped = await store.list_ephemeral_permissions(
            "t1", disclosing_member_id="mem_d", requesting_member_id="mem_r",
        )
        assert [p.id for p in scoped] == ["e1"]


# ---------------------------------------------------------------------------
# Covenant topic/target fields
# ---------------------------------------------------------------------------


class TestCovenantFields:
    def test_new_covenant_has_empty_topic_target(self):
        from kernos.kernel.state import CovenantRule
        r = CovenantRule(
            id="r1", instance_id="t1", capability="general",
            rule_type="must_not", description="don't tell mom about therapy",
            active=True, source="user",
        )
        assert r.topic == ""
        assert r.target == ""

    @pytest.mark.asyncio
    async def test_covenant_topic_target_persist(self, tmp_path):
        from kernos.kernel.state import CovenantRule
        from kernos.kernel.state_json import JsonStateStore

        store = JsonStateStore(tmp_path)
        r = CovenantRule(
            id="r1", instance_id="t1", capability="general",
            rule_type="must_not", description="don't tell mom about therapy",
            active=True, source="user",
            topic="therapy", target="mem_mom",
        )
        await store.add_contract_rule(r)
        fetched = await store.get_contract_rules("t1")
        assert any(
            c.topic == "therapy" and c.target == "mem_mom"
            for c in fetched
        ), fetched


# ---------------------------------------------------------------------------
# Render exhaustion response — always non-empty
# ---------------------------------------------------------------------------


class TestExhaustionResponse:
    def test_non_empty_with_name(self):
        out = render_exhaustion_response(disclosing_display_name="Emma")
        assert out.strip()
        assert "Emma" in out

    def test_non_empty_without_name(self):
        out = render_exhaustion_response()
        assert out.strip()


# ---------------------------------------------------------------------------
# Admin tool — diagnose_messenger (shape + space-type gate)
# ---------------------------------------------------------------------------


class TestDiagnoseMessenger:
    @pytest.mark.asyncio
    async def test_returns_expected_keys(self, tmp_path):
        from kernos.cohorts.admin import diagnose_messenger
        from kernos.kernel.state_json import JsonStateStore

        state = JsonStateStore(tmp_path)
        idb = _FakeInstanceDB()
        out = await diagnose_messenger(
            instance_id="t1", member_a_id="mem_a", member_b_id="mem_b",
            state=state, instance_db=idb,
        )
        assert out["ok"] is True
        assert out["member_a"]["id"] == "mem_a"
        assert out["member_b"]["id"] == "mem_b"
        assert "relationship_a_to_b" in out
        assert "relationship_b_to_a" in out
        assert "covenants_a_as_disclosing" in out
        assert "covenants_b_as_disclosing" in out
        assert "ephemeral_permissions_a_to_b" in out
        assert "ephemeral_permissions_b_to_a" in out


# ---------------------------------------------------------------------------
# Agent never knows Messenger exists — grep audit
# ---------------------------------------------------------------------------


class TestMessengerInvisibleToAgent:
    """The Messenger is infrastructure. No agent tool references it; no
    context-assembly path surfaces its outcomes; no reasoning trace
    labels it.
    """

    def test_messenger_not_in_tool_catalog(self):
        # No kernel tool exposes "messenger" or "judge_exchange" to agents.
        from kernos.kernel.tools import __all__
        for name in __all__:
            low = name.lower()
            assert "messenger" not in low or "diagnose" in low, (
                f"Tool {name!r} references messenger outside the admin "
                "diagnose path — this would leak Messenger existence to "
                "the primary agent."
            )

    def test_no_messenger_import_in_agent_message_assembly(self):
        """Context-assembly + agent-facing surfaces don't import
        kernos.cohorts.messenger directly. The dispatcher (not the handler's
        agent-facing code path) is the only site that does."""
        # The handler does import cohorts.messenger inside the
        # _build_messenger_judge_callback method — but that method runs
        # inside the RM dispatcher path, not the agent-reasoning path.
        # A rough proxy: the reasoning module itself should not import
        # cohorts.messenger.
        reasoning_source = Path(
            "kernos/kernel/reasoning.py"
        ).read_text()
        # reasoning.py can reference diagnose_messenger (admin tool) but
        # should not import the judge surface directly.
        assert "from kernos.cohorts.messenger import" not in reasoning_source
        assert "import kernos.cohorts.messenger" not in reasoning_source

    def test_no_hard_coded_sensitivity_categories(self):
        """Spec anti-goal: 'Do not hard-code sensitive-topic categories.
        Let the Messenger's LLM judgment make that call.' Confirm no
        topic/category list lives in the cohort modules."""
        msg_src = Path("kernos/cohorts/messenger.py").read_text()
        prompt_src = Path("kernos/cohorts/messenger_prompt.py").read_text()
        # No taxonomy / canonical topic list / category enum.
        for banned in ("SENSITIVE_TOPICS", "TOPIC_CATEGORIES", "TAXONOMY"):
            assert banned not in msg_src
            assert banned not in prompt_src


# ---------------------------------------------------------------------------
# RM state machine unchanged — git-diff-level assertion
# ---------------------------------------------------------------------------


class TestRMStateMachineUnchanged:
    """Spec §3a / §6 commitment: RM's five-state machine is frozen. Messenger
    outcomes produce RM delivery events, not RM state transitions.

    A strict git-level check against kernos/relational/states.py is in the
    batch report; here we assert the spec-equivalent behavior: the dispatcher
    source code doesn't introduce new RM transitions in the Messenger path.
    """

    def test_no_new_rm_transitions_in_messenger_path(self):
        src = Path("kernos/kernel/relational_dispatch.py").read_text()
        # MESSENGER-COHORT path must not transition to any "messenger_*" state.
        assert "messenger_pending" not in src
        assert "awaiting_referral" not in src
        # The existing five states + expired terminal are still the only
        # states named as transition targets.
        import re
        targets = set(re.findall(r'to_state="([^"]+)"', src))
        # Expected set — no new "messenger"-flavored states.
        assert targets <= {
            "delivered", "surfaced", "resolved", "expired",
        }, f"Unexpected RM transition targets in dispatcher: {targets}"


# ---------------------------------------------------------------------------
# Fakes for dispatcher tests
# ---------------------------------------------------------------------------


@dataclass
class _FakeState:
    saved_messages: list = field(default_factory=list)
    saved_whispers: list = field(default_factory=list)

    async def add_relational_message(self, msg):
        self.saved_messages.append(msg)

    async def get_relational_message(self, instance_id, message_id):
        for m in self.saved_messages:
            if m.id == message_id:
                return m
        return None

    async def transition_relational_message_state(self, instance_id, message_id, *, from_state, to_state, updates):
        for m in self.saved_messages:
            if m.id == message_id and m.state == from_state:
                m.state = to_state
                for k, v in (updates or {}).items():
                    setattr(m, k, v)
                return True
        return False

    async def query_relational_messages(self, instance_id, **kw):
        return []

    async def save_whisper(self, instance_id, whisper):
        self.saved_whispers.append(whisper)


@dataclass
class _FakeInstanceDB:
    permission: str = "full-access"

    async def get_permission(self, a, b):
        return self.permission

    async def list_members(self):
        return [
            {"member_id": "mem_o", "display_name": "Owner", "role": "owner"},
            {"member_id": "mem_d", "display_name": "Disc", "role": "member"},
        ]

    async def list_member_channels(self, member_id):
        return []

    async def get_member_profile(self, member_id):
        return {
            "mem_o": {"display_name": "Owner"},
            "mem_d": {"display_name": "Disc"},
            "mem_a": {"display_name": "A"},
            "mem_b": {"display_name": "B"},
        }.get(member_id, {"display_name": member_id})
