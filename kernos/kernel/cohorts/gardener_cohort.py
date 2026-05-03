"""Gardener cohort adapter — first cohort targeting the fan-out runner.

Per the COHORT-ADAPT-GARDENER spec. Adapts gardener's existing
state into a per-turn ``CohortOutput`` the integration layer can
consume. v1 is a STATUS SURFACE only (Option A from the spec's
conceptual question; the design review confirmed). The cohort:

  - Reads gardener's non-mutating snapshot via
    ``GardenerService.current_observation_snapshot``. No model
    calls. No coalescer drain. No event emission.
  - Resolves the active canvas from ``CohortContext.active_spaces``
    using the explicit "exactly one canvas → use it; zero or
    multiple → has_active_canvas: False" rule (the design review edit #5).
  - Filters restricted-pattern items at source per the design review edit #3.
    The cohort returns one ``CohortOutput`` per turn with
    ``visibility=Public``; restricted items are absent from the
    payload entirely (not marked, not redacted, just absent).
  - Returns a fully-typed ``CohortOutput`` from the run callable
    per the design review edit #2 (the runner mints the canonical ``cohort_run_id``).

The cohort's per-turn role is observational: surface what
gardener has already concluded so integration can fold pending
proposals and recent evolution into the briefing when relevant.
Per-turn consultation (Option B) and hybrid escalation (Option C)
are explicit future work.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from kernos.kernel.cohorts.descriptor import (
    CohortContext,
    CohortDescriptor,
    ContextSpaceRef,
    ExecutionMode,
)
from kernos.kernel.cohorts.registry import CohortRegistry
from kernos.kernel.gardener import (
    EvolutionRecord,
    GardenerService,
    GardenerSnapshot,
    PendingProposal,
)
from kernos.kernel.integration.briefing import (
    CohortOutput,
    Public,
    now_iso,
)


logger = logging.getLogger(__name__)


COHORT_ID = "gardener"
TIMEOUT_MS = 200  # state read; well below this in practice
RATIONALE_SHORT_CAP = 200
RECENT_EVOLUTION_LIMIT = 3


# Resolver shape: given a CohortContext, return the canvas_id this
# turn is for, or None if the exactly-one-canvas rule isn't met.
CanvasResolver = Callable[[CohortContext], str | None]

# Pattern-privacy predicate: given a pattern name, return True iff
# the pattern is restricted. Restricted items are filtered out of
# the cohort's payload entirely (the design review edit #3 / spec Section 4).
RestrictedPatternPredicate = Callable[[str], bool]


# ---------------------------------------------------------------------------
# Active-canvas resolution (the design review edit #5)
# ---------------------------------------------------------------------------


def _default_canvas_resolver(
    ctx: CohortContext,
    *,
    instance_db: Any | None = None,
) -> str | None:
    """Default active-canvas resolution.

    Rule per spec Section 2a + the design review edit #5: exactly one canvas
    space in ``ctx.active_spaces`` → use it. Zero or multiple →
    return None (cohort emits ``has_active_canvas: False``).

    When ``instance_db`` is provided, candidate spaces are
    filtered to those whose ``get_canvas(space_id)`` returns
    truthy. Otherwise every space in ``active_spaces`` is treated
    as a candidate (test-convenience default; production wires
    instance_db).
    """
    if instance_db is None:
        candidates = list(ctx.active_spaces)
    else:
        candidates = []
        for space in ctx.active_spaces:
            try:
                row = instance_db.get_canvas(space.space_id)
            except Exception:  # defensive — DB errors don't fail the turn
                row = None
            if row:
                candidates.append(space)
    if len(candidates) != 1:
        return None
    return candidates[0].space_id


def make_canvas_resolver(
    instance_db: Any | None = None,
) -> CanvasResolver:
    """Build a resolver bound to the given instance_db."""

    def _resolver(ctx: CohortContext) -> str | None:
        return _default_canvas_resolver(ctx, instance_db=instance_db)

    return _resolver


# ---------------------------------------------------------------------------
# Output payload conversion
# ---------------------------------------------------------------------------


def _truncate_rationale(text: str) -> str:
    text = text or ""
    if len(text) <= RATIONALE_SHORT_CAP:
        return text
    return text[: RATIONALE_SHORT_CAP - 1] + "…"


def _proposal_pattern(proposal: PendingProposal) -> str:
    """Best-effort extraction of pattern name from a PendingProposal."""
    try:
        value = proposal.payload.get("pattern", "")
        return str(value) if value else ""
    except (AttributeError, TypeError):
        return ""


def _proposal_summary(
    proposal: PendingProposal, sequence: int, canvas_id: str,
) -> dict[str, Any]:
    captured = proposal.captured_at
    captured_str = captured.isoformat() if hasattr(captured, "isoformat") else str(captured)
    proposal_id = (
        f"{canvas_id}:{captured_str}:{sequence}"
        if captured_str
        else f"{canvas_id}:{sequence}"
    )
    return {
        "proposal_id": proposal_id,
        "pattern": _proposal_pattern(proposal),
        "action": proposal.action,
        "confidence": proposal.confidence,
        "rationale_short": _truncate_rationale(proposal.rationale),
        "affected_pages": list(proposal.affected_pages or ()),
        "captured_at": captured_str,
    }


def _evolution_summary(record: EvolutionRecord) -> dict[str, Any]:
    return {
        "decision_id": record.decision_id,
        "action": record.action,
        "confidence": record.confidence,
        "pattern": record.pattern,
        "occurred_at": record.occurred_at.isoformat()
        if hasattr(record.occurred_at, "isoformat")
        else str(record.occurred_at),
        "consultation": record.consultation,
    }


# ---------------------------------------------------------------------------
# Run callable factory
# ---------------------------------------------------------------------------


def _empty_output(
    *, ctx: CohortContext, canvas_id: str | None = None,
) -> CohortOutput:
    """The "no observation" CohortOutput shape.

    Used when the active-canvas rule is unmet (zero or multiple
    canvas spaces) or when gardener has no state for the canvas.
    Emits ``has_active_canvas: False`` so integration's filter
    phase can dismiss cleanly.
    """
    payload: dict[str, Any] = {
        "has_active_canvas": canvas_id is not None,
        "canvas_id": canvas_id,
        "pending_proposals": [],
        "recent_evolution": [],
        "observation_age_seconds": None,
    }
    return CohortOutput(
        cohort_id=COHORT_ID,
        cohort_run_id=f"{ctx.turn_id}:{COHORT_ID}:provisional",
        output=payload,
        visibility=Public(),
        produced_at=now_iso(),
    )


def make_gardener_cohort_run(
    gardener_service: GardenerService,
    *,
    canvas_resolver: CanvasResolver | None = None,
    instance_db: Any | None = None,
    restricted_pattern_check: RestrictedPatternPredicate | None = None,
) -> Callable[[CohortContext], Awaitable[CohortOutput]]:
    """Build the async run callable bound to a GardenerService.

    The factory closures over the gardener service and resolver
    so the cohort registry can register a single descriptor per
    instance. ``canvas_resolver`` defaults to one bound to
    ``instance_db`` (or the no-DB fallback for tests).

    ``restricted_pattern_check`` lets operators/tests mark certain
    pattern names as private. Items tied to such patterns are
    filtered from the output entirely. Default: no patterns
    restricted. When pattern-privacy becomes a real concept (e.g.
    pattern frontmatter declares ``visibility: restricted``),
    the wiring layer passes the appropriate predicate; the
    cohort's interface doesn't change.
    """
    resolver = canvas_resolver or make_canvas_resolver(instance_db=instance_db)
    is_restricted = restricted_pattern_check or (lambda _pattern: False)

    async def gardener_cohort_run(ctx: CohortContext) -> CohortOutput:
        canvas_id = resolver(ctx)
        if not canvas_id:
            return _empty_output(ctx=ctx, canvas_id=None)

        try:
            snapshot = gardener_service.current_observation_snapshot(
                instance_id=ctx.instance_id,
                member_id=ctx.member_id,
                canvas_id=canvas_id,
            )
        except Exception:  # defensive — gardener errors don't fail the turn
            logger.warning(
                "GARDENER_COHORT_SNAPSHOT_FAILED: canvas=%s",
                canvas_id,
                exc_info=True,
            )
            return _empty_output(ctx=ctx, canvas_id=canvas_id)

        public_pending: list[dict[str, Any]] = []
        for i, proposal in enumerate(snapshot.pending_proposals):
            if is_restricted(_proposal_pattern(proposal)):
                continue
            public_pending.append(
                _proposal_summary(proposal, i, canvas_id),
            )

        # recent_evolution capped at the most recent N.
        recent: list[EvolutionRecord] = list(snapshot.recent_evolution)
        recent_tail = recent[-RECENT_EVOLUTION_LIMIT:]
        public_evolution: list[dict[str, Any]] = []
        for record in recent_tail:
            if is_restricted(record.pattern):
                continue
            public_evolution.append(_evolution_summary(record))

        payload = {
            "has_active_canvas": True,
            "canvas_id": canvas_id,
            "pending_proposals": public_pending,
            "recent_evolution": public_evolution,
            "observation_age_seconds": snapshot.observation_age_seconds,
        }
        return CohortOutput(
            cohort_id=COHORT_ID,
            cohort_run_id=f"{ctx.turn_id}:{COHORT_ID}:provisional",
            output=payload,
            visibility=Public(),
            produced_at=now_iso(),
        )

    return gardener_cohort_run


# ---------------------------------------------------------------------------
# Descriptor + registration
# ---------------------------------------------------------------------------


def make_gardener_descriptor(
    gardener_service: GardenerService,
    *,
    canvas_resolver: CanvasResolver | None = None,
    instance_db: Any | None = None,
    restricted_pattern_check: RestrictedPatternPredicate | None = None,
) -> CohortDescriptor:
    """Construct the cohort descriptor for the gardener cohort.

    Spec acceptance criterion 2: ``cohort_id="gardener"``,
    ``execution_mode=ASYNC``, ``timeout_ms=200``,
    ``default_visibility=Public``, ``required=False``,
    ``safety_class=False``.
    """
    return CohortDescriptor(
        cohort_id=COHORT_ID,
        run=make_gardener_cohort_run(
            gardener_service,
            canvas_resolver=canvas_resolver,
            instance_db=instance_db,
            restricted_pattern_check=restricted_pattern_check,
        ),
        timeout_ms=TIMEOUT_MS,
        default_visibility=Public(),
        required=False,
        safety_class=False,
        execution_mode=ExecutionMode.ASYNC,
    )


def register_gardener_cohort(
    registry: CohortRegistry,
    gardener_service: GardenerService,
    *,
    canvas_resolver: CanvasResolver | None = None,
    instance_db: Any | None = None,
    restricted_pattern_check: RestrictedPatternPredicate | None = None,
) -> CohortDescriptor:
    """Register the gardener cohort on a CohortRegistry.

    Returns the descriptor that was registered. Production wiring
    (INTEGRATION-WIRE-LIVE) calls this from the boot path with a
    real instance_db + restricted_pattern_check; tests call it
    against a tmp registry.
    """
    descriptor = make_gardener_descriptor(
        gardener_service,
        canvas_resolver=canvas_resolver,
        instance_db=instance_db,
        restricted_pattern_check=restricted_pattern_check,
    )
    registry.register(descriptor)
    return descriptor


__all__ = [
    "COHORT_ID",
    "RATIONALE_SHORT_CAP",
    "RECENT_EVOLUTION_LIMIT",
    "TIMEOUT_MS",
    "CanvasResolver",
    "RestrictedPatternPredicate",
    "make_canvas_resolver",
    "make_gardener_cohort_run",
    "make_gardener_descriptor",
    "register_gardener_cohort",
]
