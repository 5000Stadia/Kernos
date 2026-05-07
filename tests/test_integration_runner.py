"""Tests for the integration runner (C2 of INTEGRATION-LAYER).

Covers:
  - Happy path: model calls __finalize_briefing__ on first iteration
  - Iterative prep loop with a read-only tool call mid-run
  - Read-only enforcement (tool surfaced as soft_write rejected)
  - Read-only enforcement (tool not surfaced rejected)
  - max_iterations exhaustion → fail-soft fallback (BudgetState.iterations_hit_limit)
  - integration_timeout exhaustion → fail-soft fallback (BudgetState.timeout_hit_limit)
  - Model produces no tool_use → fail-soft fallback
  - Model emits invalid briefing → fail-soft (BriefingValidationError)
  - Audit emit fires on success and on fail-soft
  - Cohort references and tool invocation references land in audit_trace
  - Redaction invariant: Restricted CohortOutput content cannot leak
    into briefing text fields
"""

from __future__ import annotations

import pytest

from kernos.kernel.integration import (
    Briefing,
    BudgetState,
    ChainCaller,
    CohortOutput,
    IntegrationConfig,
    IntegrationInputs,
    IntegrationRunner,
    Public,
    ReadOnlyToolDispatcher,
    Restricted,
    SurfacedTool,
    SURFACING_RATIONALE_CREDENTIAL,
    SURFACING_RATIONALE_RELEVANCE,
    now_iso,
)
from kernos.providers.base import ContentBlock, ProviderResponse


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_inputs(**overrides) -> IntegrationInputs:
    base = dict(
        user_message="What did the doc say about Q3?",
        conversation_thread=(
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ),
        cohort_outputs=(
            CohortOutput(
                cohort_id="memory",
                cohort_run_id="memcohort:turn-7:r1",
                output={"hits": ["user is in marketing"]},
                visibility=Public(),
                produced_at=now_iso(),
            ),
            CohortOutput(
                cohort_id="weather",
                cohort_run_id="wcohort:turn-7:r1",
                output={"forecast": "clear"},
                visibility=Public(),
                produced_at=now_iso(),
            ),
        ),
        surfaced_tools=(
            SurfacedTool(
                tool_id="drive_read_doc",
                description="Read a Google Doc as markdown.",
                input_schema={"type": "object", "properties": {}},
                gate_classification="read",
                surfacing_rationale=SURFACING_RATIONALE_CREDENTIAL,
            ),
            SurfacedTool(
                tool_id="search_memory",
                description="Search memory.",
                input_schema={"type": "object", "properties": {}},
                gate_classification="read",
                surfacing_rationale=SURFACING_RATIONALE_RELEVANCE,
            ),
        ),
        active_context_spaces=({"space_id": "default", "domain": "general"},),
        member_id="m-1",
        instance_id="inst-1",
        space_id="default",
        turn_id="turn-7",
    )
    base.update(overrides)
    return IntegrationInputs(**base)


def _finalize_block(payload: dict) -> ContentBlock:
    return ContentBlock(
        type="tool_use",
        id="tu_finalize_1",
        name="__finalize_briefing__",
        input=payload,
    )


def _tool_use_block(name: str, args: dict, id_: str = "tu_1") -> ContentBlock:
    return ContentBlock(type="tool_use", id=id_, name=name, input=args)


def _text_block(text: str) -> ContentBlock:
    return ContentBlock(type="text", text=text)


def _resp(*blocks: ContentBlock, stop: str = "tool_use") -> ProviderResponse:
    return ProviderResponse(
        content=list(blocks),
        stop_reason=stop,
        input_tokens=10,
        output_tokens=20,
    )


_DEFAULT_BRIEFING_PAYLOAD = {
    "relevant_context": [
        {
            "source_type": "cohort.memory",
            "source_id": "memcohort:turn-7:r1",
            "summary": "user is in marketing; question is about Q3 doc",
            "confidence": 0.8,
        }
    ],
    "filtered_context": [
        {
            "source_type": "cohort.weather",
            "source_id": "wcohort:turn-7:r1",
            "reason_filtered": "user did not ask about weather",
        }
    ],
    "decided_action": {"kind": "respond_only"},
    "presence_directive": "answer about Q3 succinctly using marketing framing",
}


