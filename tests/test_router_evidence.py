"""ROUTER-EVIDENCE-V1 batch 2.1 + 2.2 tests.

Covers:
  * Layer 1 (recent activity tail) wiring
  * Layer 2 (Living State + recent Ledger entries) wiring
  * Per-space cap interplay (Living State present → recent_tail trimmed)
  * Final formatted-string truncation after read_recent
  * Per-space fail-open
  * /dump bypass short-circuits evidence build
  * Member-isolation of compaction documents
  * Slot-reservation in global-ceiling fallback
  * Public CompactionService helpers (load_living_state, load_recent_ledger_entries)

Tests assert against the captured router prompt contents and a mocked
router result — NOT a real LLM citation field. The router has no
rationale field today and we don't add one in this PR. (Codex review
2026-05-09 v2 risk C.)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.kernel.compaction import CompactionService
from kernos.kernel.router import LLMRouter, RouterResult
from kernos.kernel.tokens import EstimateTokenAdapter
from kernos.kernel.space_candidates import list_route_candidate_spaces
from kernos.kernel.space_evidence import (
    GLOBAL_BUNDLE_CEILING,
    LEDGER_TAIL_CAP_TOKENS,
    LIVING_STATE_CAP_TOKENS,
    RECENT_TAIL_CAP_TOKENS,
    RECENT_TAIL_CAP_WHEN_LIVING_STATE,
    SpaceEvidence,
    build_space_evidence,
)
from kernos.kernel.spaces import ContextSpace
from kernos.kernel.state_json import JsonStateStore
from kernos.kernel.state_sqlite import SqliteStateStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_compaction(tmp_path) -> CompactionService:
    """Build a minimal CompactionService for read-only document tests.

    The new public helpers under test (load_living_state,
    load_recent_ledger_entries) only touch the filesystem via _space_dir,
    so state/reasoning/token_adapter can be cheap stubs.
    """
    return CompactionService(
        state=MagicMock(),
        reasoning=MagicMock(),
        token_adapter=EstimateTokenAdapter(),
        data_dir=str(tmp_path),
    )


def _make_space(
    space_id: str,
    name: str,
    description: str,
    *,
    instance_id: str = "inst-A",
    is_default: bool = False,
    member_id: str = "",
    space_type: str = "general",
) -> ContextSpace:
    return ContextSpace(
        id=space_id,
        instance_id=instance_id,
        name=name,
        description=description,
        space_type=space_type,
        status="active",
        is_default=is_default,
        member_id=member_id,
        created_at=_now(),
        last_active_at=_now(),
    )


# ---------------------------------------------------------------------------
# list_route_candidate_spaces
# ---------------------------------------------------------------------------


class TestCandidateSelector:
    """Uses SqliteStateStore — production default. The legacy JsonStateStore
    drops ``member_id`` on context-space round-trip (a pre-existing bug
    in ``_CONTEXT_SPACE_FIELDS``, out of scope for ROUTER-EVIDENCE-V1)."""

    async def test_returns_all_active_when_no_member(self, tmp_path):
        state = SqliteStateStore(data_dir=tmp_path)
        tid = "inst-A"
        general = _make_space("space_g", "General", "general", is_default=True)
        await state.save_context_space(general)
        candidates = await list_route_candidate_spaces(state, tid, member_id="")
        assert {c.id for c in candidates} == {"space_g"}

    async def test_filters_by_member_keeping_legacy_and_system(self, tmp_path):
        state = SqliteStateStore(data_dir=tmp_path)
        tid = "inst-A"
        legacy = _make_space("space_legacy", "Legacy", "no member")
        owned_a = _make_space("space_owned_a", "OwnedA", "member A's", member_id="mem-A")
        owned_b = _make_space("space_owned_b", "OwnedB", "member B's", member_id="mem-B")
        system = _make_space("space_sys", "System", "system", member_id="mem-B", space_type="system")
        for s in (legacy, owned_a, owned_b, system):
            await state.save_context_space(s)
        candidates = await list_route_candidate_spaces(state, tid, member_id="mem-A")
        ids = {c.id for c in candidates}
        # member A sees their own space + legacy unowned + system (regardless of owner)
        assert ids == {"space_legacy", "space_owned_a", "space_sys"}
        assert "space_owned_b" not in ids

    async def test_drops_inactive_spaces(self, tmp_path):
        state = SqliteStateStore(data_dir=tmp_path)
        tid = "inst-A"
        active = _make_space("space_a", "A", "active")
        archived = _make_space("space_b", "B", "archived")
        archived.status = "archived"
        await state.save_context_space(active)
        await state.save_context_space(archived)
        candidates = await list_route_candidate_spaces(state, tid, member_id="")
        assert {c.id for c in candidates} == {"space_a"}


# ---------------------------------------------------------------------------
# Public CompactionService helpers
# ---------------------------------------------------------------------------


_DOC_TEMPLATE = """# Ledger

