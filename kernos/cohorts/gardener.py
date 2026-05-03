"""Gardener cohort — canvas shape judgment (CANVAS-SECTION-MARKERS + GARDENER).

A bounded cohort responsible for canvas shape decisions only. Not a
general-purpose reasoning agent; its judgment space is explicitly
constrained to:

1. Initial-shape picking at canvas creation (consults the Workflow
   Patterns library; matches member intent to a pattern).
2. Continuous evolution (fires pattern-declared + Pattern 00 cross-pattern
   heuristics on canvas.page.* events; surfaces reshape proposals
   bounded by confidence threshold + coalescing window).
3. Section management (splits oversized sections, regenerates drifted
   summaries) — a sub-kind of evolution judgment.

Discipline (per the design review design-review round):
  - Only pattern-declared heuristics plus Pattern 00's cross-pattern set
    — no inventing heuristics.
  - High-confidence matches only — low/medium log for pattern tuning
    without surfacing.
  - Non-destructive — content is moved/split/merged/summarized, never
    discarded.
  - Does not block member-facing turns — runs asynchronously.

Follows the Messenger cohort pattern: pure functions on dependency-
injected ``reasoning_service``, no LLM client imports at module load.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Output schemas
# ---------------------------------------------------------------------------

#: Possible judgment outcomes. ``none`` means "no action"; any other
#: outcome surfaces either as a proposal or an auto-apply depending on
#: the canvas's ``gardener_consent`` setting.
JudgmentAction = Literal[
    "none",             # no action
    "pick_pattern",     # Pillar 3 — initial shape: instantiate this pattern
    "promote_to_index", # Pillar 4 — 3+ back-references
    "propose_merge",    # Pillar 4 — 40%+ overlap with another page
    "propose_split",    # Pillar 4 — 3+ sections each >80 lines
    "regenerate_summary", # Pillar 4 — section summary drifted from body
    "flag_scope_mismatch", # Pillar 4 — Pattern 00 cross-pattern heuristic
    "flag_stale",       # Pillar 4 — pattern staleness threshold exceeded
]

#: Confidence levels. Only ``high`` surfaces as a proposal; the rest log
#: for pattern-tuning audit without waking members.
Confidence = Literal["low", "medium", "high"]


GARDENER_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "none", "pick_pattern", "promote_to_index", "propose_merge",
                "propose_split", "regenerate_summary", "flag_scope_mismatch",
                "flag_stale",
            ],
        },
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "rationale": {"type": "string"},
        "pattern": {"type": "string"},
        "affected_pages": {
            "type": "array",
            "items": {"type": "string"},
        },
        "payload": {"type": "object"},
    },
    "required": ["action", "confidence"],
    "additionalProperties": True,
}


# ---------------------------------------------------------------------------
# Contexts
# ---------------------------------------------------------------------------


@dataclass
class InitialShapeContext:
    """Context for initial-shape judgment at canvas_create time (Pillar 3)."""
    instance_id: str
    canvas_id: str
    canvas_name: str
    scope: str
    creator_member_id: str
    intent: str                              # natural-language description
    available_patterns: list[dict] = field(default_factory=list)
    # Each pattern dict: {name, dials, domain_cues, initial_pages, summary}


@dataclass
class EvolutionContext:
    """Context for continuous-evolution judgment on a canvas event (Pillar 4)."""
    instance_id: str
    canvas_id: str
    canvas_pattern: str                      # from canvas.yaml; "unmatched" ok
    event_type: str                          # canvas.page.created etc.
    page_path: str
    page_summary: str                        # body in summary-view form when long
    canvas_pages_index: list[dict]           # {path, title, type, state, last_updated}
    cross_pattern_heuristics: str            # Pattern 00 rules (loaded once)
    pattern_heuristics: str = ""             # pattern-specific rules if applicable


@dataclass
class SectionContext:
    """Context for section-management judgment on a page (Pillar 4)."""
    instance_id: str
    canvas_id: str
    page_path: str
    section_slug: str
    section_heading: str
    section_body: str
    current_marker_summary: str
    current_marker_tokens: int


@dataclass
class GardenerDecision:
    """Structured output of a single Gardener consultation."""
    action: str
    confidence: str                          # low | medium | high
    rationale: str = ""
    pattern: str = ""
    affected_pages: list[str] = field(default_factory=list)
    payload: dict = field(default_factory=dict)

    @property
    def surfaces(self) -> bool:
        """High-confidence non-``none`` actions surface as proposals."""
        return self.action != "none" and self.confidence == "high"


class GardenerExhausted(Exception):
    """Raised when the lightweight chain can't serve a Gardener consultation.

    Mirrors ``MessengerExhausted`` semantics — the caller swallows this
    and treats the consultation as a no-op rather than propagating.
    """
    def __init__(self, *, chain_name: str = "lightweight", reason: str = "") -> None:
        super().__init__(f"Gardener exhausted on chain={chain_name}: {reason}")
        self.chain_name = chain_name
        self.reason = reason


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _parse_decision(raw: str) -> GardenerDecision | None:
    """Parse constrained-decoding output into a GardenerDecision.

    Defensive: unknown outcomes, empty strings, parse errors all degrade
    to ``None`` (no action). Keeps the Gardener safe by default.
    """
    if not raw or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("GARDENER_PARSE_FAILED: raw=%r", raw[:200])
        return None
    if not isinstance(data, dict):
        return None

    action = (data.get("action") or "").strip().lower()
    confidence = (data.get("confidence") or "").strip().lower()
    if action == "none":
        return GardenerDecision(action="none", confidence="low")
    if confidence not in ("low", "medium", "high"):
        confidence = "low"
    return GardenerDecision(
        action=action,
        confidence=confidence,
        rationale=(data.get("rationale") or "").strip(),
        pattern=(data.get("pattern") or "").strip(),
        affected_pages=list(data.get("affected_pages") or []),
        payload=dict(data.get("payload") or {}),
    )


# ---------------------------------------------------------------------------
# Entry points — consultations
# ---------------------------------------------------------------------------


async def judge_initial_shape(
    ctx: InitialShapeContext,
    *,
    reasoning_service: Any,
    max_tokens: int = 1024,
) -> GardenerDecision | None:
    """Pick an initial pattern for a newly-created canvas (Pillar 3).

    Reads the member's intent + the available-patterns catalog and
    produces a ``pick_pattern`` decision naming one of the catalog entries
    (or ``none`` if nothing matches cleanly — the caller falls back to a
    minimal canvas flagged ``pattern: unmatched``).
    """
    from kernos.cohorts.gardener_prompts import build_initial_shape_prompt
    system_prompt, user_content = build_initial_shape_prompt(ctx)
    try:
        raw = await reasoning_service.complete_simple(
            system_prompt=system_prompt,
            user_content=user_content,
            chain="lightweight",
            output_schema=GARDENER_OUTPUT_SCHEMA,
            max_tokens=max_tokens,
        )
    except Exception as exc:
        raise GardenerExhausted(chain_name="lightweight", reason=str(exc)[:200]) from exc
    return _parse_decision(raw)


async def judge_evolution(
    ctx: EvolutionContext,
    *,
    reasoning_service: Any,
    max_tokens: int = 768,
) -> GardenerDecision | None:
    """Run evolution heuristics on a canvas event (Pillar 4).

    Returns a GardenerDecision with confidence + action, or ``None`` on
    parse failure. The caller uses ``decision.surfaces`` to decide
    whether to buffer the proposal for coalesced delivery.
    """
    from kernos.cohorts.gardener_prompts import build_evolution_prompt
    system_prompt, user_content = build_evolution_prompt(ctx)
    try:
        raw = await reasoning_service.complete_simple(
            system_prompt=system_prompt,
            user_content=user_content,
            chain="lightweight",
            output_schema=GARDENER_OUTPUT_SCHEMA,
            max_tokens=max_tokens,
        )
    except Exception as exc:
        raise GardenerExhausted(chain_name="lightweight", reason=str(exc)[:200]) from exc
    return _parse_decision(raw)


#: Phrases that bypass the cheap-chain pre-filter and route directly to
#: full extraction (still subject-matter-validated — the design review revision #1).
#: Surface-level string match, not semantic. Case-insensitive.
EXPLICIT_PREFERENCE_PHRASES: tuple[str, ...] = (
    "remember that",
    "from now on",
    "always",
    "never",
    "don't let me",
    "keep ",            # keep [X] private
    "this canvas is ",  # this canvas is [category]
)


#: ``preferences.<name>:`` tokens in a pattern's Member-intent-hook prose.
#: Used by the extraction tool to populate
#: :attr:`PreferenceExtractionContext.known_intent_hook_names`.
_INTENT_HOOK_NAME_RE = __import__("re").compile(r"preferences\.([\w-]+)\s*:")
_INTENT_HOOK_SECTION_RE = __import__("re").compile(
    r"##\s+Member intent hooks\s*\n(.*?)(?=\n##\s+|\Z)",
    __import__("re").DOTALL | __import__("re").IGNORECASE,
)


def extract_intent_hook_names(pattern_body: str) -> list[str]:
    """Parse ``preferences.<name>:`` tokens from a pattern's
    ``## Member intent hooks`` section.

    The pattern-library convention writes intent hooks as bullets:
    ``"Track what's shipped" → `preferences.manifest-routing: operator-on-change` — ...``.
    Every ``preferences.<name>:`` occurrence in the section is harvested
    as a known preference vocabulary entry. Returns an empty list when
    the section is absent.
    """
    if not pattern_body:
        return []
    section_match = _INTENT_HOOK_SECTION_RE.search(pattern_body)
    if not section_match:
        return []
    seen: list[str] = []
    for m in _INTENT_HOOK_NAME_RE.finditer(section_match.group(1)):
        name = m.group(1)
        if name and name not in seen:
            seen.append(name)
    return seen


def detect_explicit_phrases(utterance: str) -> bool:
    """Return True when the utterance carries a preference-explicit phrase.

    Surface-level only — no LLM. Callers use this to short-circuit the
    cheap-chain pre-filter: an explicit-phrase utterance goes straight to
    full extraction. Full extraction still performs subject-matter
    validation, so a covenant-shaped utterance with an explicit phrase
    (e.g. "remember that I always share my thought process") still lands
    with covenants, not preferences.
    """
    if not utterance:
        return False
    lower = utterance.lower()
    return any(phrase in lower for phrase in EXPLICIT_PREFERENCE_PHRASES)


#: Effect kinds wired in v1. the design review revision #2: if the LLM returns any
#: other ``effect_kind``, the extraction is forced ``matched=false`` so
#: no confirmation whisper surfaces for a preference that wouldn't do
#: anything. Follow-on batches extend this set as new effects wire.
WIRED_EFFECT_KINDS = {"suppression", "threshold"}


PREFERENCE_EXTRACTION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "matched": {"type": "boolean"},
        "preference_name": {"type": "string"},
        "preference_value": {},
        "evidence": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "supersedes": {"type": ["string", "null"]},
        "effect_kind": {
            "type": "string",
            "enum": ["suppression", "threshold", "other"],
        },
    },
    "required": ["matched", "confidence"],
    "additionalProperties": True,
}


@dataclass
class PreferenceExtractionContext:
    """Context passed into :func:`judge_preference_extraction`."""
    instance_id: str
    canvas_id: str
    canvas_pattern: str
    utterance: str
    #: Pattern-declared intent-hook names the extraction treats as the
    #: accepted preference vocabulary. Preferences whose name isn't in
    #: this set have their confidence downgraded one tier (high→medium→low).
    known_intent_hook_names: list[str] = field(default_factory=list)
    #: Current confirmed preferences on the canvas, used to detect
    #: supersession (new value replacing an existing one).
    current_preferences: dict = field(default_factory=dict)
    #: Names previously declined on this canvas — extraction should avoid
    #: re-offering these within a short window, but v1 leaves the declined
    #: check to the caller (consult_preference_extraction filters).
    declined_preference_names: list[str] = field(default_factory=list)


@dataclass
class PreferenceExtractionResult:
    """Parsed + validated output of a preference-extraction consultation."""
    matched: bool
    preference_name: str = ""
    preference_value: Any = None
    evidence: str = ""
    confidence: str = "low"            # low | medium | high
    supersedes: str | None = None
    effect_kind: str = ""              # suppression | threshold | "" when matched=false

    @property
    def should_surface(self) -> bool:
        """High-confidence, matched, effect kind is wired — fit for confirmation."""
        return self.matched and self.confidence == "high" and self.effect_kind in WIRED_EFFECT_KINDS


class PreferenceExtractionExhausted(Exception):
    """Raised when the lightweight chain can't serve the extraction call."""
    def __init__(self, *, chain_name: str = "lightweight", reason: str = "") -> None:
        super().__init__(f"PreferenceExtraction exhausted on chain={chain_name}: {reason}")
        self.chain_name = chain_name
        self.reason = reason


