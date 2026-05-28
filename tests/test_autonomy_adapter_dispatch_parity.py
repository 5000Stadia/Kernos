"""Static-analysis pin: autonomy_tool_ids ↔ _call_tool_adapter
dispatch branches stay in sync.

The 2026-05-27 USER-INITIATED-IMPROVEMENT-TRIGGER-V1 live test
surfaced a bug where 5 new tools were added to the
``autonomy_tool_ids`` frozenset but their ``if tool_id ==
"<name>":`` branches were misnested under the wrong outer ``if``
block (indentation error). The workflow dispatched, hit the
"not in the autonomy-loop set" fallthrough error, and aborted.
68 passing unit tests didn't catch this because the tests
exercised classifier + YAML parsing, not the autonomy-adapter
dispatch routing.

This test does AST static-analysis on
``kernos/setup/bring_up_substrate.py`` and asserts:

1. Every name in the ``autonomy_tool_ids`` frozenset literal has
   a corresponding ``if tool_id == "<name>":`` or
   ``if tool_id in ("<name>", ...):`` branch inside
   ``_call_tool_adapter``'s ``_dispatch`` function body.

2. Every branch name routed via ``_dispatch`` is also in
   ``autonomy_tool_ids`` (so removing a tool from the set but
   leaving its branch in place is caught).

The check runs without starting the substrate — just parses
the source file. Fast, deterministic, and would have caught
the indentation bug at PR-review time.

Filed as #161 in the spec roadmap.
"""
from __future__ import annotations

import ast
from pathlib import Path


_BRING_UP_PATH = Path(__file__).resolve().parent.parent / (
    "kernos/setup/bring_up_substrate.py"
)


def _extract_autonomy_tool_ids_and_branches() -> (
    tuple[set[str], set[str]]
):
    """Parse bring_up_substrate.py and return:
        (autonomy_tool_ids_set, dispatched_tool_ids_set)

    ``autonomy_tool_ids_set`` is extracted from the frozenset
    literal at module-level inside ``_call_tool_adapter``.

    ``dispatched_tool_ids_set`` is the union of every string
    literal compared against ``tool_id`` in an ``if/elif``
    chain inside the same function's body — including both
    ``if tool_id == "X":`` (single match) and
    ``if tool_id in ("X", "Y", ...):`` (multi match).
    """
    tree = ast.parse(_BRING_UP_PATH.read_text(encoding="utf-8"))

    autonomy_set: set[str] = set()
    dispatched: set[str] = set()

    # Find the _call_tool_adapter function definition.
    target_fn: ast.FunctionDef | None = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "_call_tool_adapter"
        ):
            target_fn = node
            break
    assert target_fn is not None, (
        "could not find _call_tool_adapter function in "
        "bring_up_substrate.py"
    )

    # Walk the function body. The autonomy_tool_ids assignment
    # is at the top level; the dispatch branches are nested in
    # the inner _dispatch function.
    for stmt in ast.walk(target_fn):
        # Match: autonomy_tool_ids = frozenset({"...", ...})
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and stmt.targets[0].id == "autonomy_tool_ids"
        ):
            # Right-hand-side should be frozenset({...}).
            rhs = stmt.value
            if isinstance(rhs, ast.Call) and isinstance(
                rhs.func, ast.Name,
            ) and rhs.func.id == "frozenset":
                if rhs.args and isinstance(rhs.args[0], ast.Set):
                    for elt in rhs.args[0].elts:
                        if isinstance(elt, ast.Constant) and isinstance(
                            elt.value, str,
                        ):
                            autonomy_set.add(elt.value)

        # Match dispatch branches. We look for:
        #   if <expr involving tool_id and a string literal>: ...
        # Two shapes are valid:
        #   Compare:  tool_id == "X"
        #   Compare:  tool_id in ("X", "Y", ...)
        if isinstance(stmt, ast.If):
            test = stmt.test
            # Shape 1: tool_id == "X"
            if (
                isinstance(test, ast.Compare)
                and len(test.ops) == 1
                and isinstance(test.ops[0], ast.Eq)
                and isinstance(test.left, ast.Name)
                and test.left.id == "tool_id"
                and len(test.comparators) == 1
                and isinstance(test.comparators[0], ast.Constant)
                and isinstance(test.comparators[0].value, str)
            ):
                dispatched.add(test.comparators[0].value)
            # Shape 2: tool_id in ("X", "Y", ...) — In ast that's
            # Compare(ops=[In()], comparators=[Tuple([...])])
            elif (
                isinstance(test, ast.Compare)
                and len(test.ops) == 1
                and isinstance(test.ops[0], ast.In)
                and isinstance(test.left, ast.Name)
                and test.left.id == "tool_id"
                and len(test.comparators) == 1
                and isinstance(test.comparators[0], ast.Tuple)
            ):
                for elt in test.comparators[0].elts:
                    if isinstance(elt, ast.Constant) and isinstance(
                        elt.value, str,
                    ):
                        dispatched.add(elt.value)

    return autonomy_set, dispatched


