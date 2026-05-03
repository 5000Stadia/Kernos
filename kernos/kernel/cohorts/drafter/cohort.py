"""DrafterCohort — tool-starved system cohort (DRAFTER v2 spec).

C3 wires the full tick orchestration: two-tier evaluation (C2),
signals + receipts (C3), compiler boundary, provenance pointers,
idle resurface, and crash-idempotency wiring through
:class:`ActionLog`.

Anti-fragmentation invariant: Drafter consumes shared context surfaces
(event_stream, friction observer signals, WDP DraftRegistry, STS query
surfaces, principal cohort context). Drafter does NOT build a parallel
cohort-specific context model. When the shared-situation-model spec
lands post-CRB, Drafter slots in by consuming the new shared object
without internal rework. Reviewers should reject changes that introduce
Drafter-private context state, parallel friction-detection logic, or
shadow registries.

Future-composition invariant: Drafter is the first tool-starved cohort.
The universal cohort substrate enforces tool restriction generically;
future Pattern Observer and Curator cohorts inherit the same pattern.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from kernos.kernel.cohorts._substrate.action_log import ActionLog
from kernos.kernel.cohorts._substrate.cursor import DurableEventCursor
from kernos.kernel.cohorts._substrate.tool_restriction import (
    CohortToolWhitelist,
)
from kernos.kernel.cohorts.drafter.budget import BudgetTracker
from kernos.kernel.cohorts.drafter.errors import DrafterToolForbidden
from kernos.kernel.cohorts.drafter.evaluation import (
    EvaluationOutcome,
    Tier2Evaluator,
    evaluate_event,
)
from kernos.kernel.cohorts.drafter.multi_draft import (
    has_multi_intent,
    select_relevant_drafts,
)
from kernos.kernel.cohorts.drafter.ports import (
    DRAFTER_WHITELIST,
    DrafterDraftPort,
    DrafterEventPort,
    DrafterSubstrateToolsPort,
)
from kernos.kernel.cohorts.drafter.receipts import (
    RECEIPT_DRAFT_UPDATED,
    RECEIPT_DRY_RUN_COMPLETED,
    RECEIPT_FEEDBACK_RECEIVED,
    RECEIPT_SIGNAL_EMITTED,
    ReceiptTimeoutConfig,
    build_draft_updated_payload,
    build_dry_run_completed_payload,
    build_feedback_received_payload,
    build_signal_emitted_payload,
)
from kernos.kernel.cohorts.drafter.recognition import (
    RecognitionEvaluation,
    should_create_persistent_draft,
)
from kernos.kernel.cohorts.drafter.signals import (
    SIGNAL_DRAFT_READY,
    SIGNAL_IDLE_RESURFACE,
    SIGNAL_MULTI_INTENT_DETECTED,
    build_draft_ready_payload,
    build_idle_resurface_payload,
    build_multi_intent_payload,
)


# ---------------------------------------------------------------------------
# Tick result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TickResult:
    """Output of one ``DrafterCohort.tick()`` pass."""

    instance_id: str
    events_processed: int
    cursor_position_before: str
    cursor_position_after: str
    signals_emitted: tuple[str, ...] = ()
    receipts_emitted: int = 0
    budget_used: int = 0
    budget_remaining: int = 0
    no_op: bool = True


# ---------------------------------------------------------------------------
# Idle-resurface helper
# ---------------------------------------------------------------------------


# Paused-state statuses the resurface scan considers eligible.
_RESURFACE_STATUSES: frozenset[str] = frozenset({"shaping", "blocked"})

# How long a draft must have been idle before the periodic wake will
# resurface it. Default: 24 hours.
DEFAULT_IDLE_RESURFACE_AGE = timedelta(hours=24)


# ---------------------------------------------------------------------------
# DrafterCohort
# ---------------------------------------------------------------------------


class DrafterCohort:
    """Tool-starved system cohort observing conversation events; holds
    workflow drafts via WDP; validates via STS dry-run; signals principal
    when committable.

    Constructor takes restricted *ports* (the design review pin v1→v2), NOT raw
    ``DraftRegistry`` / ``SubstrateTools`` / ``EventEmitter`` objects.
    Forbidden capabilities (``mark_committed``, full ``register_workflow``,
    raw ``emit``) are STRUCTURALLY ABSENT from the port surface — the
    Python attribute lookup raises ``AttributeError`` before any
    whitelist check runs.

    The ``CohortToolWhitelist`` substrate is wired as belt-and-suspenders
    for any path that escapes the port surface.

    Tick loop (C3):

    1. Read next batch of events from durable cursor.
    2. For each event:
       a. Select context-relevant drafts (oldest-first).
       b. Two-tier evaluation: NO_OP / BUDGET_EXHAUSTED / EVALUATED.
       c. EVALUATED branch: handle multi-intent, create/update draft
          via the action_log-backed port, run STS dry-run, emit
          ``draft_ready`` signal on first valid hash.
       d. ``conversation.context.shifted`` branch: also runs idle-
          resurface scan.
       e. Commit cursor past this event.
    """

    def __init__(
        self,
        *,
        draft_port: DrafterDraftPort,
        substrate_tools_port: DrafterSubstrateToolsPort,
        event_port: DrafterEventPort,
        cursor: DurableEventCursor,
        action_log: ActionLog,
        budget: BudgetTracker | None = None,
        compiler_helper: Optional[Callable[[Any], dict]] = None,
        tier2_evaluator: Tier2Evaluator | None = None,
        receipt_timeout_config: ReceiptTimeoutConfig | None = None,
        idle_resurface_age: timedelta | None = None,
        clock: Callable[[], datetime] | None = None,
        whitelist: CohortToolWhitelist | None = None,
    ) -> None:
        # Codex hardening: cross-validate that all ports + cursor +
        # action_log agree on instance_id and cohort_id. Misconfiguration
        # at engine bring-up surfaces here, not at first tick.
        if draft_port.instance_id != cursor.instance_id:
            raise ValueError(
                f"draft_port.instance_id={draft_port.instance_id!r} "
                f"does not match cursor.instance_id="
                f"{cursor.instance_id!r}"
            )
        if substrate_tools_port.instance_id != cursor.instance_id:
            raise ValueError(
                f"substrate_tools_port.instance_id="
                f"{substrate_tools_port.instance_id!r} does not match "
                f"cursor.instance_id={cursor.instance_id!r}"
            )
        if event_port.instance_id != cursor.instance_id:
            raise ValueError(
                f"event_port.instance_id={event_port.instance_id!r} "
                f"does not match cursor.instance_id="
                f"{cursor.instance_id!r}"
            )
        if action_log.cohort_id != cursor.cohort_id:
            raise ValueError(
                f"action_log.cohort_id={action_log.cohort_id!r} does "
                f"not match cursor.cohort_id={cursor.cohort_id!r}"
            )
        self._draft_port = draft_port
        self._substrate_tools_port = substrate_tools_port
        self._event_port = event_port
        self._cursor = cursor
        self._action_log = action_log
        self._budget = budget or BudgetTracker()
        self._compiler_helper = compiler_helper
        self._tier2_evaluator = tier2_evaluator
        self._receipt_timeout_config = (
            receipt_timeout_config or ReceiptTimeoutConfig()
        )
        self._idle_resurface_age = (
            idle_resurface_age or DEFAULT_IDLE_RESURFACE_AGE
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._whitelist = whitelist or CohortToolWhitelist(
            cohort_name="drafter",
            allowed_tools=DRAFTER_WHITELIST,
            forbidden_exception=DrafterToolForbidden,
        )
        # In-memory dedupe for ready-signal emission: maps
        # ``(draft_id, descriptor_hash)`` -> True. Lost on restart;
        # principal can re-ack a re-fired signal. This is the
        # "logical dedupe" layer; action_log handles physical
        # crash-replay idempotency separately.
        self._ready_signal_dedupe: set[tuple[str, str]] = set()
        self._started = False

    # -- properties -----------------------------------------------------

    @property
    def cohort_id(self) -> str:
        return "drafter"

    @property
    def cursor(self) -> DurableEventCursor:
        return self._cursor

    @property
    def action_log(self) -> ActionLog:
        return self._action_log

    @property
    def whitelist(self) -> CohortToolWhitelist:
        return self._whitelist

    @property
    def draft_port(self) -> DrafterDraftPort:
        return self._draft_port

    @property
    def substrate_tools_port(self) -> DrafterSubstrateToolsPort:
        return self._substrate_tools_port

    @property
    def event_port(self) -> DrafterEventPort:
        return self._event_port

    @property
    def budget(self) -> BudgetTracker:
        return self._budget

    @property
    def receipt_timeout_config(self) -> ReceiptTimeoutConfig:
        return self._receipt_timeout_config

    # -- lifecycle ------------------------------------------------------

    def start(self, *, instance_id: str) -> None:
        if not instance_id:
            raise ValueError("instance_id is required")
        if self._cursor.instance_id != instance_id:
            raise ValueError(
                f"DrafterCohort.start instance_id={instance_id!r} does "
                f"not match cursor instance_id="
                f"{self._cursor.instance_id!r}"
            )
        self._started = True

    @property
    def is_started(self) -> bool:
        return self._started

    # -- tick orchestration ---------------------------------------------

    async def tick(self, *, instance_id: str) -> TickResult:
        """Single evaluation pass. See class docstring for the loop.

        Crash-idempotency: every side effect (draft mutation, signal,
        receipt) routes through :class:`ActionLog.record_and_perform`
        with a deterministic ``target_id``. Replay finds the log entry
        and skips the side effect.
        """
        if not self._started:
            raise RuntimeError("DrafterCohort.start() must be called first")
        if instance_id != self._cursor.instance_id:
            raise ValueError(
                f"tick instance_id={instance_id!r} does not match "
                f"cursor instance_id={self._cursor.instance_id!r}"
            )
        position_before = await self._cursor.current_position()
        events = await self._cursor.read_next_batch(max_events=10)
        signals_emitted: list[str] = []
        receipts_emitted = 0
        budget_used_before = self._budget.used(instance_id=instance_id)
        events_processed = 0
        for event in events:
            sigs, recs = await self._process_event(
                event, instance_id=instance_id,
            )
            signals_emitted.extend(sigs)
            receipts_emitted += recs
            await self._cursor.commit_position(
                event_id=event.event_id, timestamp=event.timestamp,
            )
            events_processed += 1
        position_after = await self._cursor.current_position()
        budget_used = (
            self._budget.used(instance_id=instance_id) - budget_used_before
        )
        return TickResult(
            instance_id=instance_id,
            events_processed=events_processed,
            cursor_position_before=position_before,
            cursor_position_after=position_after,
            signals_emitted=tuple(signals_emitted),
            receipts_emitted=receipts_emitted,
            budget_used=budget_used,
            budget_remaining=self._budget.remaining(instance_id=instance_id),
            no_op=not signals_emitted and not receipts_emitted,
        )

    # -- per-event handlers ---------------------------------------------

    async def _process_event(
        self, event: Any, *, instance_id: str,
    ) -> tuple[list[str], int]:
        signals: list[str] = []
        receipts = 0

        # v1.1: CRB feedback dispatch. Substrate-set envelope source is
        # the trust boundary; payload-claimed source ignored.
        if event.event_type == "crb.feedback.modify_request":
            return await self._handle_feedback_event(
                event, instance_id=instance_id,
            )

        # Select context-relevant drafts.
        home_space_id = (event.payload or {}).get("home_space_id") or event.space_id
        source_thread_id = (event.payload or {}).get("source_thread_id")
        all_drafts = await self._draft_port.list_drafts(include_terminal=False)
        relevant = select_relevant_drafts(
            all_drafts,
            home_space_id=home_space_id,
            source_thread_id=source_thread_id,
        )
        # Two-tier evaluation.
        eval_result = await evaluate_event(
            event,
            instance_id=instance_id,
            has_active_drafts=bool(relevant),
            budget=self._budget,
            tier2=self._tier2_evaluator,
        )
        if eval_result.outcome != EvaluationOutcome.EVALUATED:
            # NO_OP or BUDGET_EXHAUSTED — context-shifted events still
            # get an idle-resurface scan even when the main evaluation
            # path is a no-op.
            if event.event_type == "conversation.context.shifted":
                rs_signals = await self._scan_idle_resurface(
                    event, instance_id=instance_id,
                )
                signals.extend(rs_signals)
            return signals, receipts

        recognition = eval_result.recognition
        assert recognition is not None  # narrowing for type-checker

        # Multi-intent path — emit signal and stop further processing on
        # this event (principal asks a disambiguation question).
        candidates = list(getattr(recognition, "candidate_intents_list", []) or [])
        if has_multi_intent(candidates):
            sig_id = self._derive_signal_id(
                event_id=event.event_id,
                signal_type=SIGNAL_MULTI_INTENT_DETECTED,
                target=event.event_id,
            )
            await self._event_port.emit_signal(
                source_event_id=event.event_id,
                signal_type=SIGNAL_MULTI_INTENT_DETECTED,
                payload=build_multi_intent_payload(
                    instance_id=instance_id,
                    candidate_intents=[
                        {"summary": c.summary, "confidence": c.confidence}
                        for c in candidates
                    ],
                    source_event_id=event.event_id,
                ),
                target_id=sig_id,
            )
            signals.append(SIGNAL_MULTI_INTENT_DETECTED)
            await self._emit_signal_receipt(
                event=event, signal_type=SIGNAL_MULTI_INTENT_DETECTED,
                signal_id=sig_id,
            )
            receipts += 1
            return signals, receipts

        # Single-intent path: create new draft (if authorised) OR update
        # an existing relevant draft.
        target_draft_id = recognition.candidate_target_workflow_id
        draft_record_summary: dict | None = None
        was_update = False
        if target_draft_id and any(d.draft_id == target_draft_id for d in relevant):
            # Update existing draft. Action_log handles dedupe on
            # source_event_id.
            target = next(
                d for d in relevant if d.draft_id == target_draft_id
            )
            draft_record_summary = await self._update_existing_draft(
                target, event=event, recognition=recognition,
            )
            was_update = True
        elif should_create_persistent_draft(recognition):
            draft_record_summary = await self._create_new_draft(
                event=event, recognition=recognition,
                home_space_id=home_space_id,
                source_thread_id=source_thread_id,
            )
        # else: shape-match without permission OR no target — drop.

        if draft_record_summary is None:
            return signals, receipts
        # Codex final-pass REAL #1: a pending action_log row from a
        # previous crashed perform replays as an empty {} summary.
        # Bail out of the orchestration cleanly so the cursor can
        # advance — the conservative-skip behavior documented in the
        # action_log claim-first protocol.
        draft_id = draft_record_summary.get("draft_id")
        if not draft_id:
            return signals, receipts

        # Codex final-pass REAL #2: emit drafter.receipt.draft_updated
        # after a successful update (AC #19 receipt pattern). The
        # receipt routes through action_log so replay-skips are clean.
        if was_update:
            await self._event_port.emit_receipt(
                source_event_id=event.event_id,
                receipt_type=RECEIPT_DRAFT_UPDATED,
                payload=build_draft_updated_payload(
                    draft_id=draft_id,
                    instance_id=instance_id,
                    version_after=draft_record_summary.get("version", 0),
                ),
                target_id=f"draft_updated::{draft_id}",
            )
            receipts += 1

        # STS dry-run + ready signal.
        draft = await self._draft_port.get_draft(draft_id=draft_id)
        if draft is None:
            return signals, receipts  # Race / replay edge case.
        if self._compiler_helper is None:
            # Degraded mode; can't produce a descriptor candidate.
            return signals, receipts
        descriptor = self._compiler_helper(draft)
        dry_run = await self._substrate_tools_port.register_workflow_dry_run(
            descriptor=descriptor,
        )
        # Receipt for dry-run completion.
        await self._event_port.emit_receipt(
            source_event_id=event.event_id,
            receipt_type=RECEIPT_DRY_RUN_COMPLETED,
            payload=build_dry_run_completed_payload(
                draft_id=draft_id,
                descriptor_hash=dry_run.descriptor_hash,
                valid=dry_run.valid,
                issue_count=len(dry_run.issues),
                capability_gap_count=len(dry_run.capability_gaps),
            ),
            target_id=f"dry_run::{draft_id}::{dry_run.descriptor_hash}",
        )
        receipts += 1

        # Ready signal — emit once per (draft_id, descriptor_hash).
        if dry_run.valid:
            dedupe_key = (draft_id, dry_run.descriptor_hash)
            if dedupe_key not in self._ready_signal_dedupe:
                sig_id = self._derive_signal_id(
                    event_id=event.event_id,
                    signal_type=SIGNAL_DRAFT_READY,
                    target=f"{draft_id}::{dry_run.descriptor_hash}",
                )
                await self._event_port.emit_signal(
                    source_event_id=event.event_id,
                    signal_type=SIGNAL_DRAFT_READY,
                    payload=build_draft_ready_payload(
                        draft_id=draft_id,
                        instance_id=instance_id,
                        descriptor_hash=dry_run.descriptor_hash,
                        intent_summary=draft.intent_summary or "",
                        home_space_id=draft.home_space_id,
                        source_thread_id=draft.source_thread_id,
                    ),
                    target_id=sig_id,
                )
                signals.append(SIGNAL_DRAFT_READY)
                await self._emit_signal_receipt(
                    event=event, signal_type=SIGNAL_DRAFT_READY,
                    signal_id=sig_id,
                )
                receipts += 1
                self._ready_signal_dedupe.add(dedupe_key)

        return signals, receipts

    async def _create_new_draft(
        self,
        *,
        event: Any,
        recognition: RecognitionEvaluation,
        home_space_id: str | None,
        source_thread_id: str | None,
    ) -> dict:
        intent_summary = recognition.candidate_intent or ""
        target = self._derive_create_target(
            event_id=event.event_id,
            intent_summary=intent_summary,
        )
        return await self._draft_port.create_draft(
            source_event_id=event.event_id,
            intent_summary=intent_summary,
            home_space_id=home_space_id,
            source_thread_id=source_thread_id,
            target_draft_id=target,
        )

    async def _update_existing_draft(
        self,
        target: Any,
        *,
        event: Any,
        recognition: RecognitionEvaluation,
    ) -> dict:
        # WDP requires expected_version for CAS. Best-effort: read the
        # current version from the draft, retry once on conflict
        # (AC #23). The action_log dedupes replays of the same event.
        provenance = self._build_provenance(event=event, reason="shape_signal")
        notes = _append_provenance(target.resolution_notes, provenance)
        try:
            summary = await self._draft_port.update_draft(
                source_event_id=event.event_id,
                draft_id=target.draft_id,
                expected_version=target.version,
                resolution_notes=notes,
            )
        except Exception:
            # Retry-once after re-read (AC #23). The action_log will
            # dedupe if this is a crash-replay rather than a true
            # concurrent modification.
            fresh = await self._draft_port.get_draft(draft_id=target.draft_id)
            if fresh is None:
                raise
            notes = _append_provenance(fresh.resolution_notes, provenance)
            summary = await self._draft_port.update_draft(
                source_event_id=event.event_id,
                draft_id=target.draft_id,
                expected_version=fresh.version,
                resolution_notes=notes,
            )
        return summary

    async def _emit_signal_receipt(
        self, *, event: Any, signal_type: str, signal_id: str,
    ) -> None:
        await self._event_port.emit_receipt(
            source_event_id=event.event_id,
            receipt_type=RECEIPT_SIGNAL_EMITTED,
            payload=build_signal_emitted_payload(
                signal_type=signal_type,
                signal_id=signal_id,
            ),
            target_id=f"signal_emitted::{signal_id}",
        )

    async def _handle_feedback_event(
        self, event: Any, *, instance_id: str,
    ) -> tuple[list[str], int]:
        """Handler for ``crb.feedback.modify_request`` events (v1.1).

        Validates envelope source (must be CRB), looks up the named
        draft, appends a provenance entry to ``resolution_notes`` with
        the feedback summary + original_proposal_id, and emits
        ``drafter.receipt.feedback_received`` so CRB knows the
        feedback was ingested.

        Source authority is the substrate-set envelope; payload-claimed
        source is ignored. A draft that does not exist or has shifted
        to terminal state is a graceful no-op (cursor still advances).
        """
        signals: list[str] = []
        receipts = 0

        # Step 1: envelope source authority. Reject payload-claimed
        # source (mirrors the STS approval-gate pattern).
        if event.envelope.source_module != "crb":
            return signals, receipts

        payload = event.payload if isinstance(event.payload, dict) else {}
        draft_id = payload.get("draft_id") or ""
        original_proposal_id = payload.get("original_proposal_id") or ""
        feedback_summary = payload.get("feedback_summary") or ""
        if not draft_id or not original_proposal_id:
            return signals, receipts  # malformed; skip cleanly

        # Step 2: look up the draft via the port (instance-scoped).
        draft = await self._draft_port.get_draft(draft_id=draft_id)
        if draft is None or draft.status in ("committed", "abandoned"):
            return signals, receipts  # graceful no-op

        # Step 3: append provenance entry to resolution_notes.
        provenance = {
            "timestamp": self._clock().isoformat(),
            "source_event_ids": [event.event_id],
            "source_turn_id": payload.get("source_turn_id", ""),
            "reason": "crb_modify_feedback",
            "feedback_summary": feedback_summary,
            "original_proposal_id": original_proposal_id,
        }
        notes = _append_provenance(draft.resolution_notes, provenance)
        # Codex v1.1 fold: feedback against a ready draft requires
        # demotion (WDP's ReadyStateMutationRequiresDemotion invariant
        # rejects substantive content mutation on status="ready"
        # unless the same call sets status="shaping"). resolution_notes
        # is substantive content per WDP, so demotion is required when
        # the draft is in ready state.
        update_kwargs: dict = {"resolution_notes": notes}
        if draft.status == "ready":
            update_kwargs["status"] = "shaping"
        update_summary = await self._draft_port.update_draft(
            source_event_id=event.event_id,
            draft_id=draft_id,
            expected_version=draft.version,
            **update_kwargs,
        )

        # Step 4: receipt emission via action_log.
        if update_summary.get("draft_id"):
            await self._event_port.emit_receipt(
                source_event_id=event.event_id,
                receipt_type=RECEIPT_FEEDBACK_RECEIVED,
                payload=build_feedback_received_payload(
                    draft_id=draft_id,
                    instance_id=instance_id,
                    original_proposal_id=original_proposal_id,
                    source_event_id=event.event_id,
                ),
                target_id=f"feedback_received::{draft_id}::{event.event_id}",
            )
            receipts += 1
        return signals, receipts

    async def _scan_idle_resurface(
        self, event: Any, *, instance_id: str,
    ) -> list[str]:
        """Idle re-surface: when a context.shifted event brings the user
        back to a space where a paused draft lives, fire
        ``drafter.signal.idle_resurface`` (AC #17)."""
        signals: list[str] = []
        new_space = (event.payload or {}).get("home_space_id") or event.space_id
        if not new_space:
            return signals
        all_drafts = await self._draft_port.list_drafts(
            home_space_id=new_space, include_terminal=False,
        )
        # Resurface only paused-eligible drafts.
        eligible = [d for d in all_drafts if d.status in _RESURFACE_STATUSES]
        for draft in eligible:
            # Skip if already-resurfaced for this event (action_log
            # dedupe via deterministic target_id).
            sig_id = self._derive_signal_id(
                event_id=event.event_id,
                signal_type=SIGNAL_IDLE_RESURFACE,
                target=draft.draft_id,
            )
            await self._event_port.emit_signal(
                source_event_id=event.event_id,
                signal_type=SIGNAL_IDLE_RESURFACE,
                payload=build_idle_resurface_payload(
                    draft_id=draft.draft_id,
                    instance_id=instance_id,
                    last_touched_at=draft.last_touched_at,
                    intent_summary=draft.intent_summary or "",
                ),
                target_id=sig_id,
            )
            signals.append(SIGNAL_IDLE_RESURFACE)
        return signals

    # -- helpers --------------------------------------------------------

    @staticmethod
    def _derive_create_target(*, event_id: str, intent_summary: str) -> str:
        """Deterministic target_id for create_draft action_log entries.
        Must NOT collide for distinct (event_id, intent_summary) pairs;
        MUST collide on replay so action_log dedupes."""
        h = hashlib.sha256(
            f"{event_id}::{intent_summary}".encode("utf-8")
        ).hexdigest()
        return f"create::{h[:16]}"

    @staticmethod
    def _derive_signal_id(
        *, event_id: str, signal_type: str, target: str,
    ) -> str:
        """Deterministic signal_id used as both the action_log target_id
        and the logical signal identifier."""
        h = hashlib.sha256(
            f"{event_id}::{signal_type}::{target}".encode("utf-8")
        ).hexdigest()
        return f"{signal_type}::{h[:16]}"

    def _build_provenance(self, *, event: Any, reason: str) -> dict:
        return {
            "timestamp": self._clock().isoformat(),
            "source_event_ids": [event.event_id],
            "source_turn_id": (event.payload or {}).get("source_turn_id", ""),
            "reason": reason,
        }


def _append_provenance(existing: str | None, entry: dict) -> str:
    """Append a provenance entry to ``resolution_notes`` (which is a
    JSON string in WDP). Initializes structure if absent. Returns the
    updated JSON string.
    """
    import json
    try:
        body = json.loads(existing) if existing else {}
    except Exception:
        body = {}
    if "updates" not in body or not isinstance(body["updates"], list):
        body["updates"] = []
    body["updates"].append(entry)
    return json.dumps(body)


__all__ = ["DrafterCohort", "TickResult"]
