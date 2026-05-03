"""GardenerService — per-instance canvas shape authority.

Runs the Gardener cohort against canvas events. Pillar 2 scaffolding
(state + event subscription + public API surface). Pillars 3 and 4 fill
in the initial-shape and continuous-evolution logic.

Design notes:
  - One GardenerService per Kernos instance, held by MessageHandler.
  - Subscribes to canvas events via ``on_canvas_event(iid, etype,
    payload, *, member_id="")`` — the same signature as the existing
    handler-level ``_canvas_emit`` callback, so wiring fans the event
    out to the Gardener after emission to the unified event stream.
  - Ignores events it emitted itself (``source: gardener`` in payload)
    to prevent cascading reshapes (spec Hazard B).
  - Pattern content for initial-shape + evolution consultations is
    loaded from the Workflow Patterns canvas lazily and cached with
    mtime-style invalidation on Workflow Patterns canvas changes
    (spec Hazard A).
  - Proposals coalesce within a per-canvas 24-hour window; see
    :class:`ProposalCoalescer`.

Non-blocking discipline: every handler entry point schedules work with
``asyncio.create_task`` so member-facing turns never block on a
Gardener consultation. Errors are logged and swallowed — canvas ops
must never break because the Gardener had a bad turn.
"""
from __future__ import annotations

import asyncio
import collections
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from kernos.cohorts.gardener import (
    EvolutionContext,
    GardenerDecision,
    GardenerExhausted,
    InitialShapeContext,
    PreferenceExtractionContext,
    PreferenceExtractionExhausted,
    PreferenceExtractionResult,
    SectionContext,
    detect_explicit_phrases,
    judge_evolution,
    judge_initial_shape,
    judge_preference_extraction,
    judge_section_management,
)
from kernos.kernel.pattern_heuristics import (
    CanvasEvaluationState,
    HeuristicDecl,
    HeuristicMatch,
    evaluate_declaration,
    parse_heuristic_declarations,
    trigger_matches_event,
)

logger = logging.getLogger(__name__)


#: Default coalescing window for evolution proposals (spec Pillar 4).
DEFAULT_COALESCE_MINUTES = 24 * 60  # 24 hours

#: Sentinel used in emitted events so the Gardener skips events it caused.
GARDENER_SOURCE = "gardener"

#: Name of the team canvas that stores the judgment-input library.
WORKFLOW_PATTERNS_CANVAS_NAME = "Workflow Patterns"

#: Pattern 00 cross-pattern heuristic thresholds. Conservative defaults;
#: per-canvas overrides land with preference-capture (deferred Pillar 5).
STALENESS_DAYS = 90                  # page last_updated older than this → flag
SPLIT_SECTION_COUNT_THRESHOLD = 3    # sections exceeding the line threshold
SPLIT_SECTION_LINE_THRESHOLD = 80    # per-section line count

#: Valid gardener_consent modes (canvas-level frontmatter field).
CONSENT_MODES = (
    "propose-all",
    "auto-non-destructive",
    "auto-all",
    "propose-critical-only",
)


#: Actions classified non-destructive → eligible for auto-apply under the
#: auto-non-destructive / auto-all consent modes. ``propose_split`` and
#: ``propose_merge`` are NOT in this set — they're proposal-class even
#: under ``auto-all`` in v1 (spec Pillar 4: "all reshapes except deletions
#: auto-apply"; v1 keeps split/merge as proposal-class to let the human
#: check the section-boundary choice before disk mutations happen).
NON_DESTRUCTIVE_ACTIONS = {"regenerate_summary"}

#: Pattern 00's back-reference-promotion threshold (CANVAS-CROSS-PAGE-INDEX).
BACK_REFERENCE_PROMOTION_THRESHOLD = 3

#: Flags that are advisory only — never auto-apply, always surface.
ADVISORY_ACTIONS = {
    "flag_stale", "flag_scope_mismatch",
    # Pattern-heuristic-dispatch outputs (CANVAS-GARDENER-PATTERN-HEURISTICS)
    "flag", "whisper", "alarm", "flag_library_upgrade",
    "propose_subdivide", "propose_promote",
    "propose_archive", "propose_transition",
    # Reference-index outputs (CANVAS-CROSS-PAGE-INDEX)
    "flag_broken_reference", "propose_back_reference_index",
}


# ---------------------------------------------------------------------------
# Coalescing
# ---------------------------------------------------------------------------


@dataclass
class PendingProposal:
    canvas_id: str
    action: str
    confidence: str
    rationale: str
    affected_pages: list[str]
    captured_at: datetime
    payload: dict = field(default_factory=dict)


class ProposalCoalescer:
    """Per-canvas rolling window of high-confidence proposals.

    In-process only. Restart drops the buffer — acceptable for v1; a
    persistent variant ships as a follow-on if the post-v1 observation
    shows rapid restart cycles losing noticeable work.
    """

    def __init__(self, window: timedelta = timedelta(minutes=DEFAULT_COALESCE_MINUTES)) -> None:
        self._window = window
        self._buffers: dict[str, list[PendingProposal]] = {}
        self._last_surface: dict[str, datetime] = {}

    @property
    def window(self) -> timedelta:
        return self._window

    def add(self, proposal: PendingProposal) -> None:
        self._buffers.setdefault(proposal.canvas_id, []).append(proposal)

    def should_surface(self, canvas_id: str, now: datetime | None = None) -> bool:
        """True if the window has elapsed since the last surface for this canvas."""
        now = now or datetime.now(timezone.utc)
        last = self._last_surface.get(canvas_id)
        if last is None:
            return True
        return (now - last) >= self._window

    def drain(self, canvas_id: str, now: datetime | None = None) -> list[PendingProposal]:
        """Return pending proposals for a canvas and mark window start."""
        now = now or datetime.now(timezone.utc)
        buf = self._buffers.pop(canvas_id, [])
        if buf:
            self._last_surface[canvas_id] = now
        return buf

    def buffered_count(self, canvas_id: str) -> int:
        return len(self._buffers.get(canvas_id, []))

    def snapshot_pending(
        self, canvas_id: str,
    ) -> tuple["PendingProposal", ...]:
        """Non-mutating read of currently-buffered proposals.

        Per COHORT-ADAPT-GARDENER spec Section 7 + the design review edit #4:
        the cohort needs a stable read surface that does NOT drain
        or otherwise touch coalescer state. The returned tuple is
        a copy of the buffered list at call time; callers can
        iterate without affecting subsequent ``drain()`` semantics.
        """
        return tuple(self._buffers.get(canvas_id, []))


