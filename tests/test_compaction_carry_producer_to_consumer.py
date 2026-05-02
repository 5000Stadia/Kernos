"""Producer-to-consumer pin: compaction-carry persistence convention
matches what the read path expects. The real end-to-end flow is
exercised by the soak harness's probe_d_compaction (cli.sh + real
LLM compaction); these in-process pins close the structural gaps
that injection-based contract tests don't cover.

Codex round 7 (CCV1 soak deliberation 2026-05-02) flagged that
every existing memory-zone test injects ``memory_prefix`` via the
space-context seam (``_inject_space_context``), which means the
producer-to-consumer flow is NOT pinned end-to-end inside the
test suite. The in-process tests pass against an injected marker;
if the producer (compaction service) writes nothing the reader
finds, the injection-based tests would still pass.

This file pins the gap structurally:

1. The persistence path convention agrees between writer and
   reader (if the writer puts content at path X, the reader
   reads path X).
2. The consumer's read API surfaces the produced content as the
   shape the assemble phase expects.
3. The assemble phase actually calls the consumer's read API for
   the memory zone (architectural-interface pin).

Together these pins close the gap that the soak harness caught
at the diagnostic level.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Persistence-path convention pin
# ---------------------------------------------------------------------------


async def test_load_context_document_reads_active_document_at_canonical_path(tmp_path):
    """Pin: ``CompactionService.load_context_document`` reads from
    the canonical member-scoped path ``compaction/<space>/<member>/
    active_document.md``. If the producer writes here, the consumer
    finds it. If anyone moves the path on either side, this pin
    trips and the lived-cognition gap re-opens."""
    from kernos.kernel.compaction import CompactionService
    from kernos.kernel.state_sqlite import SqliteStateStore
    from kernos.kernel.tokens import EstimateTokenAdapter
    from kernos.utils import _safe_name

    data_dir = tmp_path / "compaction-test"
    data_dir.mkdir(parents=True, exist_ok=True)
    state = SqliteStateStore(str(data_dir))
    reasoning = MagicMock()
    adapter = EstimateTokenAdapter()
    compaction = CompactionService(
        state=state, reasoning=reasoning,
        token_adapter=adapter, data_dir=str(data_dir),
    )

    instance_id = "inst-path-pin"
    space_id = "space_path_pin"
    member_id = "mem_path_pin"

    # Write a known active document at the canonical path the
    # producer is supposed to use.
    expected_path = (
        data_dir
        / _safe_name(instance_id)
        / "state"
        / "compaction"
        / _safe_name(space_id)
        / _safe_name(member_id)
        / "active_document.md"
    )
    expected_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_carry = (
        "# Ledger\n"
        "## Compaction #1 (test seed)\n"
        "- The compaction service should expose this content.\n\n"
        "# Living State\n"
        "## Current Situation\n"
        "Test seed for the producer-to-consumer pin.\n"
    )
    expected_path.write_text(canonical_carry)

    # The consumer's read API should find this content.
    document = await compaction.load_context_document(
        instance_id, space_id, member_id=member_id,
    )
    assert document, (
        "load_context_document must return non-empty when an "
        "active_document.md exists at the canonical path. If "
        "this fails, the writer + reader disagree on the path "
        "convention — exactly the lived-cognition gap the soak "
        "harness was designed to catch."
    )
    assert "# Living State" in document, (
        "load_context_document must return the FULL document "
        "including the Living State section, not a truncated "
        "view."
    )
    assert "Test seed for the producer-to-consumer pin." in document, (
        "the carry text must reach the consumer verbatim — not "
        "a summary or rerendering."
    )


async def test_load_context_document_returns_empty_when_no_document(tmp_path):
    """Counterpart pin: when no active_document.md exists for
    the requested member-scoped key, the consumer returns empty.
    This is the legitimate empty-MEMORY-zone case (fresh space,
    no compaction yet) that the renderer correctly skips."""
    from kernos.kernel.compaction import CompactionService
    from kernos.kernel.state_sqlite import SqliteStateStore
    from kernos.kernel.tokens import EstimateTokenAdapter

    data_dir = tmp_path / "compaction-empty-test"
    data_dir.mkdir(parents=True, exist_ok=True)
    state = SqliteStateStore(str(data_dir))
    reasoning = MagicMock()
    adapter = EstimateTokenAdapter()
    compaction = CompactionService(
        state=state, reasoning=reasoning,
        token_adapter=adapter, data_dir=str(data_dir),
    )

    document = await compaction.load_context_document(
        "inst-empty", "space_empty", member_id="mem_empty",
    )
    assert not document, (
        "load_context_document must return empty/None when no "
        "active_document.md exists; not raise, not invent content"
    )


# ---------------------------------------------------------------------------
# Architectural-interface pin
# ---------------------------------------------------------------------------


def test_assemble_space_context_calls_compaction_load_context_document():
    """The assemble phase MUST call ``compaction.load_context_document``
    so the carry text reaches ``memory_prefix`` and ultimately the
    rendered system prompt's ## MEMORY zone. Source-inspection pin:
    grep the source for the call. If a future refactor moves the
    read elsewhere or skips it entirely, this trips."""
    from kernos.messages import handler

    src = inspect.getsource(handler)
    assert "compaction.load_context_document" in src, (
        "assemble's _assemble_space_context must call "
        "compaction.load_context_document to populate "
        "memory_prefix. Without that call the ## MEMORY zone "
        "stays empty even when compaction wrote content. This is "
        "the architectural-interface pin closing the structural "
        "gap between producer (compaction) and consumer "
        "(assemble → renderer)."
    )
    assert "compaction.load_state" in src, (
        "assemble must also call compaction.load_state to read "
        "the persistence side of compaction state."
    )


def test_assemble_space_context_calls_compaction_load_index():
    """The assemble phase reads the archive index too (for the
    archived-history portion of MEMORY when rotation has
    happened). Codex round 7 clarified: index_tokens=0 is OK
    pre-rotation, but the read must happen so post-rotation
    archives appear."""
    from kernos.messages import handler

    src = inspect.getsource(handler)
    assert "compaction.load_index" in src, (
        "assemble's _assemble_space_context must call "
        "compaction.load_index for the archived-history portion "
        "of the MEMORY zone."
    )


# ---------------------------------------------------------------------------
# C3a wiring pin: compaction_carry reaches the typed packet
# ---------------------------------------------------------------------------


def test_population_context_carries_compaction_carry_field():
    """C3a pin: the typed PopulationContext exposes
    ``compaction_carry`` as a field, and ``populate_field`` for
    ``memory.compaction_carry`` reads from it. This was added in
    C3a; the pin keeps it from being silently removed in a
    future refactor."""
    from kernos.kernel.cognitive_context.field_provenance import PopulationContext
    fields = {f for f in PopulationContext.__dataclass_fields__}
    assert "compaction_carry" in fields, (
        "PopulationContext must expose compaction_carry — the "
        "field the assembly populates from ctx.memory_prefix"
    )


async def test_populate_field_memory_compaction_carry_reads_from_context():
    """C3a pin: populate_field('memory.compaction_carry') reads
    from PopulationContext.compaction_carry. This connects the
    assembly's populated context to the typed packet's memory
    block. Without this connection the seam-level contract tests
    pass on injection but real production carry never reaches
    the packet."""
    from kernos.kernel.cognitive_context.field_provenance import (
        PopulationContext,
        populate_field,
    )

    ctx = PopulationContext(compaction_carry="real carry text from production")
    out = await populate_field("memory.compaction_carry", ctx)
    assert out == "real carry text from production", (
        f"populate_field('memory.compaction_carry') must read "
        f"from PopulationContext.compaction_carry; got {out!r}"
    )

    # And empty context yields empty carry — the legitimate
    # empty-MEMORY case.
    empty_ctx = PopulationContext()
    out_empty = await populate_field("memory.compaction_carry", empty_ctx)
    assert out_empty == "", (
        f"populate_field on empty context must return empty "
        f"string (renderer skips empty zones); got {out_empty!r}"
    )
