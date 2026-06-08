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