# ---------------------------------------------------------------------------
# Evolution observation ledger (COHORT-ADAPT-GARDENER)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvolutionRecord:
    """In-process record of a gardener evolution / section consultation.

    Populated alongside ``consult_evolution`` and ``consult_section``
    when they return a non-None GardenerDecision. The cohort's
    snapshot surface reads the most-recent N records to populate
    its ``recent_evolution`` field.

    Per the design review's edit #4: this is a small ledger living inside
    GardenerService — not a replacement for the existing event
    stream. Consultations still emit canvas/gardener events
    through the existing ``_event_emit`` callback; this ledger is
    a per-canvas in-memory copy bounded by ``_evolution_ledger_max``.
    """

    decision_id: str  # synthetic, derived from canvas_id + occurred_at
    action: str
    confidence: str  # "low" | "medium" | "high"
    pattern: str
    affected_pages: tuple[str, ...]
    occurred_at: datetime
    consultation: str  # "evolution" | "section"


# ---------------------------------------------------------------------------
# GardenerSnapshot — the non-mutating read returned by current_observation_snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GardenerSnapshot:
    """Frozen view of gardener's current state for a canvas.

    Returned by ``GardenerService.current_observation_snapshot``.
    The cohort wraps these records into ``CohortOutput.output``
    after applying truncation + per-item visibility decisions.
    """

    canvas_id: str
    pending_proposals: tuple["PendingProposal", ...]
    recent_evolution: tuple[EvolutionRecord, ...]
    observation_age_seconds: int | None  # age of the newest observation in either list


# ---------------------------------------------------------------------------
# Pattern cache
# ---------------------------------------------------------------------------


@dataclass
class _CachedPattern:
    name: str
    body: str
    frontmatter: dict


class PatternCache:
    """In-memory cache of Workflow Patterns canvas pages.

    Populated lazily on first consultation and invalidated on canvas
    events whose canvas_id matches the Workflow Patterns canvas
    (spec Hazard A).
    """

    def __init__(self) -> None:
        self._patterns: dict[str, _CachedPattern] = {}
        self._loaded = False
        self._workflow_canvas_id: str = ""

    def set_workflow_canvas_id(self, canvas_id: str) -> None:
        self._workflow_canvas_id = canvas_id

    def invalidate_if_workflow(self, canvas_id: str) -> bool:
        if canvas_id and canvas_id == self._workflow_canvas_id:
            self._patterns.clear()
            self._loaded = False
            return True
        return False

    @property
    def loaded(self) -> bool:
        return self._loaded

    def put(self, name: str, body: str, frontmatter: dict | None = None) -> None:
        self._patterns[name] = _CachedPattern(
            name=name, body=body, frontmatter=dict(frontmatter or {}),
        )

    def get(self, name: str) -> _CachedPattern | None:
        return self._patterns.get(name)

    def all_summaries(self) -> list[dict]:
        """Structured pattern catalog for the initial-shape prompt."""
        return [
            {
                "name": p.name,
                "summary": _short_summary(p.body),
                "frontmatter": p.frontmatter,
            }
            for p in self._patterns.values()
            if p.name not in ("library-meta", "")
        ]

    def mark_loaded(self) -> None:
        self._loaded = True

    def cross_pattern_body(self) -> str:
        meta = self._patterns.get("library-meta")
        return meta.body if meta else ""


def _short_summary(body: str, max_chars: int = 600) -> str:
    """Cheap extractive summary: first paragraph after the heading."""
    lines = [l for l in body.splitlines() if l.strip()]
    out: list[str] = []
    for line in lines:
        if line.startswith("#"):
            continue
        out.append(line)
        if sum(len(x) for x in out) > max_chars:
            break
    return "\n".join(out)[:max_chars]


# ---------------------------------------------------------------------------
# GardenerService
# ---------------------------------------------------------------------------


