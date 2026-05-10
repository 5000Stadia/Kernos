"""Per-space evidence bundles for the routing cohort.

ROUTER-EVIDENCE-V1 batches 2.1 + 2.2. Surfaces substrate-derived
orientation evidence to the router so its decision is informed by what
each space actually contains, not just its static description.

Layers populated here:
  * Layer 1 — recent activity tail (last K conv-log entries, capped).
  * Layer 2 — Living State + last N Ledger entries (capped).

Layer 3 (lexical anchor retrieval) is a separate follow-up batch and
lives in ``kernos/kernel/space_index.py`` once shipped.

The router prompt does not change shape based on whether evidence is
populated — empty fields render as omitted blocks. That keeps the
prompt builder simple and tolerant of partial substrate.

Failure mode: each per-space load is wrapped in try/except. A read
failure for one space logs and yields an empty ``SpaceEvidence`` for
that space; routing continues with whatever evidence loaded
successfully. Routing must NEVER block on evidence loading.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

from kernos.kernel.spaces import ContextSpace

logger = logging.getLogger(__name__)


# --- Caps (env-overridable) ---

def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("ROUTER_EVIDENCE: invalid %s=%r, using default %d", name, raw, default)
        return default


# Token budgets are applied via ``read_recent``'s budget param plus a
# final formatted-string truncation (see ``_truncate_to_token_cap``).
# The string cap is the load-bearing one because ``read_recent`` may
# return a single oversized entry that exceeds its budget.
RECENT_TAIL_K              = _env_int("KERNOS_ROUTER_EVIDENCE_RECENT_K", 5)
RECENT_TAIL_CAP_TOKENS     = _env_int("KERNOS_ROUTER_EVIDENCE_RECENT_CAP", 400)
RECENT_TAIL_CAP_WHEN_LIVING_STATE = _env_int(
    "KERNOS_ROUTER_EVIDENCE_RECENT_CAP_WITH_LIVING", 200,
)
LIVING_STATE_CAP_TOKENS    = _env_int("KERNOS_ROUTER_EVIDENCE_LIVING_CAP", 500)
LEDGER_TAIL_CAP_TOKENS     = _env_int("KERNOS_ROUTER_EVIDENCE_LEDGER_CAP", 200)
LEDGER_TAIL_ENTRIES        = _env_int("KERNOS_ROUTER_EVIDENCE_LEDGER_N", 3)
GLOBAL_BUNDLE_CEILING      = _env_int("KERNOS_ROUTER_EVIDENCE_GLOBAL_CEILING", 8000)

# Layer feature flags — disable wholesale to roll back without code revert.
LAYER1_ENABLED = os.environ.get("KERNOS_ROUTER_EVIDENCE_LAYER1", "1") != "0"
LAYER2_ENABLED = os.environ.get("KERNOS_ROUTER_EVIDENCE_LAYER2", "1") != "0"


# --- Dataclass ---

@dataclass(frozen=True)
class SpaceEvidence:
    """Per-space evidence bundle delivered to the router prompt."""
    space_id: str
    recent_tail: str = ""        # Layer 1, capped + formatted
    living_state: str = ""       # Layer 2, capped
    ledger_tail: str = ""        # Layer 2, capped (joined entries)
    truncated: bool = False      # any of the above hit a cap


# --- Token estimator ---

def _estimate_tokens(text: str) -> int:
    """Conservative token estimate (1 token ≈ 4 chars). Mirrors the
    estimator used in ``conversation_log.py``."""
    return len(text) // 4 if text else 0


def _truncate_to_token_cap(text: str, cap_tokens: int) -> tuple[str, bool]:
    """Truncate ``text`` so its estimated token count fits ``cap_tokens``.

    Returns ``(truncated_text, was_truncated)``. Truncation drops from
    the front so the most recent material survives — the recent-tail
    block reads chronologically, so the head is the oldest content.
    """
    if cap_tokens <= 0 or not text:
        return text, False
    char_cap = cap_tokens * 4
    if len(text) <= char_cap:
        return text, False
    return text[-char_cap:], True


# --- Layer 1: recent activity tail ---

async def _build_recent_tail(
    conv_logger,
    instance_id: str,
    space_id: str,
    member_id: str,
    cap_tokens: int,
) -> tuple[str, bool]:
    """Load the last few conv-log entries via ``read_recent`` and render
    them as a compact prompt block. Returns ``(text, truncated)``.
    """
    if cap_tokens <= 0:
        return "", False
    try:
        entries = await conv_logger.read_recent(
            instance_id,
            space_id,
            token_budget=cap_tokens,
            max_messages=RECENT_TAIL_K,
            member_id=member_id,
        )
    except Exception as exc:
        logger.warning(
            "ROUTER_EVIDENCE: read_recent failed space=%s member=%s: %s",
            space_id, member_id or "legacy", exc,
        )
        return "", False
    if not entries:
        return "", False

    lines: list[str] = []
    for e in entries:
        ts = e.get("timestamp", "") or ""
        role = e.get("role", "") or ""
        content = (e.get("content", "") or "").replace("\n", " ").strip()
        if len(content) > 200:
            content = content[:200] + "…"
        if ts:
            lines.append(f"[{ts}] ({role}): {content}")
        else:
            lines.append(f"({role}): {content}")
    text = "\n".join(lines)
    return _truncate_to_token_cap(text, cap_tokens)


# --- Layer 2: compacted summaries ---

async def _build_compacted_summary(
    compaction,
    instance_id: str,
    space_id: str,
    member_id: str,
) -> tuple[str, str, bool]:
    """Load Living State + last N ledger entries. Returns
    ``(living_state, ledger_tail, truncated)``.
    """
    living_state = ""
    ledger_tail = ""
    truncated = False

    try:
        living_state = await compaction.load_living_state(
            instance_id, space_id, member_id=member_id,
        )
    except Exception as exc:
        logger.warning(
            "ROUTER_EVIDENCE: load_living_state failed space=%s member=%s: %s",
            space_id, member_id or "legacy", exc,
        )
    if living_state:
        living_state, t = _truncate_to_token_cap(living_state, LIVING_STATE_CAP_TOKENS)
        truncated = truncated or t

    try:
        entries = await compaction.load_recent_ledger_entries(
            instance_id, space_id, member_id=member_id, n=LEDGER_TAIL_ENTRIES,
        )
    except Exception as exc:
        logger.warning(
            "ROUTER_EVIDENCE: load_recent_ledger_entries failed space=%s member=%s: %s",
            space_id, member_id or "legacy", exc,
        )
        entries = []
    if entries:
        joined = "\n\n".join(entries)
        ledger_tail, t = _truncate_to_token_cap(joined, LEDGER_TAIL_CAP_TOKENS)
        truncated = truncated or t

    return living_state, ledger_tail, truncated


# --- Per-space orchestrator ---

async def _build_for_space(
    *,
    conv_logger,
    compaction,
    instance_id: str,
    space: ContextSpace,
    member_id: str,
) -> SpaceEvidence:
    """Build a single-space evidence bundle. Fail-open per layer.

    Cap interplay: when a Living State exists for this space, the
    recent-tail cap shrinks so the per-space bundle stays bounded
    without forcing a global-ceiling fallback. (Codex review v2
    finding 3: Living State carries authoritative current-truth;
    recent-tail is mainly freshness/conversational tone.)
    """
    living_state = ""
    ledger_tail = ""
    layer2_truncated = False

    if LAYER2_ENABLED:
        try:
            living_state, ledger_tail, layer2_truncated = await _build_compacted_summary(
                compaction, instance_id, space.id, member_id,
            )
        except Exception as exc:  # pragma: no cover — defense in depth
            logger.warning(
                "ROUTER_EVIDENCE: layer2 build failed space=%s: %s",
                space.id, exc,
            )

    recent_cap = (
        RECENT_TAIL_CAP_WHEN_LIVING_STATE if living_state
        else RECENT_TAIL_CAP_TOKENS
    )
    recent_tail = ""
    layer1_truncated = False
    if LAYER1_ENABLED:
        try:
            recent_tail, layer1_truncated = await _build_recent_tail(
                conv_logger, instance_id, space.id, member_id, recent_cap,
            )
        except Exception as exc:  # pragma: no cover — defense in depth
            logger.warning(
                "ROUTER_EVIDENCE: layer1 build failed space=%s: %s",
                space.id, exc,
            )

    return SpaceEvidence(
        space_id=space.id,
        recent_tail=recent_tail,
        living_state=living_state,
        ledger_tail=ledger_tail,
        truncated=layer1_truncated or layer2_truncated,
    )


# --- Global ceiling: slot-reservation fallback ---

_TOKEN_RX = re.compile(r"\w+")


def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    return [t.lower() for t in _TOKEN_RX.findall(text) if len(t) > 1]


def _evidence_overlap_score(
    evidence: SpaceEvidence, message_tokens: set[str],
) -> int:
    """Score a SpaceEvidence by token-overlap with the new message.

    Used only when the global bundle ceiling is exceeded — ranks the
    non-current-focus candidates so the most evidence-relevant ones
    survive. Description text is intentionally NOT considered: the
    whole point of Layer 2 is that descriptions can be stale or
    generic. (Codex review v2 finding 4 / v1 finding 2.)
    """
    if not message_tokens:
        return 0
    haystack = " ".join((evidence.recent_tail, evidence.living_state, evidence.ledger_tail))
    if not haystack:
        return 0
    haystack_tokens = _tokenize(haystack)
    if not haystack_tokens:
        return 0
    score = 0
    counts: dict[str, int] = {}
    for tok in haystack_tokens:
        counts[tok] = counts.get(tok, 0) + 1
    for q in message_tokens:
        score += counts.get(q, 0)
    return score


def _bundle_token_estimate(bundles: dict[str, SpaceEvidence]) -> int:
    total = 0
    for ev in bundles.values():
        total += _estimate_tokens(ev.recent_tail)
        total += _estimate_tokens(ev.living_state)
        total += _estimate_tokens(ev.ledger_tail)
    return total


def _apply_global_ceiling(
    bundles: dict[str, SpaceEvidence],
    *,
    current_focus_id: str,
    message_content: str,
    ceiling: int,
) -> dict[str, SpaceEvidence]:
    """Drop low-evidence spaces if the aggregate exceeds ``ceiling``.

    Slot-reservation per Codex review v2 finding 4: ``current_focus_id``
    is always retained so the active conversation cannot be dropped.
    Remaining slots are kept in evidence-overlap rank order until the
    ceiling fits or only the current focus remains.
    """
    if _bundle_token_estimate(bundles) <= ceiling:
        return bundles
    msg_tokens = set(_tokenize(message_content))

    # Reserved: current focus (always present if it has a bundle).
    reserved: dict[str, SpaceEvidence] = {}
    pool: list[tuple[int, str, SpaceEvidence]] = []
    for sid, ev in bundles.items():
        if sid == current_focus_id:
            reserved[sid] = ev
        else:
            pool.append((_evidence_overlap_score(ev, msg_tokens), sid, ev))

    # Highest evidence first; ties broken by space_id for determinism.
    pool.sort(key=lambda t: (-t[0], t[1]))

    out = dict(reserved)
    for _score, sid, ev in pool:
        out[sid] = ev
        if _bundle_token_estimate(out) > ceiling:
            del out[sid]
            break
    return out


# --- Public entry point ---

async def build_space_evidence(
    *,
    conv_logger,
    compaction,
    instance_id: str,
    member_id: str,
    candidates: list[ContextSpace],
    message_content: str = "",
    current_focus_id: str = "",
) -> dict[str, SpaceEvidence]:
    """Build per-space evidence bundles for the router cohort.

    ``conv_logger`` and ``compaction`` are injected (typically the
    handler's instances) so this module stays unit-testable.

    Per-space loading is fail-open: an exception for one space yields
    an empty ``SpaceEvidence`` for that space and never aborts the
    routing call. After loading, an aggregate-token ceiling check may
    drop low-evidence non-focus spaces.
    """
    if not candidates:
        return {}

    bundles: dict[str, SpaceEvidence] = {}
    for space in candidates:
        try:
            bundles[space.id] = await _build_for_space(
                conv_logger=conv_logger,
                compaction=compaction,
                instance_id=instance_id,
                space=space,
                member_id=member_id,
            )
        except Exception as exc:
            logger.warning(
                "ROUTER_EVIDENCE: per-space build failed space=%s: %s",
                space.id, exc,
            )
            bundles[space.id] = SpaceEvidence(space_id=space.id)

    return _apply_global_ceiling(
        bundles,
        current_focus_id=current_focus_id,
        message_content=message_content,
        ceiling=GLOBAL_BUNDLE_CEILING,
    )