def _parse_preference_extraction(
    raw: str, ctx: PreferenceExtractionContext,
) -> PreferenceExtractionResult:
    """Parse LLM output + apply subject-matter validation + novel downgrade.

    the design review revision #2 enforcement: effect_kind must be in WIRED_EFFECT_KINDS
    or the result is forced matched=false. The extraction layer never
    surfaces a preference whose effect isn't wired — no confirmation
    whisper fires for a preference that wouldn't do anything.

    Novel-preference handling: if the extracted preference_name isn't in
    the pattern's declared intent-hook names, confidence downgrades one
    tier (high → medium → low). Prevents Gardener-invented vocabulary
    from reaching member confirmation.
    """
    if not raw or not raw.strip():
        return PreferenceExtractionResult(matched=False)
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("PREF_EXTRACTION_PARSE_FAILED: raw=%r", raw[:200])
        return PreferenceExtractionResult(matched=False)
    if not isinstance(data, dict):
        return PreferenceExtractionResult(matched=False)

    matched = bool(data.get("matched", False))
    if not matched:
        return PreferenceExtractionResult(matched=False)

    effect_kind = (data.get("effect_kind") or "").strip().lower()
    # the design review revision #2: force matched=false when effect isn't wired in v1.
    if effect_kind not in WIRED_EFFECT_KINDS:
        logger.info(
            "PREF_EXTRACTION_EFFECT_NOT_WIRED: effect_kind=%r — rejecting as v1 no-op",
            effect_kind,
        )
        return PreferenceExtractionResult(matched=False)

    confidence = (data.get("confidence") or "").strip().lower()
    if confidence not in ("low", "medium", "high"):
        confidence = "low"

    preference_name = (data.get("preference_name") or "").strip()
    # Novel-preference downgrade: extraction matched a name the pattern
    # doesn't declare in its intent-hook vocabulary.
    if preference_name and ctx.known_intent_hook_names:
        known = {h.strip() for h in ctx.known_intent_hook_names if h}
        if preference_name not in known:
            confidence = {"high": "medium", "medium": "low", "low": "low"}[confidence]

    supersedes_raw = data.get("supersedes")
    supersedes = supersedes_raw if isinstance(supersedes_raw, str) and supersedes_raw else None

    return PreferenceExtractionResult(
        matched=True,
        preference_name=preference_name,
        preference_value=data.get("preference_value"),
        evidence=(data.get("evidence") or "").strip(),
        confidence=confidence,
        supersedes=supersedes,
        effect_kind=effect_kind,
    )


