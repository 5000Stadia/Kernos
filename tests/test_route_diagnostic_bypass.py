"""Pin tests for the diagnostic slash-command routing bypass.

Codex round 7 (CCV1 soak deliberation) surfaced the load-bearing
finding that ``/dump`` was routing through the normal router
cohort — which classifies it as "diagnostic intent" and switches
to the System space — so the captured diagnostic was for a
different substrate than the conversation that produced it.

Fix: route phase detects the diagnostic bypass commands at the
top and short-circuits to ``current_focus_id`` without consulting
the router cohort. This file pins that behavior.

Per the project's substrate-fidelity assertion pattern, every
soak-discovered failure mode gets a NEW pin test that captures
the specific failure so it can't regress.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.kernel.router import RouterResult
from kernos.messages.models import AuthLevel, NormalizedMessage
from kernos.messages.phase_context import PhaseContext
from kernos.messages.phases.route import run as route_run


def _make_ctx(content: str, current_focus: str = "space_general") -> PhaseContext:
    """Minimal phase-context shaped for the route phase entry."""
    handler = MagicMock()
    handler.conversations = MagicMock()
    handler.conversations.get_recent_full = AsyncMock(return_value=[])
    handler.state = MagicMock()
    handler.state.get_instance_profile = AsyncMock(return_value=MagicMock(
        last_active_space_id=current_focus,
    ))
    handler.state.get_context_space = AsyncMock(return_value=None)
    handler.state.update_context_space = AsyncMock(return_value=None)
    handler.state.save_instance_profile = AsyncMock(return_value=None)
    # ROUTER-EVIDENCE-V1: route.py now calls list_route_candidate_spaces
    # which calls state.list_context_spaces. Empty list keeps non-bypass
    # paths exercising the legacy router-internal-candidates fallback.
    handler.state.list_context_spaces = AsyncMock(return_value=[])
    # ROUTER-EVIDENCE-V1: compaction service is consulted by the
    # evidence builder. AsyncMocks default-return Sentinels which the
    # builder treats as empty content via its fail-open path.
    handler.compaction = MagicMock()
    handler.compaction.load_living_state = AsyncMock(return_value="")
    handler.compaction.load_recent_ledger_entries = AsyncMock(return_value=[])
    handler.events = MagicMock()
    handler.events.emit = AsyncMock(return_value=None)
    handler._router = MagicMock()
    # The cohort returns System if invoked — proving bypass works
    # means this should NOT be reached for /dump.
    handler._router.route = AsyncMock(return_value=RouterResult(
        tags=["space_system"], focus="space_system",
        continuation=False, query_mode=False,
    ))
    handler._downward_search = AsyncMock(return_value="")
    handler._workspace = MagicMock()
    handler._workspace.ensure_registered = AsyncMock(return_value=None)
    handler._tool_catalog = MagicMock()
    handler.conv_logger = MagicMock()
    handler.conv_logger.read_current_log_text = AsyncMock(return_value="")
    handler.conv_logger.read_recent = AsyncMock(return_value=[])
    handler._check_catalog_version = AsyncMock(return_value=None)
    # _run_session_exit fires as a fire-and-forget task on space switch.
    async def _noop(*a, **k):
        return None
    handler._run_session_exit = _noop
    handler.reasoning = MagicMock()

    msg = NormalizedMessage(
        content=content,
        sender="operator",
        sender_auth_level=AuthLevel.owner_verified,
        platform="repl",
        platform_capabilities=["text"],
        conversation_id="operator",
        timestamp=__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc,
        ),
        instance_id="inst-test",
    )

    ctx = PhaseContext()
    ctx.handler = handler
    ctx.instance_id = "inst-test"
    ctx.conversation_id = "operator"
    ctx.member_id = "mem-test"
    ctx.message = msg
    return ctx


# ---------------------------------------------------------------------------
# /dump bypass
# ---------------------------------------------------------------------------


async def test_dump_bypasses_router_cohort_and_stays_in_current_focus():
    """The Codex C7 deliberation BLOCKER pin: ``/dump`` does NOT
    consult the router cohort. It stays in current_focus_id so the
    diagnostic captures the substrate of the conversation that
    produced it, not whatever space the cohort would route a
    "diagnostic intent" to."""
    ctx = _make_ctx("/dump", current_focus="space_general")
    await route_run(ctx)

    # The router cohort must NOT have been called.
    ctx.handler._router.route.assert_not_called()
    # And the active space must be current_focus, not System.
    assert ctx.active_space_id == "space_general", (
        f"/dump must stay in current_focus_id; got {ctx.active_space_id!r} "
        f"instead of 'space_general'. This is the exact failure mode "
        f"Codex caught in the CCV1 soak — substrate diagnostic "
        f"inspecting the wrong space."
    )


async def test_dump_bypass_short_circuits_evidence_build():
    """ROUTER-EVIDENCE-V1 v2 risk A: the bypass MUST short-circuit
    BEFORE building per-space evidence. Loading evidence for every
    candidate space on every /dump is wasted work and would slow the
    diagnostic noticeably on instances with many spaces."""
    ctx = _make_ctx("/dump", current_focus="space_general")
    await route_run(ctx)

    # If evidence build had been reached, list_context_spaces would
    # have been called. The bypass short-circuits before that.
    ctx.handler.state.list_context_spaces.assert_not_called()
    ctx.handler.compaction.load_living_state.assert_not_called()
    ctx.handler.compaction.load_recent_ledger_entries.assert_not_called()
    ctx.handler.conv_logger.read_recent.assert_not_called()


async def test_status_bypasses_router_cohort_and_stays_in_current_focus():
    """Same pin for /status — also a substrate-diagnostic command
    that should inspect current focus, not be re-routed."""
    ctx = _make_ctx("/status", current_focus="space_general")
    await route_run(ctx)
    ctx.handler._router.route.assert_not_called()
    assert ctx.active_space_id == "space_general"


async def test_dump_with_trailing_args_still_bypasses_router():
    """Bypass detection works on the first word, so future ``/dump
    --space X`` extensions still bypass the router."""
    ctx = _make_ctx("/dump --some-future-arg", current_focus="space_general")
    await route_run(ctx)
    ctx.handler._router.route.assert_not_called()
    assert ctx.active_space_id == "space_general"


# ---------------------------------------------------------------------------
# Non-diagnostic messages still route normally
# ---------------------------------------------------------------------------


async def test_normal_message_still_consults_router_cohort():
    """Pin: only the diagnostic bypass commands skip the router.
    Regular conversational input (or non-bypass slash commands)
    still go through normal routing."""
    ctx = _make_ctx("Hi there", current_focus="space_general")
    await route_run(ctx)
    ctx.handler._router.route.assert_awaited_once()


async def test_other_slash_commands_not_in_bypass_list_still_route():
    """Slash commands NOT in the diagnostic bypass list still
    route normally. The bypass is intentionally narrow — only
    substrate-diagnostic commands. Non-bypass commands like /wipe
    have their own routing semantics."""
    ctx = _make_ctx("/wipe", current_focus="space_general")
    await route_run(ctx)
    # /wipe is NOT in the bypass set, so the router cohort is consulted.
    ctx.handler._router.route.assert_awaited_once()


async def test_empty_current_focus_still_routes_dump():
    """Edge case: if there's no current_focus_id (truly fresh
    instance, no prior space), /dump can't bypass to nothing —
    falls through to normal routing so the system can establish a
    space for the operator."""
    ctx = _make_ctx("/dump", current_focus="")
    await route_run(ctx)
    # No current focus to bypass to → normal route runs.
    ctx.handler._router.route.assert_awaited_once()
