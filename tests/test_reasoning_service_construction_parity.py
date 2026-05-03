"""Pin test for the ReasoningService construction contract.

Per REASONING-SERVICE-CONSTRUCTION-PARITY-V1 (architect verdict +
Kit correction 2026-05-03): every callsite that constructs
ReasoningService MUST wire ``turn_runner_provider`` via the shared
helper at ``kernos.kernel.turn_runner_provider``. Copy-paste of the
closure pattern across files IS the failure mode this contract
exists to prevent.

This test walks every Python file in ``kernos/`` that imports
ReasoningService and constructs it, and asserts each construction
either:

  (a) passes ``turn_runner_provider=build_turn_runner_provider(...)``
      (or equivalent helper call) — preferred shape; OR
  (b) appears in the documented ``_EXCLUSIONS`` set with a named
      rationale — temporary opt-out for code paths that genuinely
      should not invoke cognition (e.g., type-only stubs, deprecated
      APIs scheduled for deletion).

Adding a new launcher without the shared helper fails CI.
"""

from __future__ import annotations

import ast
from pathlib import Path


# Files allowed to construct ReasoningService without the shared
# helper. Each entry must include a rationale comment describing
# WHY the file is exempt. Empty by default — exclusions added only
# with explicit architect approval.
_EXCLUSIONS: dict[str, str] = {
    # Format: "path/relative/to/repo/root.py": "rationale string"
    #
    # No exclusions exist post-CCV1-C7-flip. All five known callsites
    # (server.py, repl.py, app.py, chat.py, evals/bootstrap.py) wire
    # turn_runner_provider via the shared helper.
}


# Files that define ReasoningService (the class itself + tests-of-the-class)
# don't construct it as a consumer — they're not "callsites" in the
# CCV1 sense. Skip them by name.
_NON_CALLSITE_FILES = {
    "kernos/kernel/reasoning.py",  # defines the class
    "kernos/kernel/turn_runner_provider.py",  # the helper itself
    "kernos/setup/bring_up_substrate.py",  # tooling layer, doesn't construct
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _python_files_under(root: Path) -> list[Path]:
    out: list[Path] = []
    for p in root.rglob("*.py"):
        # Skip caches, venvs, archived dirs.
        parts = p.parts
        if any(part.startswith(".") for part in parts):
            continue
        if "__pycache__" in parts:
            continue
        if "data-archived" in parts or "data.archived-" in str(p):
            continue
        out.append(p)
    return out


def _construct_calls(tree: ast.AST) -> list[ast.Call]:
    """Find every call to a function literally named ReasoningService."""
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Direct: ReasoningService(...)
        if isinstance(func, ast.Name) and func.id == "ReasoningService":
            calls.append(node)
        # Attribute: kernos.kernel.reasoning.ReasoningService(...)
        elif isinstance(func, ast.Attribute) and func.attr == "ReasoningService":
            calls.append(node)
    return calls


def _has_turn_runner_provider_kw(call: ast.Call) -> bool:
    return any(
        kw.arg == "turn_runner_provider"
        for kw in call.keywords
    )


def test_every_reasoning_service_construction_uses_shared_helper():
    """Pin: every ReasoningService construction in kernos/ either
    wires turn_runner_provider or is in the exclusion list.

    Catches the regression where a future spec adds a new launcher
    that constructs ReasoningService without the shared helper —
    exactly the gap that surfaced post-CCV1-C7-flip on app.py /
    chat.py / evals/bootstrap.py.
    """
    root = _repo_root()
    kernos_dir = root / "kernos"
    failures: list[str] = []

    for path in _python_files_under(kernos_dir):
        rel = path.relative_to(root).as_posix()
        if rel in _NON_CALLSITE_FILES:
            continue

        try:
            source = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue

        if "ReasoningService" not in source:
            continue

        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            failures.append(f"{rel}: parse error — {exc}")
            continue

        for call in _construct_calls(tree):
            if _has_turn_runner_provider_kw(call):
                continue
            if rel in _EXCLUSIONS:
                continue
            failures.append(
                f"{rel}:{call.lineno} — ReasoningService constructed without "
                f"turn_runner_provider; either use "
                f"`turn_runner_provider=build_turn_runner_provider(ctx)` "
                f"per the shared helper, or add to _EXCLUSIONS with rationale."
            )

    assert not failures, (
        "ReasoningService construction-parity violations:\n  "
        + "\n  ".join(failures)
    )


def test_construction_contract_docstring_present():
    """Pin: the construction contract is documented at the
    ReasoningService class definition. Future authors find the
    contract at the construction site, not in a separate doc.
    """
    from kernos.kernel.reasoning import ReasoningService

    docstring = ReasoningService.__doc__ or ""
    assert "CONSTRUCTION CONTRACT" in docstring, (
        "ReasoningService class docstring must include the "
        "CONSTRUCTION CONTRACT block (per "
        "REASONING-SERVICE-CONSTRUCTION-PARITY-V1)"
    )
    assert "turn_runner_provider" in docstring, (
        "ReasoningService docstring must reference turn_runner_provider"
    )
    assert "shared helper" in docstring, (
        "ReasoningService docstring must point to the shared helper"
    )


def test_shared_helper_module_exports_expected_api():
    """Pin: the shared helper module exposes the canonical names
    every callsite imports."""
    from kernos.kernel import turn_runner_provider as helper

    expected = {
        "ThinPathContext",
        "build_turn_runner_provider",
        "setup_default_thin_path_context",
        "wire_live_thin_path",
    }
    actual = set(helper.__all__)
    missing = expected - actual
    assert not missing, (
        f"shared helper missing expected exports: {missing}"
    )
