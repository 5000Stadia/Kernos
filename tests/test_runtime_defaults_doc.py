"""RUNTIME-DEFAULTS-TRUTH-V1 — the documentation must fail CI when it lies.

`docs/reference/runtime-defaults.md` is the single authoritative statement of
the self-governance lane defaults. Prose cannot fail CI on its own; this makes
it able to. The doc is compared against the live predicates in
`kernos/kernel/governance_lanes.py` in BOTH directions.

Claim boundary: this enforces parity for the DECLARED set. The AST guardrail at
the bottom is a best-effort heuristic for lanes written to the existing
`is_enabled()` convention — it is explicitly NOT completeness enforcement, and
a lane that invents another shape can still evade it.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from kernos.kernel.governance_lanes import (
    GOVERNANCE_LANES, autonomy_loop_enabled, lane_states,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "reference" / "runtime-defaults.md"

VALUE_MATRIX = ["", "0", "1", "false", "true", "off", "on", "no", "yes",
                "disabled", "enabled", "banana"]


# --- parsing -----------------------------------------------------------------

def _backticked(cell: str) -> list[str]:
    return re.findall(r"`([^`]*)`", cell)


def _parse_table() -> list[dict]:
    """Parse the lane table. Fails LOUDLY on a non-conforming row."""
    rows: list[dict] = []
    started = False
    for line in DOC.read_text().splitlines():
        if line.startswith("| Key |"):
            started = True
            continue
        if not started:
            continue
        if line.startswith("|---"):
            continue
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        assert len(cells) == 6, f"malformed row (expected 6 cells): {line!r}"
        key, _lane, default, envs, when, module = cells

        key_m = _backticked(key)
        assert len(key_m) == 1, f"Key must be a single backticked value: {key!r}"
        assert default in ("**ON**", "**OFF**"), (
            f"Default must be exactly **ON** or **OFF**, got {default!r}")
        env_m = _backticked(envs)
        assert env_m, f"Env var(s) must be backticked: {envs!r}"
        when_m = _backticked(when)
        assert len(when_m) == 1, f"Enabled when must be backticked: {when!r}"
        mod_m = _backticked(module)
        assert len(mod_m) == 1, f"Module must be backticked: {module!r}"

        rows.append({
            "key": key_m[0],
            "default_on": default == "**ON**",
            "env_vars": tuple(env_m),
            "when": when_m[0],
            "module": mod_m[0],
        })
    assert rows, "no lane rows parsed out of the canonical table"
    return rows


def _eval_when(when: str, env_vars: tuple[str, ...], values: dict) -> bool:
    """Evaluate the documented grammar. Rejects anything not in the grammar."""
    if when == "all_nonempty":
        return all(values.get(v, "") for v in env_vars)
    m = re.fullmatch(r"(not )?in \{(.*)\}", when)
    assert m, f"ungrammatical `Enabled when`: {when!r}"
    negated = bool(m.group(1))
    members = {p.strip().strip('"') for p in m.group(2).split(",")}
    assert len(env_vars) == 1, f"membership form needs exactly 1 env var: {when!r}"
    actual = values.get(env_vars[0], "").strip().lower()
    hit = actual in members
    return (not hit) if negated else hit


# --- parity ------------------------------------------------------------------

def test_table_and_registry_declare_the_same_lanes():
    doc_keys = {r["key"] for r in _parse_table()}
    code_keys = {lane.key for lane in GOVERNANCE_LANES}
    assert doc_keys == code_keys, (
        "doc↔code lane sets diverged — a lane added to one and not the other "
        f"is exactly how the original drift happened. doc-only={doc_keys - code_keys} "
        f"code-only={code_keys - doc_keys}")


def test_every_machine_field_matches_the_registry():
    by_key = {lane.key: lane for lane in GOVERNANCE_LANES}
    for row in _parse_table():
        lane = by_key[row["key"]]
        assert row["env_vars"] == lane.env_vars, (
            f"{lane.key}: table env vars {row['env_vars']} != registry {lane.env_vars}")
        assert row["module"] == lane.module, (
            f"{lane.key}: table module {row['module']!r} != registry {lane.module!r}")


def test_documented_default_matches_live_behavior(monkeypatch):
    """The single default comparison: doc vs. the predicate with env cleared."""
    by_key = {lane.key: lane for lane in GOVERNANCE_LANES}
    for row in _parse_table():
        lane = by_key[row["key"]]
        for var in lane.env_vars:
            monkeypatch.delenv(var, raising=False)
        assert lane.predicate() is row["default_on"], (
            f"{lane.key}: doc says default {'ON' if row['default_on'] else 'OFF'}, "
            f"live predicate says {lane.predicate()}")


def test_documented_semantics_match_live_behavior(monkeypatch):
    """Every value the `Enabled when` grammar claims flips a lane actually does."""
    by_key = {lane.key: lane for lane in GOVERNANCE_LANES}
    for row in _parse_table():
        lane = by_key[row["key"]]
        for value in VALUE_MATRIX:
            values = {v: value for v in lane.env_vars}
            for var in lane.env_vars:
                monkeypatch.setenv(var, value)
            expected = _eval_when(row["when"], lane.env_vars, values)
            assert lane.predicate() is expected, (
                f"{lane.key} with {lane.env_vars}={value!r}: doc grammar "
                f"{row['when']!r} predicts {expected}, live says {lane.predicate()}")
            for var in lane.env_vars:
                monkeypatch.delenv(var, raising=False)


def test_module_paths_exist_from_repo_root():
    for lane in GOVERNANCE_LANES:
        assert (REPO_ROOT / lane.module).is_file(), (
            f"{lane.key}: module path {lane.module!r} does not exist from repo root")


def test_autonomy_loop_predicate_full_matrix():
    A, O = "KERNOS_ARCHITECT_ACTOR_ID", "KERNOS_OPERATOR_ACTOR_ID"
    assert autonomy_loop_enabled({}) is False
    assert autonomy_loop_enabled({A: "arch"}) is False       # architect only
    assert autonomy_loop_enabled({O: "op"}) is False         # operator only
    assert autonomy_loop_enabled({A: "arch", O: "op"}) is True
    assert autonomy_loop_enabled({A: "", O: "op"}) is False  # empty is not set


def test_lane_states_renders_every_lane():
    states = lane_states()
    assert {s["key"] for s in states} == {l.key for l in GOVERNANCE_LANES}


# --- pinned inconsistency: observed compatibility, NOT endorsement ------------

def test_inconsistent_truthiness_is_pinned_not_endorsed(monkeypatch):
    """These assertions record CURRENT behavior so the eventual normalization
    reads as a deliberate change rather than as a test someone broke.

    This is a real operator-intent hazard: an unrecognized value fails silently
    in inconsistent directions. Normalizing it is a behavior change to
    constitutional machinery and is tracked as its own follow-on.
    """
    from kernos.kernel.self_maintenance_review import is_enabled as smr_enabled
    from kernos.kernel.friction_response import is_enabled as fr_enabled

    monkeypatch.setenv("KERNOS_SELF_MAINTENANCE_REVIEW", "disabled")
    assert smr_enabled() is True, "an operator typing 'disabled' does NOT disable it"

    monkeypatch.setenv("KERNOS_FRICTION_RESPONSE", "enabled")
    assert fr_enabled() is False, "an operator typing 'enabled' does NOT enable it"


# --- best-effort discovery guardrail (NOT completeness enforcement) ----------

#: Modules that define an `is_enabled()` reading a KERNOS_* var but are not
#: self-governance lanes. Every entry needs a written reason.
DISCOVERY_EXEMPTIONS = {
    # (none today — add with a reason if a non-lane adopts the convention)
}


def _defines_env_gated_is_enabled(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text(errors="replace"))
    except Exception:
        return False
    for node in tree.body:  # module level only
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "is_enabled":
            continue
        return any(
            isinstance(n, ast.Constant) and isinstance(n.value, str)
            and n.value.startswith("KERNOS_")
            for n in ast.walk(node)
        )
    return False


def test_env_gated_lanes_are_registered_or_exempted():
    """Best-effort heuristic guardrail — explicitly NOT a completeness claim.

    Catches the realistic case: a new lane written to the existing
    `is_enabled()` convention. It cannot catch a lane that invents another
    shape. Closing that gap would require making registration mediate runtime
    activation, which is out of scope here.
    """
    registered = {lane.module for lane in GOVERNANCE_LANES}
    found = []
    for sub in ("kernos/kernel", "kernos/setup"):
        for path in sorted((REPO_ROOT / sub).rglob("*.py")):
            if _defines_env_gated_is_enabled(path):
                found.append(path.relative_to(REPO_ROOT).as_posix())
    unregistered = [m for m in found
                    if m not in registered and m not in DISCOVERY_EXEMPTIONS]
    assert not unregistered, (
        "module(s) define an env-gated is_enabled() but are neither in "
        f"GOVERNANCE_LANES nor exempted with a reason: {unregistered}")


# --- lane-scoped documentation audit ----------------------------------------

DEFAULT_ASSERTION = re.compile(
    r"default[- ](on|off)|defaults? to (on|off)|ships? default", re.I)


def test_default_assertions_for_these_lanes_live_only_in_the_table():
    """Identifier MENTIONS may appear anywhere; default ASSERTIONS may not.

    Scoped to these four lanes' identifiers — a global default-on/off grep is
    unsatisfiable because unrelated Kernos features legitimately use the words.
    """
    identifiers = set()
    for lane in GOVERNANCE_LANES:
        identifiers.update(lane.env_vars)
    offenders = []
    for path in sorted((REPO_ROOT / "docs").rglob("*.md")):
        if path == DOC:
            continue  # the canonical table is the one allowed place
        # Scan by PARAGRAPH, not by line: hard-wrapped prose routinely puts the
        # env var and the default claim on adjacent lines, and a line-scoped
        # check would wave that through.
        offset = 1
        for block in re.split(r"\n\s*\n", path.read_text(errors="replace")):
            if (any(ident in block for ident in identifiers)
                    and DEFAULT_ASSERTION.search(block)):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{offset}")
            offset += block.count("\n") + 2
    assert not offenders, (
        "default/enablement assertions for the governance lanes must appear "
        f"ONLY in {DOC.relative_to(REPO_ROOT)} — link there instead: {offenders}")
