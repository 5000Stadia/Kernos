"""Tests for MESSENGER-IS-THE-VOICE.

The DispatchGate excludes ``send_relational_message`` by tool-call name so
the Messenger (Layer 3) becomes the unconditional voice of cross-member
welfare judgment. Regression coverage:

1. All three RM intents pass through the gate without pause or
   confirmation request.
2. Non-RM sensitive actions still route through the gate's model check
   (exclusion is scoped to the tool-call name, not broader).
3. The exclusion reason/method fields match the
   ``messenger_delegated`` / ``messenger_handoff`` contract so auditors
   can find it in the trace.

Zero-LLM-call: the reasoning service is stubbed. The exclusion short-
circuits before any cheap-chain call fires, so the test doesn't need a
real LLM even for scenarios that would normally invoke
``_evaluate_model``.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.kernel.gate import DispatchGate, GateResult


def _make_gate(*, must_not_rules=None):
    """Build a DispatchGate against async-mocked dependencies."""
    reasoning = MagicMock()
    reasoning.complete_simple = AsyncMock(return_value="CONFIRM")

    state = MagicMock()
    state.query_covenant_rules = AsyncMock(return_value=must_not_rules or [])
    state.get_instance_profile = AsyncMock(return_value=None)

    registry = MagicMock()
    registry.get_all = MagicMock(return_value=[])

    events = MagicMock()
    events.append = AsyncMock()

    return DispatchGate(
        reasoning_service=reasoning, registry=registry, state=state,
        events=events, mcp=None,
    )


class TestSendRelationalMessageExclusion:
    """send_relational_message bypasses the gate — Messenger is the voice."""

    @pytest.mark.asyncio
    async def test_ask_question_bypasses_gate(self):
        gate = _make_gate()
        result = await gate.evaluate(
            tool_name="send_relational_message",
            tool_input={"addressee": "emma", "intent": "ask_question",
                        "content": "How's Emma handling stress?"},
            effect="unknown",
            user_message="",
            instance_id="t1",
            active_space_id="s1",
        )
        assert result.allowed is True
        assert result.reason == "messenger_delegated"
        assert result.method == "messenger_handoff"
        # Critical: the gate must NOT have called the cheap chain for RM.
        assert gate._reasoning.complete_simple.await_count == 0

    @pytest.mark.asyncio
    async def test_request_action_bypasses_gate(self):
        gate = _make_gate()
        result = await gate.evaluate(
            tool_name="send_relational_message",
            tool_input={"addressee": "harold", "intent": "request_action",
                        "content": "Please add a reminder"},
            effect="unknown",
            user_message="",
            instance_id="t1",
            active_space_id="s1",
        )
        assert result.allowed is True
        assert result.reason == "messenger_delegated"

    @pytest.mark.asyncio
    async def test_inform_bypasses_gate(self):
        gate = _make_gate()
        result = await gate.evaluate(
            tool_name="send_relational_message",
            tool_input={"addressee": "dad", "intent": "inform",
                        "content": "Heads up on the meeting"},
            effect="unknown",
            user_message="",
            instance_id="t1",
            active_space_id="s1",
        )
        assert result.allowed is True
        assert result.reason == "messenger_delegated"

    @pytest.mark.asyncio
    async def test_exclusion_covers_all_intents_uniformly(self):
        """the design review v4 note: exclusion at the tool-call-name abstraction covers
        all three intents, not by intent and not by broader category. This
        guards against a future refactor that tries to exclude by intent
        (which would drift if a new intent is added)."""
        gate = _make_gate()
        for intent in ("ask_question", "request_action", "inform"):
            result = await gate.evaluate(
                tool_name="send_relational_message",
                tool_input={"addressee": "x", "intent": intent, "content": "..."},
                effect="unknown",
                user_message="",
                instance_id="t1",
                active_space_id="s1",
            )
            assert result.allowed is True, intent
            assert result.reason == "messenger_delegated", intent

    @pytest.mark.asyncio
    async def test_exclusion_ignores_pending_denial_count(self):
        """A runaway denial loop on some other tool must not affect the
        RM bypass."""
        gate = _make_gate()
        gate._denial_counts["send_relational_message"] = 99
        result = await gate.evaluate(
            tool_name="send_relational_message",
            tool_input={"addressee": "emma", "intent": "ask_question",
                        "content": "..."},
            effect="unknown",
            user_message="",
            instance_id="t1",
            active_space_id="s1",
        )
        assert result.allowed is True
        # Denial count on this tool was cleared, not leaked.
        assert gate._denial_counts.get("send_relational_message", 0) == 0


class TestNonRMSensitiveActionsStillGate:
    """The exclusion is scoped to send_relational_message only. Other
    sensitive actions (delete_file, send-email, etc.) still route through
    the model check path.
    """

    @pytest.mark.asyncio
    async def test_delete_file_still_gates(self):
        """delete_file is not send_relational_message — the exclusion does
        NOT apply. The gate's model check fires. We stub the cheap chain to
        CONFIRM so the gate returns allowed=False (needs confirmation)."""
        gate = _make_gate()
        result = await gate.evaluate(
            tool_name="delete_file",
            tool_input={"name": "private.md"},
            effect="hard_write",
            user_message="",
            instance_id="t1",
            active_space_id="s1",
        )
        # The gate DID call the cheap-chain model — exclusion doesn't cover this.
        assert gate._reasoning.complete_simple.await_count >= 1
        assert result.method == "model_check"
        # Model returned CONFIRM → gate does not auto-allow.
        assert result.reason == "confirm"

    @pytest.mark.asyncio
    async def test_send_email_still_gates(self):
        """send-email is a sensitive action; the exclusion does NOT cover
        it because it's not send_relational_message."""
        gate = _make_gate()
        result = await gate.evaluate(
            tool_name="send-email",
            tool_input={"to": "external@example.com",
                        "subject": "Confidential", "body": "..."},
            effect="unknown",
            user_message="",
            instance_id="t1",
            active_space_id="s1",
        )
        # Model check fired — exclusion scoped correctly.
        assert gate._reasoning.complete_simple.await_count >= 1

    @pytest.mark.asyncio
    async def test_exclusion_is_by_tool_name_not_capability(self):
        """A hypothetical 'messaging' capability tool that ISN'T
        send_relational_message does NOT inherit the exclusion. The the design review
        note was explicit: exclude by tool-call name, not by capability or
        intent or broader category."""
        gate = _make_gate()
        # A different tool name that conceptually lives in the same
        # relational space would still gate.
        result = await gate.evaluate(
            tool_name="send_to_channel",   # cross-member-ish but different tool
            tool_input={"channel": "family", "message": "..."},
            effect="soft_write",
            user_message="",
            instance_id="t1",
            active_space_id="s1",
            is_reactive=False,  # force the model path
        )
        assert gate._reasoning.complete_simple.await_count >= 1
        # And the result is NOT messenger_delegated.
        assert result.reason != "messenger_delegated"
        assert result.method != "messenger_handoff"


