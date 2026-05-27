"""Probe 7 — approval_loop_invariant (SUBSTRATE-SELF-TEST-V1).

Runs the REAL ImprovementLoopOrchestrator end-to-end against a
real ephemeral git repo + real ephemeral ledger + real
approval_receipts substrate. Walks the full orchestrator path:
workspace_created → spec_iteration → impl_iteration →
approval_requested. Asserts the resulting approval receipt has
the expected binding payload shape (attempt_id,
expected_parent_sha, expected_diff_hash).

v1 scope: consult_fn is a stub returning canned convergence text
(mirrors test_improvement_loop_workflow's pattern). The spec
also called for a fake-ACPX-binary that exercises the real
ACPX dispatch path; that's deferred to a follow-up because
standing up a PATH-resolvable fake claude-code-shaped binary
adds substantial fixture infrastructure. Per Codex round-1: the
substrate-fidelity intent — "orchestrator walks the full path
and produces a correctly-shaped binding receipt" — is captured
by this probe even with stub consult.

Regression bug: none specific — umbrella probe for orchestrator
composition. Catches regressions across workspace setup, ledger
events, approval-receipt binding shape, and orchestrator
state-machine ordering.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from kernos.kernel.self_test_gate import ProbeResult


REQUIRED_BEHAVIORAL_KEYS = frozenset({
    "attempt_id",
    "approval_id",
    "final_state",
})

REQUIRED_SUBSTRATE_KEYS = frozenset({
    "binding_payload_attempt_id",
    "binding_payload_expected_parent_sha",
    "binding_payload_expected_diff_hash",
    "ledger_event_kinds",
})


def _init_repo(repo_dir: Path) -> None:
    """Initialize a minimal git repo with one commit + an
    origin remote (the orchestrator queries origin to compute
    expected_parent_sha).
    """
    repo_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=str(repo_dir), env=env, check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "probe7@test.invalid"],
        cwd=str(repo_dir), env=env, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "probe7"],
        cwd=str(repo_dir), env=env, check=True,
    )
    (repo_dir / "README.md").write_text("probe7 test\n")
    subprocess.run(
        ["git", "add", "."],
        cwd=str(repo_dir), env=env, check=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"],
        cwd=str(repo_dir), env=env, check=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(repo_dir)],
        cwd=str(repo_dir), env=env, check=True,
    )
    subprocess.run(
        ["git", "fetch", "origin"],
        cwd=str(repo_dir), env=env, check=True,
    )


def _make_converging_consult():
    """Stub consult_fn that returns convergence text on first
    call (matches test_improvement_loop_workflow pattern)."""
    async def _consult(*, target: str, prompt: str) -> str:
        return "final spec content\n\nSTATUS: GREEN"
    return _consult


async def run_probe() -> ProbeResult:
    start = time.monotonic()

    # Lazy imports so monkeypatches reach the substrate this
    # probe exercises.
    from kernos.kernel import approval_receipts as _approvals
    from kernos.kernel import improvement_ledger as _ledger
    from kernos.kernel.improvement_loop_workflow import (
        ImprovementLoopOrchestrator,
    )
    from kernos.kernel.instance_db import InstanceDB

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp_path = Path(tmp_str)
        data_dir = str(tmp_path / "data")
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        repo_dir = tmp_path / "repo"
        _init_repo(repo_dir)

        # Stand up the substrate.
        db = InstanceDB(data_dir)
        await db.connect()
        await db.close()
        await _approvals.ensure_schema(data_dir=data_dir)

        # Real orchestrator, stub consult_fn (acknowledged v1
        # scope concession per docstring above).
        orch = ImprovementLoopOrchestrator(
            instance_id="probe7_test",
            data_dir=data_dir,
            live_repo_dir=str(repo_dir),
            consult_fn=_make_converging_consult(),
        )

        # Walk the full orchestrator path.
        attempt_id = await orch.start_attempt(
            spec_requirement="add a one-line comment to README",
        )
        await orch.wait_for_running_tasks(timeout=15)

        # Inspect the ledger for the canonical event sequence.
        db = InstanceDB(data_dir)
        await db.connect()
        try:
            events = await _ledger.get_attempt_events(
                db._conn, attempt_id,
            )
            event_kinds = [e["kind"] for e in events]

            # Find the approval_requested event to extract the
            # approval_id, then load the receipt.
            approval_id = ""
            for e in events:
                if e["kind"] == "approval_requested":
                    detail = e.get("detail", "")
                    if "approval_id=" in detail:
                        approval_id = detail.split(
                            "approval_id=",
                        )[1].strip()
                    break
        finally:
            await db.close()

        binding_attempt_id = ""
        binding_expected_parent_sha = ""
        binding_expected_diff_hash = ""
        receipt_kind = ""
        if approval_id:
            receipt = await _approvals.get_receipt(
                data_dir=data_dir, approval_id=approval_id,
            )
            if receipt is not None:
                binding_json = receipt.get(
                    "binding_payload_json", "{}",
                )
                binding = json.loads(binding_json)
                binding_attempt_id = binding.get("attempt_id", "")
                binding_expected_parent_sha = binding.get(
                    "expected_parent_sha", "",
                )
                binding_expected_diff_hash = binding.get(
                    "expected_diff_hash", "",
                )
                receipt_kind = receipt.get("kind", "")

    duration_ms = int((time.monotonic() - start) * 1000)

    # Pass conditions:
    # - the canonical 4-event sequence reached the ledger IN ORDER
    #   (Codex round-2 fold: pre-fix this checked membership only,
    #   not order — a regression that emitted them out-of-sequence
    #   would have passed)
    # - approval receipt round-tripped with correct binding fields
    expected_sequence = (
        "workspace_created",
        "spec_iteration",
        "impl_iteration",
        "approval_requested",
    )

    def _is_ordered_subsequence(seq, needle):
        """True iff every element of `needle` appears in `seq`
        in the same relative order."""
        it = iter(seq)
        return all(any(x == n for x in it) for n in needle)

    cond_events = _is_ordered_subsequence(event_kinds, expected_sequence)
    cond_attempt = (binding_attempt_id == attempt_id)
    cond_parent_sha = (
        isinstance(binding_expected_parent_sha, str)
        and len(binding_expected_parent_sha) == 40
        and all(
            c in "0123456789abcdef"
            for c in binding_expected_parent_sha
        )
    )
    cond_diff_hash = (
        isinstance(binding_expected_diff_hash, str)
        and binding_expected_diff_hash.startswith("sha256:")
    )
    cond_kind = (receipt_kind == "git_commit_authorization")

    all_passed = (
        cond_events and cond_attempt
        and cond_parent_sha and cond_diff_hash and cond_kind
    )

    failure_reason = ""
    if not all_passed:
        failed = []
        if not cond_events:
            failed.append(
                f"event_sequence (got={event_kinds!r}; expected "
                f"workspace_created + spec_iteration + "
                f"impl_iteration + approval_requested)"
            )
        if not cond_attempt:
            failed.append(
                f"attempt_id_roundtrip "
                f"(got={binding_attempt_id!r}, want={attempt_id!r})"
            )
        if not cond_parent_sha:
            failed.append(
                f"parent_sha_shape "
                f"(got={binding_expected_parent_sha!r})"
            )
        if not cond_diff_hash:
            failed.append(
                f"diff_hash_shape "
                f"(got={binding_expected_diff_hash!r})"
            )
        if not cond_kind:
            failed.append(
                f"receipt_kind "
                f"(got={receipt_kind!r}, "
                f"want='git_commit_authorization')"
            )
        failure_reason = (
            f"approval-loop invariant violated: {', '.join(failed)}. "
            f"Likely regression of ImprovementLoopOrchestrator path "
            f"(workspace → ledger → approval receipt) or the "
            f"binding-payload shape."
        )

    return ProbeResult(
        probe_name="approval_loop_invariant",
        passed=all_passed,
        behavioral_evidence={
            "attempt_id": attempt_id,
            "approval_id": approval_id,
            "final_state": (
                "approval_received_with_binding"
                if all_passed else "incomplete_or_corrupted"
            ),
        },
        substrate_evidence={
            "binding_payload_attempt_id": binding_attempt_id,
            "binding_payload_expected_parent_sha": (
                binding_expected_parent_sha
            ),
            "binding_payload_expected_diff_hash": (
                binding_expected_diff_hash
            ),
            "ledger_event_kinds": event_kinds,
        },
        duration_ms=duration_ms,
        failure_reason=failure_reason,
    )