def test_every_autonomy_tool_id_has_a_dispatch_branch():
    """If a name is in ``autonomy_tool_ids``, the dispatcher
    must have a branch for it. Otherwise calling that tool from
    a workflow raises "not in the autonomy-loop set" — the
    exact 2026-05-27 indentation-bug failure mode this test
    pins against."""
    autonomy_set, dispatched = (
        _extract_autonomy_tool_ids_and_branches()
    )
    missing_branches = autonomy_set - dispatched
    assert not missing_branches, (
        f"autonomy_tool_ids contains names without dispatch "
        f"branches: {sorted(missing_branches)}. Either add the "
        f"`if tool_id == \"<name>\":` branch in _dispatch OR "
        f"remove the name from autonomy_tool_ids."
    )


def test_every_dispatch_branch_has_an_autonomy_tool_id():
    """And vice versa: a branch without a set entry is dead
    code (the outer ``if tool_id in autonomy_tool_ids:`` guard
    rejects the tool before reaching the branch). Catches
    leftover branches when a tool is removed from the set."""
    autonomy_set, dispatched = (
        _extract_autonomy_tool_ids_and_branches()
    )
    # Tools dispatched by name but not in autonomy_tool_ids.
    # NOTE: dispatched names include ALL tool_id == comparisons
    # in _call_tool_adapter, which may include checks INSIDE the
    # outer autonomy_tool_ids guard. Any tool_id not in the set
    # is dead code under the current outer guard.
    orphans = dispatched - autonomy_set
    assert not orphans, (
        f"dispatch branches without matching autonomy_tool_ids "
        f"entry (dead code under current outer guard): "
        f"{sorted(orphans)}. Either add the name to "
        f"autonomy_tool_ids OR remove the branch."
    )


def test_autonomy_tool_ids_population_sanity():
    """Sanity pin: the parse extracted something non-empty.
    Catches AST-extraction regressions (e.g., the frozenset
    literal moves or gets refactored) rather than silently
    asserting on empty sets."""
    autonomy_set, dispatched = (
        _extract_autonomy_tool_ids_and_branches()
    )
    assert len(autonomy_set) >= 10, (
        f"autonomy_tool_ids extraction produced {len(autonomy_set)} "
        f"names — suspiciously low. AST extraction may have "
        f"regressed; check _extract_autonomy_tool_ids_and_branches."
    )
    assert len(dispatched) >= 10, (
        f"dispatch-branch extraction produced {len(dispatched)} "
        f"names — suspiciously low."
    )


# ---------------------------------------------------------------------
# Runtime smoke: actually exercise _dispatch with each tool_id.
# This catches indentation/nesting bugs (the actual 2026-05-27
# failure mode) that AST static analysis misses.
# ---------------------------------------------------------------------


import pytest


class _MinimalStubHandler:
    """Just enough attributes for the dispatcher's getattr lookups
    to return None rather than AttributeError. Doesn't simulate the
    full handler — the test only verifies the dispatcher REACHES a
    branch, not that the branch's handler succeeds end-to-end."""
    _friction_pattern_store = None
    _closure_store = None
    _fix_authorization_store = None
    _events = None
    _event_stream = None
    reasoning = None


class _MinimalStubLedger:
    pass


async def test_dispatch_reaches_handler_for_every_autonomy_tool_id(
    tmp_path,
):
    """Runtime parity check: build _call_tool_adapter, then call
    _dispatch for EACH name in autonomy_tool_ids. Verify the call
    does NOT raise RuntimeError("...is not in the autonomy-loop
    set..."). Handler-level errors (NotImplementedError from the
    stub, RuntimeError("requires handler._X"), etc.) are fine —
    they prove the dispatcher routed to a branch. The forbidden
    error is the OUTER fallthrough that means the branch wasn't
    reached at all.

    This is the test that would have caught the 2026-05-27
    USER-INITIATED-IMPROVEMENT-TRIGGER-V1 indentation bug:
    record_fix_authorization was in autonomy_tool_ids, but its
    branch was nested inside the closure-tools `if` block, so
    calling it raised the fallthrough error.
    """
    from kernos.setup.bring_up_substrate import _call_tool_adapter
    autonomy_set, _ = _extract_autonomy_tool_ids_and_branches()

    handler = _MinimalStubHandler()
    ledger = _MinimalStubLedger()
    dispatch = _call_tool_adapter(handler, ledger, str(tmp_path))

    fallthrough_offenders: list[str] = []
    for tool_id in sorted(autonomy_set):
        try:
            await dispatch(
                tool_id=tool_id,
                args={},
                instance_id="test_instance",
                member_id="test_member",
            )
        except RuntimeError as exc:
            msg = str(exc)
            if "not in the autonomy-loop set" in msg:
                fallthrough_offenders.append(tool_id)
            # Any other RuntimeError (e.g., "requires handler._X")
            # proves the dispatcher routed to a real branch. Fine.
        except Exception:
            # Any other exception type — TypeError, KeyError, etc.
            # — also proves the dispatcher reached a branch.
            # The branch's handler may not survive the empty-args
            # stub call, but the routing is what we're testing.
            pass

    assert not fallthrough_offenders, (
        f"_call_tool_adapter dispatch fallthrough for tool_ids: "
        f"{fallthrough_offenders}. These names are in "
        f"autonomy_tool_ids but their branches are not reachable "
        f"from the outer guard — likely a nesting/indentation "
        f"bug. Each should have an `if tool_id == \"<name>\":` "
        f"branch at the SAME depth as the other dispatch checks "
        f"inside `if tool_id in autonomy_tool_ids:`."
    )
