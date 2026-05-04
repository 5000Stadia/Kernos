"""Static parity assertion for legacy-loop-primitive contracts.

TEST-INFRA-PARITY-V1 Part 4 (architect spec 2026-05-03): for the
legacy-loop-primitive behaviors that the Bucket 2 tests pinned, this
file documents where each contract is verified at the thin-path
production seam, and pins that each named verification surface
exists.

Why this file is lightweight, not a behavior test
-------------------------------------------------

The Bucket 2 tests in test_reasoning, test_handler, etc. now run
against the shared test fixture in ``tests/_thin_path_test_fixture.py``.
That fixture mimics the legacy reasoning loop's behavior so bare
``ReasoningService`` construction works post-CCV1-C7-strike. The
Bucket 2 tests therefore verify what their assertions say (e.g.,
"tools fire concurrently"), but against the fixture's stub, not
against the production thin-path pipeline (TurnRunner /
IntegrationService / EnactmentService / StepDispatcher).

The contracts the legacy loop delivered MUST also hold on the
production thin-path pipeline. Production-pipeline tests live at
the thin-path seam — see the table below. This parity-assertion
file pins that those tests exist; their detailed assertions live
in their own files.

The discipline is: the Bucket 2 tests pin agent-experience-shape
contracts (when the agent fires two read tools, both fire); the
thin-path-seam tests pin implementation-shape contracts (when the
StepDispatcher receives two read steps, asyncio.gather dispatches
them); the soak harness pins end-to-end-shape contracts on real
LLM calls.

Contract surface
----------------

| Legacy-loop primitive       | Thin-path production seam                             | New-seam contract test                                |
|------------------------------|--------------------------------------------------------|--------------------------------------------------------|
| Tool concurrency for reads   | StepDispatcher / live integration dispatcher           | tests/test_thin_path_executor_wiring.py + test_step_dispatcher* |
| Tool-result block ordering   | PresenceRenderer tool-use loop + integration adapter   | tests/test_thin_path_tool_use_loop.py                  |
| Per-turn token aggregation   | ProductionResponseDelivery + AggregatedTelemetry       | tests/test_response_delivery.py                        |

Future kick-back trigger
------------------------

If a fourth Bucket 2 behavior surfaces during the rewrite that
isn't yet pinned at a thin-path-seam test, that's a real
architectural gap. Don't fold a stub into the fixture; surface as
a parity sub-spec per the architect's documented kick-back
contract.
"""

from __future__ import annotations

from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent


# Each entry: (legacy primitive name, thin-path seam, list of contract-test files)
_LOOP_PRIMITIVE_CONTRACTS: list[tuple[str, str, list[str]]] = [
    (
        "tool_concurrency_for_reads",
        "StepDispatcher / live integration dispatcher",
        [
            "tests/test_thin_path_executor_wiring.py",
        ],
    ),
    (
        "tool_result_block_ordering",
        "PresenceRenderer tool-use loop",
        [
            "tests/test_thin_path_tool_use_loop.py",
        ],
    ),
    (
        "per_turn_token_aggregation",
        "ProductionResponseDelivery + AggregatedTelemetry",
        [
            "tests/test_response_delivery.py",
        ],
    ),
]


def test_each_loop_primitive_has_named_thin_path_contract_tests():
    """Pin: every legacy-loop-primitive behavior the Bucket 2 tests
    pinned has a named thin-path-seam contract test that exists on
    disk.

    Future kick-back trigger: when a Bucket 2 behavior is added to
    this list without an existing contract-test file, this test
    fails — surfacing the real architectural gap rather than letting
    the fixture's stub paper over it.
    """
    missing: list[str] = []
    for name, seam, contract_files in _LOOP_PRIMITIVE_CONTRACTS:
        for f in contract_files:
            path = _REPO_ROOT / f
            if not path.exists():
                missing.append(f"{name} ({seam}): {f} does not exist")
    assert not missing, (
        "TEST-INFRA-PARITY-V1 Part 4: legacy-loop-primitive contracts "
        "without thin-path-seam contract tests:\n  "
        + "\n  ".join(missing)
        + "\nAdd the missing test or kick back per the spec's "
        "documented contract."
    )


def test_thin_path_test_fixture_module_exists():
    """The shared fixture is the canonical surface that the Bucket 1
    integration tests use to construct ReasoningService post-strike.
    Pin: the module exists and exports its public API."""
    from tests import _thin_path_test_fixture as fixture
    assert hasattr(fixture, "make_test_turn_runner_provider")
    assert hasattr(fixture, "wire_test_thin_path")
    assert "make_test_turn_runner_provider" in fixture.__all__
    assert "wire_test_thin_path" in fixture.__all__


def test_three_make_helpers_use_shared_fixture():
    """Pin: the three test-helper functions that bare-construct
    ReasoningService (test_reasoning::_make_service,
    test_engine::_make_engine, test_handler::_make_handler) all
    invoke wire_test_thin_path on the constructed service.

    Without this pin, a future test-file edit that drops the
    fixture-wire call would silently re-introduce the
    TurnRunnerNotWired regression at unit-test scope.
    """
    helper_files = [
        "tests/test_reasoning.py",
        "tests/test_engine.py",
        "tests/test_handler.py",
    ]
    missing: list[str] = []
    for relpath in helper_files:
        path = _REPO_ROOT / relpath
        src = path.read_text()
        if "wire_test_thin_path" not in src:
            missing.append(relpath)
    assert not missing, (
        "TEST-INFRA-PARITY-V1: test-helper functions missing the "
        f"wire_test_thin_path call: {missing}. "
        "Each _make_* helper that constructs ReasoningService "
        "must invoke wire_test_thin_path on the service so the "
        "post-strike construction contract is honored."
    )


def test_every_reasoning_service_construction_in_named_test_files_is_wired():
    """Pin: every ``ReasoningService(...)`` construction in the three
    helper files is followed by a ``wire_test_thin_path(reasoning,
    ...)`` call within ten lines.

    Codex C5-review (2026-05-03) found that test_handler.py had two
    ad-hoc ReasoningService constructions outside ``_make_handler``
    that the original parity-V1 commit didn't wire. Latent bug —
    masked by the legacy reasoning loop being still alive — would
    have manifested post-strike. This pin catches the same shape of
    drift in any future addition.
    """
    import re
    helper_files = [
        "tests/test_reasoning.py",
        "tests/test_engine.py",
        "tests/test_handler.py",
    ]
    failures: list[str] = []
    for relpath in helper_files:
        src = (_REPO_ROOT / relpath).read_text()
        lines = src.splitlines()
        for i, line in enumerate(lines):
            if not re.search(r"=\s*ReasoningService\(", line):
                continue
            # Look ahead up to 10 lines for wire_test_thin_path.
            window = "\n".join(lines[i:i + 10])
            if "wire_test_thin_path" not in window:
                failures.append(f"{relpath}:{i + 1}")
    assert not failures, (
        "TEST-INFRA-PARITY-V1: ReasoningService constructions "
        f"without wire_test_thin_path within 10 lines: {failures}. "
        "Each construction site must wire the stub turn-runner-"
        "provider so the post-strike construction contract is "
        "honored. See _thin_path_test_fixture.wire_test_thin_path."
    )