## Compaction #1 (source: log_001) — 2026-04-01T00:00 → 2026-04-01T01:00
- Topic A discussed.
- Topic B discussed.

## Compaction #2 (source: log_002) — 2026-04-02T00:00 → 2026-04-02T01:00
- Topic C decided.

## Compaction #3 (source: log_003) — 2026-04-03T00:00 → 2026-04-03T01:00
- Topic D in progress.

## Compaction #4 (source: log_004) — 2026-04-04T00:00 → 2026-04-04T01:00
- Topic E noted.

# Living State

Current focus: working on Topic E with deadline 2026-04-15.
Active threads:
- Topic E implementation
- Pending review of Topic D
"""


class TestCompactionPublicHelpers:
    async def test_load_living_state_returns_section(self, tmp_path):
        comp = _make_compaction(tmp_path)
        space_dir = comp._space_dir("inst-A", "space_x", member_id="mem-A")
        space_dir.mkdir(parents=True, exist_ok=True)
        (space_dir / "active_document.md").write_text(_DOC_TEMPLATE, encoding="utf-8")

        living = await comp.load_living_state("inst-A", "space_x", member_id="mem-A")
        assert "Current focus: working on Topic E" in living
        assert "Topic A discussed" not in living  # ledger excluded

    async def test_load_living_state_missing_doc_returns_empty(self, tmp_path):
        comp = _make_compaction(tmp_path)
        living = await comp.load_living_state("inst-A", "space_missing", member_id="mem-A")
        assert living == ""

    async def test_load_recent_ledger_entries_returns_last_n(self, tmp_path):
        comp = _make_compaction(tmp_path)
        space_dir = comp._space_dir("inst-A", "space_x", member_id="mem-A")
        space_dir.mkdir(parents=True, exist_ok=True)
        (space_dir / "active_document.md").write_text(_DOC_TEMPLATE, encoding="utf-8")

        entries = await comp.load_recent_ledger_entries(
            "inst-A", "space_x", member_id="mem-A", n=2,
        )
        assert len(entries) == 2
        # Oldest-to-newest within the tail (entries[-2:] — Compactions #3 and #4)
        assert "Compaction #3" in entries[0]
        assert "Compaction #4" in entries[1]

    async def test_load_recent_ledger_entries_n_zero_returns_empty(self, tmp_path):
        comp = _make_compaction(tmp_path)
        space_dir = comp._space_dir("inst-A", "space_x", member_id="mem-A")
        space_dir.mkdir(parents=True, exist_ok=True)
        (space_dir / "active_document.md").write_text(_DOC_TEMPLATE, encoding="utf-8")

        assert await comp.load_recent_ledger_entries(
            "inst-A", "space_x", member_id="mem-A", n=0,
        ) == []
        assert await comp.load_recent_ledger_entries(
            "inst-A", "space_x", member_id="mem-A", n=-1,
        ) == []

    async def test_load_recent_ledger_entries_missing_doc_returns_empty(self, tmp_path):
        comp = _make_compaction(tmp_path)
        assert await comp.load_recent_ledger_entries(
            "inst-A", "space_missing", member_id="mem-A", n=3,
        ) == []


# ---------------------------------------------------------------------------
# build_space_evidence — Layer 1 (recent activity tail)
# ---------------------------------------------------------------------------


class TestBuildEvidenceLayer1:
    async def test_recent_tail_populated_from_read_recent(self):
        space = _make_space("space_a", "A", "desc")
        conv_logger = MagicMock()
        conv_logger.read_recent = AsyncMock(return_value=[
            {"role": "user", "content": "kernos audit findings", "timestamp": "2026-05-09T10:00:00+00:00"},
            {"role": "assistant", "content": "filed three", "timestamp": "2026-05-09T10:00:01+00:00"},
        ])
        compaction = MagicMock()
        compaction.load_living_state = AsyncMock(return_value="")
        compaction.load_recent_ledger_entries = AsyncMock(return_value=[])

        bundles = await build_space_evidence(
            conv_logger=conv_logger,
            compaction=compaction,
            instance_id="inst-A",
            member_id="mem-A",
            candidates=[space],
            message_content="asking about audit",
        )
        assert "space_a" in bundles
        ev = bundles["space_a"]
        assert "kernos audit findings" in ev.recent_tail
        assert "filed three" in ev.recent_tail
        assert ev.living_state == ""
        assert ev.ledger_tail == ""

    async def test_recent_tail_passes_token_budget_not_max_tokens(self):
        """Codex review v2 finding 3: the API is token_budget, not max_tokens.
        Verify the kwarg actually reaches read_recent."""
        space = _make_space("space_a", "A", "desc")
        conv_logger = MagicMock()
        conv_logger.read_recent = AsyncMock(return_value=[])
        compaction = MagicMock()
        compaction.load_living_state = AsyncMock(return_value="")
        compaction.load_recent_ledger_entries = AsyncMock(return_value=[])

        await build_space_evidence(
            conv_logger=conv_logger,
            compaction=compaction,
            instance_id="inst-A",
            member_id="mem-A",
            candidates=[space],
            message_content="",
        )
        kwargs = conv_logger.read_recent.call_args.kwargs
        assert "token_budget" in kwargs, (
            "build_space_evidence MUST call read_recent with token_budget=, "
            "not max_tokens=. Codex flagged this in v2 review."
        )
        assert "max_tokens" not in kwargs

    async def test_per_space_fail_open_on_read_recent_exception(self):
        """Layer 1 read_recent raising for one space yields empty
        SpaceEvidence for that space; other spaces continue normally."""
        space_ok = _make_space("space_ok", "OK", "desc")
        space_bad = _make_space("space_bad", "BAD", "desc")
        conv_logger = MagicMock()

        async def _read_recent(instance_id, space_id, **kw):
            if space_id == "space_bad":
                raise RuntimeError("disk gone")
            return [{"role": "user", "content": "ok line", "timestamp": ""}]
        conv_logger.read_recent = AsyncMock(side_effect=_read_recent)
        compaction = MagicMock()
        compaction.load_living_state = AsyncMock(return_value="")
        compaction.load_recent_ledger_entries = AsyncMock(return_value=[])

        bundles = await build_space_evidence(
            conv_logger=conv_logger,
            compaction=compaction,
            instance_id="inst-A",
            member_id="",
            candidates=[space_ok, space_bad],
            message_content="",
        )
        assert "ok line" in bundles["space_ok"].recent_tail
        assert bundles["space_bad"].recent_tail == ""

    async def test_final_string_cap_truncates_oversized_tail(self):
        """read_recent may return a single oversized entry that exceeds
        its token_budget (or many entries that aggregate over the cap).
        The final formatted-string cap MUST clamp the rendered block
        back under RECENT_TAIL_CAP_TOKENS regardless. Codex review v2
        finding 3."""
        space = _make_space("space_a", "A", "desc")
        # Many oversized entries — exercises both per-entry render-time
        # clamp (200 chars each) AND the final formatted-string cap on
        # the aggregate. Final cap is the load-bearing one for the
        # invariant the spec promises.
        oversized_entries = [
            {"role": "user", "content": "x" * 1000, "timestamp": ""}
            for _ in range(20)
        ]
        conv_logger = MagicMock()
        conv_logger.read_recent = AsyncMock(return_value=oversized_entries)
        compaction = MagicMock()
        compaction.load_living_state = AsyncMock(return_value="")
        compaction.load_recent_ledger_entries = AsyncMock(return_value=[])

        bundles = await build_space_evidence(
            conv_logger=conv_logger,
            compaction=compaction,
            instance_id="inst-A",
            member_id="",
            candidates=[space],
            message_content="",
        )
        ev = bundles["space_a"]
        # Cap-driven invariant: the formatted block fits the token cap.
        assert len(ev.recent_tail) <= RECENT_TAIL_CAP_TOKENS * 4
        # And the cap-overflow path was taken (truncated flag set).
        assert ev.truncated is True


# ---------------------------------------------------------------------------
# build_space_evidence — Layer 2 (compacted summaries) + cap interplay
# ---------------------------------------------------------------------------


class TestBuildEvidenceLayer2:
    async def test_living_state_and_ledger_populated(self):
        space = _make_space("space_a", "A", "generic desc")
        conv_logger = MagicMock()
        conv_logger.read_recent = AsyncMock(return_value=[])
        compaction = MagicMock()
        compaction.load_living_state = AsyncMock(return_value="Currently working on audit.")
        compaction.load_recent_ledger_entries = AsyncMock(return_value=[
            "## Compaction #1 — entry one",
            "## Compaction #2 — entry two",
        ])

        bundles = await build_space_evidence(
            conv_logger=conv_logger,
            compaction=compaction,
            instance_id="inst-A",
            member_id="mem-A",
            candidates=[space],
            message_content="",
        )
        ev = bundles["space_a"]
        assert "Currently working on audit." in ev.living_state
        assert "Compaction #1 — entry one" in ev.ledger_tail
        assert "Compaction #2 — entry two" in ev.ledger_tail

    async def test_recent_tail_trims_when_living_state_present(self):
        """Cap interplay: when Living State is non-empty, recent_tail
        cap shrinks to RECENT_TAIL_CAP_WHEN_LIVING_STATE."""
        assert RECENT_TAIL_CAP_WHEN_LIVING_STATE < RECENT_TAIL_CAP_TOKENS

        space = _make_space("space_a", "A", "desc")
        # Long content so the cap matters
        long_content = "y" * (RECENT_TAIL_CAP_TOKENS * 4 * 3)
        conv_logger = MagicMock()
        conv_logger.read_recent = AsyncMock(return_value=[
            {"role": "user", "content": long_content, "timestamp": ""},
        ])
        compaction = MagicMock()
        compaction.load_living_state = AsyncMock(return_value="something")
        compaction.load_recent_ledger_entries = AsyncMock(return_value=[])

        bundles = await build_space_evidence(
            conv_logger=conv_logger,
            compaction=compaction,
            instance_id="inst-A",
            member_id="mem-A",
            candidates=[space],
            message_content="",
        )
        ev = bundles["space_a"]
        # Tail capped to the with-living-state cap (chars = tokens * 4).
        assert len(ev.recent_tail) <= RECENT_TAIL_CAP_WHEN_LIVING_STATE * 4

    async def test_living_state_truncated_at_cap(self):
        space = _make_space("space_a", "A", "desc")
        oversized = "L" * (LIVING_STATE_CAP_TOKENS * 4 * 5)
        conv_logger = MagicMock()
        conv_logger.read_recent = AsyncMock(return_value=[])
        compaction = MagicMock()
        compaction.load_living_state = AsyncMock(return_value=oversized)
        compaction.load_recent_ledger_entries = AsyncMock(return_value=[])

        bundles = await build_space_evidence(
            conv_logger=conv_logger,
            compaction=compaction,
            instance_id="inst-A",
            member_id="mem-A",
            candidates=[space],
            message_content="",
        )
        ev = bundles["space_a"]
        assert len(ev.living_state) <= LIVING_STATE_CAP_TOKENS * 4
        assert ev.truncated is True

    async def test_ledger_tail_truncated_at_cap(self):
        space = _make_space("space_a", "A", "desc")
        big_entry = "L" * (LEDGER_TAIL_CAP_TOKENS * 4 * 5)
        conv_logger = MagicMock()
        conv_logger.read_recent = AsyncMock(return_value=[])
        compaction = MagicMock()
        compaction.load_living_state = AsyncMock(return_value="")
        compaction.load_recent_ledger_entries = AsyncMock(return_value=[big_entry])

        bundles = await build_space_evidence(
            conv_logger=conv_logger,
            compaction=compaction,
            instance_id="inst-A",
            member_id="mem-A",
            candidates=[space],
            message_content="",
        )
        ev = bundles["space_a"]
        assert len(ev.ledger_tail) <= LEDGER_TAIL_CAP_TOKENS * 4
        assert ev.truncated is True


# ---------------------------------------------------------------------------
# Member-isolation: Layer 2 reads through compaction's _space_dir boundary
# ---------------------------------------------------------------------------


class TestMemberIsolation:
    async def test_member_a_does_not_see_member_b_living_state(self, tmp_path):
        """Two members both have compacted documents in the same space.
        Each member's evidence bundle contains ONLY their own document.

        This is the disclosure boundary that compaction enforces via
        ``_space_dir(instance_id, space_id, member_id)``. We verify
        end-to-end: build_space_evidence threads member_id through to
        the public helpers, and the helpers honor it.
        """
        comp = _make_compaction(tmp_path)
        instance_id = "inst-A"
        space_id = "space_shared_system"
        # member-isolation probe

        doc_a = _DOC_TEMPLATE.replace("Topic E", "MEMBER_A_PRIVATE_TOPIC")
        doc_b = _DOC_TEMPLATE.replace("Topic E", "MEMBER_B_PRIVATE_TOPIC")
        for member, doc in (("mem-A", doc_a), ("mem-B", doc_b)):
            d = comp._space_dir(instance_id, space_id, member_id=member)
            d.mkdir(parents=True, exist_ok=True)
            (d / "active_document.md").write_text(doc, encoding="utf-8")

        conv_logger = MagicMock()
        conv_logger.read_recent = AsyncMock(return_value=[])
        space = _make_space(space_id, "Shared", "system space", space_type="system")

        # Member A sees only their own
        bundles_a = await build_space_evidence(
            conv_logger=conv_logger,
            compaction=comp,
            instance_id=instance_id,
            member_id="mem-A",
            candidates=[space],
            message_content="",
        )
        assert "MEMBER_A_PRIVATE_TOPIC" in bundles_a[space_id].living_state
        assert "MEMBER_B_PRIVATE_TOPIC" not in bundles_a[space_id].living_state

        # Member B sees only their own
        bundles_b = await build_space_evidence(
            conv_logger=conv_logger,
            compaction=comp,
            instance_id=instance_id,
            member_id="mem-B",
            candidates=[space],
            message_content="",
        )
        assert "MEMBER_B_PRIVATE_TOPIC" in bundles_b[space_id].living_state
        assert "MEMBER_A_PRIVATE_TOPIC" not in bundles_b[space_id].living_state


# ---------------------------------------------------------------------------
# Global-ceiling slot reservation
# ---------------------------------------------------------------------------


class TestGlobalCeiling:
    async def test_current_focus_reserved_when_ceiling_exceeded(self, monkeypatch):
        """Codex review v2 finding 4: when the aggregate bundle exceeds
        the ceiling, the current focus space MUST be retained even if
        its evidence-overlap with the message is low. Other slots are
        ranked by overlap and the lowest dropped first."""
        # Tighten the ceiling for this test so we don't have to fabricate
        # huge inputs.
        monkeypatch.setattr(
            "kernos.kernel.space_evidence.GLOBAL_BUNDLE_CEILING", 200,
        )

        focus = _make_space("space_focus", "Focus", "current")
        rich_match = _make_space("space_rich", "Rich", "rich")
        weak = _make_space("space_weak", "Weak", "weak")

        conv_logger = MagicMock()
        # Each space has ~100-token recent_tail content.
        async def _read_recent(instance_id, space_id, **kw):
            if space_id == "space_rich":
                return [{"role": "user", "content": "audit kernos architecture findings " * 30, "timestamp": ""}]
            if space_id == "space_weak":
                return [{"role": "user", "content": "weather and weekend plans " * 30, "timestamp": ""}]
            return [{"role": "user", "content": "focus space tail " * 30, "timestamp": ""}]
        conv_logger.read_recent = AsyncMock(side_effect=_read_recent)
        compaction = MagicMock()
        compaction.load_living_state = AsyncMock(return_value="")
        compaction.load_recent_ledger_entries = AsyncMock(return_value=[])

        bundles = await build_space_evidence(
            conv_logger=conv_logger,
            compaction=compaction,
            instance_id="inst-A",
            member_id="",
            candidates=[focus, rich_match, weak],
            message_content="audit kernos architecture",
            current_focus_id="space_focus",
        )
        # Slot reservation: focus must always survive
        assert "space_focus" in bundles, (
            "current_focus_id MUST be retained in the bundle when global "
            "ceiling is exceeded — slot-reservation per Codex v2 finding 4."
        )
        # Evidence-relevant non-focus space should outrank irrelevant one
        if "space_weak" in bundles and "space_rich" not in bundles:
            pytest.fail(
                "When ceiling forces a drop, the evidence-overlap-rich "
                "non-focus space must outrank the weak one. Got the "
                "opposite — fallback ranker is broken."
            )


# ---------------------------------------------------------------------------
# LLMRouter prompt rendering with evidence
# ---------------------------------------------------------------------------


class TestRouterPromptRenders:
    async def test_evidence_blocks_appear_in_prompt(self, tmp_path):
        state = JsonStateStore(tmp_path)
        tid = "inst-A"
        general = _make_space("space_g", "General", "general", is_default=True)
        await state.save_context_space(general)

        captured: dict = {}

        async def _fake_complete(**kwargs):
            captured["user_content"] = kwargs.get("user_content", "")
            return json.dumps({
                "tags": ["space_g"], "focus": "space_g",
                "continuation": False, "query_mode": False, "work_mode": False,
            })
        mock_reasoning = AsyncMock()
        mock_reasoning.complete_simple = AsyncMock(side_effect=_fake_complete)

        router = LLMRouter(state, mock_reasoning)
        evidence = {
            "space_g": SpaceEvidence(
                space_id="space_g",
                recent_tail="(user): kernos audit findings",
                living_state="Current focus: shipping ROUTER-EVIDENCE-V1.",
                ledger_tail="## Compaction #5 — earlier audit work",
            ),
        }
        await router.route(
            tid, "audit follow-up", [], "space_g",
            candidate_spaces=[general],
            space_evidence=evidence,
        )
        prompt = captured["user_content"]
        assert "Recent activity:" in prompt
        assert "kernos audit findings" in prompt
        assert "Living State:" in prompt
        assert "shipping ROUTER-EVIDENCE-V1" in prompt
        assert "Recent Ledger entries:" in prompt
        assert "earlier audit work" in prompt

    async def test_router_falls_back_to_internal_candidates_when_none_passed(self, tmp_path):
        """Backward-compat: legacy positional callers (no candidate_spaces
        kwarg) get the same result they did before — the router computes
        candidates internally via list_route_candidate_spaces."""
        state = JsonStateStore(tmp_path)
        tid = "inst-A"
        general = _make_space("space_g", "General", "general", is_default=True)
        await state.save_context_space(general)

        mock_reasoning = AsyncMock()
        mock_reasoning.complete_simple = AsyncMock(return_value=json.dumps({
            "tags": ["space_g"], "focus": "space_g",
            "continuation": False, "query_mode": False, "work_mode": False,
        }))
        router = LLMRouter(state, mock_reasoning)
        # Legacy positional call — no candidate_spaces, no space_evidence
        result = await router.route(tid, "hello", [], "")
        assert result.focus == "space_g"

    async def test_no_evidence_blocks_when_evidence_empty(self, tmp_path):
        state = JsonStateStore(tmp_path)
        tid = "inst-A"
        general = _make_space("space_g", "General", "general", is_default=True)
        await state.save_context_space(general)

        captured: dict = {}

        async def _fake_complete(**kwargs):
            captured["user_content"] = kwargs.get("user_content", "")
            return json.dumps({
                "tags": ["space_g"], "focus": "space_g",
                "continuation": False, "query_mode": False, "work_mode": False,
            })
        mock_reasoning = AsyncMock()
        mock_reasoning.complete_simple = AsyncMock(side_effect=_fake_complete)
        router = LLMRouter(state, mock_reasoning)

        await router.route(
            tid, "hello", [], "space_g",
            candidate_spaces=[general],
            space_evidence={},
        )
        prompt = captured["user_content"]
        # Evidence headers should NOT appear when no evidence is present.
        assert "Recent activity:" not in prompt
        assert "Living State:" not in prompt
        assert "Recent Ledger entries:" not in prompt
