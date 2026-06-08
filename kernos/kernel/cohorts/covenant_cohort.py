"""Covenant cohort adapter — fourth cohort targeting the fan-out runner.

First cohort to ship with `required: True, safety_class: True`.
Surfaces the active covenant set as a structured CohortOutput per
turn so integration can pivot response shape *before* presence
generates.

Visibility model: whole-output Restricted{reason: "covenant_set"}.
V1's redaction invariant applies automatically — descriptions sit
inside the redaction boundary; integration encodes their effect
into presence_directive without quoting; presence never sees them.

Per the design review edits #4 / #6 / #7 / #8:
- Description hard-capped at 2000 chars (pathological-rule defense).
- Rule count capped at 50 with safety-priority order
  (must_not+block > must_not+confirm > must_not+notify|silent >
  must > escalation > preference; recency tiebreaker).
- Member filtering applied Python-side after query
  (query_covenant_rules abstract signature does not take
  member_id; sqlite-only extra parameter would couple to a
  specific store implementation).
- Audit log redaction excludes description, topic, AND target.

The cohort does NOT replace the existing
`validate_covenant_set` post-write hook. Validation (LLM,
post-write, mutating) and surfacing (read-only, per-turn,
observation) are different jobs; the cohort separates them.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from kernos.kernel.cohorts.descriptor import (
    CohortContext,
    CohortDescriptor,
    ExecutionMode,
)
from kernos.kernel.cohorts.registry import CohortRegistry
from kernos.kernel.integration.briefing import (
    CohortOutput,
    Restricted,
    now_iso,
)
from kernos.kernel.state import CovenantRule, StateStore


logger = logging.getLogger(__name__)


COHORT_ID = "covenant"
TIMEOUT_MS = 300
DESCRIPTION_CAP = 2000  # chars (the design review edit #5)
RULE_COUNT_CAP = 50
RESTRICTED_REASON = "covenant_set"


# Safety-priority truncation order (the design review edit #6). Within each tier,
# recency (newest first). Safety-critical rules are never silently
# dropped under the rule-count cap.
def _safety_priority(rule: CovenantRule) -> int:
    """Lower number = higher priority. Within same tier, recency
    decides via secondary sort on created_at."""
    if rule.rule_type == "must_not" and rule.enforcement_tier == "block":
        return 0
    if rule.rule_type == "must_not" and rule.enforcement_tier == "confirm":
        return 1
    if rule.rule_type == "must_not" and rule.enforcement_tier in (
        "notify", "silent",
    ):
        return 2
    if rule.rule_type == "must":
        return 3
    if rule.rule_type == "escalation":
        return 4
    # Preferences and anything unknown go to the bottom.
    return 5


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


def _truncate_description(text: str) -> tuple[str, bool]:
    """Cap description at DESCRIPTION_CAP chars. Returns (text, truncated)."""
    if not text:
        return "", False
    if len(text) <= DESCRIPTION_CAP:
        return text, False
    return text[: DESCRIPTION_CAP - 1] + "…", True


def _scope_label(rule: CovenantRule) -> str:
    """Render a CovenantRule's scope as a single string for the
    cohort payload. global / context_space_id / member:<member_id>."""
    if rule.member_id:
        return f"member:{rule.member_id}"
    if rule.context_space:
        return rule.context_space
    return "global"


def _rule_summary(rule: CovenantRule) -> dict[str, Any]:
    description, truncated = _truncate_description(rule.description)
    return {
        "rule_id": rule.id,
        "capability": rule.capability,
        "rule_type": rule.rule_type,
        "layer": rule.layer,
        "description": description,
        "description_truncated": truncated,
        "enforcement_tier": rule.enforcement_tier,
        "fallback_action": rule.fallback_action,
        "scope": _scope_label(rule),
        "topic": rule.topic,
        "target": rule.target,
        "trigger_tool": rule.trigger_tool,
        "action_class": rule.action_class,
    }


def _rank_and_truncate(
    rules: list[CovenantRule],
) -> tuple[list[CovenantRule], list[str]]:
    """Apply safety-priority order + recency tiebreaker; cap at
    RULE_COUNT_CAP. Returns (kept, dropped_rule_ids).

    Within each safety tier, newer rules surface first (recency
    tiebreaker). Across tiers, safety-critical rules ALWAYS
    survive the cap before lower-priority rules — never silently
    drop a must_not+block rule for a recency-newer preference
    (the design review edit #6).
    """
    # Sort by (priority_tier, -created_at). created_at is ISO 8601 so
    # lexicographic descending = newer-first within tier.
    ordered = sorted(
        rules,
        key=lambda r: (_safety_priority(r), _negative_iso(r.created_at)),
    )
    if len(ordered) <= RULE_COUNT_CAP:
        return ordered, []
    kept = ordered[:RULE_COUNT_CAP]
    dropped = [r.id for r in ordered[RULE_COUNT_CAP:]]
    return kept, dropped


def _negative_iso(iso: str) -> str:
    """Sort-key helper: returns a string that sorts in reverse ISO
    order so newer timestamps come first within the same priority
    tier. Empty timestamps sort last within their tier."""
    if not iso:
        return "0"
    # Python sorts strings ascending; we want newer-first → invert
    # by character mapping. Cheapest correct approach: prefix with
    # the negation of the timestamp's ordinal interpretation.
    # Simpler: use a tuple-key via a sentinel that puts later dates
    # first by inverting char codes.
    return "".join(chr(0x10FFFF - ord(c)) for c in iso)


# ---------------------------------------------------------------------------
# Member + scope filtering
# ---------------------------------------------------------------------------


def _filter_member_scoped(
    rules: list[CovenantRule], member_id: str,
) -> list[CovenantRule]:
    """Apply member filter Python-side (the design review edit #7).

    Keeps:
      - instance-level rules (member_id == "")
      - member-specific rules whose member_id == ctx.member_id
    Drops:
      - other members' member-specific rules
    """
    out: list[CovenantRule] = []
    for r in rules:
        if not r.member_id or r.member_id == member_id:
            out.append(r)
    return out


# ---------------------------------------------------------------------------
# Run callable factory
# ---------------------------------------------------------------------------


def _empty_payload(member_id: str, active_space_ids: tuple[str, ...]) -> dict[str, Any]:
    return {
        "rule_count": 0,
        "has_principle_layer": False,
        "has_practice_layer": False,
        "rules": [],
        "scope_resolution": {
            "active_spaces": list(active_space_ids),
            "member_id": member_id,
            "instance_level_rules": 0,
            "member_specific_rules": 0,
            "space_scoped_rules": 0,
            "truncated": False,
            "truncation_dropped": [],
        },
    }


def make_covenant_cohort_run(
    state: StateStore,
) -> Callable[[CohortContext], Awaitable[CohortOutput]]:
    """Build the async run callable bound to a StateStore.

    Reads active covenants for ctx.member_id + ctx.active_spaces
    via query_covenant_rules; applies member filter Python-side;
    ranks via safety-priority + recency; caps at 50 rules; wraps
    into a CohortOutput with whole-output Restricted visibility.
    """

    async def covenant_cohort_run(ctx: CohortContext) -> CohortOutput:
        active_space_ids = tuple(s.space_id for s in ctx.active_spaces)
        # Build context_space_scope: all active spaces + None (global).
        scope: list[str | None] = list(active_space_ids)
        scope.append(None)

        # Read all matching rules. capability=None pulls every
        # capability; the cohort surfaces the entire active set
        # rather than pre-filtering by relevance (integration's job).
        try:
            raw_rules = await state.query_covenant_rules(
                ctx.instance_id,
                capability=None,
                context_space_scope=scope,
                active_only=True,
            )
        except Exception:
            # Per spec Section 6: state query failure propagates so
            # the runner registers outcome=error. The fan-out runner
            # then sets required_safety_cohort_failed and the
            # integration runner forces defer/constrained_response.
            logger.exception("COVENANT_COHORT_QUERY_FAILED")
            raise

        # Member filtering Python-side (the design review edit #7).
        filtered = _filter_member_scoped(raw_rules, ctx.member_id)

        if not filtered:
            payload = _empty_payload(ctx.member_id, active_space_ids)
            return CohortOutput(
                cohort_id=COHORT_ID,
                cohort_run_id=f"{ctx.turn_id}:{COHORT_ID}:provisional",
                output=payload,
                visibility=Restricted(reason=RESTRICTED_REASON),
                produced_at=now_iso(),
                # Self-scoped: _filter_member_scoped keeps only this member's
                # own + instance-level rules, never another member's — so the
                # recipient owns this content and redaction exempts it for them.
                owner_member_id=ctx.member_id,
            )

        # Safety-priority ranking + count cap.
        kept, dropped_ids = _rank_and_truncate(filtered)

        # Build the payload.
        rules_summary = [_rule_summary(r) for r in kept]
        instance_level = sum(1 for r in kept if not r.member_id and not r.context_space)
        member_specific = sum(1 for r in kept if r.member_id == ctx.member_id)
        space_scoped = sum(
            1 for r in kept
            if r.context_space and r.context_space in active_space_ids
        )
        has_principle = any(r.layer == "principle" for r in kept)
        has_practice = any(r.layer == "practice" for r in kept)

        payload = {
            "rule_count": len(kept),
            "has_principle_layer": has_principle,
            "has_practice_layer": has_practice,
            "rules": rules_summary,
            "scope_resolution": {
                "active_spaces": list(active_space_ids),
                "member_id": ctx.member_id,
                "instance_level_rules": instance_level,
                "member_specific_rules": member_specific,
                "space_scoped_rules": space_scoped,
                "truncated": bool(dropped_ids),
                "truncation_dropped": dropped_ids,
            },
        }

        return CohortOutput(
            cohort_id=COHORT_ID,
            cohort_run_id=f"{ctx.turn_id}:{COHORT_ID}:provisional",
            output=payload,
            visibility=Restricted(reason=RESTRICTED_REASON),
            produced_at=now_iso(),
            # Owner = recipient, so the redaction invariant exempts this output
            # for them (③). Why this is sound — and NOT a cross-member leak
            # (Codex review): the kept set is exclusively content the recipient
            # is authorized to see. Two upstream guarantees enforce that:
            #   1. query_covenant_rules is called with context_space_scope =
            #      active_spaces + [None], so raw_rules is limited to the
            #      member's ACTIVE spaces + instance-global — never a space the
            #      member isn't in.
            #   2. _filter_member_scoped drops every OTHER member's
            #      member-specific rules.
            # → kept = {own rules, instance-global, their active-space rules},
            # all of which the recipient already lives under. If either
            # guarantee is ever loosened, this ownership assertion must be
            # revisited (see test_covenant_cohort scope invariants).
            owner_member_id=ctx.member_id,
        )

    return covenant_cohort_run


# ---------------------------------------------------------------------------
# Descriptor + registration
# ---------------------------------------------------------------------------


def make_covenant_descriptor(state: StateStore) -> CohortDescriptor:
    """Construct the cohort descriptor for the covenant cohort.

    Spec acceptance criterion 2: cohort_id="covenant",
    execution_mode=ASYNC, timeout_ms=300,
    default_visibility=Restricted{reason: "covenant_set"},
    required=True, safety_class=True.
    """
    return CohortDescriptor(
        cohort_id=COHORT_ID,
        run=make_covenant_cohort_run(state),
        timeout_ms=TIMEOUT_MS,
        default_visibility=Restricted(reason=RESTRICTED_REASON),
        required=True,
        safety_class=True,
        execution_mode=ExecutionMode.ASYNC,
    )


def register_covenant_cohort(
    registry: CohortRegistry,
    state: StateStore,
) -> CohortDescriptor:
    """Register the covenant cohort on a CohortRegistry."""
    descriptor = make_covenant_descriptor(state)
    registry.register(descriptor)
    return descriptor


__all__ = [
    "COHORT_ID",
    "DESCRIPTION_CAP",
    "RESTRICTED_REASON",
    "RULE_COUNT_CAP",
    "TIMEOUT_MS",
    "make_covenant_cohort_run",
    "make_covenant_descriptor",
    "register_covenant_cohort",
]
