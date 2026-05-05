"""REFERENCE-PRIMITIVE-V1 C6 — auto-induction (signal-matching).

Pins:

* Mechanical token-overlap matching — no LLM calls inside induce().
* Bounded set: at most N=2 sections auto-induce; remaining matches
  surface as (section_title, one_line) pairs.
* Trust-tier threshold modulation: external_snapshot needs a
  stronger overlap than canonical / agent_authored; quarantined
  never matches (filtered out before scoring).
* Visibility filter composes with the catalog: agent in domain X
  never sees domain Y entries surface even with strong overlap.
* Empty signal set yields an empty result (auto-induction does
  not speculate without a signal).
* Collection-level entries surface their purpose + member-file
  count; never a content bundle.
* Budget cap: a single match exceeding the cap surfaces with the
  "section too large to auto-induce" hint."""
from __future__ import annotations

import pytest

from kernos.kernel import event_stream
from kernos.kernel.reference.catalog import (
    CatalogEntry,
    CatalogStore,
    ENTRY_TYPE_COLLECTION,
    ENTRY_TYPE_FILE,
    SCOPE_INSTANCE,
    TRUST_AGENT_AUTHORED,
    TRUST_CANONICAL,
    TRUST_EXTERNAL_SNAPSHOT,
    TRUST_QUARANTINED,
    scope_for_domain,
)
from kernos.kernel.reference.induction import (
    BOUNDED_SET_N,
    BUDGET_TOKEN_CAP,
    induce,
    score_overlap,
    tokenize,
)


@pytest.fixture
async def event_stream_started(tmp_path):
    await event_stream._reset_for_tests()
    await event_stream.start_writer(str(tmp_path), flush_interval_s=0.05)
    yield
    await event_stream.stop_writer()
    await event_stream._reset_for_tests()


@pytest.fixture
async def induction_catalog(tmp_path, event_stream_started):
    catalog = CatalogStore()
    await catalog.start(str(tmp_path))
    yield catalog
    await catalog.stop()


def _file_entry(
    *,
    entry_id: str,
    section_title: str,
    one_line: str,
    category: str = "architecture",
    trust_tier: str = TRUST_CANONICAL,
    scope: str = SCOPE_INSTANCE,
    file_path: str = "/tmp/x.md",
    auto_inducible: bool = True,
    line_start: int = 1,
    line_end: int = 5,
) -> CatalogEntry:
    return CatalogEntry(
        entry_id=entry_id,
        instance_id="inst1",
        entry_type=ENTRY_TYPE_FILE,
        scope=scope,
        category=category,
        indexed_at="2026-05-04T00:00:00+00:00",
        trust_tier=trust_tier,
        auto_inducible=auto_inducible,
        file_path=file_path,
        section_title=section_title,
        one_line=one_line,
        line_start=line_start,
        line_end=line_end,
        source_hash="h",
    )


async def _seed(catalog: CatalogStore, entries: list[CatalogEntry]) -> None:
    by_path: dict[str, list[CatalogEntry]] = {}
    for e in entries:
        by_path.setdefault(e.file_path, []).append(e)
    for fp, group in by_path.items():
        await catalog.replace_file_entries(
            instance_id="inst1", file_path=fp, new_entries=group,
        )


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------


def test_tokenize_drops_stopwords_and_short_tokens():
    tokens = tokenize("The gate decides what is destructive.")
    assert "gate" in tokens
    # "decides" → normalized to "decide" by minimal plural-s
    # stripping; the post-normalization form is what the matcher
    # uses to handle "covenants" vs "covenant" cleanly.
    assert "decide" in tokens
    assert "destructive" in tokens
    assert "the" not in tokens
    assert "is" not in tokens


def test_tokenize_lowercases():
    assert "gate" in tokenize("Gate Classification")


def test_tokenize_normalizes_plurals():
    tokens = tokenize("covenants gates compose")
    assert "covenant" in tokens
    assert "gate" in tokens
    assert "compose" in tokens
    # Short tokens (<=4 chars) keep their trailing s.
    assert "gas" in tokenize("gas")


