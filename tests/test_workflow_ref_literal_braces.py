"""Regression: workflow ref-resolver must not crash on literal braces.

Surfaced by the live `/fix` run (2026-05-29): the
`user_initiated_improvement` workflow's investigate step embeds a JSON
example (`{"failure_mode": ...}`) in its prompt. The resolver's
`{...}` token pattern matched the whole JSON block as a bogus
reference whose head is not a known namespace, and parameter-mode
raised `RefResolutionError` -> the workflow aborted at step 2 on
every invocation (silently, behind an "Investigating..." ack).

Fix: a `{...}` token whose head is NOT a known namespace
(workflow / idea_payload / step / gate) is treated as literal text,
not a failed reference. Genuine references into a known namespace
that fail to resolve still raise (typo detection preserved).
"""
from __future__ import annotations

import pytest

from kernos.kernel.workflows.refs import (
    RefResolutionError,
    ResolutionContext,
    resolve_references_in_value,
)


class _Exec:
    pass


def _ctx(mode: str = "parameter") -> ResolutionContext:
    return ResolutionContext(
        execution=_Exec(),
        trigger_payload={},
        step_outputs={},
        gate_outputs={},
        mode=mode,
    )


def test_embedded_json_block_is_left_literal():
    prompt = (
        "Investigate, then append:\n"
        "```json\n"
        "{\n"
        '  "failure_mode": "1-line classification",\n'
        '  "touches_paths": ["a.py"]\n'
        "}\n"
        "```"
    )
    out = resolve_references_in_value(prompt, _ctx())
    assert out == prompt  # unchanged; no RefResolutionError raised


def test_unknown_namespace_token_is_literal():
    assert (
        resolve_references_in_value("see {notes.foo}", _ctx())
        == "see {notes.foo}"
    )


def test_known_namespace_miss_still_raises():
    # A real reference into a known namespace that doesn't resolve must
    # still raise so genuine typos are caught.
    with pytest.raises(RefResolutionError):
        resolve_references_in_value("{step.s.value.nope}", _ctx())


def test_known_namespace_miss_predicate_mode_no_match():
    # Predicate mode never raises; unresolved -> no-match sentinel path.
    from kernos.kernel.workflows.refs import _NOT_FOUND

    assert (
        resolve_references_in_value("{step.s.value.nope}", _ctx("predicate"))
        is _NOT_FOUND
    )


def test_sole_reference_still_resolves_native_type():
    ctx = _ctx()
    ctx.step_outputs["mystep"] = {"value": {"approval_id": "abc123"}}
    assert (
        resolve_references_in_value("{step.mystep.value.approval_id}", ctx)
        == "abc123"
    )
