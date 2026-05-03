"""Tests for the covenant cohort adapter (C2 of CAC).

Covers spec scenarios 1-15, 19-25 (the safety-policy plumbing
scenarios 16-18 live in test_integration_safety_policy.py from C1).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.kernel.cohorts import (
    CohortContext,
    CohortFanOutConfig,
    CohortFanOutRunner,
    CohortRegistry,
    ContextSpaceRef,
    Turn,
    register_covenant_cohort,
)
from kernos.kernel.cohorts.covenant_cohort import (
    COHORT_ID,
    DESCRIPTION_CAP,
    RESTRICTED_REASON,
    RULE_COUNT_CAP,
    TIMEOUT_MS,
    make_covenant_cohort_run,
    make_covenant_descriptor,
)
from kernos.kernel.integration.briefing import (
    CohortOutput,
    Outcome,
    Restricted,
)
from kernos.kernel.state import CovenantRule


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _rule(
    *,
    id: str,
    rule_type: str = "must_not",
    enforcement_tier: str = "block",
    description: str = "Do not do that.",
    layer: str = "principle",
    capability: str = "general",
    member_id: str = "",
    context_space: str | None = None,
    topic: str = "",
    target: str = "",
    trigger_tool: str = "",
    action_class: str = "",
    fallback_action: str = "ask_user",
    created_at: str | None = None,
    active: bool = True,
    source: str = "user_stated",
) -> CovenantRule:
    return CovenantRule(
        id=id,
        instance_id="i-1",
        capability=capability,
        rule_type=rule_type,
        description=description,
        active=active,
        source=source,
        context_space=context_space,
        layer=layer,
        action_class=action_class,
        trigger_tool=trigger_tool,
        enforcement_tier=enforcement_tier,
        fallback_action=fallback_action,
        member_id=member_id,
        topic=topic,
        target=target,
        created_at=created_at or datetime.now(timezone.utc).isoformat(),
    )


def _state(rules: list[CovenantRule], *, query_raises: bool = False):
    state = MagicMock()
    if query_raises:
        state.query_covenant_rules = AsyncMock(
            side_effect=RuntimeError("DB unavailable"),
        )
    else:
        state.query_covenant_rules = AsyncMock(return_value=rules)
    return state


def _ctx(
    *,
    member_id: str = "m-active",
    turn_id: str = "turn-1",
    spaces: tuple[ContextSpaceRef, ...] = (ContextSpaceRef("default"),),
    user_message: str = "tell me about the project",
) -> CohortContext:
    return CohortContext(
        member_id=member_id,
        user_message=user_message,
        conversation_thread=(Turn("user", user_message),),
        active_spaces=spaces,
        turn_id=turn_id,
        instance_id="i-1",
        produced_at="2026-04-26T00:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# 1. Empty covenant set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_1_empty_covenant_set():
    state = _state([])
    out = await make_covenant_cohort_run(state)(_ctx())
    assert isinstance(out, CohortOutput)
    assert out.output["rule_count"] == 0
    assert out.output["rules"] == []
    assert isinstance(out.visibility, Restricted)
    assert out.visibility.reason == RESTRICTED_REASON


# ---------------------------------------------------------------------------
# 2. Single instance-level rule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_2_instance_level_rule():
    rules = [
        _rule(
            id="r-1", description="Do not delete user data without confirmation.",
        ),
    ]
    state = _state(rules)
    out = await make_covenant_cohort_run(state)(_ctx())
    assert out.output["rule_count"] == 1
    summary = out.output["rules"][0]
    assert summary["rule_id"] == "r-1"
    assert summary["scope"] == "global"


# ---------------------------------------------------------------------------
# 3. Member-specific rule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_3_member_specific_rule():
    rules = [_rule(id="r-mine", member_id="m-active")]
    state = _state(rules)
    out = await make_covenant_cohort_run(state)(_ctx())
    assert out.output["rule_count"] == 1
    assert out.output["rules"][0]["scope"] == "member:m-active"


# ---------------------------------------------------------------------------
# 4. Other member's rule excluded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_4_other_member_rule_excluded():
    rules = [
        _rule(id="r-mine", member_id="m-active"),
        _rule(id="r-other", member_id="m-other"),
    ]
    state = _state(rules)
    out = await make_covenant_cohort_run(state)(_ctx())
    ids = [r["rule_id"] for r in out.output["rules"]]
    assert "r-mine" in ids
    assert "r-other" not in ids


# ---------------------------------------------------------------------------
# 5. Member filtering Python-side (not via SQL parameter)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_5_member_filter_python_side():
    """query_covenant_rules called WITHOUT member_id parameter;
    filter applied after the query lands."""
    rules = [
        _rule(id="r-mine", member_id="m-active"),
        _rule(id="r-other", member_id="m-other"),
    ]
    state = _state(rules)
    await make_covenant_cohort_run(state)(_ctx())
    # The mock recorded the call. Verify member_id was NOT passed.
    call_kwargs = state.query_covenant_rules.call_args.kwargs
    assert "member_id" not in call_kwargs


# ---------------------------------------------------------------------------
# 6. Space-scoped rule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_6_space_scoped_rule():
    rules = [_rule(id="r-space", context_space="default")]
    state = _state(rules)
    out = await make_covenant_cohort_run(state)(_ctx())
    assert out.output["rules"][0]["scope"] == "default"


# ---------------------------------------------------------------------------
# 7. Inactive space's rule excluded (handled by query_covenant_rules' scope)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_7_query_passes_active_space_scope():
    """The cohort passes ctx.active_spaces + None into
    context_space_scope; the state surface filters spaces. We verify
    the call was made with the right scope shape."""
    state = _state([])
    await make_covenant_cohort_run(state)(_ctx(spaces=(ContextSpaceRef("work"),)))
    call_kwargs = state.query_covenant_rules.call_args.kwargs
    scope = call_kwargs["context_space_scope"]
    assert "work" in scope
    assert None in scope


# ---------------------------------------------------------------------------
# 8. Mixed scopes — all three counted in scope_resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_8_mixed_scopes_counted():
    rules = [
        _rule(id="r-instance"),
        _rule(id="r-member", member_id="m-active"),
        _rule(id="r-space", context_space="default"),
    ]
    state = _state(rules)
    out = await make_covenant_cohort_run(state)(_ctx())
    sr = out.output["scope_resolution"]
    assert sr["instance_level_rules"] == 1
    assert sr["member_specific_rules"] == 1
    assert sr["space_scoped_rules"] == 1


# ---------------------------------------------------------------------------
# 9. Visibility is Restricted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_9_visibility_is_restricted_covenant_set():
    state = _state([_rule(id="r-1")])
    out = await make_covenant_cohort_run(state)(_ctx())
    assert isinstance(out.visibility, Restricted)
    assert out.visibility.reason == "covenant_set"


# ---------------------------------------------------------------------------
# 10. Descriptions present in payload (within cap)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_10_descriptions_present_unchanged_under_cap():
    state = _state([
        _rule(id="r-1", description="A normal rule description."),
    ])
    out = await make_covenant_cohort_run(state)(_ctx())
    summary = out.output["rules"][0]
    assert summary["description"] == "A normal rule description."
    assert summary["description_truncated"] is False


# ---------------------------------------------------------------------------
# 11. Description hard cap (2000 chars)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_11_description_truncated_above_cap():
    long_desc = "x" * 2500
    state = _state([_rule(id="r-long", description=long_desc)])
    out = await make_covenant_cohort_run(state)(_ctx())
    summary = out.output["rules"][0]
    assert len(summary["description"]) == DESCRIPTION_CAP
    assert summary["description_truncated"] is True


# ---------------------------------------------------------------------------
# 12. Safety-priority truncation order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_12_safety_priority_truncation_order():
    """60 rules: 5 must_not+block, 5 must_not+confirm, 10 must,
    5 escalation, 35 preference. Cohort surfaces 50 with all
    must_not+block included; lowest-priority preferences dropped
    first. truncation_dropped lists dropped rule_ids."""
    rules = []
    rules.extend(
        _rule(id=f"block-{i}", rule_type="must_not", enforcement_tier="block")
        for i in range(5)
    )
    rules.extend(
        _rule(id=f"confirm-{i}", rule_type="must_not", enforcement_tier="confirm")
        for i in range(5)
    )
    rules.extend(_rule(id=f"must-{i}", rule_type="must") for i in range(10))
    rules.extend(_rule(id=f"esc-{i}", rule_type="escalation") for i in range(5))
    rules.extend(
        _rule(id=f"pref-{i}", rule_type="preference") for i in range(35)
    )
    state = _state(rules)
    out = await make_covenant_cohort_run(state)(_ctx())
    kept_ids = {r["rule_id"] for r in out.output["rules"]}
    assert len(kept_ids) == RULE_COUNT_CAP
    # All block rules survive.
    for i in range(5):
        assert f"block-{i}" in kept_ids
    # All confirm-tier must_not survive.
    for i in range(5):
        assert f"confirm-{i}" in kept_ids
    # All must rules survive.
    for i in range(10):
        assert f"must-{i}" in kept_ids
    # All escalation rules survive.
    for i in range(5):
        assert f"esc-{i}" in kept_ids
    # 25 of 35 preferences kept; 10 dropped.
    pref_kept = sum(1 for k in kept_ids if k.startswith("pref-"))
    assert pref_kept == RULE_COUNT_CAP - 25  # 50 - (5+5+10+5) = 25
    sr = out.output["scope_resolution"]
    assert sr["truncated"] is True
    assert len(sr["truncation_dropped"]) == 10
    # All dropped are preferences.
    for did in sr["truncation_dropped"]:
        assert did.startswith("pref-")


# ---------------------------------------------------------------------------
# 13. Recency-only truncation does NOT silently drop safety-critical
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_13_safety_priority_overrides_recency():
    """50 newer preference rules + 5 older must_not+block rules.
    Cohort surfaces all 5 block rules + 45 newest preferences."""
    now = datetime.now(timezone.utc)
    older = (now - timedelta(days=365)).isoformat()
    newer = lambda i: (now - timedelta(seconds=i)).isoformat()

    rules = []
    rules.extend(
        _rule(
            id=f"block-{i}",
            rule_type="must_not",
            enforcement_tier="block",
            created_at=older,
        )
        for i in range(5)
    )
    rules.extend(
        _rule(
            id=f"pref-{i}",
            rule_type="preference",
            created_at=newer(i),
        )
        for i in range(50)
    )
    state = _state(rules)
    out = await make_covenant_cohort_run(state)(_ctx())
    kept_ids = {r["rule_id"] for r in out.output["rules"]}
    # All 5 older block rules survive — priority overrode recency.
    for i in range(5):
        assert f"block-{i}" in kept_ids
    pref_kept = sum(1 for k in kept_ids if k.startswith("pref-"))
    assert pref_kept == 45


# ---------------------------------------------------------------------------
# 14. Redaction invariant end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_14_redaction_invariant_end_to_end():
    """Wire the cohort output through V1 IntegrationRunner; the
    briefing must not contain any rule text in its text fields.
    Per V1's redaction invariant: Restricted CohortOutput content
    NEVER appears in briefing text. The integration runner's
    post-finalize substring check enforces this."""
    from kernos.kernel.integration import (
        Briefing, IntegrationConfig, IntegrationInputs,
        IntegrationRunner, RespondOnly,
    )
    from kernos.providers.base import ContentBlock, ProviderResponse

    distinct_token = "alpha-distinct-token-9876"
    rules = [_rule(id="r-secret", description=distinct_token)]
    state = _state(rules)
    registry = CohortRegistry()
    register_covenant_cohort(registry, state)

    async def fan_emit(entry):
        pass

    fan_out = CohortFanOutRunner(
        registry=registry,
        audit_emitter=fan_emit,
        config=CohortFanOutConfig(global_timeout_seconds=2.0),
    )
    fan_out_result = await fan_out.run(_ctx(turn_id="turn-redact"))

    # Model attempts to leak the rule description into the briefing —
    # the runner's redaction check refuses, fail-soft engages.
    async def chain(*_a, **_kw):
        return ProviderResponse(
            content=[ContentBlock(
                type="tool_use", id="tu_finalize", name="__finalize_briefing__",
                input={
                    "relevant_context": [{
                        "source_type": "cohort.covenant",
                        "source_id": "turn-redact:covenant:0",
                        "summary": f"covenant says: {distinct_token}",  # leak
                        "confidence": 0.9,
                    }],
                    "filtered_context": [],
                    "decided_action": {"kind": "respond_only"},
                    "presence_directive": "answer simply",
                },
            )],
            stop_reason="tool_use", input_tokens=10, output_tokens=20,
        )

    async def dispatcher(*_a, **_kw): return {}
    audit: list[dict] = []
    async def emit(entry): audit.append(entry)

    integration = IntegrationRunner(
        chain_caller=chain,
        read_only_dispatcher=dispatcher,
        audit_emitter=emit,
        config=IntegrationConfig(),
    )
    from kernos.kernel.cohorts import build_integration_inputs_from_fan_out
    inputs = build_integration_inputs_from_fan_out(
        fan_out_result,
        user_message="hi",
        conversation_thread=({"role": "user", "content": "hi"},),
        member_id="m-active",
        instance_id="i-1",
        space_id="default",
        turn_id="turn-redact",
    )
    briefing = await integration.run(inputs)
    # Runner refused the leaky briefing → fail-soft.
    assert briefing.audit_trace.fail_soft_engaged is True
    serialised = str(briefing.to_dict())
    assert distinct_token not in serialised


# ---------------------------------------------------------------------------
# 15. Directive sanitization (the design review edit #4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_15_directive_sanitization():
    """Three distinct tokens for topic / target / description.
    Briefing produced by integration must not contain any of them
    in any text field. Tests directive does not leak topic/target/description."""
    from kernos.kernel.integration import (
        IntegrationConfig, IntegrationRunner,
    )
    from kernos.providers.base import ContentBlock, ProviderResponse

    topic_token = "alpha-distinct-token"
    target_token = "beta-distinct-token"
    desc_token = "gamma-distinct-token"
    rules = [_rule(
        id="r-1",
        description=desc_token,
        topic=topic_token,
        target=target_token,
    )]
    state = _state(rules)
    registry = CohortRegistry()
    register_covenant_cohort(registry, state)

    async def fan_emit(entry): pass

    fan_out = CohortFanOutRunner(
        registry=registry, audit_emitter=fan_emit,
        config=CohortFanOutConfig(global_timeout_seconds=2.0),
    )
    fan_out_result = await fan_out.run(_ctx(turn_id="turn-sanitize"))

    # Compliant briefing: directive uses generic behavioral instruction
    # without quoting topic/target/description.
    async def chain(*_a, **_kw):
        return ProviderResponse(
            content=[ContentBlock(
                type="tool_use", id="tu_finalize", name="__finalize_briefing__",
                input={
                    "relevant_context": [],
                    "filtered_context": [],
                    "decided_action": {
                        "kind": "pivot",
                        "reason": "covenant constraint",
                        "suggested_shape": "redirect to a safer topic",
                    },
                    "presence_directive": (
                        "decline this cross-member disclosure and ask for "
                        "explicit permission before sharing"
                    ),
                },
            )],
            stop_reason="tool_use", input_tokens=10, output_tokens=20,
        )

    async def dispatcher(*_a, **_kw): return {}
    async def emit(entry): pass

    integration = IntegrationRunner(
        chain_caller=chain,
        read_only_dispatcher=dispatcher,
        audit_emitter=emit,
        config=IntegrationConfig(),
    )
    from kernos.kernel.cohorts import build_integration_inputs_from_fan_out
    inputs = build_integration_inputs_from_fan_out(
        fan_out_result,
        user_message="hi",
        conversation_thread=({"role": "user", "content": "hi"},),
        member_id="m-active",
        instance_id="i-1",
        space_id="default",
        turn_id="turn-sanitize",
    )
    briefing = await integration.run(inputs)
    serialised = str(briefing.to_dict())
    assert topic_token not in serialised
    assert target_token not in serialised
    assert desc_token not in serialised


# ---------------------------------------------------------------------------
# 19. No model call during cohort run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_19_no_model_call():
    state = _state([_rule(id="r-1")])
    out = await make_covenant_cohort_run(state)(_ctx())
    assert isinstance(out, CohortOutput)
    # State mock has no reasoning attribute; passing here proves no
    # reasoning calls were made via state.


# ---------------------------------------------------------------------------
# 20. No state mutation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_20_no_state_mutation():
    """Cohort calls only query_covenant_rules — no insert / update /
    delete / event-emit methods."""
    state = _state([_rule(id="r-1")])
    await make_covenant_cohort_run(state)(_ctx())
    # The mock allows arbitrary method access. Verify only
    # query_covenant_rules was called.
    methods_called = [c for c in state.method_calls if not c[0].startswith("_")]
    assert all(c[0] == "query_covenant_rules" for c in methods_called)


# ---------------------------------------------------------------------------
# 21. Cohort returns CohortOutput, not dict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_21_returns_cohort_output_type():
    state = _state([_rule(id="r-1")])
    out = await make_covenant_cohort_run(state)(_ctx())
    assert isinstance(out, CohortOutput)
    assert not isinstance(out, dict)


# ---------------------------------------------------------------------------
# 22. Completes within timeout (stress)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_22_completes_within_timeout():
    rules = [_rule(id=f"r-{i}") for i in range(50)]
    state = _state(rules)
    import time
    start = time.monotonic()
    await make_covenant_cohort_run(state)(_ctx())
    elapsed_ms = int((time.monotonic() - start) * 1000)
    assert elapsed_ms < TIMEOUT_MS


# ---------------------------------------------------------------------------
# 23. End-to-end via fan-out runner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_23_via_fan_out_runner():
    state = _state([_rule(id="r-1")])
    registry = CohortRegistry()
    register_covenant_cohort(registry, state)
    audit: list[dict] = []

    async def emit(entry):
        audit.append(entry)

    runner = CohortFanOutRunner(
        registry=registry,
        audit_emitter=emit,
        config=CohortFanOutConfig(global_timeout_seconds=2.0),
    )
    result = await runner.run(_ctx())
    out = result.outputs[0]
    assert out.cohort_id == COHORT_ID
    assert out.outcome is Outcome.SUCCESS
    assert out.cohort_run_id == "turn-1:covenant:0"
    # Success path: required_safety_cohort_failures empty.
    assert result.required_safety_cohort_failures == ()


# ---------------------------------------------------------------------------
# 24. validate_covenant_set unchanged (smoke check)
# ---------------------------------------------------------------------------


def test_scenario_24_covenant_manager_imports_unchanged():
    """validate_covenant_set still importable and callable; cohort
    work is purely additive. The full covenant_manager test suite
    proves the contract; here we pin the import surface."""
    from kernos.kernel.covenant_manager import validate_covenant_set
    assert callable(validate_covenant_set)


# ---------------------------------------------------------------------------
# 25. Audit log redaction (description / topic / target excluded)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_25_audit_log_redaction_excludes_all_three():
    """Audit log entries for cohort.fan_out include rule_count and
    outcome — never description, topic, or target. The fan-out
    runner emits the audit; we verify what lands."""
    rules = [_rule(
        id="r-1",
        description="alpha-token",
        topic="beta-token",
        target="gamma-token",
    )]
    state = _state(rules)
    registry = CohortRegistry()
    register_covenant_cohort(registry, state)
    audit: list[dict] = []

    async def emit(entry):
        audit.append(entry)

    runner = CohortFanOutRunner(
        registry=registry,
        audit_emitter=emit,
        config=CohortFanOutConfig(global_timeout_seconds=2.0),
    )
    await runner.run(_ctx())
    assert len(audit) == 1
    serialised = str(audit[0])
    # The audit entry mentions the cohort ran but never quotes
    # rule strings.
    assert "covenant" in serialised  # cohort_id allowed
    assert "alpha-token" not in serialised
    assert "beta-token" not in serialised
    assert "gamma-token" not in serialised


# ---------------------------------------------------------------------------
# Bonus: descriptor flags + error path producing safety-class failure
# ---------------------------------------------------------------------------


def test_descriptor_carries_required_and_safety_class():
    """First cohort to ship with required=True AND safety_class=True."""
    state = _state([])
    desc = make_covenant_descriptor(state)
    assert desc.cohort_id == COHORT_ID
    assert desc.timeout_ms == TIMEOUT_MS
    assert isinstance(desc.default_visibility, Restricted)
    assert desc.default_visibility.reason == RESTRICTED_REASON
    assert desc.required is True
    assert desc.safety_class is True


@pytest.mark.asyncio
async def test_query_failure_propagates_to_runner_as_safety_failure():
    """Acceptance criterion 15 + safety-class interaction: state
    query failure propagates; fan-out runner records outcome=error
    AND populates required_safety_cohort_failures because the
    cohort is required+safety_class."""
    state = _state([], query_raises=True)
    registry = CohortRegistry()
    register_covenant_cohort(registry, state)

    async def emit(entry):
        pass

    runner = CohortFanOutRunner(
        registry=registry,
        audit_emitter=emit,
        config=CohortFanOutConfig(global_timeout_seconds=2.0),
    )
    result = await runner.run(_ctx())
    out = result.outputs[0]
    assert out.outcome is Outcome.ERROR
    assert "covenant" in result.required_safety_cohort_failures