# ---------------------------------------------------------------------------
# Empty signal short-circuits
# ---------------------------------------------------------------------------


async def test_empty_signal_returns_empty_result(induction_catalog):
    await _seed(induction_catalog, [
        _file_entry(
            entry_id="r1", section_title="Gate classification",
            one_line="How the gate decides destructive writes.",
            file_path="/tmp/a.md",
        ),
    ])
    result = await induce(
        catalog=induction_catalog,
        instance_id="inst1",
        domain_id="space-A",
        signals=[],
    )
    assert result.injected == []
    assert result.surfaced_pairs == []


# ---------------------------------------------------------------------------
# Confident matches with bounded set
# ---------------------------------------------------------------------------


async def test_bounded_set_caps_inject_count(induction_catalog):
    """When 4 entries match strongly, only N=2 inject; the rest
    surface as (title, one_line) pairs."""
    entries = [
        _file_entry(
            entry_id=f"r{i}",
            section_title=f"covenant binding {i}",
            one_line="covenant gate compose binding",
            file_path=f"/tmp/c{i}.md",
        )
        for i in range(4)
    ]
    await _seed(induction_catalog, entries)
    result = await induce(
        catalog=induction_catalog,
        instance_id="inst1",
        domain_id="space-A",
        signals=["how do covenants compose with gates"],
    )
    assert len(result.injected) == BOUNDED_SET_N
    # Remaining matches surface as pairs (no content).
    assert len(result.surfaced_pairs) == 2


# ---------------------------------------------------------------------------
# Trust-tier modulation
# ---------------------------------------------------------------------------


async def test_external_snapshot_needs_stronger_overlap(induction_catalog):
    """Three matching tokens overlap canonical (>=2) but not
    external_snapshot (>=4)."""
    entries = [
        _file_entry(
            entry_id="r_canon", section_title="gate covenant binding",
            one_line="gate covenant binding",
            trust_tier=TRUST_CANONICAL,
            file_path="/tmp/canon.md",
        ),
        _file_entry(
            entry_id="r_snap", section_title="gate covenant binding",
            one_line="gate covenant binding",
            trust_tier=TRUST_EXTERNAL_SNAPSHOT,
            file_path="/tmp/snap.md",
            scope=scope_for_domain("space-A"),
        ),
    ]
    await _seed(induction_catalog, entries)
    result = await induce(
        catalog=induction_catalog,
        instance_id="inst1",
        domain_id="space-A",
        signals=["gate covenant binding"],
    )
    injected_ids = {c.entry.entry_id for c in result.injected}
    assert "r_canon" in injected_ids
    assert "r_snap" not in injected_ids


async def test_quarantined_never_auto_induces(induction_catalog):
    entries = [
        _file_entry(
            entry_id="r_quarantined",
            section_title="suspicious section",
            one_line="suspicious section content",
            trust_tier=TRUST_QUARANTINED,
            auto_inducible=False,
            file_path="/tmp/q.md",
            scope=scope_for_domain("space-A"),
        ),
    ]
    await _seed(induction_catalog, entries)
    result = await induce(
        catalog=induction_catalog,
        instance_id="inst1",
        domain_id="space-A",
        signals=["suspicious section content"],
    )
    assert result.injected == []
    assert result.surfaced_pairs == []


# ---------------------------------------------------------------------------
# Cross-domain isolation pin
# ---------------------------------------------------------------------------