class TestLayer3UnconditionalFiringPinned:
    """Pinning test for the Layer-3 unconditional-firing invariant.

    The safety argument for removing the dispatch-gate intercept on
    ``send_relational_message`` depends on the Messenger hook firing on
    every RM-permitted outbound. That hook is wired at the single
    production instantiation site
    ``MessageHandler._get_relational_dispatcher``. If a future refactor
    lands a RelationalDispatcher without ``messenger_judge`` wired, the
    privacy backstop disappears silently.

    This test pins the coupling: it instantiates the handler's
    ``_get_relational_dispatcher`` through an in-memory state + minimal
    fakes and asserts the returned dispatcher has a non-None
    ``_messenger_judge``. If the constructor ever drops the argument, or
    a conditional path returns a dispatcher without the callback, this
    test fails.
    """

    def test_production_dispatcher_always_wires_messenger_judge(self, tmp_path):
        """The handler's _get_relational_dispatcher returns a dispatcher
        with _messenger_judge wired — no conditional path exists that
        strips it."""
        from unittest.mock import MagicMock

        from kernos.messages.handler import MessageHandler

        # Bypass MessageHandler.__init__ — we only need _get_relational_dispatcher
        # to be callable. Minimal attribute injection.
        handler = MessageHandler.__new__(MessageHandler)
        handler._relational_dispatcher = None
        handler._instance_db = MagicMock()  # non-None so the early-return branch doesn't fire
        handler.state = MagicMock()
        handler._adapters = {}
        # _build_messenger_judge_callback references self.reasoning, which is
        # only evaluated lazily when the callback fires — for the construction
        # path we just need it to exist.
        handler.reasoning = MagicMock()

        dispatcher = handler._get_relational_dispatcher()

        assert dispatcher is not None, (
            "_get_relational_dispatcher returned None — the handler's "
            "wiring contract is broken."
        )
        assert dispatcher._messenger_judge is not None, (
            "RelationalDispatcher was constructed without messenger_judge. "
            "Layer 3 unconditional-firing invariant is broken — the "
            "dispatch-gate exclusion on send_relational_message now leaves "
            "cross-member outbound exchanges without a welfare backstop. "
            "This is a privacy regression."
        )
        assert callable(dispatcher._messenger_judge), (
            "messenger_judge is wired but not callable."
        )
