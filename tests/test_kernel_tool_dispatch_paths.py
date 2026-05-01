"""CLEANUP-BATCH-V1 item 11: structural drift detection for kernel
tool dispatch.

The audit observed that ``kernos/kernel/reasoning.py`` has two elif
chains dispatching kernel tools:

* Chain 1 in ``execute_tool()`` — the confirmed-action path. Raises
  on errors. Used when a gate-blocked tool call gets confirmed and
  also by the scheduler when triggers fire tools.
* Chain 2 in ``reason()`` — the main agent tool loop. Wraps each
  handler in try/except with friendly fallback strings.

The two chains are not equivalent. Five tools (``inspect_state``,
``set_chain_model``, ``diagnose_llm_chain``, ``diagnose_messenger``,
``remember_details``) appear only in Chain 2 by design — they're
read-only or chain-management surfaces that never produce
confirmable PendingActions.

Full handler extraction was scoped out (separate follow-on spec).
Instead, ``ReasoningService._KERNEL_TOOL_PATHS`` declares which
dispatch paths each tool joins. This test pins both chains to that
declaration:

1. Every name in ``_KERNEL_TOOLS`` has a paths entry.
2. Every paths entry's tool name is in ``_KERNEL_TOOLS``.
3. Tools declaring ``"loop"`` have a matching ``elif block.name ==
   "<name>"`` branch in the reason loop.
4. Tools declaring ``"confirmed"`` have a matching ``elif
   tool_name == "<name>"`` branch in execute_tool, OR are routed
   via the canvas helper in execute_tool's tail.

Drift detection: adding or moving a tool to one chain without
updating its paths declaration fails this test.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from kernos.kernel.reasoning import ReasoningService

REPO_ROOT = Path(__file__).resolve().parent.parent
REASONING_PATH = REPO_ROOT / "kernos" / "kernel" / "reasoning.py"


def _reasoning_source() -> str:
    return REASONING_PATH.read_text(encoding="utf-8")


def _loop_chain_tool_names() -> set[str]:
    """Parse ``if/elif block.name == "<name>"`` and tuple-membership
    branches out of reason()'s main tool-loop chain (Chain 2).

    Captures three forms:
      * Direct equality: ``if/elif block.name == "<name>"``
      * Tuple membership: ``elif block.name in ("diagnose_issue", ...)``
        (diagnostic tools share dispatch through the diagnostics
        module).
    """
    text = _reasoning_source()
    direct = set(re.findall(
        r"(?:if|elif)\s+block\.name\s*==\s*\"([a-z_]+)\"", text,
    ))
    tuple_names: set[str] = set()
    for tuple_match in re.finditer(
        r"(?:if|elif)\s+block\.name\s+in\s+\(([^)]+)\):", text,
    ):
        for tok in re.findall(r"\"([a-z_]+)\"", tuple_match.group(1)):
            tuple_names.add(tok)
    return direct | tuple_names


def _confirmed_chain_tool_names() -> set[str]:
    """Parse ``if/elif tool_name == "<name>"`` lines out of
    execute_tool() (Chain 1) plus the canvas-tuple branch.

    Captures three forms:
      * Direct equality: ``if/elif tool_name == "<name>"``
      * Tuple membership: ``elif tool_name in ("canvas_list", ...)``
        (canvas tools route through _handle_canvas_tool)
    """
    text = _reasoning_source()
    direct = set(re.findall(
        r"(?:if|elif)\s+tool_name\s*==\s*\"([a-z_]+)\"", text,
    ))
    # Canvas tail: a single elif handles a tuple of names.
    canvas_names: set[str] = set()
    for tuple_match in re.finditer(
        r"(?:if|elif)\s+tool_name\s+in\s+\(([^)]+)\):", text,
    ):
        for tok in re.findall(r"\"([a-z_]+)\"", tuple_match.group(1)):
            canvas_names.add(tok)
    return direct | canvas_names


# ---------------------------------------------------------------------------
# Registry shape invariants
# ---------------------------------------------------------------------------


class TestRegistryCompleteness:
    def test_every_kernel_tool_has_paths_entry(self):
        missing = ReasoningService._KERNEL_TOOLS - set(
            ReasoningService._KERNEL_TOOL_PATHS,
        )
        assert not missing, (
            f"_KERNEL_TOOLS contains {sorted(missing)} that have no "
            "_KERNEL_TOOL_PATHS entry. Add the missing entries to "
            "the registry in kernos/kernel/reasoning.py."
        )

    def test_no_orphan_paths_entries(self):
        extra = set(ReasoningService._KERNEL_TOOL_PATHS) - (
            ReasoningService._KERNEL_TOOLS
        )
        assert not extra, (
            f"_KERNEL_TOOL_PATHS contains {sorted(extra)} that are "
            "not in _KERNEL_TOOLS. Either remove the entries or add "
            "the names to _KERNEL_TOOLS."
        )

    def test_paths_use_known_tokens(self):
        valid = {"loop", "confirmed", "helper"}
        for name, paths in ReasoningService._KERNEL_TOOL_PATHS.items():
            invalid = set(paths) - valid
            assert not invalid, (
                f"tool {name!r} declares unknown paths "
                f"{sorted(invalid)}; valid tokens are "
                f"{sorted(valid)}"
            )
            assert paths, (
                f"tool {name!r} declares empty paths set; pick at "
                "least one of: loop, confirmed, helper"
            )


# ---------------------------------------------------------------------------
# Chain conformance — declarations must match actual elif branches
# ---------------------------------------------------------------------------


class TestLoopChainConformance:
    def test_loop_path_tools_have_loop_elif(self):
        loop_chain = _loop_chain_tool_names()
        declared_loop = {
            name for name, paths in
            ReasoningService._KERNEL_TOOL_PATHS.items()
            if "loop" in paths
        }
        missing_in_chain = declared_loop - loop_chain
        assert not missing_in_chain, (
            f"Tools declared as loop-path but missing from the "
            f"reason() elif chain: {sorted(missing_in_chain)}. "
            "Either add the matching `elif block.name == \"<name>\"` "
            "branch to reasoning.py, or remove `loop` from the "
            "tool's _KERNEL_TOOL_PATHS entry."
        )

    def test_loop_chain_tools_are_declared(self):
        loop_chain = _loop_chain_tool_names()
        # Filter to names actually in _KERNEL_TOOLS (exclude any
        # accidental match on non-tool elif lines).
        loop_chain &= ReasoningService._KERNEL_TOOLS
        declared_loop = {
            name for name, paths in
            ReasoningService._KERNEL_TOOL_PATHS.items()
            if "loop" in paths
        }
        not_declared = loop_chain - declared_loop
        assert not not_declared, (
            f"reason() elif chain handles {sorted(not_declared)} "
            "but their _KERNEL_TOOL_PATHS entries don't include "
            "`loop`. Add `loop` to those entries or remove the elif "
            "branches."
        )


class TestConfirmedChainConformance:
    def test_confirmed_path_tools_have_confirmed_elif(self):
        confirmed_chain = _confirmed_chain_tool_names()
        declared_confirmed = {
            name for name, paths in
            ReasoningService._KERNEL_TOOL_PATHS.items()
            if "confirmed" in paths
        }
        missing_in_chain = declared_confirmed - confirmed_chain
        assert not missing_in_chain, (
            f"Tools declared as confirmed-path but missing from "
            f"execute_tool()'s elif chain: "
            f"{sorted(missing_in_chain)}. Either add the matching "
            "`elif tool_name == \"<name>\"` branch, or remove "
            "`confirmed` from the tool's paths entry."
        )

    def test_confirmed_chain_tools_are_declared(self):
        confirmed_chain = _confirmed_chain_tool_names()
        confirmed_chain &= ReasoningService._KERNEL_TOOLS
        declared_confirmed = {
            name for name, paths in
            ReasoningService._KERNEL_TOOL_PATHS.items()
            if "confirmed" in paths
        }
        not_declared = confirmed_chain - declared_confirmed
        assert not not_declared, (
            f"execute_tool() handles {sorted(not_declared)} but "
            "their _KERNEL_TOOL_PATHS entries don't include "
            "`confirmed`. Add `confirmed` to those entries or "
            "remove the elif branches."
        )


class TestIntentionalLoopOnlyTools:
    """Pin the intentional Chain 1 omissions so future maintainers
    don't accidentally move them. These five tools are read-only or
    chain-management surfaces; they shouldn't produce confirmable
    PendingActions, so they stay loop-only by design."""

    EXPECTED_LOOP_ONLY = frozenset({
        "remember_details",
        "inspect_state",
        "set_chain_model",
        "diagnose_llm_chain",
        "diagnose_messenger",
    })

    def test_expected_tools_are_loop_only(self):
        for name in self.EXPECTED_LOOP_ONLY:
            paths = ReasoningService._KERNEL_TOOL_PATHS[name]
            assert paths == frozenset({"loop"}), (
                f"tool {name!r} should remain loop-only by design "
                f"(read-only / chain-management); got paths={paths}. "
                "If this changed deliberately, also update "
                "EXPECTED_LOOP_ONLY in this test."
            )
