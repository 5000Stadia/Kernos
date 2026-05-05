"""Auto-induction by signal-matching — REFERENCE-PRIMITIVE-V1 C6.

The integration layer assembles next-turn briefings; it includes a
mechanical signal-matching pass against catalog metadata. Catalog
entries whose ``one_line + section_title + category`` overlap with
the integration-layer signals (current conversation topic, active
workflow, surfacer hints) become candidates for auto-induction.

Mechanical, no LLM call. Conservative defaults (Kit pre-spec
review):

* **Bounded set rule:** at most ``N=2`` sections per turn auto-
  induce. Beyond ``N``, the additional matches surface as
  ``(section_title, one_line)`` pairs without content — the agent
  can explicitly ``request_reference`` for any that look useful.
* **Budget cap:** the auto-induced content per turn is capped at
  ``BUDGET_TOKEN_CAP=8000`` tokens. If the bounded set exceeds the
  cap, the lowest-relevance matches drop. If a single match
  exceeds the cap, it is replaced with a "section too large to
  auto-induce; explicitly request to retrieve" surface.
* **Trust-tier threshold modulation:**

  * ``canonical`` and ``agent_authored`` auto-induce on confident
    matches.
  * ``external_snapshot`` requires a stronger signal-overlap
    threshold.
  * ``quarantined`` never auto-induces; ``auto_inducible`` is also
    flipped off in the catalog when the entry is quarantined, so
    this rule is enforced at two layers.

* **Collection-level vs file-level matching:**

  * File-level signal match → algorithmic injection of the matched
    section (handled by the caller via :func:`inject_entry`).
  * Collection-level signal match → surface the collection-level
    catalog entry (purpose, member-file count, provenance). Never
    a bundle of all member files.

This module returns ranked candidates; the caller (the integration
layer's briefing assembler) is responsible for invoking
:func:`inject_entry` on each candidate to materialize content.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterable

from kernos.kernel.reference.catalog import (
    CatalogEntry,
    CatalogStore,
    ENTRY_TYPE_COLLECTION,
    ENTRY_TYPE_FILE,
    TRUST_AGENT_AUTHORED,
    TRUST_CANONICAL,
    TRUST_EXTERNAL_SNAPSHOT,
    TRUST_QUARANTINED,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables (post-deploy adjustable per Kit caveat — defaults conservative)
# ---------------------------------------------------------------------------


BOUNDED_SET_N = 2
BUDGET_TOKEN_CAP = 8000

# Token-overlap thresholds per trust tier. Token-overlap counts
# unique non-stopword tokens that appear in BOTH the signal set AND
# the candidate's catalog metadata (one_line + section_title +
# category + collection_name + purpose). Higher → more overlap
# required → fewer matches.
THRESHOLD_CANONICAL = 2
THRESHOLD_AGENT_AUTHORED = 2
THRESHOLD_EXTERNAL_SNAPSHOT = 4
# quarantined never matches (filtered out before scoring).


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------


_TOKEN_RE = re.compile(r"[a-z0-9]+")
# Minimal stopword list — auto-induction is conservative, so a
# short list is preferable to an aggressive one. Tokens shorter
# than three characters are also dropped.
_STOPWORDS: frozenset[str] = frozenset({
    "the", "and", "for", "you", "are", "with", "this", "that",
    "from", "into", "what", "have", "has", "was", "were", "but",
    "not", "can", "will", "would", "should", "could", "does",
    "did", "your", "any", "all", "use", "uses", "using", "used",
    "via", "per", "one", "two", "off", "out", "more", "less",
    "than", "then", "their", "them", "they", "its", "his", "her",
    "him", "she", "who", "how", "why", "when", "where", "which",
    "about", "above", "below", "after", "before", "during", "into",
    "such", "some", "very", "just", "much", "many", "most", "few",
    "still", "also", "even", "ever", "well", "want", "wants",
})


def _normalize(token: str) -> str:
    """Minimal English plural normalization. ``covenants`` →
    ``covenant``, ``gates`` → ``gate``. Conservative: only strips a
    trailing ``s`` when the token is long enough (>4 chars) and
    isn't ``ss``-ending. Avoids over-normalization (``gas`` →
    ``ga``). No full stemming — that's an LLM-shaped concern; the
    spec wants this layer mechanical."""
    if len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def tokenize(text: str) -> list[str]:
    """Lowercase, alphanumeric tokens; stopwords + len<3 dropped;
    minimal plural-s stripping applied. Returns ordered tokens
    (caller may dedupe to a set as needed)."""
    if not text:
        return []
    return [
        _normalize(t) for t in _TOKEN_RE.findall(text.lower())
        if len(t) >= 3 and t not in _STOPWORDS
    ]


# ---------------------------------------------------------------------------
# Candidate result shape
# ---------------------------------------------------------------------------


@dataclass
class InductionCandidate:
    entry: CatalogEntry
    overlap: int
    """Number of unique non-stopword tokens overlapping between the
    signal set and the candidate's catalog metadata text."""


@dataclass
class InductionResult:
    """The shape returned by :func:`induce`.

    ``injected`` carries the bounded-set survivors (at most
    :data:`BOUNDED_SET_N`); the caller resolves these by calling
    :func:`inject_entry` per entry.

    ``surfaced_pairs`` carries ``(section_title, one_line)`` for
    additional matches beyond the bounded set — the agent can
    explicitly ``request_reference`` if any look relevant. Entries
    that exceed the budget cap individually appear here too with a
    ``one_line`` hint replaced by the user-facing "section too large
    to auto-induce" message."""

    injected: list[InductionCandidate] = field(default_factory=list)
    surfaced_pairs: list[tuple[str, str]] = field(default_factory=list)