async def judge_preference_extraction(
    ctx: PreferenceExtractionContext,
    *,
    reasoning_service: Any,
    max_tokens: int = 512,
) -> PreferenceExtractionResult:
    """Run preference extraction on one member utterance.

    Returns a PreferenceExtractionResult. Caller decides whether to
    surface based on ``result.should_surface``. Raises
    PreferenceExtractionExhausted on cheap-chain failure — caller
    should catch and treat as no-op.
    """
    from kernos.cohorts.gardener_prompts import build_preference_extraction_prompt
    system_prompt, user_content = build_preference_extraction_prompt(ctx)
    try:
        raw = await reasoning_service.complete_simple(
            system_prompt=system_prompt,
            user_content=user_content,
            chain="lightweight",
            output_schema=PREFERENCE_EXTRACTION_SCHEMA,
            max_tokens=max_tokens,
        )
    except Exception as exc:
        raise PreferenceExtractionExhausted(
            chain_name="lightweight", reason=str(exc)[:200],
        ) from exc
    return _parse_preference_extraction(raw, ctx)


async def judge_section_management(
    ctx: SectionContext,
    *,
    reasoning_service: Any,
    max_tokens: int = 512,
) -> GardenerDecision | None:
    """Produce a section-management action (Pillar 4 sub-judgment).

    Currently used for: summary regeneration on marker-body drift,
    split proposals on oversized sections.
    """
    from kernos.cohorts.gardener_prompts import build_section_prompt
    system_prompt, user_content = build_section_prompt(ctx)
    try:
        raw = await reasoning_service.complete_simple(
            system_prompt=system_prompt,
            user_content=user_content,
            chain="lightweight",
            output_schema=GARDENER_OUTPUT_SCHEMA,
            max_tokens=max_tokens,
        )
    except Exception as exc:
        raise GardenerExhausted(chain_name="lightweight", reason=str(exc)[:200]) from exc
    return _parse_decision(raw)