async def test_cross_domain_isolation(induction_catalog):
    entries = [
        _file_entry(
            entry_id="r_A",
            section_title="domain-A specific",
            one_line="domain-A specific token here",
            scope=scope_for_domain("space-A"),
            trust_tier=TRUST_AGENT_AUTHORED,
            file_path="/tmp/A.md",
        ),
        _file_entry(
            entry_id="r_B",
            section_title="domain-B specific",
            one_line="domain-B specific token here",
            scope=scope_for_domain("space-B"),
            trust_tier=TRUST_AGENT_AUTHORED,
            file_path="/tmp/B.md",
        ),
    ]
    await _seed(induction_catalog, entries)

    result_A = await induce(
        catalog=induction_catalog,
        instance_id="inst1",
        domain_id="space-A",
        signals=["domain-A specific token"],
    )
    a_ids = {c.entry.entry_id for c in result_A.injected}
    assert "r_A" in a_ids
    assert "r_B" not in a_ids

    result_B = await induce(
        catalog=induction_catalog,
        instance_id="inst1",
        domain_id="space-B",
        signals=["domain-B specific token"],
    )
    b_ids = {c.entry.entry_id for c in result_B.injected}
    assert "r_B" in b_ids
    assert "r_A" not in b_ids


# ---------------------------------------------------------------------------
# Collection-level entries
# ---------------------------------------------------------------------------


async def test_collection_level_surfaces_purpose_not_bundle(induction_catalog):
    coll = CatalogEntry(
        entry_id="ref_coll_vendor",
        instance_id="inst1",
        entry_type=ENTRY_TYPE_COLLECTION,
        scope=scope_for_domain("space-A"),
        category="vendor-test-api",
        indexed_at="2026-05-04T00:00:00+00:00",
        trust_tier=TRUST_EXTERNAL_SNAPSHOT,
        auto_inducible=True,
        collection_name="vendor-test-api",
        purpose="Test vendor API authentication and rate limits.",
        refresh_policy="snapshot",
        member_file_count=2,
        member_file_paths=["auth.md", "rate-limits.md"],
        owner_domain_id="space-A",
    )
    await induction_catalog.upsert_collection_entry(entry=coll)

    result = await induce(
        catalog=induction_catalog,
        instance_id="inst1",
        domain_id="space-A",
        signals=[
            "vendor authentication rate limits documentation",
        ],
    )
    # The collection entry's purpose carries enough overlap with
    # the signal that it appears in injected (external_snapshot
    # threshold met).
    injected_ids = {c.entry.entry_id for c in result.injected}
    assert "ref_coll_vendor" in injected_ids
    surfaced_entry = next(c for c in result.injected if c.entry.entry_id == "ref_coll_vendor")
    assert surfaced_entry.entry.entry_type == ENTRY_TYPE_COLLECTION
    assert surfaced_entry.entry.member_file_count == 2
    # No content bundle — the catalog entry only carries metadata.
    assert surfaced_entry.entry.purpose


# ---------------------------------------------------------------------------
# Budget cap
# ---------------------------------------------------------------------------


async def test_budget_cap_reduces_to_surfaced_pairs(induction_catalog):
    """A single match whose catalog metadata (not content) exceeds
    the budget cap surfaces with the user-facing too-large hint."""
    huge_one_line = "covenant gate binding " * 4000  # huge metadata text
    await _seed(induction_catalog, [
        _file_entry(
            entry_id="r_huge",
            section_title="covenant gate binding",
            one_line=huge_one_line,
            file_path="/tmp/huge.md",
        ),
    ])
    result = await induce(
        catalog=induction_catalog,
        instance_id="inst1",
        domain_id="space-A",
        signals=["covenant gate binding"],
        budget_token_cap=100,  # very small to trigger the path
    )
    assert result.injected == []
    assert len(result.surfaced_pairs) == 1
    title, hint = result.surfaced_pairs[0]
    assert "covenant gate binding" in title
    assert "explicitly request to retrieve" in hint


# ---------------------------------------------------------------------------
# Score helper
# ---------------------------------------------------------------------------


def test_score_overlap_counts_unique_tokens():
    e = _file_entry(
        entry_id="r1",
        section_title="gate covenant",
        one_line="gate covenant binding",
        file_path="/tmp/x.md",
    )
    overlap = score_overlap(
        signal_tokens=set(tokenize("gate covenant binding")),
        entry=e,
    )
    # "gate", "covenant", "binding" — three unique overlapping tokens.
    assert overlap == 3
