"""Self-directed SPINE (2026-06-07): a plan must advance step-to-step in the
substrate, not depend on the model calling manage_plan(continue) each step
(which it does unreliably — plans silently stalled after step 1). _next_plan_
step_to_run picks the next step to auto-run, with guards.
"""
from kernos.messages.handler import _next_plan_step_to_run


def _plan(statuses, *, status="active", used=0, max_steps=30):
    return {
        "status": status,
        "budget": {"max_steps": max_steps},
        "usage": {"steps_used": used},
        "phases": [{"id": "p1", "title": "P", "steps": [
            {"id": f"s{i}", "title": f"step {i}", "status": st}
            for i, st in enumerate(statuses, 1)
        ]}],
    }


def test_advances_to_next_pending():
    nxt = _next_plan_step_to_run(_plan(["complete", "pending", "pending"]))
    assert nxt is not None and nxt["id"] == "s2"


def test_no_advance_when_a_step_in_progress():
    # model already advanced → don't double-advance
    assert _next_plan_step_to_run(_plan(["complete", "in_progress", "pending"])) is None


def test_no_advance_when_all_done():
    assert _next_plan_step_to_run(_plan(["complete", "complete"])) is None


def test_no_advance_when_paused():
    assert _next_plan_step_to_run(_plan(["complete", "pending"], status="paused")) is None


def test_no_advance_when_budget_spent():
    assert _next_plan_step_to_run(_plan(["complete", "pending"], used=30, max_steps=30)) is None


def test_advances_within_budget():
    nxt = _next_plan_step_to_run(_plan(["complete", "pending"], used=5, max_steps=30))
    assert nxt is not None and nxt["id"] == "s2"


# --- ⑥ plan results ledger (2026-06-08) ---------------------------------------
from kernos.messages.handler import _plan_ledger_block


def test_ledger_block_empty_when_no_results():
    assert _plan_ledger_block({}) == ""
    assert _plan_ledger_block({"step_results": []}) == ""


def test_ledger_block_renders_prior_step_results():
    plan = {"step_results": [
        {"step_id": "s1", "title": "Identity", "summary": "PASS — name is set"},
        {"step_id": "s2", "title": "Memory", "summary": "PASS — cerulean stored"},
    ]}
    block = _plan_ledger_block(plan)
    assert "PRIOR COMPLETED STEPS" in block
    assert "[s1] Identity: PASS — name is set" in block
    assert "[s2] Memory: PASS — cerulean stored" in block


def test_ledger_block_caps_and_truncates():
    plan = {"step_results": [
        {"step_id": f"s{i}", "title": "t", "summary": "x" * 500}
        for i in range(40)
    ]}
    block = _plan_ledger_block(plan)
    # only the last 25 are shown
    assert block.count("\n- ") <= 25
    # each summary truncated to 300 chars
    assert "x" * 301 not in block


def test_record_plan_step_result_appends_and_caps():
    from kernos.messages.handler import _record_plan_step_result, _plan_ledger_block
    plan = {}
    _record_plan_step_result(plan, "s1", "Identity", "PASS")
    assert plan["step_results"][-1] == {"step_id": "s1", "title": "Identity", "summary": "PASS"}
    # truncation
    _record_plan_step_result(plan, "s2", "t" * 200, "y" * 900)
    assert len(plan["step_results"][-1]["title"]) == 120
    assert len(plan["step_results"][-1]["summary"]) == 500
    # cap at 50
    for i in range(60):
        _record_plan_step_result(plan, f"x{i}", "t", "s")
    assert len(plan["step_results"]) == 50
    # and the ledger reads what was recorded
    assert "PRIOR COMPLETED STEPS" in _plan_ledger_block(plan)


# --- STEP-COMPLETION DISCIPLINE (#189, 2026-06-10) -----------------------------
# A turn producing text != the step's named actions having run (live: Test 7's
# register skipped, the final report-write skipped — both narrated honestly,
# both bookkept complete). verify_step_completion reads the narration; the
# spine re-dispatches the step ONCE with the named deficit. continue issued
# from inside a plan turn defers to the spine (the original #189 race).
import json as _json

from kernos.kernel.execution import verify_step_completion


class _FakeReasoning:
    def __init__(self, payload):
        self._payload = payload

    async def complete_simple(self, **kw):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