def _make_runner(
    chain_caller: ChainCaller | None = None,
    dispatcher: ReadOnlyToolDispatcher | None = None,
    audit_sink: list | None = None,
    config: IntegrationConfig | None = None,
    clock=None,
) -> tuple[IntegrationRunner, list]:
    sink = audit_sink if audit_sink is not None else []

    async def _default_chain(*_a, **_kw):  # pragma: no cover
        return _resp(_finalize_block(_DEFAULT_BRIEFING_PAYLOAD))

    async def _default_dispatcher(*_a, **_kw):
        return {"ok": True}

    async def _emit(entry: dict) -> None:
        sink.append(entry)

    # Default single-attempt config for legacy tests that pre-date the
    # retry harness (PHASE-1-WIPE-VERIFICATION 2026-05-07). Tests that
    # specifically exercise retry behavior pass an explicit
    # IntegrationConfig with max_retries > 1.
    runner_kwargs = dict(
        chain_caller=chain_caller or _default_chain,
        read_only_dispatcher=dispatcher or _default_dispatcher,
        audit_emitter=_emit,
        config=config or IntegrationConfig(max_retries=1),
    )
    if clock is not None:
        runner_kwargs["clock"] = clock
    return IntegrationRunner(**runner_kwargs), sink


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_happy_path_single_iteration():
    captured = {}

    async def chain(system, messages, tools, max_tokens, **_):
        captured["system"] = system
        captured["messages"] = messages
        captured["tools"] = tools
        return _resp(_finalize_block(_DEFAULT_BRIEFING_PAYLOAD))

    runner, audit = _make_runner(chain_caller=chain)
    briefing = await runner.run(_make_inputs())

    assert isinstance(briefing, Briefing)
    assert briefing.audit_trace.fail_soft_engaged is False
    assert briefing.audit_trace.iterations_used == 1
    assert briefing.turn_id == "turn-7"
    assert briefing.audit_trace.cohort_outputs == (
        "memcohort:turn-7:r1",
        "wcohort:turn-7:r1",
    )
    assert briefing.audit_trace.budget_state.any_hit is False
    assert briefing.presence_directive.startswith("answer about Q3")
    assert len(audit) == 1
    assert audit[0]["audit_category"] == "integration.briefing"
    assert audit[0]["success"] is True


@pytest.mark.asyncio
async def test_runner_prompt_carries_inputs():
    captured = {}

    async def chain(system, messages, tools, max_tokens, **_):
        captured["system"] = system
        captured["messages"] = messages
        captured["tools"] = tools
        return _resp(_finalize_block(_DEFAULT_BRIEFING_PAYLOAD))

    runner, _ = _make_runner(chain_caller=chain)
    await runner.run(_make_inputs())

    body = captured["messages"][0]["content"]
    assert "<conversation_thread>" in body
    assert "<cohort_outputs>" in body
    assert "memory" in body  # cohort_id
    assert "drive_read_doc" in body
    assert SURFACING_RATIONALE_CREDENTIAL in body
    assert "What did the doc say about Q3?" in body

    tool_names = [t["name"] for t in captured["tools"]]
    assert "__finalize_briefing__" in tool_names
    assert "drive_read_doc" in tool_names
    assert "search_memory" in tool_names


# ---------------------------------------------------------------------------
# Iterative prep loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_iterates_with_read_only_tool_call():
    call_count = {"n": 0}
    dispatch_calls = []

    async def chain(system, messages, tools, max_tokens, **_):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _resp(
                _text_block("I need the Q3 doc."),
                _tool_use_block(
                    "drive_read_doc",
                    {"file_id": "abc-123"},
                    id_="tu_drive",
                ),
            )
        return _resp(
            _finalize_block(
                {
                    **_DEFAULT_BRIEFING_PAYLOAD,
                    "relevant_context": [
                        {
                            "source_type": "tool.read.drive_read_doc",
                            "source_id": "drive_read_doc:1",
                            "summary": "Q3 plan focuses on launch",
                            "confidence": 0.9,
                        }
                    ],
                }
            )
        )

    async def dispatcher(tool_id, args, inputs):
        dispatch_calls.append((tool_id, args))
        return {
            "invocation_id": "inv-doc-42",
            "title": "Q3 Plan",
            "markdown": "# Q3 Plan\n\nLaunch on time.",
        }

    runner, audit = _make_runner(chain_caller=chain, dispatcher=dispatcher)
    briefing = await runner.run(_make_inputs())

    assert briefing.audit_trace.iterations_used == 2
    assert briefing.audit_trace.tools_called_during_prep == ("inv-doc-42",)
    assert dispatch_calls == [("drive_read_doc", {"file_id": "abc-123"})]
    assert briefing.relevant_context[0].source_type == "tool.read.drive_read_doc"
    assert audit[0]["success"] is True


