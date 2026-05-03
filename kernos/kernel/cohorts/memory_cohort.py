"""Memory cohort adapter — second cohort targeting the fan-out runner.

Per the COHORT-ADAPT-MEMORY spec. Decouples memory retrieval from
being a model-decided `remember`-tool call inside reasoning into a
per-turn pre-fan-out cohort. v1 uses Option A (raw user message
as embedding query); no LLM call inside the cohort path.

The complementary push/pull split is preserved:

  - Push (this cohort): runs every turn with the user's message
    as the embedding query; surfaces structured knowledge + entity
    results pre-integration.
  - Pull (legacy `remember` tool): integration calls during its
    prep loop when archive depth is needed. Archive search keeps
    its existing Haiku-driven path; the cohort excludes archives
    to honor the no-LLM-call invariant.
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
    Public,
    now_iso,
)
from kernos.kernel.retrieval import (
    ArchiveMatch,
    EntityMatch,
    KnowledgeMatch,
    RetrievalService,
    RetrievalSnapshot,
)


logger = logging.getLogger(__name__)


COHORT_ID = "memory"
TIMEOUT_MS = 1500  # embedding + parallel knowledge/entity searches
KNOWLEDGE_CONTENT_CAP = 300
ARCHIVE_SUMMARY_CAP = 500


# ---------------------------------------------------------------------------
# Payload assembly
# ---------------------------------------------------------------------------


def _truncate(text: str, cap: int) -> str:
    if not text or len(text) <= cap:
        return text or ""
    return text[: cap - 1] + "…"


def _knowledge_summary(km: KnowledgeMatch) -> dict[str, Any]:
    return {
        "entry_id": km.entry_id,
        "content_short": _truncate(km.content, KNOWLEDGE_CONTENT_CAP),
        "authored_by": km.authored_by,
        "created_at": km.created_at,
        "quality_score": km.quality_score,
        "source_space_id": km.source_space_id,
    }


def _entity_summary(em: EntityMatch) -> dict[str, Any]:
    return {
        "entity_id": em.entity_id,
        "name": em.canonical_name,
        "entity_type": em.entity_type,
        "knowledge_count": em.knowledge_count,
        "uncertainty_notes": list(em.uncertainty_notes),
    }


def _archive_summary(archive: ArchiveMatch | None) -> dict[str, Any] | None:
    if archive is None:
        return None
    return {
        "archive_id": archive.archive_id,
        "span_summary": _truncate(archive.span_summary, ARCHIVE_SUMMARY_CAP),
        "ancestor_space_id": archive.ancestor_space_id,
    }


def _truncation_info(snapshot: RetrievalSnapshot) -> dict[str, Any]:
    """Project the snapshot's truncation flag into the cohort's
    reporting shape. The structured snapshot already capped to
    top-N during budget-shape; we surface that fact plus per-list
    flags so integration can decide whether to re-query."""
    return {
        "knowledge_truncated": snapshot.truncated and len(snapshot.knowledge) > 0,
        "entities_truncated": snapshot.truncated and len(snapshot.entities) > 0,
        "archive_truncated": False,
        "tokens_used": 0,
        "tokens_budget": 0,
    }


# ---------------------------------------------------------------------------
# Run callable factory
# ---------------------------------------------------------------------------


def _empty_payload(
    *,
    query_used: str,
    retrieval_attempted: bool,
    state_intercept: str | None = None,
    source: str = "normal",
) -> dict[str, Any]:
    return {
        "query_used": query_used,
        "retrieval_attempted": retrieval_attempted,
        "knowledge": [],
        "entities": [],
        "archive_summary": None,
        "state_intercept": state_intercept,
        "source": source,
        "truncation": {
            "knowledge_truncated": False,
            "entities_truncated": False,
            "archive_truncated": False,
            "tokens_used": 0,
            "tokens_budget": 0,
        },
    }


def make_memory_cohort_run(
    retrieval_service: RetrievalService,
    *,
    instance_db: Any | None = None,
) -> Callable[[CohortContext], Awaitable[CohortOutput]]:
    """Build the async run callable bound to a RetrievalService.

    The factory closures over the retrieval service so the cohort
    registry can register a single descriptor per instance. The
    optional `instance_db` is forwarded to `search_structured`
    for the disclosure-gate permission lookup; tests may pass
    None for the no-DB path (no cross-member visibility logic).
    """

    async def memory_cohort_run(ctx: CohortContext) -> CohortOutput:
        active_space_id = ""
        if ctx.active_spaces:
            # The cohort fires per (member, turn). Pick the first
            # active space for retrieval scope; the existing
            # remember-tool semantics work the same way today.
            active_space_id = ctx.active_spaces[0].space_id

        try:
            snapshot = await retrieval_service.search_structured(
                instance_id=ctx.instance_id,
                query=ctx.user_message,
                active_space_id=active_space_id,
                requesting_member_id=ctx.member_id,
                instance_db=instance_db,
                trace=None,
                include_archives=False,  # spec Section 4d: archives via remember pull
            )
        except Exception:  # pragma: no cover - defensive guard
            # Per the design review edit #6: only embedding/vector failure becomes
            # graceful empty inside the retrieval service. Anything
            # propagating to here is an unexpected bug; let the
            # runner observe outcome=error.
            logger.exception("MEMORY_COHORT_UNEXPECTED_ERROR")
            raise

        # Build the cohort output payload.
        if snapshot.source == "state_intercept":
            payload = _empty_payload(
                query_used=ctx.user_message,
                retrieval_attempted=False,
                state_intercept=snapshot.state_intercept,
                source="state_intercept",
            )
        else:
            payload = {
                "query_used": ctx.user_message,
                "retrieval_attempted": snapshot.retrieval_attempted,
                "knowledge": [
                    _knowledge_summary(km) for km in snapshot.knowledge
                ],
                "entities": [
                    _entity_summary(em) for em in snapshot.entities
                ],
                "archive_summary": _archive_summary(snapshot.archive),
                "state_intercept": None,
                "source": "normal",
                "truncation": _truncation_info(snapshot),
            }

        return CohortOutput(
            cohort_id=COHORT_ID,
            cohort_run_id=f"{ctx.turn_id}:{COHORT_ID}:provisional",
            output=payload,
            visibility=Public(),
            produced_at=now_iso(),
        )

    return memory_cohort_run


# ---------------------------------------------------------------------------
# Descriptor + registration
# ---------------------------------------------------------------------------


def make_memory_descriptor(
    retrieval_service: RetrievalService,
    *,
    instance_db: Any | None = None,
) -> CohortDescriptor:
    """Construct the cohort descriptor for the memory cohort.

    Spec acceptance criterion 2: ``cohort_id="memory"``,
    ``execution_mode=ASYNC``, ``timeout_ms=1500``,
    ``default_visibility=Public``, ``required=False``,
    ``safety_class=False``.
    """
    return CohortDescriptor(
        cohort_id=COHORT_ID,
        run=make_memory_cohort_run(
            retrieval_service, instance_db=instance_db,
        ),
        timeout_ms=TIMEOUT_MS,
        default_visibility=Public(),
        required=False,
        safety_class=False,
        execution_mode=ExecutionMode.ASYNC,
    )


def register_memory_cohort(
    registry: CohortRegistry,
    retrieval_service: RetrievalService,
    *,
    instance_db: Any | None = None,
) -> CohortDescriptor:
    """Register the memory cohort on a CohortRegistry. Returns the
    registered descriptor."""
    descriptor = make_memory_descriptor(
        retrieval_service, instance_db=instance_db,
    )
    registry.register(descriptor)
    return descriptor


__all__ = [
    "ARCHIVE_SUMMARY_CAP",
    "COHORT_ID",
    "KNOWLEDGE_CONTENT_CAP",
    "TIMEOUT_MS",
    "make_memory_cohort_run",
    "make_memory_descriptor",
    "register_memory_cohort",
]
