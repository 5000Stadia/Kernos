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

    runner_kwargs = dict(
        chain_caller=chain_caller or _default_chain,
        read_only_dispatcher=dispatcher or _default_dispatcher,
        audit_emitter=_emit,
        config=config or IntegrationConfig(),
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
async def test_runner_max_iterations_triggers_fail_soft():
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
        config=IntegrationConfig(max_iterations=3),
    )
    briefing = await runner.run(_make_inputs())

    assert briefing.audit_trace.fail_soft_engaged is True
    assert "max_iterations" in briefing.audit_trace.notes
    assert briefing.audit_trace.iterations_used == 3
    assert briefing.audit_trace.budget_state.iterations_hit_limit is True
    assert audit[0]["success"] is False


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
            max_iterations=10, integration_timeout_seconds=1.0
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