# Approximate token cost — chars / 4. Conservative; the budget
# cap is enforced after content materializes; this is the
# pre-injection bound for catalog-metadata sized estimates.
def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _candidate_text(entry: CatalogEntry) -> str:
    """Catalog-metadata text used for signal matching. Fields that
    matter: section title, one-liner, category, collection_name,
    and (for collection-level entries) purpose + member-file paths."""
    parts: list[str] = [
        entry.section_title,
        entry.one_line,
        entry.category,
        entry.collection_name,
    ]
    if entry.entry_type == ENTRY_TYPE_COLLECTION:
        parts.append(entry.purpose)
        parts.extend(entry.member_file_paths)
    if entry.collection_back_reference:
        parts.append(entry.collection_back_reference)
    return " ".join(p for p in parts if p)


def _threshold_for(entry: CatalogEntry) -> int | None:
    """Per-trust-tier threshold. Returns ``None`` for tiers that
    never auto-induce (quarantined)."""
    if entry.trust_tier == TRUST_CANONICAL:
        return THRESHOLD_CANONICAL
    if entry.trust_tier == TRUST_AGENT_AUTHORED:
        return THRESHOLD_AGENT_AUTHORED
    if entry.trust_tier == TRUST_EXTERNAL_SNAPSHOT:
        return THRESHOLD_EXTERNAL_SNAPSHOT
    return None  # quarantined


def score_overlap(
    *, signal_tokens: set[str], entry: CatalogEntry,
) -> int:
    """Number of unique tokens overlapping between the signal set
    and the candidate text."""
    candidate_tokens = set(tokenize(_candidate_text(entry)))
    return len(signal_tokens & candidate_tokens)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def induce(
    *,
    catalog: CatalogStore,
    instance_id: str,
    domain_id: str,
    signals: Iterable[str],
    bounded_set_n: int = BOUNDED_SET_N,
    budget_token_cap: int = BUDGET_TOKEN_CAP,
) -> InductionResult:
    """Mechanical induction over the visible catalog.

    Visibility filter: ``scope='instance'`` rows + ``scope='domain:<X>'``
    rows where ``X = domain_id``. Quarantined entries are excluded.
    Tombstoned entries are excluded.

    Returns ``InductionResult`` with ``injected`` ranked by overlap
    descending and ``surfaced_pairs`` for low-rank or budget-exceeding
    matches."""
    # Tokenize signals; stop early if there's no signal to match
    # against — auto-induction never fires on empty signal.
    signal_tokens: set[str] = set()
    for sig in signals:
        signal_tokens.update(tokenize(sig))
    if not signal_tokens:
        return InductionResult()

    rows = await catalog.list_visible(
        instance_id=instance_id,
        domain_id=domain_id,
        include_quarantined=False,
    )

    candidates: list[InductionCandidate] = []
    for entry in rows:
        if not entry.auto_inducible:
            continue
        threshold = _threshold_for(entry)
        if threshold is None:
            continue
        overlap = score_overlap(
            signal_tokens=signal_tokens, entry=entry,
        )
        if overlap < threshold:
            continue
        candidates.append(InductionCandidate(entry=entry, overlap=overlap))

    # Rank: highest overlap first; ties broken by canonical >
    # agent_authored > external_snapshot (more trusted first), then
    # by entry_type (collection-level surfaces first when it ties
    # with file-level, since the spec wants collection-as-map to
    # take precedence on broad-topic matches).
    _trust_order = {
        TRUST_CANONICAL: 0,
        TRUST_AGENT_AUTHORED: 1,
        TRUST_EXTERNAL_SNAPSHOT: 2,
    }
    _entry_type_order = {
        ENTRY_TYPE_COLLECTION: 0,
        ENTRY_TYPE_FILE: 1,
    }
    candidates.sort(
        key=lambda c: (
            -c.overlap,
            _trust_order.get(c.entry.trust_tier, 99),
            _entry_type_order.get(c.entry.entry_type, 99),
            c.entry.indexed_at,
        ),
    )

    injected: list[InductionCandidate] = []
    surfaced: list[tuple[str, str]] = []
    used_tokens = 0
    for c in candidates:
        approx = _approx_tokens(_candidate_text(c.entry))
        if len(injected) < bounded_set_n and used_tokens + approx <= budget_token_cap:
            injected.append(c)
            used_tokens += approx
        else:
            # Beyond the bounded set OR over the budget — surface
            # as a (title, one_line) pair so the agent can ask
            # explicitly. Single-entry-too-large case uses a
            # special marker.
            label = (
                c.entry.section_title
                if c.entry.entry_type == ENTRY_TYPE_FILE
                else f"[collection] {c.entry.collection_name}"
            )
            if approx > budget_token_cap:
                surfaced.append((
                    label,
                    "Section too large to auto-induce; explicitly request to retrieve.",
                ))
            else:
                surfaced.append((label, c.entry.one_line or c.entry.purpose))

    return InductionResult(injected=injected, surfaced_pairs=surfaced)


__all__ = [
    "BOUNDED_SET_N",
    "BUDGET_TOKEN_CAP",
    "InductionCandidate",
    "InductionResult",
    "THRESHOLD_AGENT_AUTHORED",
    "THRESHOLD_CANONICAL",
    "THRESHOLD_EXTERNAL_SNAPSHOT",
    "induce",
    "score_overlap",
    "tokenize",
]