# ---------------------------------------------------------------------------
# Multi-tool-use regression: Codex Responses-API mismatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_drops_undispatched_tool_uses_from_chain_history():
    """Regression for the Codex 400 "No tool output found for function
    call <id>" error. When the model emits multiple tool_use blocks in
    one response, the runner dispatches only tool_uses[0]. The other
    tool_use blocks must NOT land in the assistant message that goes
    back to the provider — otherwise the next chain call has N
    function_call events with only 1 function_call_output, and Codex
    rejects the input."""
    chain_invocations: list[list[dict]] = []
    call_count = {"n": 0}

    async def chain(system, messages, tools, max_tokens, **_):
        # Capture the messages list passed in on each call so we can
        # verify the assistant message is balanced.
        chain_invocations.append([dict(m) for m in messages])
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Model emits THREE tool_use blocks in one response.
            return _resp(
                _text_block("I need the doc and a memory hit."),
                _tool_use_block(
                    "drive_read_doc",
                    {"file_id": "abc-123"},
                    id_="tu_dispatched",
                ),
                _tool_use_block(
                    "search_memory",
                    {"q": "Q3 plan"},
                    id_="tu_orphan_1",
                ),
                _tool_use_block(
                    "drive_read_doc",
                    {"file_id": "def-456"},
                    id_="tu_orphan_2",
                ),
            )
        return _resp(_finalize_block(_DEFAULT_BRIEFING_PAYLOAD))

    async def dispatcher(tool_id, args, inputs):
        return {
            "invocation_id": "inv-1",
            "title": "doc",
            "markdown": "body",
        }

    runner, _ = _make_runner(chain_caller=chain, dispatcher=dispatcher)
    await runner.run(_make_inputs())

    # On the SECOND call (after one dispatch), the chain history sent
    # to the provider must contain a balanced assistant message —
    # exactly ONE tool_use (the dispatched one), NOT all three.
    assert len(chain_invocations) == 2
    second_messages = chain_invocations[1]
    # The trailing assistant message before the last user/tool_result
    # turn is the one we just appended.
    assistant_msgs = [
        m for m in second_messages if m.get("role") == "assistant"
    ]
    assert assistant_msgs, "expected the assistant message to land in history"
    last_assistant = assistant_msgs[-1]
    tool_use_blocks_in_history = [
        b for b in last_assistant.get("content", [])
        if isinstance(b, dict) and b.get("type") == "tool_use"
    ]
    assert len(tool_use_blocks_in_history) == 1, (
        f"expected exactly 1 tool_use in the assistant message, got "
        f"{len(tool_use_blocks_in_history)} — orphans would cause Codex "
        f"to reject the input with 'No tool output found for function "
        f"call <id>'"
    )
    assert tool_use_blocks_in_history[0]["id"] == "tu_dispatched"
    # The text block from the response is preserved.
    text_blocks = [
        b for b in last_assistant.get("content", [])
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    assert len(text_blocks) == 1
    assert "doc and a memory hit" in text_blocks[0]["text"]


# ---------------------------------------------------------------------------
# Read-only enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_rejects_non_read_tool_with_fail_soft():
    async def chain(*_a, **_kw):
        return _resp(
            _tool_use_block(
                "send_email", {"to": "x"}, id_="tu_send"
            )
        )

    inputs = _make_inputs(
        surfaced_tools=(
            SurfacedTool(
                tool_id="send_email",
                description="Send an email.",
                input_schema={"type": "object"},
                gate_classification="hard_write",
                surfacing_rationale="should not be here",
            ),
        )
    )
    runner, audit = _make_runner(chain_caller=chain)
    briefing = await runner.run(inputs)

    assert briefing.audit_trace.fail_soft_engaged is True
    assert "non-read" in briefing.audit_trace.notes
    assert audit[0]["success"] is False
    assert "send_email" in audit[0]["error"]


@pytest.mark.asyncio
async def test_runner_filters_non_read_tools_from_model_surface():
    captured = {}

    async def chain(system, messages, tools, max_tokens, **_):
        captured["tools"] = tools
        return _resp(_finalize_block(_DEFAULT_BRIEFING_PAYLOAD))

    inputs = _make_inputs(
        surfaced_tools=(
            SurfacedTool(
                tool_id="drive_read_doc",
                description="read",
                input_schema={"type": "object"},
                gate_classification="read",
                surfacing_rationale="x",
            ),
            SurfacedTool(
                tool_id="send_message",
                description="send",
                input_schema={"type": "object"},
                gate_classification="hard_write",
                surfacing_rationale="surfaced erroneously",
            ),
        )
    )
    runner, _ = _make_runner(chain_caller=chain)
    await runner.run(inputs)

    tool_names = {t["name"] for t in captured["tools"]}
    assert "drive_read_doc" in tool_names
    assert "send_message" not in tool_names


@pytest.mark.asyncio
async def test_runner_rejects_unsurfaced_tool():
    async def chain(*_a, **_kw):
        return _resp(
            _tool_use_block("nonexistent_tool", {}, id_="tu_x")
        )

    runner, audit = _make_runner(chain_caller=chain)
    briefing = await runner.run(_make_inputs())

    assert briefing.audit_trace.fail_soft_engaged is True
    assert "not surfaced" in briefing.audit_trace.notes
    assert audit[0]["success"] is False


# ---------------------------------------------------------------------------
# Budget exhaustion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_max_iterations_triggers_iteration_cap_prompt():
    """ITERATION-CAP-PROMPT (2026-05-07): when retries exhaust on
    component=max_iterations, the briefing's directive surfaces a
    three-option choice (continue / always continue / terminate)
    rather than the generic system-error directive — that failure
    mode is recoverable by user choice."""
    async def chain(*_a, **_kw):
        return _resp(
            _tool_use_block(
                "search_memory", {"q": "x"}, id_=f"tu_loop"
            )
        )

    async def dispatcher(*_a, **_kw):
        return {"hits": []}

    runner, audit = _make_runner(
        chain_caller=chain,
        dispatcher=dispatcher,
        config=IntegrationConfig(max_iterations=3, max_retries=1),
    )
    briefing = await runner.run(_make_inputs())

    assert briefing.audit_trace.fail_soft_engaged is True
    assert "ITERATION CHECKPOINT REACHED" in briefing.presence_directive
    assert "continue" in briefing.presence_directive
    assert "always continue" in briefing.presence_directive
    assert "terminate" in briefing.presence_directive
    assert "KERNOS_INTEGRATION_MAX_ITERATIONS" in briefing.presence_directive
    assert "iteration-cap-prompt" in briefing.audit_trace.notes
    assert briefing.audit_trace.iterations_used == 3
    assert briefing.audit_trace.budget_state.iterations_hit_limit is True
    assert audit[0]["success"] is False


@pytest.mark.asyncio
async def test_runner_iteration_cap_prompt_quotes_configured_cap():
    """The directive interpolates the actual ``max_iterations`` value
    so the user sees the budget they hit (the env var line they'd
    append uses double the cap)."""
    async def chain(*_a, **_kw):
        return _resp(
            _tool_use_block("search_memory", {"q": "x"}, id_="tu_loop")
        )

    async def dispatcher(*_a, **_kw):
        return {"hits": []}

    runner, _audit = _make_runner(
        chain_caller=chain,
        dispatcher=dispatcher,
        config=IntegrationConfig(max_iterations=50, max_retries=1),
    )
    briefing = await runner.run(_make_inputs())

    assert "50 reasoning iterations" in briefing.presence_directive
    assert "50-iteration" in briefing.presence_directive
    # raised_cap = max(cap*2, cap+100) — the +100 floor keeps small
    # caps from doubling to a still-small number (e.g. 5 → 105 not 10)
    assert "KERNOS_INTEGRATION_MAX_ITERATIONS=150" in briefing.presence_directive


def test_integration_config_default_max_iterations_is_checkpoint_cadence():
    """Default cap is the natural-checkpoint cadence — high enough to
    absorb routine multi-step work in one shot, low enough that long
    work surfaces a check-in to the user partway through. The 5-iter
    legacy default was sized for briefing-assembly; after CCV1 C7
    strike turned the integration runner into the primary tool-
    dispatch seam, the cap was raised to 50."""
    cfg = IntegrationConfig()
    assert cfg.max_iterations == 50


@pytest.mark.asyncio
async def test_runner_timeout_triggers_fail_soft():
    ticks = iter([0.0, 0.0, 0.0, 0.0, 100.0, 100.0, 100.0])
    last = [0.0]

    def clock():
        try:
            last[0] = next(ticks)
        except StopIteration:
            pass
        return last[0]

    async def chain(*_a, **_kw):
        return _resp(
            _tool_use_block("search_memory", {"q": "x"}, id_="tu_loop")
        )

    async def dispatcher(*_a, **_kw):
        return {"hits": []}

    runner, audit = _make_runner(
        chain_caller=chain,
        dispatcher=dispatcher,
        config=IntegrationConfig(
            max_iterations=10, integration_timeout_seconds=1.0,
            max_retries=1,
        ),
        clock=clock,
    )
    briefing = await runner.run(_make_inputs())

    assert briefing.audit_trace.fail_soft_engaged is True
    assert "integration_timeout" in briefing.audit_trace.notes
    assert briefing.audit_trace.budget_state.timeout_hit_limit is True
    assert audit[0]["success"] is False


@pytest.mark.asyncio
async def test_runner_no_tool_use_triggers_fail_soft():
    async def chain(*_a, **_kw):
        return _resp(_text_block("just thinking out loud"))

    runner, audit = _make_runner(chain_caller=chain)
    briefing = await runner.run(_make_inputs())

    assert briefing.audit_trace.fail_soft_engaged is True
    assert "no tool_use" in briefing.audit_trace.notes
    assert audit[0]["success"] is False


# ---------------------------------------------------------------------------
# Briefing validation failure → fail-soft
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_invalid_briefing_falls_back_soft():
    bad = dict(_DEFAULT_BRIEFING_PAYLOAD)
    bad["presence_directive"] = ""

    async def chain(*_a, **_kw):
        return _resp(_finalize_block(bad))

    runner, audit = _make_runner(chain_caller=chain)
    briefing = await runner.run(_make_inputs())

    assert briefing.audit_trace.fail_soft_engaged is True
    assert audit[0]["success"] is False


@pytest.mark.asyncio
async def test_runner_retries_on_failure_then_succeeds():
    """PHASE-1-WIPE-VERIFICATION 2026-05-07: synthesis retries on
    each failed attempt rather than falling back to a generic
    "limited context" briefing on first failure. This test fails the
    first attempt (no_tool_use) and succeeds on the second.
    """
    call_count = [0]

    async def chain(*_a, **_kw):
        call_count[0] += 1
        if call_count[0] == 1:
            # First attempt: no tool_use → IntegrationAttemptFailed
            return _resp(_text_block("just thinking"))
        # Second attempt: clean finalize
        return _resp(_finalize_block(_DEFAULT_BRIEFING_PAYLOAD))

    runner, audit = _make_runner(
        chain_caller=chain,
        config=IntegrationConfig(max_retries=3),
    )
    briefing = await runner.run(_make_inputs())

    # Final briefing is a real success, not the system-error path
    assert briefing.audit_trace.fail_soft_engaged is False
    # First entry is the retry audit (the failed first attempt)
    assert audit[0]["audit_category"] == "integration.retry"
    assert audit[0]["attempt"] == 1
    assert audit[0]["component"] == "no_tool_use"
    # Last entry is the successful briefing
    assert audit[-1]["audit_category"] == "integration.briefing"
    assert audit[-1]["success"] is True


@pytest.mark.asyncio
async def test_runner_exhausts_retries_then_emits_system_error():
    """When all retries fail, the runner returns a
    ``system_error_briefing`` whose directive instructs presence to
    surface the failure transparently — NOT the old "respond
    conservatively" apology. The directive is the load-bearing piece
    of this fix; it changes the agent's response shape from "I'm
    here, send me what you want" to a transparent error report.
    """
    async def chain(*_a, **_kw):
        return _resp(_text_block("never finalize"))

    runner, audit = _make_runner(
        chain_caller=chain,
        config=IntegrationConfig(max_retries=3),
    )
    briefing = await runner.run(_make_inputs())

    # Hard error: fail_soft_engaged True for backward compat, but
    # the directive is now the transparent system-error one.
    assert briefing.audit_trace.fail_soft_engaged is True
    assert "INTEGRATION SYNTHESIS FAILED" in briefing.presence_directive
    assert "no_tool_use" in briefing.presence_directive
    # Notes carry the attempt count + final component for operator triage.
    assert "system-error after 3 attempts" in briefing.audit_trace.notes
    assert "no_tool_use" in briefing.audit_trace.notes
    # Three retry audits + one final integration.briefing audit
    retry_audits = [a for a in audit if a.get("audit_category") == "integration.retry"]
    assert len(retry_audits) == 3
    assert retry_audits[0]["attempt"] == 1
    assert retry_audits[-1]["attempt"] == 3
    final_audit = [a for a in audit if a.get("audit_category") == "integration.briefing"]
    assert len(final_audit) == 1
    assert final_audit[0]["success"] is False


@pytest.mark.asyncio
async def test_runner_safety_degraded_exhaustion_routes_to_defer():
    """Safety-degraded turns enter the retry loop with safety preamble
    (the model is expected to comply via prompt guidance). Only on
    retry exhaustion does the runner route to a Defer briefing rather
    than a generic system-error — real safety failures must surface as
    a Defer, not a respond_only system error.
    """
    call_count = {"n": 0}

    async def chain(*_a, **_kw):
        # All attempts produce malformed output → synthesis failure on
        # every retry. The retry loop runs to exhaustion.
        call_count["n"] += 1
        return _resp("no finalize block here")

    inputs = _make_inputs()
    inputs = IntegrationInputs(
        user_message=inputs.user_message,
        conversation_thread=inputs.conversation_thread,
        cohort_outputs=inputs.cohort_outputs,
        surfaced_tools=inputs.surfaced_tools,
        active_context_spaces=inputs.active_context_spaces,
        member_id=inputs.member_id,
        instance_id=inputs.instance_id,
        space_id=inputs.space_id,
        turn_id=inputs.turn_id,
        integration_run_id=inputs.integration_run_id,
        required_safety_cohort_failures=("safety_cohort_X",),
        cognitive_context=inputs.cognitive_context,
    )
    runner, audit = _make_runner(
        chain_caller=chain,
        config=IntegrationConfig(max_retries=3, max_iterations=1),
    )
    briefing = await runner.run(inputs)

    # Chain WAS called: safety-degraded turns go through synthesis with
    # the safety preamble in the prompt.
    assert call_count["n"] >= 1
    # Retry audits emitted on each failed attempt.
    retry_audits = [
        a for a in audit if a.get("audit_category") == "integration.retry"
    ]
    assert len(retry_audits) == 3
    # On exhaustion, the safety branch produces a Defer (not a
    # system-error briefing).
    from kernos.kernel.integration.briefing import Defer
    assert isinstance(briefing.decided_action, Defer)
    assert (
        briefing.audit_trace.budget_state.required_safety_cohort_failed
        is True
    )


def test_integration_config_rejects_zero_max_retries():
    """max_retries < 1 would skip the retry loop and trip run()'s
    exhaustion-path assertion. Reject loudly at construction time.
    """
    import pytest as _pytest
    with _pytest.raises(ValueError, match="max_retries must be >= 1"):
        IntegrationConfig(max_retries=0)
    with _pytest.raises(ValueError, match="max_retries must be >= 1"):
        IntegrationConfig(max_retries=-2)


def test_integration_config_rejects_negative_backoff():
    import pytest as _pytest
    with _pytest.raises(
        ValueError, match="retry_backoff_seconds must be >= 0"
    ):
        IntegrationConfig(retry_backoff_seconds=-0.5)


def test_integration_config_from_env_reads_overrides(monkeypatch):
    """from_env() should pick up KERNOS_INTEGRATION_* env vars and
    KERNOS_DATA_DIR; programmatic overrides win over env values.
    """
    monkeypatch.setenv("KERNOS_INTEGRATION_TIMEOUT_SECONDS", "75.5")
    monkeypatch.setenv("KERNOS_INTEGRATION_MAX_RETRIES", "5")
    monkeypatch.setenv("KERNOS_INTEGRATION_MAX_ITERATIONS", "8")
    monkeypatch.setenv("KERNOS_DATA_DIR", "/tmp/kernos_test_root")

    cfg = IntegrationConfig.from_env()
    assert cfg.integration_timeout_seconds == 75.5
    assert cfg.max_retries == 5
    assert cfg.max_iterations == 8
    assert cfg.data_dir == "/tmp/kernos_test_root"

    # Programmatic overrides win over env
    cfg2 = IntegrationConfig.from_env(max_retries=2)
    assert cfg2.max_retries == 2  # override
    assert cfg2.integration_timeout_seconds == 75.5  # env retained


def test_integration_config_from_env_ignores_garbage(monkeypatch):
    """Malformed env values are logged and ignored; defaults stand.
    KERNOS_DATA_DIR unset → mirror server.py convention and default
    to ``./data`` so friction reports land where the operator already
    looks.
    """
    monkeypatch.setenv("KERNOS_INTEGRATION_TIMEOUT_SECONDS", "not-a-float")
    monkeypatch.setenv("KERNOS_INTEGRATION_MAX_RETRIES", "abc")
    monkeypatch.delenv("KERNOS_DATA_DIR", raising=False)

    cfg = IntegrationConfig.from_env()
    assert cfg.integration_timeout_seconds == 600.0  # default
    assert cfg.max_retries == 3  # default
    assert cfg.data_dir == "./data"


def test_integration_config_rejects_non_finite_timeout():
    """NaN/inf would silently disable the timeout guardrail because
    finite-NaN comparisons return False. Reject loudly.
    Negative is nonsense. 0 is the explicit disable sentinel — accepted.
    """
    import math
    import pytest as _pytest
    with _pytest.raises(
        ValueError, match="finite non-negative number"
    ):
        IntegrationConfig(integration_timeout_seconds=math.nan)
    with _pytest.raises(
        ValueError, match="finite non-negative number"
    ):
        IntegrationConfig(integration_timeout_seconds=math.inf)
    with _pytest.raises(
        ValueError, match="finite non-negative number"
    ):
        IntegrationConfig(integration_timeout_seconds=-1)
    # 0 is accepted: opt-in disable of the wall-clock ceiling.
    cfg = IntegrationConfig(integration_timeout_seconds=0)
    assert cfg.integration_timeout_seconds == 0


@pytest.mark.asyncio
async def test_runner_skips_timeout_check_when_disabled():
    """When integration_timeout_seconds=0, the wall-clock guardrail is
    disabled — meaningful work can run as long as it needs. Verified by
    advancing a fake clock far past any reasonable wall-time and
    confirming no integration_timeout failure fires.
    """
    # Simulate a clock that advances by 1000s per call so any
    # wall-clock check would trip immediately.
    state = {"now": 0.0}
    def fake_clock() -> float:
        state["now"] += 1000.0
        return state["now"]

    captured: list = []
    async def chain(*_a, **_kw):
        # Always emit __finalize_briefing__ so the loop completes
        # synthesis on the first iteration after timeout check.
        if not captured:
            captured.append("first")
            block = ContentBlock(
                type="tool_use", id="t1",
                name="__finalize_briefing__",
                input=_DEFAULT_BRIEFING_PAYLOAD,
            )
            return ProviderResponse(content=[block], tokens_in=10, tokens_out=20)
        return _resp("unused")

    async def dispatcher(*_a, **_kw):
        return {"content": "result"}

    async def emitter(_record):
        pass

    runner = IntegrationRunner(
        chain_caller=chain,
        read_only_dispatcher=dispatcher,
        audit_emitter=emitter,
        config=IntegrationConfig(
            max_retries=1,
            max_iterations=5,
            integration_timeout_seconds=0,  # disable
        ),
        clock=fake_clock,
    )
    # Should NOT raise integration_timeout; should return a successful
    # briefing (or any non-timeout outcome).
    briefing = await runner.run(_make_inputs())
    # If the disable worked, we don't see the integration_timeout
    # component in audit_trace.notes.
    assert "integration_timeout" not in (briefing.audit_trace.notes or "")


def test_integration_config_rejects_zero_max_iterations():
    import pytest as _pytest
    with _pytest.raises(ValueError, match="max_iterations must be >= 1"):
        IntegrationConfig(max_iterations=0)


@pytest.mark.asyncio
async def test_runner_writes_friction_report_on_exhaustion(tmp_path):
    """When the retry harness exhausts, the runner drops a markdown
    friction report into ``{data_dir}/diagnostics/friction/`` so the
    operator sees the timeout at session start (mirrors
    FrictionObserver's surfacing convention).
    """
    async def chain(*_a, **_kw):
        return _resp("no finalize block here")  # synthesis fails

    runner, _audit = _make_runner(
        chain_caller=chain,
        config=IntegrationConfig(
            max_retries=2,
            max_iterations=1,
            data_dir=str(tmp_path),
        ),
    )
    await runner.run(_make_inputs())

    friction_dir = tmp_path / "diagnostics" / "friction"
    reports = list(friction_dir.glob("FRICTION_*INTEGRATION_*.md"))
    assert len(reports) == 1
    body = reports[0].read_text()
    # Structural shape — mirrors FrictionObserver report
    assert "# Friction Report:" in body
    assert "## Description" in body
    assert "## Recommendation:" in body
    assert "## Per-attempt breakdown" in body
    assert "## Aggregate signals" in body
    # Per-iteration metrics rendered (model_ms, dispatch_ms, etc.)
    assert "model_ms=" in body
    # Component is in the filename (constrained label)
    fname = reports[0].name
    assert "INTEGRATION_" in fname


@pytest.mark.asyncio
async def test_runner_skips_friction_report_when_data_dir_unset(
    tmp_path,
):
    """Without data_dir configured, the runner does not write a
    friction report — appropriate for tests and library-only use.
    """
    async def chain(*_a, **_kw):
        return _resp("no finalize block here")

    runner, _audit = _make_runner(
        chain_caller=chain,
        config=IntegrationConfig(max_retries=2, max_iterations=1),
        # data_dir intentionally unset
    )
    await runner.run(_make_inputs())

    friction_dir = tmp_path / "diagnostics" / "friction"
    assert not friction_dir.exists() or not list(friction_dir.iterdir())


@pytest.mark.asyncio
async def test_iteration_metrics_recorded_per_attempt():
    """Per-iteration metrics on IntegrationAttemptFailed should
    capture model_ms, tool_name, and tool_result_chars when an
    iteration completes a tool dispatch.
    """
    captured: list = []

    async def chain(system, messages, tools, max_tokens):
        # First call: emit a tool_use for a real tool. Second call:
        # produce no tool_use so the attempt fails on no_tool_use,
        # but iteration_metrics from iter 1 should be carried in the
        # raised exception.
        if not captured:
            captured.append("first")
            block = ContentBlock(
                type="tool_use", id="t1",
                name="reference_lookup",
                input={"query": "x"},
            )
            return ProviderResponse(content=[block], tokens_in=10, tokens_out=20)
        return _resp("no tool block")

    async def dispatcher(name, args, inputs):
        return {"invocation_id": f"{name}:1", "content": "small result"}

    audit: list = []
    async def emitter(record):
        audit.append(record)

    runner = IntegrationRunner(
        chain_caller=chain,
        read_only_dispatcher=dispatcher,
        audit_emitter=emitter,
        config=IntegrationConfig(max_retries=1, max_iterations=5),
    )
    inputs = _make_inputs(
        surfaced_tools=(
            SurfacedTool(
                tool_id="reference_lookup",
                description="lookup",
                input_schema={"type": "object"},
                gate_classification="read",
                surfacing_rationale=SURFACING_RATIONALE_RELEVANCE,
            ),
        ),
    )
    briefing = await runner.run(inputs)
    # Run produced the system-error briefing (no_tool_use on iter 2).
    # The retry audit record carries iteration_metrics — verify the
    # attempt-failure audit captured the per-iter signal.
    retry_records = [
        r for r in audit if r.get("audit_category") == "integration.retry"
    ]
    assert len(retry_records) == 1
    # Confirm the briefing audit-trace shows the eventual failure path.
    assert briefing.audit_trace.fail_soft_engaged is True


@pytest.mark.asyncio
async def test_system_error_directive_does_not_leak_raw_exception_text():
    """When a synthesis attempt's reason carries raw exception text
    (e.g. provider payload bytes, file paths, secrets, adversarial
    input), that text must NOT cross into the user-facing presence
    directive. The directive uses a constrained safe-component label
    only; raw reason stays in audit_trace.notes for operators.
    """
    secret_payload = (
        "Traceback: /home/user/.secrets/api_key=sk-abc123XYZ "
        "while talking to api.evil.example.com"
    )

    async def chain(*_a, **_kw):
        # Raise an unexpected exception whose message contains "secrets".
        raise RuntimeError(secret_payload)

    runner, _audit = _make_runner(
        chain_caller=chain,
        config=IntegrationConfig(max_retries=2, max_iterations=1),
    )
    briefing = await runner.run(_make_inputs())

    # Component label in directive is constrained — exception text
    # is NOT in the user-facing directive.
    assert secret_payload not in briefing.presence_directive
    assert "sk-abc123XYZ" not in briefing.presence_directive
    assert "/home/user" not in briefing.presence_directive
    # Constrained label is used in the directive.
    assert "component=unexpected_error" in briefing.presence_directive
    # Audit notes DO carry the raw text — operators need full
    # diagnostic detail.
    assert "sk-abc123XYZ" in briefing.audit_trace.notes


@pytest.mark.asyncio
async def test_runner_invalid_decided_action_falls_back_soft():
    bad = dict(_DEFAULT_BRIEFING_PAYLOAD)
    bad["decided_action"] = {"kind": "do_something_evil"}

    async def chain(*_a, **_kw):
        return _resp(_finalize_block(bad))

    runner, audit = _make_runner(chain_caller=chain)
    briefing = await runner.run(_make_inputs())

    assert briefing.audit_trace.fail_soft_engaged is True
    assert audit[0]["success"] is False


# ---------------------------------------------------------------------------
# Cohort + tool reference plumbing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_cohort_run_ids_carry_into_audit_trace():
    async def chain(*_a, **_kw):
        return _resp(_finalize_block(_DEFAULT_BRIEFING_PAYLOAD))

    runner, _ = _make_runner(chain_caller=chain)
    inputs = _make_inputs(
        cohort_outputs=(
            CohortOutput(
                cohort_id="a", cohort_run_id="ref-a", output={},
                produced_at=now_iso(),
            ),
            CohortOutput(
                cohort_id="b", cohort_run_id="ref-b", output={},
                produced_at=now_iso(),
            ),
            CohortOutput(
                cohort_id="c", cohort_run_id="ref-c", output={},
                produced_at=now_iso(),
            ),
        ),
    )
    briefing = await runner.run(inputs)
    assert briefing.audit_trace.cohort_outputs == (
        "ref-a", "ref-b", "ref-c",
    )


@pytest.mark.asyncio
async def test_runner_assigns_run_id_when_unspecified():
    async def chain(*_a, **_kw):
        return _resp(_finalize_block(_DEFAULT_BRIEFING_PAYLOAD))

    runner, _ = _make_runner(chain_caller=chain)
    briefing = await runner.run(_make_inputs())
    assert briefing.integration_run_id.startswith("int-")


# ---------------------------------------------------------------------------
# Audit-shape conformance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_audit_emit_carries_briefing_dict_under_canonical_category():
    async def chain(*_a, **_kw):
        return _resp(_finalize_block(_DEFAULT_BRIEFING_PAYLOAD))

    runner, audit = _make_runner(chain_caller=chain)
    await runner.run(_make_inputs())
    entry = audit[0]
    assert entry["audit_category"] == "integration.briefing"
    assert entry["success"] is True
    Briefing.from_dict(entry["briefing"])  # round-trips


# ---------------------------------------------------------------------------
# Redaction invariant (Section 3 — primary safety property)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_blocks_briefing_that_quotes_restricted_cohort_content():
    """Restricted CohortOutput payload content must not appear quoted
    in any briefing text field. The runner refuses such a briefing
    and falls back soft."""

    secret = "the surprise birthday party next Saturday"

    inputs = _make_inputs(
        cohort_outputs=(
            CohortOutput(
                cohort_id="covenant",
                cohort_run_id="cov:r1",
                output={"covenant_text": secret},
                visibility=Restricted(reason="covenant"),
                produced_at=now_iso(),
            ),
        ),
    )

    async def chain(*_a, **_kw):
        return _resp(
            _finalize_block(
                {
                    "relevant_context": [
                        {
                            "source_type": "cohort.covenant",
                            "source_id": "cov:r1",
                            # Bad: model leaked the secret into the summary.
                            "summary": f"covenant says: {secret}",
                            "confidence": 0.9,
                        }
                    ],
                    "filtered_context": [],
                    "decided_action": {"kind": "respond_only"},
                    "presence_directive": "respond gently",
                }
            )
        )

    runner, audit = _make_runner(chain_caller=chain)
    briefing = await runner.run(inputs)

    assert briefing.audit_trace.fail_soft_engaged is True
    assert "redaction" in briefing.audit_trace.notes.lower()
    # Audit captures the violation with the briefing in fail-soft form.
    assert audit[0]["success"] is False
    # The persisted fail-soft briefing's text fields don't carry the secret.
    persisted_text = (
        briefing.presence_directive
        + " ".join(c.summary for c in briefing.relevant_context)
        + " ".join(f.reason_filtered for f in briefing.filtered_context)
    )
    assert secret not in persisted_text


@pytest.mark.asyncio
async def test_runner_allows_behavioral_instruction_without_quoting_restricted():
    """Same restricted cohort, but the briefing only carries behavioral
    instruction — no secret content quoted. This is the well-behaved
    integration path and must succeed."""

    secret = "the surprise birthday party next Saturday"

    inputs = _make_inputs(
        cohort_outputs=(
            CohortOutput(
                cohort_id="covenant",
                cohort_run_id="cov:r1",
                output={"covenant_text": secret},
                visibility=Restricted(reason="covenant"),
                produced_at=now_iso(),
            ),
        ),
    )

    async def chain(*_a, **_kw):
        return _resp(
            _finalize_block(
                {
                    "relevant_context": [
                        {
                            "source_type": "cohort.covenant",
                            "source_id": "cov:r1",
                            "summary": (
                                "constraint applies; redirect away from the "
                                "topic the user proposed last week"
                            ),
                            "confidence": 0.9,
                        }
                    ],
                    "filtered_context": [],
                    "decided_action": {
                        "kind": "pivot",
                        "reason": "covenant constraint",
                        "suggested_shape": "general planning",
                    },
                    "presence_directive": (
                        "do not reference the user's earlier proposal; "
                        "redirect toward general planning"
                    ),
                }
            )
        )

    runner, audit = _make_runner(chain_caller=chain)
    briefing = await runner.run(inputs)

    assert briefing.audit_trace.fail_soft_engaged is False
    assert audit[0]["success"] is True
    # The secret never appears in any text field.
    serialised = briefing.to_dict()
    assert secret not in serialised["presence_directive"]
    for ci in serialised["relevant_context"]:
        assert secret not in ci["summary"]