class GardenerService:
    """Per-instance canvas shape authority. See module docstring."""

    def __init__(
        self,
        *,
        canvas_service: Any,
        instance_db: Any,
        reasoning_service: Any,
        coalesce_window: timedelta | None = None,
        event_emit: Callable[..., Any] | None = None,
    ) -> None:
        self._canvas = canvas_service
        self._db = instance_db
        self._reasoning = reasoning_service
        self._coalescer = ProposalCoalescer(
            window=coalesce_window or timedelta(minutes=DEFAULT_COALESCE_MINUTES),
        )
        self._patterns = PatternCache()
        #: Event-emit callback for Gardener-produced events
        #: (``canvas.reshaped``, ``gardener.proposal_batch``). Same signature
        #: as CanvasService's event_emit.
        self._event_emit = event_emit
        #: Track in-flight consultation tasks so tests can await them.
        self._in_flight: set[asyncio.Task] = set()
        #: Per-canvas evolution observation ledger (COHORT-ADAPT-GARDENER).
        #: In-process, capped — populated alongside consult_evolution /
        #: consult_section when they return non-None decisions. The cohort
        #: snapshot surface reads from here for ``recent_evolution``.
        self._evolution_ledger: dict[str, "collections.deque[EvolutionRecord]"] = {}
        self._evolution_ledger_max = 10

    # ---- Public API ------------------------------------------------------

    async def on_canvas_event(
        self, instance_id: str, event_type: str, payload: dict,
        *, member_id: str = "",
    ) -> None:
        """Event-stream consumer. Non-blocking; schedules background work.

        Swallows everything. The canvas pipeline called us; we mustn't
        propagate errors back into it.
        """
        try:
            if not isinstance(payload, dict):
                return
            # Ignore our own emissions (prevents cascades — spec Hazard B).
            if (payload.get("source") or "").lower() == GARDENER_SOURCE:
                return
            # Invalidate pattern cache if the Workflow Patterns canvas changed.
            cid = payload.get("canvas_id", "")
            if cid and self._patterns.invalidate_if_workflow(cid):
                logger.info("GARDENER_PATTERN_CACHE_INVALIDATED: canvas=%s", cid)

            # Schedule background dispatch — never block the caller.
            task = asyncio.create_task(
                self._dispatch(instance_id, event_type, payload),
                name=f"gardener_dispatch_{event_type}",
            )
            self._in_flight.add(task)
            task.add_done_callback(self._in_flight.discard)
        except Exception as exc:  # noqa: BLE001
            logger.debug("GARDENER_ON_EVENT_FAILED: %s", exc)

    async def consult_initial_shape(
        self, ctx: InitialShapeContext,
    ) -> GardenerDecision | None:
        """Pillar 3 entry — pick a pattern for a new canvas."""
        await self._ensure_patterns_loaded(ctx.instance_id)
        ctx.available_patterns = self._patterns.all_summaries()
        if not ctx.available_patterns:
            logger.info("GARDENER_NO_PATTERNS_AVAILABLE: canvas=%s", ctx.canvas_id)
            return None
        try:
            return await judge_initial_shape(
                ctx, reasoning_service=self._reasoning,
            )
        except GardenerExhausted as exc:
            logger.info("GARDENER_EXHAUSTED_INITIAL_SHAPE: %s", exc.reason)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("GARDENER_INITIAL_SHAPE_FAILED: %s", exc)
            return None

    async def apply_initial_shape(
        self,
        *,
        instance_id: str,
        canvas_id: str,
        canvas_name: str,
        scope: str,
        creator_member_id: str,
        intent: str = "",
        explicit_pattern: str = "",
    ) -> dict:
        """Orchestrate Pillar 3 — pick a pattern, instantiate pages, record it.

        Flow:
          1. If ``explicit_pattern`` is set, skip consultation and use it.
          2. Otherwise consult the LLM to pick a pattern from the catalog.
          3. If a pattern is picked, instantiate its "Initial canvas shape"
             pages and record ``pattern: <name>`` in canvas.yaml.
          4. If nothing matches (or consult fails), record
             ``pattern: unmatched`` so follow-on evolution judgments know
             no pattern-specific heuristics apply.

        Returns a dict with keys: ``pattern`` (name applied or ``unmatched``),
        ``pages_created`` (int), ``source`` (``explicit`` / ``gardener`` /
        ``fallback``). Always succeeds-or-logs; never raises — canvas
        creation already succeeded upstream; pattern application is a
        best-effort enrichment.
        """
        result = {"pattern": "unmatched", "pages_created": 0, "source": "fallback"}

        # 1. Explicit pattern bypass (escape hatch for known-pattern cases).
        if explicit_pattern:
            pattern_name = explicit_pattern.strip()
            await self._ensure_patterns_loaded(instance_id)
            if self._patterns.get(pattern_name) is None:
                logger.warning(
                    "GARDENER_EXPLICIT_PATTERN_UNKNOWN: pattern=%s", pattern_name,
                )
            else:
                pages = await self._instantiate_pattern(
                    instance_id=instance_id, canvas_id=canvas_id,
                    pattern_name=pattern_name,
                    creator_member_id=creator_member_id,
                )
                await self._canvas.set_canvas_pattern(
                    instance_id=instance_id, canvas_id=canvas_id,
                    pattern=pattern_name,
                )
                result.update({
                    "pattern": pattern_name,
                    "pages_created": pages,
                    "source": "explicit",
                })
                await self._emit(
                    instance_id, "canvas.pattern_applied",
                    {
                        "canvas_id": canvas_id,
                        "pattern": pattern_name,
                        "pages_created": pages,
                        "source": "explicit",
                    },
                    member_id=creator_member_id,
                )
                return result

        # 2. Consult the Gardener to pick.
        ctx = InitialShapeContext(
            instance_id=instance_id,
            canvas_id=canvas_id,
            canvas_name=canvas_name,
            scope=scope,
            creator_member_id=creator_member_id,
            intent=intent,
        )
        decision = await self.consult_initial_shape(ctx)
        picked: str = ""
        if decision and decision.action == "pick_pattern" and decision.pattern:
            picked = decision.pattern.strip()

        # 3. Apply if the pick matches a known pattern.
        if picked and self._patterns.get(picked) is not None:
            pages = await self._instantiate_pattern(
                instance_id=instance_id, canvas_id=canvas_id,
                pattern_name=picked,
                creator_member_id=creator_member_id,
            )
            await self._canvas.set_canvas_pattern(
                instance_id=instance_id, canvas_id=canvas_id, pattern=picked,
            )
            result.update({
                "pattern": picked,
                "pages_created": pages,
                "source": "gardener",
            })
            await self._emit(
                instance_id, "canvas.pattern_applied",
                {
                    "canvas_id": canvas_id, "pattern": picked,
                    "pages_created": pages, "source": "gardener",
                },
                member_id=creator_member_id,
            )
            return result

        # 4. Fallback — unmatched.
        await self._canvas.set_canvas_pattern(
            instance_id=instance_id, canvas_id=canvas_id, pattern="unmatched",
        )
        await self._emit(
            instance_id, "canvas.pattern_applied",
            {
                "canvas_id": canvas_id, "pattern": "unmatched",
                "pages_created": 0, "source": "fallback",
            },
            member_id=creator_member_id,
        )
        logger.info(
            "GARDENER_PATTERN_UNMATCHED: canvas=%s intent=%r",
            canvas_id, (intent or "")[:80],
        )
        return result

    async def _instantiate_pattern(
        self, *, instance_id: str, canvas_id: str, pattern_name: str,
        creator_member_id: str,
    ) -> int:
        """Create the pages a pattern declares in its Initial canvas shape.

        Pages are seeded empty except for a short boilerplate line derived
        from the pattern's description of that page. The member fills in
        content; Gardener does not generate substantive body content.
        Skips pages that already exist (idempotent safety).
        """
        from kernos.kernel.canvas import parse_initial_shape

        cached = self._patterns.get(pattern_name)
        if cached is None:
            return 0
        page_specs = parse_initial_shape(cached.body)
        if not page_specs:
            return 0

        pages_created = 0
        for spec in page_specs:
            path = spec.get("path", "")
            if not path:
                continue
            # Skip existing pages — pattern instantiation is idempotent.
            existing = await self._canvas.page_read(
                instance_id=instance_id, canvas_id=canvas_id, page_slug=path,
            )
            if existing.ok:
                continue
            body = self._scaffold_body_from_spec(spec)
            write_result = await self._canvas.page_write(
                instance_id=instance_id, canvas_id=canvas_id,
                page_slug=path, body=body,
                writer_member_id=creator_member_id,
                title=spec.get("path", path).split("/")[-1].rsplit(".", 1)[0].replace("-", " ").title(),
                page_type=spec.get("type", "note"),
                state="drafted",
            )
            if write_result.ok:
                pages_created += 1
        logger.info(
            "GARDENER_PATTERN_INSTANTIATED: canvas=%s pattern=%s pages=%d",
            canvas_id, pattern_name, pages_created,
        )
        return pages_created

    @staticmethod
    def _scaffold_body_from_spec(spec: dict) -> str:
        """Short boilerplate body from the pattern's page description."""
        title = spec.get("path", "").rsplit("/", 1)[-1].rsplit(".", 1)[0]
        description = spec.get("description", "").strip()
        lines = [f"# {title.replace('-', ' ').title()}"]
        if description:
            lines.append("")
            lines.append(f"_{description}_")
        lines.append("")
        return "\n".join(lines)

    async def consult_evolution(
        self, ctx: EvolutionContext,
    ) -> GardenerDecision | None:
        """Pillar 4 entry — run evolution heuristics on a canvas event."""
        await self._ensure_patterns_loaded(ctx.instance_id)
        ctx.cross_pattern_heuristics = self._patterns.cross_pattern_body()
        try:
            decision = await judge_evolution(
                ctx, reasoning_service=self._reasoning,
            )
        except GardenerExhausted as exc:
            logger.info("GARDENER_EXHAUSTED_EVOLUTION: %s", exc.reason)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("GARDENER_EVOLUTION_FAILED: %s", exc)
            return None
        if decision is not None:
            self._record_evolution(
                ctx.canvas_id, decision, consultation="evolution",
            )
        return decision

    async def consult_preference_extraction(
        self, ctx: PreferenceExtractionContext,
    ) -> PreferenceExtractionResult:
        """Pillar 5 entry — run preference-extraction consultation.

        Caller provides the member utterance + canvas pattern; the
        consultation returns a structured result the caller routes into
        the confirmation flow (see :meth:`capture_canvas_preference`
        tool in the reasoning dispatch).

        Returns ``PreferenceExtractionResult(matched=False)`` on any
        exception — never raises. Callers should gate surfacing on
        ``result.should_surface``, which composes the confirmation
        floor (high-confidence + wired effect_kind).
        """
        try:
            return await judge_preference_extraction(
                ctx, reasoning_service=self._reasoning,
            )
        except PreferenceExtractionExhausted as exc:
            logger.info("GARDENER_PREF_EXTRACTION_EXHAUSTED: %s", exc.reason)
            return PreferenceExtractionResult(matched=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("GARDENER_PREF_EXTRACTION_FAILED: %s", exc)
            return PreferenceExtractionResult(matched=False)

    async def consult_section(
        self, ctx: SectionContext,
    ) -> GardenerDecision | None:
        """Pillar 4 sub-entry — section-management judgment."""
        try:
            decision = await judge_section_management(
                ctx, reasoning_service=self._reasoning,
            )
        except GardenerExhausted as exc:
            logger.info("GARDENER_EXHAUSTED_SECTION: %s", exc.reason)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("GARDENER_SECTION_FAILED: %s", exc)
            return None
        if decision is not None:
            self._record_evolution(
                ctx.canvas_id, decision, consultation="section",
            )
        return decision

    @property
    def coalescer(self) -> ProposalCoalescer:
        return self._coalescer

    @property
    def patterns(self) -> PatternCache:
        return self._patterns

    # ---- COHORT-ADAPT-GARDENER read surface ------------------------------

    def _record_evolution(
        self,
        canvas_id: str,
        decision: GardenerDecision,
        *,
        consultation: str,
    ) -> None:
        """Append an EvolutionRecord to the per-canvas observation ledger.

        Internal — called by ``consult_evolution`` and ``consult_section``
        immediately after a non-None decision is produced. Tests may
        call this directly to seed the ledger without running real
        consultations.
        """
        if not canvas_id or decision is None:
            return
        occurred_at = datetime.now(timezone.utc)
        decision_id = f"{canvas_id}:{occurred_at.timestamp():.6f}"
        record = EvolutionRecord(
            decision_id=decision_id,
            action=decision.action,
            confidence=decision.confidence,
            pattern=decision.pattern or "",
            affected_pages=tuple(decision.affected_pages or ()),
            occurred_at=occurred_at,
            consultation=consultation,
        )
        if canvas_id not in self._evolution_ledger:
            self._evolution_ledger[canvas_id] = collections.deque(
                maxlen=self._evolution_ledger_max,
            )
        self._evolution_ledger[canvas_id].append(record)

    def current_observation_snapshot(
        self,
        *,
        instance_id: str,
        member_id: str,
        canvas_id: str,
    ) -> GardenerSnapshot:
        """Non-mutating read of gardener's current state for a canvas.

        Per COHORT-ADAPT-GARDENER spec Section 7 + the design review edit #4.
        Returns a frozen ``GardenerSnapshot``. Guarantees:

          - No LLM invocation. The method is a pure state read.
          - No mutation of the proposal coalescer or evolution ledger.
            ``snapshot_pending()`` returns a tuple-copy; the ledger
            read iterates without dequeing.
          - No event emission. The snapshot is purely observational.

        ``observation_age_seconds`` is the age of the newest record
        across both pending proposals and the evolution ledger, or
        ``None`` if both are empty. Useful for integration to gauge
        how stale the observation is.

        ``instance_id`` and ``member_id`` are accepted for future
        per-member scoping; v1 reads global per-canvas state. The
        cohort adapter's ``active_spaces`` resolution already
        confirms the active member's relationship to the canvas.
        """
        pending = self._coalescer.snapshot_pending(canvas_id)
        recent = tuple(self._evolution_ledger.get(canvas_id, ()))

        newest: datetime | None = None
        for p in pending:
            if newest is None or p.captured_at > newest:
                newest = p.captured_at
        for r in recent:
            if newest is None or r.occurred_at > newest:
                newest = r.occurred_at
        age: int | None = None
        if newest is not None:
            age = max(0, int((datetime.now(timezone.utc) - newest).total_seconds()))

        return GardenerSnapshot(
            canvas_id=canvas_id,
            pending_proposals=pending,
            recent_evolution=recent,
            observation_age_seconds=age,
        )

    async def wait_idle(self) -> None:
        """Test helper — wait for all in-flight dispatch tasks to complete."""
        while self._in_flight:
            await asyncio.gather(*list(self._in_flight), return_exceptions=True)

    # ---- Internal --------------------------------------------------------

    async def _dispatch(
        self, instance_id: str, event_type: str, payload: dict,
    ) -> None:
        """Route a canvas event through Pattern 00 cross-pattern heuristics.

        Pillar 4 implementation. Ships only the Pattern-00 heuristics
        that are tractable deterministically:
          - staleness (page last_updated older than STALENESS_DAYS)
          - split (3+ sections each exceeding SPLIT_SECTION_LINE_THRESHOLD)
          - scope-mismatch (basic check — member-authored personal page
            on a team canvas, or vice versa)

        Merge (40%+ content overlap) and back-reference promotion are
        deferred — they require cross-page analysis and a cross-page
        link-graph index that doesn't exist yet.

        Pattern-specific heuristics (spec Pillar 4 fallback) are
        NOT run in this batch. A follow-on batch composes them with
        Pattern 00's cross-pattern set.
        """
        if not isinstance(payload, dict):
            return
        if event_type not in (
            "canvas.page.created", "canvas.page.changed",
            "canvas.page.state_changed",
        ):
            return
        canvas_id = payload.get("canvas_id", "")
        page_path = payload.get("page_path", "")
        if not canvas_id or not page_path:
            return

        # Pull canvas context: canvas.yaml (for scope + gardener_consent),
        # the target page's frontmatter + body, and the cross-page index.
        try:
            canvas_row = await self._db.get_canvas(canvas_id)
        except Exception:
            canvas_row = None
        if not canvas_row:
            return
        canvas_scope = (canvas_row or {}).get("scope", "team")
        defaults = await self._load_canvas_yaml(instance_id, canvas_id)
        consent_mode = (defaults.get("gardener_consent") or "propose-all").strip()
        if consent_mode not in CONSENT_MODES:
            consent_mode = "propose-all"

        # CANVAS-GARDENER-PREFERENCE-CAPTURE: drop pending preferences that
        # expired without member response (24h TTL). Runs before heuristic
        # evaluation so current pending-state is accurate. Any drop mutates
        # canvas.yaml, so re-fetch defaults afterwards if needed.
        try:
            dropped = await self._canvas.drop_expired_pending_preferences(
                instance_id=instance_id, canvas_id=canvas_id,
            )
            if dropped:
                logger.info(
                    "GARDENER_PENDING_PREFS_EXPIRED: canvas=%s dropped=%d",
                    canvas_id, dropped,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("GARDENER_PREFS_TTL_FAILED: %s", exc)

        page_read = await self._canvas.page_read(
            instance_id=instance_id, canvas_id=canvas_id, page_slug=page_path,
        )
        if not page_read.ok:
            return
        page_fm = page_read.extra.get("frontmatter", {}) or {}
        page_body = page_read.extra.get("body", "") or ""

        # Run the Pattern 00 cross-pattern heuristics. Each returns an
        # Optional GardenerDecision with confidence; only HIGH confidence
        # surfaces. split/staleness/scope-mismatch are synchronous pure
        # checks; back-reference-promotion reads the canvas's reference
        # index and is async.
        proposals: list[GardenerDecision] = []
        for check in (
            self._heuristic_split,
            self._heuristic_staleness,
            self._heuristic_scope_mismatch,
        ):
            try:
                decision = check(
                    canvas_scope=canvas_scope,
                    page_path=page_path,
                    page_fm=page_fm,
                    page_body=page_body,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("GARDENER_HEURISTIC_FAILED: %s", exc)
                continue
            if decision is None:
                continue
            if not decision.surfaces:
                # Low/medium confidence logs for pattern tuning but
                # doesn't wake members. Still useful audit trail.
                logger.info(
                    "GARDENER_HEURISTIC_LOW_CONFIDENCE: canvas=%s page=%s "
                    "action=%s confidence=%s",
                    canvas_id, page_path, decision.action, decision.confidence,
                )
                continue
            proposals.append(decision)

        # Pattern 00 back-reference-promotion (CANVAS-CROSS-PAGE-INDEX smoke
        # activation). Page that has accumulated BACK_REFERENCE_PROMOTION_THRESHOLD+
        # inbound references → propose promotion to a link-hub/index page.
        back_ref_decision = await self._heuristic_back_reference_promotion(
            instance_id=instance_id,
            canvas_id=canvas_id,
            page_path=page_path,
        )
        if back_ref_decision is not None and back_ref_decision.surfaces:
            proposals.append(back_ref_decision)

        # Broken-reference findings (CANVAS-CROSS-PAGE-INDEX). Query the
        # index directly at dispatch time rather than relying on the
        # event payload — keeps the event shape unchanged and lets the
        # Gardener be the single source for broken-ref surfacing.
        broken_for_source = await self._collect_broken_references_for_page(
            instance_id=instance_id, canvas_id=canvas_id, page_path=page_path,
        )
        for br in broken_for_source:
            proposals.append(self._make_broken_reference_decision(
                page_path=page_path, broken=br,
            ))

        # Pattern-specific heuristics (CANVAS-GARDENER-PATTERN-HEURISTICS).
        # Runs only when the canvas declares a pattern. Adds to the same
        # ``proposals`` bucket so confidence floor + coalescing + consent
        # routing apply uniformly — Pattern 00 + pattern-declared use the
        # same surface infrastructure.
        pattern_name = (defaults.get("pattern") or "").strip()
        if pattern_name and pattern_name != "unmatched":
            await self._ensure_patterns_loaded(instance_id)
            pattern_proposals = await self._evaluate_pattern_heuristics(
                instance_id=instance_id,
                canvas_id=canvas_id,
                canvas_scope=canvas_scope,
                canvas_defaults=defaults,
                pattern_name=pattern_name,
                event_type=event_type,
                event_payload=payload,
                page_path=page_path,
                page_fm=page_fm,
                page_body=page_body,
            )
            for decision in pattern_proposals:
                if decision.surfaces:
                    proposals.append(decision)
                else:
                    logger.info(
                        "GARDENER_PATTERN_HEURISTIC_LOW_CONFIDENCE: "
                        "canvas=%s pattern=%s action=%s confidence=%s",
                        canvas_id, pattern_name, decision.action,
                        decision.confidence,
                    )

        if not proposals:
            return

        # Bucket proposals by consent mode: some auto-apply, some propose.
        for decision in proposals:
            should_auto = self._is_auto_apply(decision, consent_mode)
            if should_auto:
                await self._auto_apply(
                    instance_id=instance_id, canvas_id=canvas_id,
                    page_path=page_path, decision=decision,
                )
            else:
                self._coalescer.add(PendingProposal(
                    canvas_id=canvas_id,
                    action=decision.action,
                    confidence=decision.confidence,
                    rationale=decision.rationale,
                    affected_pages=decision.affected_pages or [page_path],
                    captured_at=datetime.now(timezone.utc),
                    payload=decision.payload,
                ))

    # ---- Heuristics (Pattern 00 cross-pattern) ---------------------------

    @staticmethod
    def _heuristic_split(
        *, canvas_scope: str, page_path: str, page_fm: dict, page_body: str,
    ) -> GardenerDecision | None:
        """3+ sections each exceeding ~80 lines → propose_split."""
        from kernos.kernel.canvas import parse_sections
        _, sections = parse_sections(page_body)
        big_sections = [
            s for s in sections
            if s.body.count("\n") >= SPLIT_SECTION_LINE_THRESHOLD
        ]
        if len(big_sections) < SPLIT_SECTION_COUNT_THRESHOLD:
            return None
        return GardenerDecision(
            action="propose_split",
            confidence="high",
            rationale=(
                f"{len(big_sections)} sections each exceed "
                f"{SPLIT_SECTION_LINE_THRESHOLD} lines — page could split."
            ),
            affected_pages=[page_path],
            payload={
                "section_slugs": [s.slug for s in big_sections],
                "line_threshold": SPLIT_SECTION_LINE_THRESHOLD,
            },
        )

    @staticmethod
    def _heuristic_staleness(
        *, canvas_scope: str, page_path: str, page_fm: dict, page_body: str,
    ) -> GardenerDecision | None:
        """Page last_updated older than STALENESS_DAYS → flag for review."""
        last = (page_fm.get("last_updated") or "").strip()
        if not last:
            return None
        try:
            when = datetime.fromisoformat(last.replace("Z", "+00:00"))
        except ValueError:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - when).days
        if age_days < STALENESS_DAYS:
            return None
        return GardenerDecision(
            action="flag_stale",
            confidence="high",
            rationale=(
                f"Page {page_path} last updated {age_days} days ago; "
                f"Pattern 00 staleness threshold is {STALENESS_DAYS} days."
            ),
            affected_pages=[page_path],
            payload={"age_days": age_days},
        )

    @staticmethod
    def _heuristic_scope_mismatch(
        *, canvas_scope: str, page_path: str, page_fm: dict, page_body: str,
    ) -> GardenerDecision | None:
        """Basic scope-mismatch check — personal scope declared on a team canvas.

        The frontmatter ``scope`` field on a page contradicts its canvas's
        scope. Narrow check: a page declaring ``scope: personal`` sitting
        inside a canvas with ``scope: team`` is the clearest mismatch and
        the easiest to flag; subtler detections (content cues, reference
        patterns) are deferred to future heuristics.
        """
        page_scope = (page_fm.get("scope") or "").strip().lower()
        if not page_scope or page_scope == canvas_scope:
            return None
        if canvas_scope == "team" and page_scope == "personal":
            return GardenerDecision(
                action="flag_scope_mismatch",
                confidence="high",
                rationale=(
                    f"Page {page_path} declares scope={page_scope!r} "
                    f"but canvas is scope={canvas_scope!r}."
                ),
                affected_pages=[page_path],
                payload={
                    "page_scope": page_scope, "canvas_scope": canvas_scope,
                },
            )
        return None

    # ---- Pattern 00 back-reference-promotion + broken-reference ----------

    async def _heuristic_back_reference_promotion(
        self, *, instance_id: str, canvas_id: str, page_path: str,
    ) -> GardenerDecision | None:
        """Page accumulates N+ inbound wiki-links → propose promotion.

        Uses the per-canvas ReferenceIndex. Pattern 00 cross-pattern
        heuristic — applies to every canvas regardless of declared
        pattern. Deterministic: count comparison only, no LLM.
        """
        if not page_path:
            return None
        try:
            index = await self._canvas.get_reference_index(
                instance_id=instance_id, canvas_id=canvas_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("GARDENER_BACK_REF_INDEX_READ_FAILED: %s", exc)
            return None
        inbound = index.count_inbound(page_path)
        if inbound < BACK_REFERENCE_PROMOTION_THRESHOLD:
            return None
        by_source = index.count_inbound_by_source(page_path)
        return GardenerDecision(
            action="propose_back_reference_index",
            confidence="high",
            rationale=(
                f"Page {page_path!r} has {inbound} inbound references from "
                f"{len(by_source)} source pages — candidate for promotion "
                f"to a link-hub or index page."
            ),
            affected_pages=[page_path] + sorted(by_source.keys())[:10],
            payload={
                "page_path": page_path,
                "inbound_count": inbound,
                "threshold": BACK_REFERENCE_PROMOTION_THRESHOLD,
                "sources": by_source,
            },
        )

    async def _collect_broken_references_for_page(
        self, *, instance_id: str, canvas_id: str, page_path: str,
    ) -> list[dict]:
        """Dict form of :class:`BrokenReference` for a single source page.

        Queries the canvas's ReferenceIndex + page list; filters to
        this source only so we don't re-surface every canvas-wide broken
        ref on every single write.
        """
        if not page_path:
            return []
        from kernos.kernel.canvas_reference_index import slug_from_path
        try:
            index = await self._canvas.get_reference_index(
                instance_id=instance_id, canvas_id=canvas_id,
            )
            pages = await self._canvas.page_list(
                instance_id=instance_id, canvas_id=canvas_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("GARDENER_BROKEN_REF_FETCH_FAILED: %s", exc)
            return []
        known = {p.get("path", "") for p in pages if p.get("path")}
        this_source = slug_from_path(page_path)
        broken = index.find_broken_references(known)
        return [
            {"source": b.source_slug, "target": b.target_slug,
             "count": b.count, "reason": b.reason}
            for b in broken if b.source_slug == this_source
        ]

    @staticmethod
    def _make_broken_reference_decision(
        *, page_path: str, broken: dict,
    ) -> GardenerDecision:
        """Emit a broken-reference finding with repair-grade provenance.

        Payload carries source page, target slug, count, and reason —
        "target_missing" in v1 since rename-vs-delete distinction needs
        a rename operation we don't ship yet. Spec's "when known"
        qualifier covers this.
        """
        target = broken.get("target", "")
        count = int(broken.get("count", 1))
        return GardenerDecision(
            action="flag_broken_reference",
            confidence="high",
            rationale=(
                f"Page {page_path!r} contains {count} broken "
                f"reference{'s' if count != 1 else ''} to [[{target}]] — "
                f"target page does not exist in this canvas."
            ),
            affected_pages=[page_path],
            payload={
                "source_page": broken.get("source", page_path),
                "target_slug": target,
                "count": count,
                "reason": broken.get("reason", "target_missing"),
            },
        )

    # ---- Pattern-specific heuristics (CANVAS-GARDENER-PATTERN-HEURISTICS) -

    async def _evaluate_pattern_heuristics(
        self,
        *,
        instance_id: str,
        canvas_id: str,
        canvas_scope: str,
        canvas_defaults: dict,
        pattern_name: str,
        event_type: str,
        event_payload: dict,
        page_path: str,
        page_fm: dict,
        page_body: str,
    ) -> list[GardenerDecision]:
        """Dispatch the declared pattern's heuristics against a canvas event.

        Pulls the pattern page from the PatternCache, parses its fenced
        YAML declarations, filters to active + trigger-matching entries,
        and evaluates deterministic heuristics inline. Semantic heuristics
        (when active) route through the existing Gardener consultation
        path — per spec, no new prompt infrastructure is introduced.

        Emits the upgrade-finding path (``flag_library_upgrade``) when the
        pattern page exists but carries no structured declaration block —
        seeded canvases lagging the library become visibly less capable
        rather than silently-Pattern-00-only.

        Always returns a list (possibly empty). Errors are logged and
        swallowed — a bad declaration must not break canvas ops.
        """
        decisions: list[GardenerDecision] = []
        cached = self._patterns.get(pattern_name)
        if cached is None:
            # Pattern named on canvas but not in the library cache — the
            # library may be missing pages. Surface as upgrade finding so
            # the operator investigates rather than silent downgrade.
            decisions.append(self._make_upgrade_finding_decision(
                canvas_id=canvas_id,
                pattern_name=pattern_name,
                reason=f"pattern {pattern_name!r} not found in Workflow Patterns canvas",
            ))
            return decisions

        declarations = parse_heuristic_declarations(cached.body)
        if not declarations:
            # Pattern page exists but has no structured block — pre-batch
            # pattern. Emit upgrade finding once per canvas event; the
            # coalescer dedupes repeated emissions across the window.
            decisions.append(self._make_upgrade_finding_decision(
                canvas_id=canvas_id,
                pattern_name=pattern_name,
                reason=(
                    f"pattern {pattern_name!r} in library has no structured "
                    "heuristics block; seeded before CANVAS-GARDENER-PATTERN-"
                    "HEURISTICS and reads as pre-batch. Edit the library page "
                    "or re-seed to gain pattern-specific dispatch."
                ),
            ))
            return decisions

        # Build the evaluation state once; handlers read from it.
        # Preferences pulled once per dispatch and passed into the evaluator
        # so suppressed_by_preference + threshold_preference gates apply
        # (CANVAS-GARDENER-PREFERENCE-CAPTURE).
        try:
            canvas_preferences = await self._canvas.get_preferences(
                instance_id=instance_id, canvas_id=canvas_id,
            )
        except Exception:
            canvas_preferences = {}

        eval_state = CanvasEvaluationState(
            canvas_id=canvas_id,
            page_index=await self._build_page_index(instance_id, canvas_id),
            page_path=page_path,
            page_body=page_body,
            page_frontmatter=page_fm,
            event_type=event_type,
            event_payload=event_payload,
            canvas_anchors=self._extract_canvas_anchors(canvas_defaults),
            preferences=canvas_preferences,
        )

        for decl in declarations:
            if not decl.is_active:
                continue
            if not trigger_matches_event(decl, event_type, event_payload):
                continue

            if decl.is_semantic:
                # Semantic declarations route through the existing Gardener
                # consultation path. Per spec, Pattern 01's two semantic
                # heuristics ship with status=disabled by default; this
                # branch fires only when a pattern author opts in.
                semantic_decision = await self._evaluate_semantic_declaration(
                    instance_id=instance_id, canvas_id=canvas_id,
                    pattern_name=pattern_name, decl=decl,
                    eval_state=eval_state,
                )
                if semantic_decision is not None:
                    decisions.append(semantic_decision)
                continue

            match = evaluate_declaration(decl, eval_state)
            if match is None or not match.fired:
                continue
            decisions.append(self._match_to_decision(decl, match))

        return decisions

    @staticmethod
    def _make_upgrade_finding_decision(
        *, canvas_id: str, pattern_name: str, reason: str,
    ) -> GardenerDecision:
        """Upgrade-finding surface for pre-batch pattern pages (acceptance criterion 11)."""
        return GardenerDecision(
            action="flag_library_upgrade",
            confidence="high",
            rationale=(
                f"Canvas uses pattern {pattern_name!r} but its library page "
                f"is missing structured heuristic declarations — falling back "
                f"to Pattern 00 cross-pattern rules only. {reason}"
            ),
            affected_pages=[],
            payload={
                "pattern": pattern_name,
                "canvas_id": canvas_id,
                "upgrade_required": True,
            },
        )

    @staticmethod
    def _match_to_decision(
        decl: HeuristicDecl, match: HeuristicMatch,
    ) -> GardenerDecision:
        """Convert a HeuristicMatch into a GardenerDecision for surfacing.

        The declaration's ``confidence`` vocabulary (``deterministic-high``
        / ``llm-judgment``) doesn't map 1:1 to GardenerDecision's
        ``confidence`` (``low`` / ``medium`` / ``high``). The mapping:

          - ``deterministic-high`` → ``high`` (threshold clearly met by
            construction — otherwise the deterministic check would have
            returned None before reaching here)
          - ``llm-judgment`` → inherits the LLM output's own confidence;
            if the match carries a raw ``low``/``medium``/``high``, pass
            through, else default to ``high`` for a fired semantic match

        Keeps the declaration-layer vocabulary separate from the internal
        surface-or-not gate so the two can evolve independently.
        """
        raw = match.confidence
        if raw == "deterministic-high":
            surface_conf = "high"
        elif raw in ("low", "medium", "high"):
            surface_conf = raw
        else:
            # llm-judgment without a clamped inner confidence — treat as
            # surfacing by default (the match fired, the LLM was decisive).
            surface_conf = "high"
        return GardenerDecision(
            action=decl.action.get("kind", "flag"),
            confidence=surface_conf,
            rationale=match.rationale,
            affected_pages=list(match.affected_pages),
            payload={
                "declaration_id": decl.id,
                "declared_confidence": raw,
                **(match.payload or {}),
            },
        )

    async def _build_page_index(
        self, instance_id: str, canvas_id: str,
    ) -> list[dict]:
        try:
            return await self._canvas.page_list(
                instance_id=instance_id, canvas_id=canvas_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("PATTERN_HEURISTICS_PAGE_INDEX_FAILED: %s", exc)
            return []

    @staticmethod
    def _extract_canvas_anchors(canvas_defaults: dict) -> dict:
        """Named date anchors on the canvas — used by date-relative-window.

        Pattern 05 (time-bounded-event) expects canvas.yaml to carry an
        ``event_date`` field; generic extraction pattern here so future
        patterns can add their own named anchors without code changes.
        """
        anchors: dict = {}
        for key in ("event_date", "launch_date", "anchor_date"):
            value = canvas_defaults.get(key)
            if value:
                anchors[key] = value
        return anchors

    async def _evaluate_semantic_declaration(
        self, *, instance_id: str, canvas_id: str, pattern_name: str,
        decl: HeuristicDecl, eval_state: CanvasEvaluationState,
    ) -> GardenerDecision | None:
        """Route a semantic declaration through the existing consultation path.

        Per spec acceptance criterion 5: no new prompt infrastructure.
        We build an EvolutionContext around the declaration + eval state
        and let ``consult_evolution`` produce a judgment. The declared
        action kind wins over whatever the LLM proposes; confidence is
        clamped by the declaration's declared ``llm-judgment`` level
        combined with the LLM's own confidence output.

        Pattern 01 ships its two semantic heuristics with
        ``status: disabled`` by default; this branch fires only when a
        library author has enabled one. Minimal-viable wiring — the
        MODEL-SELECTION-MODULE / future prompt-registry batch would
        harden this further.
        """
        try:
            ctx = EvolutionContext(
                instance_id=instance_id,
                canvas_id=canvas_id,
                canvas_pattern=pattern_name,
                event_type=eval_state.event_type,
                page_path=eval_state.page_path,
                page_summary=eval_state.page_body[:2000],
                canvas_pages_index=eval_state.page_index,
                cross_pattern_heuristics=self._patterns.cross_pattern_body(),
                pattern_heuristics=(
                    f"Heuristic id: {decl.id}\n"
                    f"Prompt key: {decl.signal.get('prompt_key')}\n"
                    f"Declared action on match: {decl.action.get('kind')}\n"
                    f"Inputs available: {sorted((decl.signal.get('inputs') or {}).keys())}"
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("GARDENER_SEMANTIC_CTX_BUILD_FAILED: %s", exc)
            return None

        llm_decision = await self.consult_evolution(ctx)
        if llm_decision is None or llm_decision.action == "none":
            return None
        # Clamp to the declaration's action kind — declaration authority
        # trumps the LLM's action selection (LLM provides the judgment,
        # not the taxonomy).
        return GardenerDecision(
            action=decl.action.get("kind", llm_decision.action),
            confidence=llm_decision.confidence,
            rationale=(
                f"[{decl.id}] {llm_decision.rationale or 'semantic heuristic fired'}"
            ),
            affected_pages=llm_decision.affected_pages or [eval_state.page_path],
            payload={
                "declaration_id": decl.id,
                "source": "semantic",
                **(llm_decision.payload or {}),
            },
        )

    # ---- Consent routing -------------------------------------------------

    @staticmethod
    def _is_auto_apply(decision: GardenerDecision, consent_mode: str) -> bool:
        """Decide whether a high-confidence decision auto-applies.

        v1 keeps split/merge proposal-class under every mode (the
        ``propose_split`` action is never auto even under ``auto-all``)
        because reorganizing page structure is the kind of change
        where human-in-the-loop serves the outcome better than speed.
        Summary regeneration is auto-eligible where declared.
        """
        if decision.action in ADVISORY_ACTIONS:
            # Advisory flags never auto — they're surface-only signals.
            return False
        if decision.action in NON_DESTRUCTIVE_ACTIONS:
            return consent_mode in ("auto-non-destructive", "auto-all")
        return False

    async def _auto_apply(
        self, *, instance_id: str, canvas_id: str, page_path: str,
        decision: GardenerDecision,
    ) -> None:
        """Execute a non-destructive action immediately + emit audit event."""
        # v1 ships auto-apply as a thin wrapper that emits the audit event.
        # The concrete on-disk mutation for summary regeneration ships with
        # the cohort-prompt round (Pillar 6 + follow-on). For this batch
        # the path is live so tests can verify the routing; the actual
        # in-place summary rewrite is a no-op placeholder until the
        # cohort LLM roundtrip is wired in a follow-on.
        logger.info(
            "GARDENER_AUTO_APPLY: canvas=%s page=%s action=%s",
            canvas_id, page_path, decision.action,
        )
        await self._emit(
            instance_id, "canvas.reshaped",
            {
                "canvas_id": canvas_id,
                "page_path": page_path,
                "action": decision.action,
                "confidence": decision.confidence,
                "rationale": decision.rationale,
                "applied": True,
            },
        )

    # ---- Proposal drain (coalesced surface) ------------------------------

    async def drain_proposals(
        self, *, canvas_id: str,
    ) -> list[PendingProposal]:
        """Drain buffered proposals for a canvas if the window has elapsed.

        Returns the drained list for the caller (typically the whisper-
        dispatch layer) to turn into a single coalesced whisper. Returns
        ``[]`` when the window hasn't elapsed yet.
        """
        if not self._coalescer.should_surface(canvas_id):
            return []
        return self._coalescer.drain(canvas_id)

    async def _load_canvas_yaml(self, instance_id: str, canvas_id: str) -> dict:
        """Best-effort read of canvas.yaml for canvas-level defaults."""
        try:
            return await self._canvas._canvas_defaults(instance_id, canvas_id)
        except Exception:
            return {}

    async def _ensure_patterns_loaded(self, instance_id: str) -> None:
        """Lazy-load the Workflow Patterns canvas into the pattern cache."""
        if self._patterns.loaded:
            return
        if self._db is None or self._canvas is None:
            return
        try:
            row = await self._db.find_canvas_by_name(
                name=WORKFLOW_PATTERNS_CANVAS_NAME, scope="team",
            )
            if not row:
                logger.info(
                    "GARDENER_PATTERNS_MISSING: Workflow Patterns canvas not seeded",
                )
                self._patterns.mark_loaded()  # avoid hot-looping the lookup
                return
            canvas_id = row["canvas_id"]
            self._patterns.set_workflow_canvas_id(canvas_id)

            pages = await self._canvas.page_list(
                instance_id=instance_id, canvas_id=canvas_id,
            )
            for page in pages:
                path = page.get("path", "")
                if not path or path == "index.md":
                    continue
                pr = await self._canvas.page_read(
                    instance_id=instance_id, canvas_id=canvas_id,
                    page_slug=path,
                )
                if not pr.ok:
                    continue
                fm = pr.extra.get("frontmatter", {}) or {}
                body = pr.extra.get("body", "") or ""
                name = fm.get("pattern") or path.rsplit(".", 1)[0]
                self._patterns.put(name, body, fm)
            self._patterns.mark_loaded()
            logger.info(
                "GARDENER_PATTERNS_LOADED: count=%d", len(self._patterns._patterns),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("GARDENER_PATTERN_LOAD_FAILED: %s", exc)
            self._patterns.mark_loaded()  # don't thrash the cache on repeated failures

    async def _emit(
        self, instance_id: str, event_type: str, payload: dict,
        *, member_id: str = "",
    ) -> None:
        """Emit an event tagged ``source: gardener`` so we skip our own events."""
        if self._event_emit is None:
            return
        try:
            tagged = dict(payload)
            tagged.setdefault("source", GARDENER_SOURCE)
            await self._event_emit(instance_id, event_type, tagged, member_id=member_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("GARDENER_EMIT_FAILED: %s %s", event_type, exc)
