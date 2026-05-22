"""DURABLE-APPROVAL-RECEIPTS-V1 (2026-05-21) acceptance tests.

Covers spec ACs 1-10 + a few derived invariants:
- AC1  schema ensure idempotent
- AC2  request_approval creates pending row + emits approval.requested
- AC3  two-step /approve (preview, then CONFIRM) transitions + emits
- AC4  /reject single-step transitions + captures reason
- AC5  expiry pass on pending; emits BOTH approval.decision_recorded(expired)
       AND approval.expired
- AC6  expiry guards consume: approved + expired cannot consume
- AC7  consume CAS atomic vs double-consume; full predicate
- AC8  restart fidelity (DB-only state survives reopen)
- AC9  boot reconcile re-emits terminal receipts w/ NULL decision_emitted_at
- AC9.5 crash-between-enqueue-and-flush recoverable (flush-before-marker)
- AC10 workflow integration smoke: event payload shape matches what
       the existing _on_post_flush_for_gates expects
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest


# ============================================================
# Test fixtures
# ============================================================


class _CapturingEventStream:
    """Stub event stream that captures emit calls + flush_now AND
    persists events to the receipts' instance.db so the durability
    read-back (kernos.kernel.approval_receipts._verify_event_in_db)
    actually finds them. The receipts impl now verifies events are
    durable in the DB before marking decision_emitted_at — a
    capturing-only stub leaves the marker NULL.

    Set ``flush_should_raise=True`` to simulate a flush that doesn't
    persist (events stay captured in memory only). That's the
    crash-recovery test path: the marker stays NULL, boot reconcile
    must re-emit."""

    def __init__(self, data_dir: str | Path | None = None):
        self.events: list[tuple[str, str, dict]] = []
        self.flush_calls = 0
        self.flush_should_raise = False
        self._data_dir = data_dir
        self._pending_inserts: list[tuple] = []

    async def emit(self, instance_id, event_type, payload):
        event_id = f"evt_{len(self.events) + 1}"
        self.events.append((instance_id, event_type, payload))
        # Queue for flush; only land in DB on flush_now success.
        self._pending_inserts.append(
            (event_id, instance_id, event_type, payload),
        )
        return event_id

    async def flush_now(self):
        self.flush_calls += 1
        if self.flush_should_raise:
            return  # Simulate flush_now returning despite no DB write
        if self._data_dir is None or not self._pending_inserts:
            self._pending_inserts.clear()
            return
        import json as _json
        import aiosqlite as _aiosqlite
        # Ensure the events table exists (event_stream writes to
        # the same instance.db; our stub uses a minimal schema).
        async with _aiosqlite.connect(
            str(Path(self._data_dir) / "instance.db"),
        ) as db:
            await db.execute(
                "CREATE TABLE IF NOT EXISTS events ("
                " event_id TEXT PRIMARY KEY, instance_id TEXT, "
                " member_id TEXT, space_id TEXT, timestamp TEXT, "
                " event_type TEXT, payload TEXT, "
                " correlation_id TEXT, source_module TEXT)"
            )
            for event_id, instance_id, event_type, payload in self._pending_inserts:
                await db.execute(
                    "INSERT OR REPLACE INTO events "
                    "(event_id, instance_id, event_type, payload) "
                    "VALUES (?, ?, ?, ?)",
                    (event_id, instance_id, event_type, _json.dumps(payload)),
                )
            await db.commit()
        self._pending_inserts.clear()


# ============================================================
# AC1 — schema ensure idempotent
# ============================================================


class TestSchemaEnsure:
    @pytest.mark.asyncio
    async def test_creates_schema_on_first_call(self, tmp_path):
        from kernos.kernel.approval_receipts import ensure_schema, get_receipt
        await ensure_schema(str(tmp_path))
        # Schema present: a lookup against an unknown id returns None
        # rather than raising
        result = await get_receipt(
            data_dir=str(tmp_path), approval_id="nonexistent",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_idempotent_re_call(self, tmp_path):
        from kernos.kernel.approval_receipts import ensure_schema
        await ensure_schema(str(tmp_path))
        # Second call must not raise
        await ensure_schema(str(tmp_path))


# ============================================================
# AC2 — request_approval creates pending + emits requested event
# ============================================================


class TestRequestApproval:
    @pytest.mark.asyncio
    async def test_creates_pending_row_and_emits(self, tmp_path):
        from kernos.kernel.approval_receipts import (
            ensure_schema, request_approval, get_receipt,
        )
        await ensure_schema(str(tmp_path))
        stream = _CapturingEventStream(str(tmp_path))
        approval_id = await request_approval(
            data_dir=str(tmp_path),
            instance_id="test_inst",
            kind="autonomous_commit",
            requested_for_actor="agent_a",
            operator_actor_id="owner_member",
            request_summary="approve a test action",
            binding_payload={"foo": "bar"},
            ttl_seconds=600,
            event_stream=stream,
        )
        assert isinstance(approval_id, str) and len(approval_id) > 0
        receipt = await get_receipt(
            data_dir=str(tmp_path), approval_id=approval_id,
        )
        assert receipt is not None
        assert receipt["state"] == "pending"
        assert receipt["kind"] == "autonomous_commit"
        assert json.loads(receipt["binding_payload_json"]) == {"foo": "bar"}
        assert receipt["decision_emitted_at"] is None
        # operator_member_id defaults to operator_actor_id per v1 contract
        assert receipt["operator_member_id"] == "owner_member"
        # approval.requested event was queued
        emitted_types = [t for _, t, _ in stream.events]
        assert "approval.requested" in emitted_types

    @pytest.mark.asyncio
    async def test_workflow_fields_must_be_paired(self, tmp_path):
        from kernos.kernel.approval_receipts import (
            ensure_schema, request_approval,
        )
        await ensure_schema(str(tmp_path))
        # Supplying execution_id without gate_nonce must raise
        with pytest.raises(ValueError):
            await request_approval(
                data_dir=str(tmp_path),
                instance_id="test_inst",
                kind="autonomous_commit",
                requested_for_actor="agent_a",
                operator_actor_id="owner_member",
                request_summary="test",
                binding_payload={},
                workflow_execution_id="exec_1",
                gate_nonce=None,
                event_stream=None,
            )


# ============================================================
# AC3 — two-step /approve
# ============================================================


class TestApprove:
    @pytest.mark.asyncio
    async def test_approve_transitions_and_emits_full_payload(self, tmp_path):
        from kernos.kernel.approval_receipts import (
            ensure_schema, request_approval, approve, get_receipt,
        )
        await ensure_schema(str(tmp_path))
        stream = _CapturingEventStream(str(tmp_path))
        approval_id = await request_approval(
            data_dir=str(tmp_path),
            instance_id="test_inst",
            kind="test_kind",
            requested_for_actor="agent",
            operator_actor_id="owner",
            request_summary="t",
            binding_payload={},
            workflow_execution_id="exec_42",
            gate_nonce="nonce_xyz",
            event_stream=stream,
        )
        ok, msg = await approve(
            data_dir=str(tmp_path),
            approval_id=approval_id,
            instance_id="test_inst",
            invoking_member_id="owner",
            event_stream=stream,
        )
        assert ok is True
        receipt = await get_receipt(
            data_dir=str(tmp_path), approval_id=approval_id,
        )
        assert receipt["state"] == "approved"
        assert receipt["decided_at"] is not None
        assert receipt["decision_emitted_at"] is not None
        # The decision_recorded event must carry execution_id + gate_nonce
        # (required by _on_post_flush_for_gates binding check)
        decision_events = [
            p for _, t, p in stream.events
            if t == "approval.decision_recorded"
        ]
        assert len(decision_events) == 1
        payload = decision_events[0]
        assert payload["approval_id"] == approval_id
        assert payload["decision"] == "approved"
        assert payload["execution_id"] == "exec_42"
        assert payload["gate_nonce"] == "nonce_xyz"
        assert stream.flush_calls >= 1

    @pytest.mark.asyncio
    async def test_wrong_member_refused(self, tmp_path):
        from kernos.kernel.approval_receipts import (
            ensure_schema, request_approval, approve, get_receipt,
        )
        await ensure_schema(str(tmp_path))
        stream = _CapturingEventStream(str(tmp_path))
        approval_id = await request_approval(
            data_dir=str(tmp_path), instance_id="i", kind="k",
            requested_for_actor="a", operator_actor_id="owner_xyz",
            request_summary="t", binding_payload={},
            event_stream=stream,
        )
        ok, msg = await approve(
            data_dir=str(tmp_path),
            approval_id=approval_id,
            instance_id="i",
            invoking_member_id="not_the_owner",
            event_stream=stream,
        )
        assert ok is False
        assert "operator" in msg.lower()
        receipt = await get_receipt(
            data_dir=str(tmp_path), approval_id=approval_id,
        )
        assert receipt["state"] == "pending"


# ============================================================
# AC4 — /reject single-step + captures reason
# ============================================================


class TestReject:
    @pytest.mark.asyncio
    async def test_reject_captures_reason(self, tmp_path):
        from kernos.kernel.approval_receipts import (
            ensure_schema, request_approval, reject, get_receipt,
        )
        await ensure_schema(str(tmp_path))
        stream = _CapturingEventStream(str(tmp_path))
        approval_id = await request_approval(
            data_dir=str(tmp_path), instance_id="i", kind="k",
            requested_for_actor="a", operator_actor_id="owner",
            request_summary="t", binding_payload={},
            event_stream=stream,
        )
        ok, msg = await reject(
            data_dir=str(tmp_path), approval_id=approval_id,
            instance_id="i", invoking_member_id="owner",
            reason="not aligned with intent",
            event_stream=stream,
        )
        assert ok is True
        receipt = await get_receipt(
            data_dir=str(tmp_path), approval_id=approval_id,
        )
        assert receipt["state"] == "rejected"
        assert receipt["state_reason"] == "not aligned with intent"
        # decision_recorded event with decision="rejected"
        decision_events = [
            p for _, t, p in stream.events
            if t == "approval.decision_recorded"
        ]
        assert len(decision_events) == 1
        assert decision_events[0]["decision"] == "rejected"
        assert decision_events[0]["reason"] == "not aligned with intent"


# ============================================================
# AC5 — expiry pass on pending
# ============================================================


class TestExpiryPass:
    @pytest.mark.asyncio
    async def test_pending_with_past_expiry_transitions_to_expired(
        self, tmp_path,
    ):
        from kernos.kernel.approval_receipts import (
            ensure_schema, request_approval, expire_pass, get_receipt,
        )
        await ensure_schema(str(tmp_path))
        stream = _CapturingEventStream(str(tmp_path))
        # Request with 0s TTL so it expires immediately
        approval_id = await request_approval(
            data_dir=str(tmp_path), instance_id="i", kind="k",
            requested_for_actor="a", operator_actor_id="owner",
            request_summary="t", binding_payload={},
            ttl_seconds=0,
            event_stream=stream,
        )
        # Wait a hair for any time precision issues
        await asyncio.sleep(0.05)
        count = await expire_pass(
            data_dir=str(tmp_path), event_stream=stream,
        )
        assert count == 1
        receipt = await get_receipt(
            data_dir=str(tmp_path), approval_id=approval_id,
        )
        assert receipt["state"] == "expired"
        # BOTH events emitted: decision_recorded(expired) AND approval.expired
        emitted_types = [t for _, t, _ in stream.events]
        assert "approval.decision_recorded" in emitted_types
        assert "approval.expired" in emitted_types
        decision_events = [
            p for _, t, p in stream.events
            if t == "approval.decision_recorded"
        ]
        assert decision_events[-1]["decision"] == "expired"


# ============================================================
# AC6 — expiry guards consume
# ============================================================


class TestExpiryGuardsConsume:
    @pytest.mark.asyncio
    async def test_approved_but_expired_cannot_be_consumed(self, tmp_path):
        from kernos.kernel.approval_receipts import (
            ensure_schema, request_approval, approve, consume_approval,
        )
        import aiosqlite
        await ensure_schema(str(tmp_path))
        stream = _CapturingEventStream(str(tmp_path))
        # Request with longer TTL so approve succeeds, then expire manually
        approval_id = await request_approval(
            data_dir=str(tmp_path), instance_id="i", kind="k",
            requested_for_actor="a", operator_actor_id="owner",
            request_summary="t", binding_payload={},
            ttl_seconds=3600, event_stream=stream,
        )
        ok, _ = await approve(
            data_dir=str(tmp_path), approval_id=approval_id,
            instance_id="i", invoking_member_id="owner",
            event_stream=stream,
        )
        assert ok
        # Manually rewind expires_at into the past
        async with aiosqlite.connect(
            str(Path(tmp_path) / "instance.db"),
        ) as db:
            await db.execute(
                "UPDATE approval_receipts SET expires_at='2020-01-01T00:00:00+00:00' "
                "WHERE approval_id=?", (approval_id,),
            )
            await db.commit()
        # Consume must refuse
        consumed = await consume_approval(
            data_dir=str(tmp_path),
            approval_id=approval_id,
            instance_id="i",
        )
        assert consumed is False


# ============================================================
# AC7 — atomic CAS double-consume
# ============================================================


class TestConsumeAtomic:
    @pytest.mark.asyncio
    async def test_concurrent_consume_only_one_wins(self, tmp_path):
        from kernos.kernel.approval_receipts import (
            ensure_schema, request_approval, approve, consume_approval,
            get_receipt,
        )
        await ensure_schema(str(tmp_path))
        stream = _CapturingEventStream(str(tmp_path))
        approval_id = await request_approval(
            data_dir=str(tmp_path), instance_id="i", kind="k",
            requested_for_actor="a", operator_actor_id="owner",
            request_summary="t", binding_payload={},
            ttl_seconds=3600, event_stream=stream,
        )
        await approve(
            data_dir=str(tmp_path), approval_id=approval_id,
            instance_id="i", invoking_member_id="owner",
            event_stream=stream,
        )
        # Two concurrent consume calls
        results = await asyncio.gather(
            consume_approval(
                data_dir=str(tmp_path),
                approval_id=approval_id, instance_id="i",
            ),
            consume_approval(
                data_dir=str(tmp_path),
                approval_id=approval_id, instance_id="i",
            ),
        )
        assert sorted(results) == [False, True]
        receipt = await get_receipt(
            data_dir=str(tmp_path), approval_id=approval_id,
        )
        assert receipt["state"] == "consumed"

    @pytest.mark.asyncio
    async def test_consume_wrong_instance_refused(self, tmp_path):
        from kernos.kernel.approval_receipts import (
            ensure_schema, request_approval, approve, consume_approval,
        )
        await ensure_schema(str(tmp_path))
        stream = _CapturingEventStream(str(tmp_path))
        approval_id = await request_approval(
            data_dir=str(tmp_path), instance_id="instance_a", kind="k",
            requested_for_actor="a", operator_actor_id="owner",
            request_summary="t", binding_payload={},
            ttl_seconds=3600, event_stream=stream,
        )
        await approve(
            data_dir=str(tmp_path), approval_id=approval_id,
            instance_id="instance_a", invoking_member_id="owner",
            event_stream=stream,
        )
        # Wrong instance must refuse
        ok = await consume_approval(
            data_dir=str(tmp_path), approval_id=approval_id,
            instance_id="instance_b",
        )
        assert ok is False


# ============================================================
# AC8 — restart fidelity (DB-only state survives reopen)
# ============================================================


class TestRestartFidelity:
    @pytest.mark.asyncio
    async def test_pending_receipt_survives_module_reimport(self, tmp_path):
        from kernos.kernel.approval_receipts import (
            ensure_schema, request_approval, get_receipt, approve,
        )
        await ensure_schema(str(tmp_path))
        stream = _CapturingEventStream(str(tmp_path))
        approval_id = await request_approval(
            data_dir=str(tmp_path), instance_id="i", kind="k",
            requested_for_actor="a", operator_actor_id="owner",
            request_summary="t", binding_payload={},
            ttl_seconds=3600, event_stream=stream,
        )
        # Simulate restart by creating a fresh stream (in-memory state
        # like event stream emit-queue is reset; DB persists)
        fresh_stream = _CapturingEventStream(str(tmp_path))
        receipt = await get_receipt(
            data_dir=str(tmp_path), approval_id=approval_id,
        )
        assert receipt is not None
        assert receipt["state"] == "pending"
        # /approve works post-"restart"
        ok, _ = await approve(
            data_dir=str(tmp_path), approval_id=approval_id,
            instance_id="i", invoking_member_id="owner",
            event_stream=fresh_stream,
        )
        assert ok is True


# ============================================================
# AC9 + AC9.5 — boot reconcile re-emits NULL decision_emitted_at
# ============================================================


class TestBootReconcile:
    @pytest.mark.asyncio
    async def test_boot_reconcile_reemits_null_marker(self, tmp_path):
        from kernos.kernel.approval_receipts import (
            ensure_schema, request_approval, boot_reconcile, get_receipt,
        )
        import aiosqlite
        await ensure_schema(str(tmp_path))
        stream = _CapturingEventStream(str(tmp_path))
        approval_id = await request_approval(
            data_dir=str(tmp_path), instance_id="i", kind="k",
            requested_for_actor="a", operator_actor_id="owner",
            request_summary="t", binding_payload={},
            ttl_seconds=3600, event_stream=stream,
        )
        # Simulate "crashed mid-approve" — set state=approved but leave
        # decision_emitted_at NULL
        async with aiosqlite.connect(
            str(Path(tmp_path) / "instance.db"),
        ) as db:
            await db.execute(
                "UPDATE approval_receipts SET state='approved', "
                "decided_at='2026-05-21T00:00:00+00:00', "
                "decision_emitted_at=NULL WHERE approval_id=?",
                (approval_id,),
            )
            await db.commit()
        # Reconcile must re-emit
        fresh_stream = _CapturingEventStream(str(tmp_path))
        count = await boot_reconcile(
            data_dir=str(tmp_path), event_stream=fresh_stream,
        )
        assert count >= 1
        receipt = await get_receipt(
            data_dir=str(tmp_path), approval_id=approval_id,
        )
        assert receipt["decision_emitted_at"] is not None
        emitted = [t for _, t, _ in fresh_stream.events]
        assert "approval.decision_recorded" in emitted
        # Second reconcile must NOT re-emit (marker is populated)
        second_stream = _CapturingEventStream(str(tmp_path))
        await boot_reconcile(
            data_dir=str(tmp_path), event_stream=second_stream,
        )
        assert all(
            t != "approval.decision_recorded"
            for _, t, _ in second_stream.events
        )

    @pytest.mark.asyncio
    async def test_flush_failure_leaves_marker_null_for_reconcile(self, tmp_path):
        """AC9.5: if flush_now raises, decision_emitted_at stays NULL
        so boot reconcile catches the orphan."""
        from kernos.kernel.approval_receipts import (
            ensure_schema, request_approval, approve, get_receipt,
        )
        await ensure_schema(str(tmp_path))
        stream = _CapturingEventStream(str(tmp_path))
        approval_id = await request_approval(
            data_dir=str(tmp_path), instance_id="i", kind="k",
            requested_for_actor="a", operator_actor_id="owner",
            request_summary="t", binding_payload={},
            ttl_seconds=3600, event_stream=stream,
        )
        # Set the stream to raise on flush
        stream.flush_should_raise = True
        ok, _ = await approve(
            data_dir=str(tmp_path), approval_id=approval_id,
            instance_id="i", invoking_member_id="owner",
            event_stream=stream,
        )
        # approve still returned True — the state CAS succeeded;
        # only the post-CAS emit/flush failed. Receipt should be
        # in state=approved with decision_emitted_at=NULL.
        assert ok is True
        receipt = await get_receipt(
            data_dir=str(tmp_path), approval_id=approval_id,
        )
        assert receipt["state"] == "approved"
        assert receipt["decision_emitted_at"] is None


# ============================================================
# AC10 — workflow gate integration smoke
# ============================================================


class TestWorkflowGateIntegrationSmoke:
    @pytest.mark.asyncio
    async def test_decision_event_payload_has_execution_id_and_gate_nonce(
        self, tmp_path,
    ):
        """The existing _on_post_flush_for_gates binding check
        (execution_engine.py:1688) requires payload.execution_id and
        payload.gate_nonce. Pin that the receipt's decision event
        carries both."""
        from kernos.kernel.approval_receipts import (
            ensure_schema, request_approval, approve,
        )
        await ensure_schema(str(tmp_path))
        stream = _CapturingEventStream(str(tmp_path))
        approval_id = await request_approval(
            data_dir=str(tmp_path), instance_id="i", kind="k",
            requested_for_actor="a", operator_actor_id="owner",
            request_summary="t", binding_payload={},
            workflow_execution_id="exec_workflow_a",
            gate_nonce="nonce_a",
            event_stream=stream,
        )
        await approve(
            data_dir=str(tmp_path), approval_id=approval_id,
            instance_id="i", invoking_member_id="owner",
            event_stream=stream,
        )
        decision = [
            p for _, t, p in stream.events
            if t == "approval.decision_recorded"
        ][0]
        assert decision["execution_id"] == "exec_workflow_a"
        assert decision["gate_nonce"] == "nonce_a"
        assert decision["approval_id"] == approval_id


# ============================================================
# Outcome payload write-back (used by improvement loop's git_commit)
# ============================================================


class TestSchemaIndexesAndConstraints:
    """AC1 strengthening: assert the schema actually carries the
    indexes + CHECK constraints the spec specifies."""

    @pytest.mark.asyncio
    async def test_indexes_present(self, tmp_path):
        import aiosqlite
        from kernos.kernel.approval_receipts import ensure_schema
        await ensure_schema(str(tmp_path))
        async with aiosqlite.connect(
            str(Path(tmp_path) / "instance.db"),
        ) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND tbl_name='approval_receipts'"
            ) as cur:
                names = sorted(r["name"] for r in await cur.fetchall())
        for required in (
            "idx_approval_receipts_state",
            "idx_approval_receipts_pending_per_instance",
            "idx_approval_receipts_expiry",
            "idx_approval_receipts_workflow",
            "idx_approval_receipts_reconcile_pending_emit",
        ):
            assert required in names, f"missing index {required}"

    @pytest.mark.asyncio
    async def test_state_check_constraint_rejects_invalid_state(self, tmp_path):
        import aiosqlite
        from kernos.kernel.approval_receipts import (
            ensure_schema, request_approval,
        )
        await ensure_schema(str(tmp_path))
        approval_id = await request_approval(
            data_dir=str(tmp_path), instance_id="i", kind="k",
            requested_for_actor="a", operator_actor_id="owner",
            request_summary="t", binding_payload={},
            event_stream=None,
        )
        async with aiosqlite.connect(
            str(Path(tmp_path) / "instance.db"),
        ) as db:
            with pytest.raises(aiosqlite.IntegrityError):
                await db.execute(
                    "UPDATE approval_receipts SET state='bogus' WHERE approval_id=?",
                    (approval_id,),
                )
                await db.commit()


class TestExpiryPassBatch:
    """AC5 strengthening: multiple receipts in one pass all
    transition; per-row CAS holds."""

    @pytest.mark.asyncio
    async def test_multiple_expired_receipts_in_one_pass(self, tmp_path):
        from kernos.kernel.approval_receipts import (
            ensure_schema, request_approval, expire_pass, get_receipt,
        )
        await ensure_schema(str(tmp_path))
        stream = _CapturingEventStream(str(tmp_path))
        ids = []
        for i in range(3):
            ids.append(await request_approval(
                data_dir=str(tmp_path), instance_id="i", kind="k",
                requested_for_actor="a", operator_actor_id="owner",
                request_summary=f"t{i}", binding_payload={},
                ttl_seconds=0, event_stream=stream,
            ))
        await asyncio.sleep(0.05)
        count = await expire_pass(
            data_dir=str(tmp_path), event_stream=stream,
        )
        assert count == 3
        for approval_id in ids:
            receipt = await get_receipt(
                data_dir=str(tmp_path), approval_id=approval_id,
            )
            assert receipt["state"] == "expired"
            assert receipt["decision_emitted_at"] is not None


class TestSingleUseZero:
    """AC7 strengthening: single_use=0 receipts can't be consumed by
    the strict CAS predicate (single_use=1 only)."""

    @pytest.mark.asyncio
    async def test_single_use_zero_receipt_refused_by_consume(self, tmp_path):
        from kernos.kernel.approval_receipts import (
            ensure_schema, request_approval, approve, consume_approval,
        )
        await ensure_schema(str(tmp_path))
        stream = _CapturingEventStream(str(tmp_path))
        approval_id = await request_approval(
            data_dir=str(tmp_path), instance_id="i", kind="k",
            requested_for_actor="a", operator_actor_id="owner",
            request_summary="t", binding_payload={},
            ttl_seconds=3600, single_use=False, event_stream=stream,
        )
        await approve(
            data_dir=str(tmp_path), approval_id=approval_id,
            instance_id="i", invoking_member_id="owner",
            event_stream=stream,
        )
        ok = await consume_approval(
            data_dir=str(tmp_path), approval_id=approval_id,
            instance_id="i",
        )
        # Codex implementation nit: keep single_use=True for v1
        # callers — multi-use semantics not yet defined. The CAS
        # refuses single_use=0 here, which pins the v1 contract.
        assert ok is False


class TestBootReconcileAfterFlushFailureRecovers:
    """AC9.5 strengthening: full crash-recovery cycle — simulate flush
    failure, then run boot reconcile, assert the event is durably
    re-emitted + marker populated."""

    @pytest.mark.asyncio
    async def test_full_recovery_cycle(self, tmp_path):
        from kernos.kernel.approval_receipts import (
            ensure_schema, request_approval, approve, boot_reconcile,
            get_receipt,
        )
        await ensure_schema(str(tmp_path))
        stream = _CapturingEventStream(str(tmp_path))
        approval_id = await request_approval(
            data_dir=str(tmp_path), instance_id="i", kind="k",
            requested_for_actor="a", operator_actor_id="owner",
            request_summary="t", binding_payload={},
            ttl_seconds=3600, event_stream=stream,
        )
        stream.flush_should_raise = True
        await approve(
            data_dir=str(tmp_path), approval_id=approval_id,
            instance_id="i", invoking_member_id="owner",
            event_stream=stream,
        )
        # Marker should be NULL — flush raised
        receipt = await get_receipt(
            data_dir=str(tmp_path), approval_id=approval_id,
        )
        assert receipt["decision_emitted_at"] is None

        # Simulate "restart" with a healthy stream
        fresh_stream = _CapturingEventStream(str(tmp_path))
        count = await boot_reconcile(
            data_dir=str(tmp_path), event_stream=fresh_stream,
        )
        assert count >= 1
        receipt = await get_receipt(
            data_dir=str(tmp_path), approval_id=approval_id,
        )
        # Now durable + marker populated
        assert receipt["decision_emitted_at"] is not None
        # Exactly one decision_recorded event emitted by the reconcile
        decision_events = [
            e for e in fresh_stream.events
            if e[1] == "approval.decision_recorded"
        ]
        assert len(decision_events) == 1


class TestExactlyOneEmitOnApprove:
    """AC9 strengthening: assert exactly one decision_recorded event
    per approve call (not zero, not duplicates)."""

    @pytest.mark.asyncio
    async def test_exactly_one_decision_event_per_approve(self, tmp_path):
        from kernos.kernel.approval_receipts import (
            ensure_schema, request_approval, approve,
        )
        await ensure_schema(str(tmp_path))
        stream = _CapturingEventStream(str(tmp_path))
        approval_id = await request_approval(
            data_dir=str(tmp_path), instance_id="i", kind="k",
            requested_for_actor="a", operator_actor_id="owner",
            request_summary="t", binding_payload={},
            ttl_seconds=3600, event_stream=stream,
        )
        ok, _ = await approve(
            data_dir=str(tmp_path), approval_id=approval_id,
            instance_id="i", invoking_member_id="owner",
            event_stream=stream,
        )
        assert ok
        decision_events = [
            e for e in stream.events
            if e[1] == "approval.decision_recorded"
        ]
        assert len(decision_events) == 1


class TestConsumeOutcomePayload:
    @pytest.mark.asyncio
    async def test_consume_writes_outcome_payload(self, tmp_path):
        """consume_approval can carry an outcome_payload that lands in
        outcome_payload_json (NOT binding_payload_json — binding is
        immutable). The improvement loop's git_commit writes commit_sha
        through this path."""
        from kernos.kernel.approval_receipts import (
            ensure_schema, request_approval, approve, consume_approval,
            get_receipt,
        )
        await ensure_schema(str(tmp_path))
        stream = _CapturingEventStream(str(tmp_path))
        approval_id = await request_approval(
            data_dir=str(tmp_path), instance_id="i", kind="k",
            requested_for_actor="a", operator_actor_id="owner",
            request_summary="t",
            binding_payload={"expected_parent_sha": "abc123"},
            ttl_seconds=3600, event_stream=stream,
        )
        await approve(
            data_dir=str(tmp_path), approval_id=approval_id,
            instance_id="i", invoking_member_id="owner",
            event_stream=stream,
        )
        ok = await consume_approval(
            data_dir=str(tmp_path), approval_id=approval_id,
            instance_id="i",
            outcome_payload={"commit_sha": "def456"},
        )
        assert ok is True
        receipt = await get_receipt(
            data_dir=str(tmp_path), approval_id=approval_id,
        )
        # binding_payload_json unchanged (immutable)
        assert json.loads(receipt["binding_payload_json"]) == {
            "expected_parent_sha": "abc123",
        }
        # outcome_payload_json populated
        assert json.loads(receipt["outcome_payload_json"]) == {
            "commit_sha": "def456",
        }
