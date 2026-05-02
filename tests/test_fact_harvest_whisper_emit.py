"""Pin tests for ``fact_harvest._emit_whisper``.

The soak-harness probe_d_compaction surfaced a real production bug:
``_emit_whisper`` was calling ``state_store.add_whisper`` but the
StateStore interface defines ``save_whisper``. Every operational-
insight or stewardship whisper produced during compaction was
silently failing with ``OPERATIONAL_INSIGHT_WHISPER_FAILED:
'SqliteStateStore' object has no attribute 'add_whisper'`` — a real
substrate gap that automated unit tests didn't catch because no test
exercised this seam.

This file pins the seam so it can't regress silently. Two pins:

1. ``_emit_whisper`` calls the canonical method name (save_whisper).
2. The whisper passed to save_whisper carries the right fields
   (whisper_id, insight_text, owner_member_id, foresight_signal).

Per the project's substrate-fidelity assertion pattern, this is the
"new pin test that captures the specific failure mode" required
when folding a soak-discovered bug.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.kernel.fact_harvest import _emit_whisper


async def test_emit_whisper_calls_canonical_save_whisper():
    """The canonical state-store method is ``save_whisper``. Pin
    this so a future rename / typo doesn't silently degrade
    operational-insight or stewardship whisper emission again."""
    state_store = MagicMock()
    state_store.save_whisper = AsyncMock(return_value=None)

    ok = await _emit_whisper(
        state_store=state_store,
        instance_id="inst-test",
        space_id="space-test",
        text="A test stewardship signal worth surfacing.",
        whisper_type="STEWARDSHIP",
        evidence="evidence text",
        member_id="mem-test",
    )

    assert ok is True, (
        "_emit_whisper must return True when save_whisper succeeds"
    )
    state_store.save_whisper.assert_awaited_once()
    args = state_store.save_whisper.call_args
    assert args.args[0] == "inst-test", (
        "save_whisper must be called with instance_id as first arg"
    )


async def test_emit_whisper_passes_correct_fields_to_state_store():
    """The Whisper passed to save_whisper must carry the
    fields downstream consumers (disclosure gate, awareness loop,
    /dump rendering) read. Pin owner_member_id specifically since
    it's load-bearing for the cross-member disclosure filter."""
    state_store = MagicMock()
    state_store.save_whisper = AsyncMock(return_value=None)

    await _emit_whisper(
        state_store=state_store,
        instance_id="inst-test",
        space_id="space-test",
        text="The user's restraint shows steadiness.",
        whisper_type="STEWARDSHIP_INSIGHT",
        evidence="user volunteered the framing themselves",
        member_id="mem-target",
    )

    whisper = state_store.save_whisper.call_args.args[1]
    assert whisper.insight_text == (
        "The user's restraint shows steadiness."
    ), "insight_text must be the text arg verbatim"
    assert whisper.owner_member_id == "mem-target", (
        "owner_member_id must be the member_id arg — load-bearing for "
        "cross-member disclosure filtering"
    )
    assert whisper.foresight_signal == "STEWARDSHIP_INSIGHT", (
        "foresight_signal must be whisper_type for suppression-match "
        "discriminator"
    )
    assert whisper.whisper_id, (
        "whisper_id must be non-empty so persistence + suppression "
        "tracking work"
    )


async def test_emit_whisper_returns_false_when_save_raises():
    """When save_whisper raises (real-bug case: missing method, db
    write error), _emit_whisper must return False and log a warning
    rather than re-raising. This isolates the whisper failure from
    the surrounding harvest flow."""
    state_store = MagicMock()
    state_store.save_whisper = AsyncMock(side_effect=AttributeError(
        "simulated method-missing case"
    ))

    ok = await _emit_whisper(
        state_store=state_store,
        instance_id="inst-test",
        space_id="space-test",
        text="An insight that fails to save.",
        whisper_type="OPERATIONAL_INSIGHT",
        evidence="evidence",
        member_id="",
    )

    assert ok is False, (
        "_emit_whisper must return False on save_whisper failure"
    )


async def test_state_stores_expose_save_whisper_method():
    """Both StateStore implementations (json + sqlite) must expose
    save_whisper. This is the architectural-interface pin: any new
    StateStore implementation in the future must implement this
    method or the whisper-emission path silently fails the same way
    the soak harness caught."""
    from kernos.kernel.state_json import JsonStateStore
    from kernos.kernel.state_sqlite import SqliteStateStore
    assert hasattr(JsonStateStore, "save_whisper"), (
        "JsonStateStore must expose save_whisper"
    )
    assert hasattr(SqliteStateStore, "save_whisper"), (
        "SqliteStateStore must expose save_whisper"
    )
    # And neither should accidentally expose an alias under the
    # wrong name; the prior bug was a caller using add_whisper
    # against a state store that only had save_whisper.
    assert not hasattr(JsonStateStore, "add_whisper"), (
        "JsonStateStore must NOT expose add_whisper — "
        "the canonical name is save_whisper"
    )
    assert not hasattr(SqliteStateStore, "add_whisper"), (
        "SqliteStateStore must NOT expose add_whisper — "
        "the canonical name is save_whisper"
    )