async def test_verifier_flags_named_skipped_action():
    # The live Test-7 shape: step names register; report admits it didn't run.
    fake = _FakeReasoning(_json.dumps(
        {"complete": False, "missing": "register_tool was not invoked"}))
    ok, missing = await verify_step_completion(
        fake, "Test 7 — build/test/register local coin flip tool",
        "Build/test passed. Registration was not attempted because ...",
    )
    assert ok is False
    assert "register" in missing


async def test_verifier_passes_complete_step():
    fake = _FakeReasoning(_json.dumps({"complete": True, "missing": ""}))
    ok, missing = await verify_step_completion(fake, "step", "did everything")
    assert ok is True and missing == ""


async def test_verifier_fails_open_on_error_and_garbage():
    ok, _ = await verify_step_completion(
        _FakeReasoning(RuntimeError("model down")), "step", "report")
    assert ok is True  # a broken verifier must never stall a plan
    ok, _ = await verify_step_completion(
        _FakeReasoning("not json at all"), "step", "report")
    assert ok is True


async def test_verifier_incomplete_without_deficit_is_treated_complete():
    # An INCOMPLETE verdict with no named deficit is unactionable.
    fake = _FakeReasoning(_json.dumps({"complete": False, "missing": ""}))
    ok, _ = await verify_step_completion(fake, "step", "report")
    assert ok is True


def test_envelope_carries_completion_retry_fields():
    from kernos.kernel.execution import ExecutionEnvelope
    env = ExecutionEnvelope(
        plan_id="p", step_id="s", workspace_id="", step_description="d")
    assert env.completion_retry is False and env.completion_deficit == ""
    import dataclasses
    retry = dataclasses.replace(
        env, completion_retry=True, completion_deficit="register the tool")
    assert retry.completion_retry and "register" in retry.completion_deficit


async def test_continue_in_plan_turn_defers_to_spine():
    # The original #189 race: a model-issued continue from INSIDE a plan
    # step's turn double-dispatches against the spine's auto-advance. The
    # early-return path needs no handler state — call the unbound method.
    from unittest.mock import MagicMock
    from kernos.messages.handler import MessageHandler
    result = await MessageHandler._handle_manage_plan(
        MagicMock(), "inst", "space", {"action": "continue"},
        in_plan_turn=True,
    )
    assert "automatically" in result and "double-dispatch" in result


async def test_reasoning_flags_plan_turn_conversations():
    # The dispatch derives in_plan_turn from the plan_<id> conversation id.
    from unittest.mock import AsyncMock, MagicMock
    from kernos.kernel.reasoning import ReasoningService
    svc = ReasoningService(AsyncMock(), AsyncMock(), MagicMock(), AsyncMock())
    svc._handler = MagicMock()
    svc._handler._handle_manage_plan = AsyncMock(return_value="ok")
    request = MagicMock()
    request.instance_id = "inst"
    request.active_space_id = "space"
    request.member_id = "m1"
    request.conversation_id = "plan_abc123"
    await svc.execute_tool("manage_plan", {"action": "continue"}, request)
    assert svc._handler._handle_manage_plan.await_args.kwargs["in_plan_turn"] is True

    request.conversation_id = "1500261768486977587"  # ordinary user channel
    await svc.execute_tool("manage_plan", {"action": "continue"}, request)
    assert svc._handler._handle_manage_plan.await_args.kwargs["in_plan_turn"] is False


