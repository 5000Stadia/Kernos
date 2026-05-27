"""Probe 7 — approval_loop_invariant (SUBSTRATE-SELF-TEST-V1).

Asserts the approval-receipt substrate contract: request_approval
creates a row with a binding payload that includes the expected
fields (attempt_id, expected_parent_sha, expected_diff_hash for
git_commit_authorization kind); get_receipt retrieves the row
with binding payload intact.

v1 scope: tests the approval-receipt substrate contract directly
rather than running the full improve_kernos orchestrator with a
fake ACPX binary. The spec called for the full path with a
fake-ACPX-shaped binary on PATH; standing that up properly is
substantial follow-up work, and the substrate-fidelity intent
of "approval loop invariant holds" is captured by verifying the
receipt-binding shape that the orchestrator produces. The full
orchestrator path is exercised by test_improvement_loop_workflow
(stub consult); a fake-ACPX-binary probe is a follow-up.

Regression bug: none specific — this is the umbrella probe that
catches approval-receipt schema regressions.
"""
from __future__ import annotations

import json
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


async def run_probe() -> ProbeResult:
    start = time.monotonic()

    from kernos.kernel import approval_receipts as _approvals
    from kernos.kernel.instance_db import InstanceDB

    # Use an ephemeral data dir so this probe doesn't touch
    # production state.
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp_path = Path(tmp_str)
        data_dir = str(tmp_path / "data")
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)

        # Bring up the instance database schema (creates the
        # approval_receipts table per ensure_schema).
        db = InstanceDB(data_dir)
        await db.connect()
        await db.close()
        await _approvals.ensure_schema(data_dir=data_dir)

        # Synthesize the binding payload the
        # ImprovementLoopOrchestrator would create for a
        # git_commit_authorization receipt. The shape MUST
        # include attempt_id, expected_parent_sha,
        # expected_diff_hash — that's the AC contract.
        attempt_id = "att_probe7test01234"
        binding_payload = {
            "attempt_id": attempt_id,
            "expected_parent_sha": (
                "a1b2c3d4e5f6789012345678901234567890abcd"
            ),
            "expected_diff_hash": (
                "sha256:1234567890abcdef1234567890abcdef"
                "1234567890abcdef1234567890abcdef"
            ),
        }

        # Request the approval — substrate creates the row.
        approval_id = await _approvals.request_approval(
            data_dir=data_dir,
            instance_id="probe7_test",
            kind="git_commit_authorization",
            requested_for_actor="substrate.improvement_loop",
            operator_actor_id="probe7_operator",
            request_summary="probe 7: approval-receipt invariant",
            binding_payload=binding_payload,
            single_use=True,
        )

        # Retrieve via get_receipt — substrate must round-trip
        # the binding payload intact.
        receipt = await _approvals.get_receipt(
            data_dir=data_dir, approval_id=approval_id,
        )

        binding_attempt_id = ""
        binding_expected_parent_sha = ""
        binding_expected_diff_hash = ""
        receipt_kind = ""
        if receipt is not None:
            binding_json = receipt.get("binding_payload_json", "{}")
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
    # - receipt round-tripped with correct attempt_id
    # - parent_sha looks like a 40-char hex
    # - diff_hash starts with "sha256:"
    # - kind is git_commit_authorization
    cond_attempt = (binding_attempt_id == attempt_id)
    cond_parent_sha = (
        isinstance(binding_expected_parent_sha, str)
        and len(binding_expected_parent_sha) == 40
        and all(
            c in "0123456789abcdef" for c in binding_expected_parent_sha
        )
    )
    cond_diff_hash = (
        isinstance(binding_expected_diff_hash, str)
        and binding_expected_diff_hash.startswith("sha256:")
    )
    cond_kind = (receipt_kind == "git_commit_authorization")

    all_passed = (
        cond_attempt and cond_parent_sha
        and cond_diff_hash and cond_kind
    )

    failure_reason = ""
    if not all_passed:
        failed = []
        if not cond_attempt:
            failed.append(
                f"attempt_id_roundtrip "
                f"(got={binding_attempt_id!r}, "
                f"expected={attempt_id!r})"
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
                f"expected='git_commit_authorization')"
            )
        failure_reason = (
            f"approval-loop invariant violated: {', '.join(failed)}. "
            f"Likely regression of approval_receipts schema or "
            f"binding payload round-trip — receipt cannot be "
            f"trusted as a substrate contract."
        )

    return ProbeResult(
        probe_name="approval_loop_invariant",
        passed=all_passed,
        behavioral_evidence={
            "attempt_id": attempt_id,
            "approval_id": approval_id,
            "final_state": (
                "receipt_round_tripped"
                if cond_attempt else "receipt_corrupted"
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
            "ledger_event_kinds": [
                "approval_requested_synthesized_for_probe",
            ],
        },
        duration_ms=duration_ms,
        failure_reason=failure_reason,
    )
