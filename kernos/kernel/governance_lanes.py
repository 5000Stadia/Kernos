"""RUNTIME-DEFAULTS-TRUTH-V1 — the declared set of self-governance lanes.

KERNOS's self-governance lane defaults were restated in four documents. When
SELF-MAINTENANCE-REVIEW-V3 flipped the daily review to default-ON, only one was
swept, and the stale docs were then capable of producing a confident false
public claim about the system's operational maturity.

This module is the code-side half of the fix: one production-owned declaration
of the lanes, compared against ``docs/reference/runtime-defaults.md`` by
``tests/test_runtime_defaults_doc.py``. Prose cannot fail CI; this can.

CLAIM BOUNDARY (deliberate, reviewed): this registry pins the DECLARED set and
enforces doc↔code parity for it. It does NOT claim to discover a lane
implemented entirely outside the ``is_enabled()`` convention — closing that
would require making registration mediate runtime activation, a behavior change
to constitutional startup machinery that does not belong in a documentation-
integrity change. A best-effort AST discovery guardrail supplements this; see
the test.

Note there is deliberately NO ``default_on`` field. The live default is
whatever ``predicate()`` returns with the environment cleared, so the doc is
compared against observed behavior rather than against a third restatement of
the same fact.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Mapping


@dataclass(frozen=True)
class GovernanceLane:
    """One self-governance lane and how to observe whether it is enabled."""

    key: str                      # stable machine key
    title: str                    # human label
    module: str                   # repo-relative path
    env_vars: tuple[str, ...]     # one, or two for the bring-up gate
    predicate: Callable[[], bool]  # the LIVE enable predicate
    summary: str                  # what it actually does when on


def autonomy_loop_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether ``improve_kernos``'s autonomy loop is wired at bring-up.

    Extracted as a pure predicate so it is testable across the full matrix
    (neither set / architect only / operator only / both). Both identities are
    required: the operator actor authorizes the workflow's autonomy-tool calls
    at execution time, so architect-without-operator would register a loop whose
    every call fails — the "no half-initialised autonomy loop" invariant.
    """
    src = os.environ if env is None else env
    return bool(src.get("KERNOS_ARCHITECT_ACTOR_ID", "")
                and src.get("KERNOS_OPERATOR_ACTOR_ID", ""))


def _self_maintenance_review_enabled() -> bool:
    from kernos.kernel.self_maintenance_review import is_enabled
    return is_enabled()


def _friction_response_enabled() -> bool:
    from kernos.kernel.friction_response import is_enabled
    return is_enabled()


def _recursive_self_heal_enabled() -> bool:
    from kernos.kernel.recursive_self_heal import is_enabled
    return is_enabled()


#: The authoritative declared set. Adding a lane here without adding a row to
#: docs/reference/runtime-defaults.md fails the parity test, and vice versa.
GOVERNANCE_LANES: tuple[GovernanceLane, ...] = (
    GovernanceLane(
        key="self_maintenance_review",
        title="Daily self-maintenance review (Shape A)",
        module="kernos/kernel/self_maintenance_review.py",
        env_vars=("KERNOS_SELF_MAINTENANCE_REVIEW",),
        predicate=_self_maintenance_review_enabled,
        summary=("Reviews ONE element of its own code per day through a "
                 "corrective and a generative lens and surfaces a reflection "
                 "to consider. Reflection-only — it never changes code."),
    ),
    GovernanceLane(
        key="friction_response",
        title="Friction response (Shape B)",
        module="kernos/kernel/friction_response.py",
        env_vars=("KERNOS_FRICTION_RESPONSE",),
        predicate=_friction_response_enabled,
        summary=("Reactively diagnoses the most-pressing open friction report "
                 "and surfaces a diagnosis, with anti-loop two-key memory."),
    ),
    GovernanceLane(
        key="recursive_self_heal",
        title="Recursive self-heal",
        module="kernos/kernel/recursive_self_heal.py",
        env_vars=("KERNOS_RECURSIVE_SELF_HEAL",),
        predicate=_recursive_self_heal_enabled,
        summary=("A BOUNDED one-child repair when an attempt aborts on a bug "
                 "in the loop machinery itself — not a general self-repair "
                 "capability. Guardrail-touching repairs route to human "
                 "review even when verified."),
    ),
    GovernanceLane(
        key="autonomy_loop",
        title="improve_kernos autonomy loop (bring-up)",
        module="kernos/setup/bring_up_substrate.py",
        env_vars=("KERNOS_ARCHITECT_ACTOR_ID", "KERNOS_OPERATOR_ACTOR_ID"),
        predicate=autonomy_loop_enabled,
        summary=("Wires the spec→implement→review→approve→deploy loop at "
                 "bring-up. Skipped entirely unless BOTH identities are set, "
                 "so the loop is either fully wireable or not started."),
    ),
)


def lane_states() -> list[dict]:
    """Live lane state for operator diagnostics. Best-effort per lane."""
    out: list[dict] = []
    for lane in GOVERNANCE_LANES:
        try:
            enabled = bool(lane.predicate())
        except Exception:
            enabled = None  # unobservable — say so rather than guess
        out.append({
            "key": lane.key, "title": lane.title, "module": lane.module,
            "env_vars": list(lane.env_vars), "enabled": enabled,
        })
    return out