async def test_spine_redispatches_incomplete_step_then_completes(
    tmp_path, monkeypatch,
):
    """End-to-end through the real spine: a step whose report admits a named
    action didn't run is NOT marked complete — it re-dispatches ONCE as a
    CONTINUATION carrying the deficit; the continuation then completes the
    step. Ledger keeps both outcomes (partial + final)."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from kernos.messages.handler import MessageHandler
    from kernos.kernel.execution import ExecutionEnvelope, save_plan, load_plan

    monkeypatch.setenv("KERNOS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KERNOS_STEP_COMPLETION_CHECK", "on")

    plan = {
        "plan_id": "plan_x", "status": "active", "title": "T",
        "budget": {"max_steps": 30}, "usage": {"steps_used": 1},
        "phases": [{"id": "p1", "title": "P", "steps": [
            {"id": "s1", "title": "build/test/register tool",
             "status": "in_progress"},
        ]}],
    }
    await save_plan(str(tmp_path), "t1", "sp1", plan)

    handler = MessageHandler.__new__(MessageHandler)
    handler.process = AsyncMock(
        return_value="Build/test passed. Registration was not attempted.")
    handler.reasoning = MagicMock()
    handler.reasoning.complete_simple = AsyncMock(return_value=_json.dumps(
        {"complete": False, "missing": "register_tool was never invoked"}))
    handler._plan_progress_msgs = {}
    handler._instance_db = None
    handler.send_outbound = AsyncMock()
    handler._delete_discord_msg = AsyncMock()

    env = ExecutionEnvelope(
        plan_id="plan_x", step_id="s1", workspace_id="",
        step_description="build, test, and register the coin flip tool",
        steps_used=1,
    )
    await handler._execute_self_directed_step("t1", "sp1", env)

    # First pass must NOT have completed the step — partial recorded instead.
    mid = await load_plan(str(tmp_path), "t1", "sp1")
    step = mid["phases"][0]["steps"][0]
    results = mid.get("step_results", [])
    assert any("PARTIAL" in (r.get("title") or "") for r in results)

    # Let the spawned continuation task run to completion.
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending)

    final = await load_plan(str(tmp_path), "t1", "sp1")
    assert final["phases"][0]["steps"][0]["status"] == "complete"
    # process ran twice; the second turn carried the deficit continuation.
    assert handler.process.await_count == 2
    second_msg = handler.process.await_args_list[1].args[0]
    assert "CONTINUATION" in second_msg.content
    assert "register_tool was never invoked" in second_msg.content
    # The verifier ran exactly once — continuations are never re-verified.
    assert handler.reasoning.complete_simple.await_count == 1


async def test_no_continuation_when_step_paused_its_own_plan(
    tmp_path, monkeypatch,
):
    """Codex review P2: a step that legitimately paused its plan (blocked)
    must NOT get a continuation dispatched — the deficit is noted, the
    paused plan stays paused, and the step falls through to the normal
    completion path."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from kernos.messages.handler import MessageHandler
    from kernos.kernel.execution import ExecutionEnvelope, save_plan, load_plan

    monkeypatch.setenv("KERNOS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KERNOS_STEP_COMPLETION_CHECK", "on")

    plan = {
        "plan_id": "plan_y", "status": "active", "title": "T",
        "budget": {"max_steps": 30}, "usage": {"steps_used": 1},
        "phases": [{"id": "p1", "title": "P", "steps": [
            {"id": "s1", "title": "blocked step", "status": "in_progress"},
        ]}],
    }
    await save_plan(str(tmp_path), "t1", "sp1", plan)

    handler = MessageHandler.__new__(MessageHandler)

    async def _process_and_pause(msg):
        # Simulate the turn pausing its own plan mid-step (blocked on input),
        # then reporting a deferred action.
        p = await load_plan(str(tmp_path), "t1", "sp1")
        p["status"] = "paused"
        p["paused_reason"] = "needs user input"
        await save_plan(str(tmp_path), "t1", "sp1", p)
        return "Blocked: the send was not performed; awaiting user input."

    handler.process = AsyncMock(side_effect=_process_and_pause)
    handler.reasoning = MagicMock()
    handler.reasoning.complete_simple = AsyncMock(return_value=_json.dumps(
        {"complete": False, "missing": "the send was not performed"}))
    handler._plan_progress_msgs = {}
    handler._instance_db = None
    handler.send_outbound = AsyncMock()
    handler._delete_discord_msg = AsyncMock()

    env = ExecutionEnvelope(
        plan_id="plan_y", step_id="s1", workspace_id="",
        step_description="send the message", steps_used=1,
    )
    await handler._execute_self_directed_step("t1", "sp1", env)
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending)

    # The P2 contract (r2): the blocked step is HELD, not completed — no
    # continuation dispatched, the partial outcome is recorded for resume,
    # the step stays in_progress, and the plan stays paused.
    assert handler.process.await_count == 1
    final = await load_plan(str(tmp_path), "t1", "sp1")
    assert final["status"] == "paused"
    # Codex P1: the held step resets to PENDING so a normal resume
    # (continue selects pending steps) re-runs the blocked work.
    assert final["phases"][0]["steps"][0]["status"] == "pending"
    held = [r for r in final.get("step_results", [])
            if "PARTIAL" in (r.get("title") or "")]
    assert held and "held" in held[0]["title"]
