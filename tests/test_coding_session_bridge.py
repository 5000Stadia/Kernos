"""CODING-SESSION-BRIDGE-V1 implementation tests.

Covers the spec's three live-test categories:
  * Round-trip: ask + response written + read returns completed
  * Async-state: read before response → attempted (not pending/error)
  * Tool-implementation scope: out-of-scope writes refused by tool's
    own path validation

Plus the architect's event-emission revision contract: emit fires
exactly once per response arrival via sentinel file; correlation_id
equals request_id literally; payload carries originating_member_id.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kernos.kernel.coding_session_bridge import (
    ASK_CODING_SESSION_TOOL,
    READ_CODING_SESSION_RESPONSE_TOOL,
    VALID_TARGETS,
    _bridge_dir,
    _requests_dir,
    _responses_dir,
    _safe_request_id,
    handle_ask_coding_session,
    handle_read_coding_session_response,
)


def _write_response(
    data_dir: str,
    instance_id: str,
    request_id: str,
    **overrides,
) -> Path:
    """Helper: synthesize a response file the way the operator/CC
    would after relaying. Mimics the message-format spec."""
    body = {
        "request_id": request_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "target": "claude_code",
        "findings": "Looking at phases/persist.py:64 — filters on member_id.",
        "source_references": [
            {
                "path": "kernos/messages/phases/persist.py",
                "line_range": "60-70",
                "relevance": "the filter you asked about",
            },
        ],
        "caveats": "",
        "investigation_outcome": "completed",
        **overrides,
    }
    response_path = _responses_dir(data_dir, instance_id) / f"{request_id}.json"
    response_path.parent.mkdir(parents=True, exist_ok=True)
    response_path.write_text(
        json.dumps(body, ensure_ascii=False), encoding="utf-8",
    )
    return response_path


# ---------------------------------------------------------------------------
# Round-trip test
# ---------------------------------------------------------------------------


class TestRoundTrip:
    async def test_ask_writes_request_file_and_returns_attempted_record(
        self, tmp_path,
    ):
        summary, record = await handle_ask_coding_session(
            instance_id="inst-A",
            member_id="mem-A",
            active_space_id="space-A",
            data_dir=str(tmp_path),
            target="claude_code",
            question="Audit phases/persist.py:64 filter behavior.",
            context={"suspected_paths": ["kernos/messages/phases/persist.py"]},
        )
        assert record.execution_state == "attempted"
        assert record.operation == "ask_coding_session"
        assert record.operation_class == "mutate"
        assert len(record.receipt_refs) == 1
        request_id = record.receipt_refs[0]
        assert request_id in summary

        # Request file exists and round-trips.
        request_path = _requests_dir(str(tmp_path), "inst-A") / f"{request_id}.json"
        assert request_path.exists()
        body = json.loads(request_path.read_text(encoding="utf-8"))
        assert body["target"] == "claude_code"
        assert body["question"] == "Audit phases/persist.py:64 filter behavior."
        assert body["originating_kernos_instance"] == "inst-A"
        assert body["originating_member_id"] == "mem-A"
        assert body["originating_space"] == "space-A"
        assert body["context"]["suspected_paths"] == [
            "kernos/messages/phases/persist.py",
        ]
        assert body["request_id"] == request_id

    async def test_read_returns_completed_with_findings_after_response_written(
        self, tmp_path,
    ):
        # Step 1: Kernos asks.
        _summary, ask_record = await handle_ask_coding_session(
            instance_id="inst-A",
            member_id="mem-A",
            active_space_id="space-A",
            data_dir=str(tmp_path),
            target="claude_code",
            question="What does phases/persist.py:64 filter on?",
        )
        request_id = ask_record.receipt_refs[0]

        # Step 2: Operator/CC writes the response.
        _write_response(str(tmp_path), "inst-A", request_id)

        # Step 3: Kernos reads.
        summary, read_record = await handle_read_coding_session_response(
            instance_id="inst-A",
            data_dir=str(tmp_path),
            request_id=request_id,
        )
        assert read_record.execution_state == "completed"
        assert read_record.operation == "read_coding_session_response"
        assert read_record.operation_class == "read"
        assert request_id in read_record.receipt_refs
        # Summary contains findings + source references.
        assert "phases/persist.py:64" in summary
        assert "member_id" in summary
        assert "kernos/messages/phases/persist.py" in summary

    async def test_action_state_record_chain_preserves_request_id(
        self, tmp_path,
    ):
        """Provenance: ask record's receipt_refs and read record's
        receipt_refs both reference the same request_id, anchoring the
        consultation cycle for audit."""
        _, ask_record = await handle_ask_coding_session(
            instance_id="inst-A",
            member_id="mem-A",
            active_space_id="space-A",
            data_dir=str(tmp_path),
            target="codex",
            question="Confirm something.",
        )
        request_id = ask_record.receipt_refs[0]
        _write_response(str(tmp_path), "inst-A", request_id, target="codex")
        _, read_record = await handle_read_coding_session_response(
            instance_id="inst-A",
            data_dir=str(tmp_path),
            request_id=request_id,
        )
        assert ask_record.receipt_refs == read_record.receipt_refs == (request_id,)


# ---------------------------------------------------------------------------
# Async-state test (spec category 2)
# ---------------------------------------------------------------------------


class TestAsyncState:
    async def test_read_before_response_returns_attempted(self, tmp_path):
        _, ask_record = await handle_ask_coding_session(
            instance_id="inst-A",
            member_id="mem-A",
            active_space_id="space-A",
            data_dir=str(tmp_path),
            target="claude_code",
            question="A question.",
        )
        request_id = ask_record.receipt_refs[0]

        # Read BEFORE response is written.
        _, read_record = await handle_read_coding_session_response(
            instance_id="inst-A",
            data_dir=str(tmp_path),
            request_id=request_id,
        )
        # Per spec Finding B: execution_state is "attempted", not
        # "pending" (which isn't in the substrate vocabulary).
        assert read_record.execution_state == "attempted"

    async def test_polling_has_no_side_effects(self, tmp_path):
        _, ask_record = await handle_ask_coding_session(
            instance_id="inst-A",
            member_id="mem-A",
            active_space_id="space-A",
            data_dir=str(tmp_path),
            target="claude_code",
            question="A question.",
        )
        request_id = ask_record.receipt_refs[0]
        # Poll three times before response.
        for _ in range(3):
            _, rec = await handle_read_coding_session_response(
                instance_id="inst-A",
                data_dir=str(tmp_path),
                request_id=request_id,
            )
            assert rec.execution_state == "attempted"
        # No response file, no sentinel.
        responses_dir = _responses_dir(str(tmp_path), "inst-A")
        assert not (responses_dir / f"{request_id}.json").exists()
        assert not (responses_dir / f"{request_id}.emitted").exists()

    async def test_polling_then_response_returns_completed(self, tmp_path):
        _, ask_record = await handle_ask_coding_session(
            instance_id="inst-A",
            member_id="mem-A",
            active_space_id="space-A",
            data_dir=str(tmp_path),
            target="claude_code",
            question="A question.",
        )
        request_id = ask_record.receipt_refs[0]
        _, rec = await handle_read_coding_session_response(
            instance_id="inst-A",
            data_dir=str(tmp_path),
            request_id=request_id,
        )
        assert rec.execution_state == "attempted"
        _write_response(str(tmp_path), "inst-A", request_id)
        _, rec2 = await handle_read_coding_session_response(
            instance_id="inst-A",
            data_dir=str(tmp_path),
            request_id=request_id,
        )
        assert rec2.execution_state == "completed"

    async def test_read_with_unknown_request_id_returns_failed(self, tmp_path):
        _, rec = await handle_read_coding_session_response(
            instance_id="inst-A",
            data_dir=str(tmp_path),
            request_id="nonexistent-request-id",
        )
        assert rec.execution_state == "failed"

    async def test_read_after_timeout_returns_failed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KERNOS_CODING_SESSION_BRIDGE_TIMEOUT_SECONDS", "1")
        _, ask_record = await handle_ask_coding_session(
            instance_id="inst-A",
            member_id="mem-A",
            active_space_id="space-A",
            data_dir=str(tmp_path),
            target="claude_code",
            question="A question.",
        )
        request_id = ask_record.receipt_refs[0]
        # Rewrite the request file with an old timestamp.
        request_path = _requests_dir(str(tmp_path), "inst-A") / f"{request_id}.json"
        body = json.loads(request_path.read_text(encoding="utf-8"))
        body["timestamp"] = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat()
        request_path.write_text(
            json.dumps(body, ensure_ascii=False), encoding="utf-8",
        )

        _, rec = await handle_read_coding_session_response(
            instance_id="inst-A",
            data_dir=str(tmp_path),
            request_id=request_id,
        )
        assert rec.execution_state == "failed"
        assert "timeout" in rec.user_visible_summary.lower()


# ---------------------------------------------------------------------------
# Tool-implementation scope test (spec category 3)
# ---------------------------------------------------------------------------


class TestToolImplementationScope:
    async def test_ask_rejects_invalid_target(self, tmp_path):
        _, rec = await handle_ask_coding_session(
            instance_id="inst-A",
            member_id="mem-A",
            active_space_id="space-A",
            data_dir=str(tmp_path),
            target="some-other-tool",
            question="A question.",
        )
        assert rec.execution_state == "failed"
        assert "target" in rec.user_visible_summary

    async def test_ask_rejects_empty_question(self, tmp_path):
        _, rec = await handle_ask_coding_session(
            instance_id="inst-A",
            member_id="mem-A",
            active_space_id="space-A",
            data_dir=str(tmp_path),
            target="claude_code",
            question="",
        )
        assert rec.execution_state == "failed"

    async def test_read_rejects_path_traversal_via_request_id(self, tmp_path):
        # If the tool's request_id validation is missing or broken, an
        # attacker-supplied value with .. could escape the bridge dir
        # when constructing the path. Validation must refuse the input
        # at the tool boundary.
        bad_ids = [
            "../escape",
            "../../etc/passwd",
            "evil/path",
            "a/b/c",
            "with spaces",
            "with.dot",
        ]
        for bad in bad_ids:
            _, rec = await handle_read_coding_session_response(
                instance_id="inst-A",
                data_dir=str(tmp_path),
                request_id=bad,
            )
            assert rec.execution_state == "failed", f"id={bad!r} should reject"

    def test_safe_request_id_accepts_valid_shapes(self):
        # Real UUIDs and request_id-like strings should pass.
        valid = [
            "0123456789abcdef0123456789abcdef",
            "abc-def-123",
            "request_42",
            "0123456789abcdef0123456789abcdef-test",
        ]
        for v in valid:
            assert _safe_request_id(v) == v

    def test_safe_request_id_rejects_unsafe(self):
        for bad in ["", "../x", "a/b", "with space", "with.dot", "x;y"]:
            with pytest.raises(ValueError):
                _safe_request_id(bad)

    async def test_bridge_directory_is_per_instance(self, tmp_path):
        await handle_ask_coding_session(
            instance_id="inst-A",
            member_id="mem-A",
            active_space_id="space-A",
            data_dir=str(tmp_path),
            target="claude_code",
            question="Q1",
        )
        await handle_ask_coding_session(
            instance_id="inst-B",
            member_id="mem-B",
            active_space_id="space-B",
            data_dir=str(tmp_path),
            target="codex",
            question="Q2",
        )
        a_requests = list((_requests_dir(str(tmp_path), "inst-A")).iterdir())
        b_requests = list((_requests_dir(str(tmp_path), "inst-B")).iterdir())
        assert len(a_requests) == 1
        assert len(b_requests) == 1
        # Cross-instance isolation: B can't see A's request.
        a_request_id = a_requests[0].stem
        responses_b_path = (
            _responses_dir(str(tmp_path), "inst-B") / f"{a_request_id}.json"
        )
        assert not responses_b_path.exists()


# ---------------------------------------------------------------------------
# Event emission with sentinel-file idempotency
# ---------------------------------------------------------------------------


class TestEventEmission:
    async def test_event_emitted_once_on_first_response_read(self, tmp_path):
        captured: list[tuple[str, dict]] = []

        async def _emit(event_type: str, payload: dict) -> None:
            captured.append((event_type, payload))

        # Ask + write response.
        _, ask_record = await handle_ask_coding_session(
            instance_id="inst-A",
            member_id="mem-PROVENANCE",
            active_space_id="space-A",
            data_dir=str(tmp_path),
            target="claude_code",
            question="A question.",
        )
        request_id = ask_record.receipt_refs[0]
        _write_response(str(tmp_path), "inst-A", request_id)

        # Inject emit_event closure via direct call to the handler.
        from kernos.kernel.coding_session_bridge import (
            _emit_response_received_once,
            _responses_dir,
        )
        response_data = json.loads(
            (_responses_dir(str(tmp_path), "inst-A") / f"{request_id}.json")
            .read_text(encoding="utf-8")
        )
        await _emit_response_received_once(
            instance_id="inst-A",
            request_id=request_id,
            response_payload=response_data,
            data_dir=str(tmp_path),
            emit_event=_emit,
        )
        assert len(captured) == 1
        event_type, payload = captured[0]
        assert event_type == "coding_consult.response_received"
        assert payload["request_id"] == request_id
        assert payload["originating_member_id"] == "mem-PROVENANCE"
        assert payload["originating_kernos_instance"] == "inst-A"
        assert payload["target"] == "claude_code"
        assert payload["investigation_outcome"] == "completed"

    async def test_sentinel_prevents_re_emit_on_repeated_reads(self, tmp_path):
        captured: list[tuple[str, dict]] = []

        async def _emit(event_type: str, payload: dict) -> None:
            captured.append((event_type, payload))

        # Set up request + response.
        _, ask_record = await handle_ask_coding_session(
            instance_id="inst-A",
            member_id="mem-A",
            active_space_id="space-A",
            data_dir=str(tmp_path),
            target="claude_code",
            question="Q.",
        )
        request_id = ask_record.receipt_refs[0]
        _write_response(str(tmp_path), "inst-A", request_id)

        from kernos.kernel.coding_session_bridge import (
            _emit_response_received_once,
            _responses_dir,
        )
        response_data = json.loads(
            (_responses_dir(str(tmp_path), "inst-A") / f"{request_id}.json")
            .read_text(encoding="utf-8")
        )
        # First call emits + writes sentinel.
        await _emit_response_received_once(
            instance_id="inst-A",
            request_id=request_id,
            response_payload=response_data,
            data_dir=str(tmp_path),
            emit_event=_emit,
        )
        # Second + third calls: sentinel blocks re-emission.
        await _emit_response_received_once(
            instance_id="inst-A",
            request_id=request_id,
            response_payload=response_data,
            data_dir=str(tmp_path),
            emit_event=_emit,
        )
        await _emit_response_received_once(
            instance_id="inst-A",
            request_id=request_id,
            response_payload=response_data,
            data_dir=str(tmp_path),
            emit_event=_emit,
        )
        assert len(captured) == 1
        # Sentinel file present.
        sentinel = (
            _responses_dir(str(tmp_path), "inst-A") / f"{request_id}.emitted"
        )
        assert sentinel.exists()

    async def test_read_completed_triggers_emit_with_correlation_id_eq_request_id(
        self, tmp_path, monkeypatch,
    ):
        """Architect's event-emission revision pins correlation_id =
        request_id literally (no prefix). When falling back to
        event_stream.emit, the call must use that contract."""
        captured: list[dict] = []

        async def _stub_emit(
            instance_id: str,
            event_type: str,
            payload: dict | None = None,
            *,
            member_id=None,
            space_id=None,
            correlation_id=None,
        ):
            captured.append({
                "instance_id": instance_id,
                "event_type": event_type,
                "payload": payload,
                "correlation_id": correlation_id,
            })
            return "evt-stub"

        from kernos.kernel import event_stream as es_mod
        monkeypatch.setattr(es_mod, "emit", _stub_emit)

        _, ask_record = await handle_ask_coding_session(
            instance_id="inst-A",
            member_id="mem-A",
            active_space_id="space-A",
            data_dir=str(tmp_path),
            target="claude_code",
            question="Q.",
        )
        request_id = ask_record.receipt_refs[0]
        _write_response(str(tmp_path), "inst-A", request_id)

        # Read with no emit_event callable → fallback to event_stream.
        await handle_read_coding_session_response(
            instance_id="inst-A",
            data_dir=str(tmp_path),
            request_id=request_id,
        )
        assert len(captured) == 1
        assert captured[0]["event_type"] == "coding_consult.response_received"
        # Architect's revision: correlation_id == request_id literally.
        assert captured[0]["correlation_id"] == request_id
        assert captured[0]["payload"]["request_id"] == request_id


# ---------------------------------------------------------------------------
# Tool schema sanity
# ---------------------------------------------------------------------------


class TestResponseRobustness:
    """Codex post-impl H1 + M4: partial response writes don't permanently
    fail the request within timeout; body request_id mismatch refused."""

    async def test_partial_json_response_within_timeout_returns_attempted(
        self, tmp_path,
    ):
        """Simulate a polling read catching a partial response write
        (writer wrote opening brace and stopped). Within the timeout
        window, this should return attempted (poll again later), not
        failed (Codex H1)."""
        _, ask_record = await handle_ask_coding_session(
            instance_id="inst-A",
            member_id="mem-A",
            active_space_id="space-A",
            data_dir=str(tmp_path),
            target="claude_code",
            question="A question.",
        )
        request_id = ask_record.receipt_refs[0]

        # Write a partial response file (invalid JSON).
        response_path = (
            _responses_dir(str(tmp_path), "inst-A") / f"{request_id}.json"
        )
        response_path.parent.mkdir(parents=True, exist_ok=True)
        response_path.write_text("{\"request_id\": \"par", encoding="utf-8")

        _, rec = await handle_read_coding_session_response(
            instance_id="inst-A",
            data_dir=str(tmp_path),
            request_id=request_id,
        )
        assert rec.execution_state == "attempted"
        assert "poll" in rec.user_visible_summary.lower() or \
               "partial" in rec.user_visible_summary.lower()

    async def test_partial_json_response_past_timeout_returns_failed(
        self, tmp_path, monkeypatch,
    ):
        """Past timeout, malformed JSON becomes a real failure."""
        monkeypatch.setenv("KERNOS_CODING_SESSION_BRIDGE_TIMEOUT_SECONDS", "1")
        _, ask_record = await handle_ask_coding_session(
            instance_id="inst-A",
            member_id="mem-A",
            active_space_id="space-A",
            data_dir=str(tmp_path),
            target="claude_code",
            question="A question.",
        )
        request_id = ask_record.receipt_refs[0]

        # Backdate the request timestamp past the timeout.
        request_path = (
            _requests_dir(str(tmp_path), "inst-A") / f"{request_id}.json"
        )
        body = json.loads(request_path.read_text(encoding="utf-8"))
        body["timestamp"] = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat()
        request_path.write_text(
            json.dumps(body, ensure_ascii=False), encoding="utf-8",
        )

        # Write a malformed response.
        response_path = (
            _responses_dir(str(tmp_path), "inst-A") / f"{request_id}.json"
        )
        response_path.parent.mkdir(parents=True, exist_ok=True)
        response_path.write_text("not-json", encoding="utf-8")

        _, rec = await handle_read_coding_session_response(
            instance_id="inst-A",
            data_dir=str(tmp_path),
            request_id=request_id,
        )
        assert rec.execution_state == "failed"

    async def test_response_body_request_id_mismatch_refused(self, tmp_path):
        """Codex M4: a misplaced response file (whose body request_id
        differs from the requested id) must NOT complete the wrong
        consultation. The handler refuses with failed."""
        _, ask_record = await handle_ask_coding_session(
            instance_id="inst-A",
            member_id="mem-A",
            active_space_id="space-A",
            data_dir=str(tmp_path),
            target="claude_code",
            question="A question.",
        )
        request_id = ask_record.receipt_refs[0]

        # Write a response file but with a different request_id inside.
        response_path = (
            _responses_dir(str(tmp_path), "inst-A") / f"{request_id}.json"
        )
        response_path.parent.mkdir(parents=True, exist_ok=True)
        response_path.write_text(
            json.dumps({
                "request_id": "some-other-id",  # mismatch
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "target": "claude_code",
                "findings": "looked at the wrong thing",
                "source_references": [],
                "caveats": "",
                "investigation_outcome": "completed",
            }, ensure_ascii=False),
            encoding="utf-8",
        )

        _, rec = await handle_read_coding_session_response(
            instance_id="inst-A",
            data_dir=str(tmp_path),
            request_id=request_id,
        )
        assert rec.execution_state == "failed"
        assert "request_id" in rec.user_visible_summary.lower()
        assert "mismatch" in rec.user_visible_summary.lower()

    async def test_unknown_investigation_outcome_normalized(self, tmp_path):
        """An out-of-vocabulary investigation_outcome is normalized to
        unable_to_investigate rather than passed through unchecked."""
        _, ask_record = await handle_ask_coding_session(
            instance_id="inst-A",
            member_id="mem-A",
            active_space_id="space-A",
            data_dir=str(tmp_path),
            target="claude_code",
            question="A question.",
        )
        request_id = ask_record.receipt_refs[0]

        response_path = (
            _responses_dir(str(tmp_path), "inst-A") / f"{request_id}.json"
        )
        response_path.parent.mkdir(parents=True, exist_ok=True)
        response_path.write_text(
            json.dumps({
                "request_id": request_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "target": "claude_code",
                "findings": "looked",
                "source_references": [],
                "caveats": "",
                "investigation_outcome": "made_up_value",
            }, ensure_ascii=False),
            encoding="utf-8",
        )

        captured: list[dict] = []

        async def _stub_emit(
            instance_id, event_type, payload=None, *,
            member_id=None, space_id=None, correlation_id=None,
        ):
            captured.append({"payload": payload})
            return "evt"

        from kernos.kernel import event_stream as es_mod
        import pytest
        from _pytest.monkeypatch import MonkeyPatch
        mp = MonkeyPatch()
        try:
            mp.setattr(es_mod, "emit", _stub_emit)
            summary, rec = await handle_read_coding_session_response(
                instance_id="inst-A",
                data_dir=str(tmp_path),
                request_id=request_id,
            )
        finally:
            mp.undo()

        assert rec.execution_state == "completed"
        # The emitted payload's investigation_outcome is normalized.
        assert captured[0]["payload"]["investigation_outcome"] == "unable_to_investigate"


class TestAtomicSentinelClaim:
    """Codex post-impl M2: O_CREAT|O_EXCL atomic claim ensures only one
    of two concurrent emitters actually emits the event."""

    async def test_concurrent_emits_dedup_at_sentinel_claim(self, tmp_path):
        import asyncio
        _, ask_record = await handle_ask_coding_session(
            instance_id="inst-A",
            member_id="mem-A",
            active_space_id="space-A",
            data_dir=str(tmp_path),
            target="claude_code",
            question="A question.",
        )
        request_id = ask_record.receipt_refs[0]
        _write_response(str(tmp_path), "inst-A", request_id)

        emit_count = 0

        async def _slow_emit(
            event_type: str, payload: dict, *, correlation_id=None,
        ) -> None:
            nonlocal emit_count
            # Tiny await so the scheduler interleaves the second
            # coroutine while we're inside; if the claim is atomic, the
            # second one finds the sentinel already present and bails.
            await asyncio.sleep(0.01)
            emit_count += 1

        from kernos.kernel.coding_session_bridge import (
            _emit_response_received_once,
            _responses_dir,
        )
        response_data = json.loads(
            (_responses_dir(str(tmp_path), "inst-A") / f"{request_id}.json")
            .read_text(encoding="utf-8")
        )
        # Fire two coroutines concurrently.
        await asyncio.gather(
            _emit_response_received_once(
                instance_id="inst-A",
                request_id=request_id,
                response_payload=response_data,
                data_dir=str(tmp_path),
                emit_event=_slow_emit,
            ),
            _emit_response_received_once(
                instance_id="inst-A",
                request_id=request_id,
                response_payload=response_data,
                data_dir=str(tmp_path),
                emit_event=_slow_emit,
            ),
        )
        assert emit_count == 1, (
            f"expected exactly one emit due to O_CREAT|O_EXCL atomic "
            f"sentinel claim; got {emit_count}"
        )

    async def test_correlation_id_kwarg_passed_to_callable(self, tmp_path):
        """Codex M3: injected emit_event callable can accept
        correlation_id explicitly per the documented contract."""
        captured: list[dict] = []

        async def _emit_with_correlation(
            event_type: str, payload: dict, *, correlation_id=None,
        ) -> None:
            captured.append({
                "event_type": event_type,
                "payload": payload,
                "correlation_id": correlation_id,
            })

        _, ask_record = await handle_ask_coding_session(
            instance_id="inst-A",
            member_id="mem-A",
            active_space_id="space-A",
            data_dir=str(tmp_path),
            target="claude_code",
            question="Q.",
        )
        request_id = ask_record.receipt_refs[0]
        _write_response(str(tmp_path), "inst-A", request_id)

        from kernos.kernel.coding_session_bridge import (
            _emit_response_received_once,
            _responses_dir,
        )
        response_data = json.loads(
            (_responses_dir(str(tmp_path), "inst-A") / f"{request_id}.json")
            .read_text(encoding="utf-8")
        )
        await _emit_response_received_once(
            instance_id="inst-A",
            request_id=request_id,
            response_payload=response_data,
            data_dir=str(tmp_path),
            emit_event=_emit_with_correlation,
        )
        assert len(captured) == 1
        assert captured[0]["correlation_id"] == request_id

    async def test_minimal_signature_callable_still_works(self, tmp_path):
        """Backward-compat: callables that don't accept correlation_id
        still work; the bridge falls back to the minimal signature."""
        captured: list[tuple] = []

        async def _legacy_emit(event_type: str, payload: dict) -> None:
            captured.append((event_type, payload))

        _, ask_record = await handle_ask_coding_session(
            instance_id="inst-A",
            member_id="mem-A",
            active_space_id="space-A",
            data_dir=str(tmp_path),
            target="claude_code",
            question="Q.",
        )
        request_id = ask_record.receipt_refs[0]
        _write_response(str(tmp_path), "inst-A", request_id)

        from kernos.kernel.coding_session_bridge import (
            _emit_response_received_once,
            _responses_dir,
        )
        response_data = json.loads(
            (_responses_dir(str(tmp_path), "inst-A") / f"{request_id}.json")
            .read_text(encoding="utf-8")
        )
        await _emit_response_received_once(
            instance_id="inst-A",
            request_id=request_id,
            response_payload=response_data,
            data_dir=str(tmp_path),
            emit_event=_legacy_emit,
        )
        assert len(captured) == 1
        assert captured[0][0] == "coding_consult.response_received"


class TestToolSchemas:
    def test_ask_schema_required_fields(self):
        schema = ASK_CODING_SESSION_TOOL["input_schema"]
        assert set(schema["required"]) == {"target", "question"}
        assert (
            set(schema["properties"]["target"]["enum"]) == VALID_TARGETS
        )

    def test_read_schema_required_fields(self):
        schema = READ_CODING_SESSION_RESPONSE_TOOL["input_schema"]
        assert schema["required"] == ["request_id"]

    def test_tool_names_match_module_constants(self):
        assert ASK_CODING_SESSION_TOOL["name"] == "ask_coding_session"
        assert (
            READ_CODING_SESSION_RESPONSE_TOOL["name"]
            == "read_coding_session_response"
        )


class TestDispatchIntegration:
    """Codex post-impl coverage gap: verifies registry exposure +
    confirmed-only dispatch + _turn_action_records append work
    together via the actual ReasoningService.execute_tool path."""

    def test_kernel_tools_set_includes_both_names(self):
        from kernos.kernel.reasoning import ReasoningService
        assert "ask_coding_session" in ReasoningService._KERNEL_TOOLS
        assert "read_coding_session_response" in ReasoningService._KERNEL_TOOLS

    def test_kernel_tool_paths_are_confirmed_only(self):
        from kernos.kernel.reasoning import ReasoningService
        ask_paths = ReasoningService._KERNEL_TOOL_PATHS["ask_coding_session"]
        read_paths = ReasoningService._KERNEL_TOOL_PATHS[
            "read_coding_session_response"
        ]
        assert ask_paths == frozenset({"confirmed"})
        assert read_paths == frozenset({"confirmed"})

    def test_registry_surfaces_both_schemas(self):
        from kernos.kernel.kernel_tool_registry import kernel_tool_schema_map
        schemas = kernel_tool_schema_map()
        names = {s["name"] for s in schemas.values()}
        assert "ask_coding_session" in names
        assert "read_coding_session_response" in names

    async def test_dispatch_appends_action_state_record(self, tmp_path, monkeypatch):
        """Round-trip the dispatch path: instantiate a ReasoningService,
        call execute_tool for ask_coding_session, verify the record was
        appended to self._turn_action_records (same shape as note_this)."""
        monkeypatch.setenv("KERNOS_DATA_DIR", str(tmp_path))
        from unittest.mock import AsyncMock, MagicMock
        from kernos.kernel.reasoning import ReasoningService
        from kernos.kernel.integration import ActionStateRecord

        # Stub provider + events + audit + mcp.
        rs = ReasoningService(
            AsyncMock(),  # provider
            AsyncMock(),  # events
            MagicMock(),  # mcp
            AsyncMock(),  # audit
        )

        # Synthesize a minimal request-like object.
        request = MagicMock()
        request.instance_id = "inst-A"
        request.active_space_id = "space-A"
        request.member_id = "mem-A"
        request.conversation_id = "turn-1"

        # Call execute_tool for ask_coding_session.
        summary = await rs.execute_tool(
            "ask_coding_session",
            {
                "target": "claude_code",
                "question": "Audit a thing.",
                "context": {},
            },
            request,
        )
        assert isinstance(summary, str)
        assert "request_id=" in summary
        # Record was appended.
        assert len(rs._turn_action_records) == 1
        record = rs._turn_action_records[0]
        assert isinstance(record, ActionStateRecord)
        assert record.operation == "ask_coding_session"
        assert record.operation_class == "mutate"
        assert record.execution_state == "attempted"
        assert len(record.receipt_refs) == 1


# ===========================================================================
# Spec 6 round-3 fold — normalize_investigation_outcome edge cases
# ===========================================================================
#
# Pins the canonical normalization helper's behavior for every
# input class (None, empty string, valid, invalid, non-string falsy).
# Round-3 MEDIUM 2 distinguished None/empty (→ "completed", the
# missing-default) from other falsy JSON values (→ "unable_to_investigate",
# treating False/0/[]/{} as invalid outcomes that shouldn't pollute
# the canonical enum).


class TestNormalizeInvestigationOutcome:

    def test_none_returns_completed(self):
        from kernos.kernel.coding_session_bridge import (
            normalize_investigation_outcome,
        )
        assert normalize_investigation_outcome(None) == "completed"

    def test_empty_string_returns_completed(self):
        from kernos.kernel.coding_session_bridge import (
            normalize_investigation_outcome,
        )
        assert normalize_investigation_outcome("") == "completed"

    def test_valid_values_unchanged(self):
        from kernos.kernel.coding_session_bridge import (
            normalize_investigation_outcome,
        )
        assert normalize_investigation_outcome("completed") == "completed"
        assert normalize_investigation_outcome("partial") == "partial"
        assert (
            normalize_investigation_outcome("unable_to_investigate")
            == "unable_to_investigate"
        )

    def test_invalid_string_returns_unable_to_investigate(self):
        from kernos.kernel.coding_session_bridge import (
            normalize_investigation_outcome,
        )
        assert (
            normalize_investigation_outcome("made_up_outcome")
            == "unable_to_investigate"
        )

    def test_boolean_false_returns_unable_to_investigate(self):
        """Round-3 MEDIUM 2 pin: False is NOT a missing value; it's
        an invalid JSON payload. Normalize to unable_to_investigate
        so the canonical enum doesn't get a non-string."""
        from kernos.kernel.coding_session_bridge import (
            normalize_investigation_outcome,
        )
        assert (
            normalize_investigation_outcome(False)
            == "unable_to_investigate"
        )

    def test_integer_zero_returns_unable_to_investigate(self):
        """Round-3 MEDIUM 2 pin: 0 is not "missing", it's an
        invalid type. Normalize to unable_to_investigate."""
        from kernos.kernel.coding_session_bridge import (
            normalize_investigation_outcome,
        )
        assert (
            normalize_investigation_outcome(0)
            == "unable_to_investigate"
        )

    def test_empty_list_returns_unable_to_investigate(self):
        from kernos.kernel.coding_session_bridge import (
            normalize_investigation_outcome,
        )
        assert (
            normalize_investigation_outcome([])
            == "unable_to_investigate"
        )

    def test_empty_dict_returns_unable_to_investigate(self):
        from kernos.kernel.coding_session_bridge import (
            normalize_investigation_outcome,
        )
        assert (
            normalize_investigation_outcome({})
            == "unable_to_investigate"
        )

    def test_helper_in_all_for_cross_module_import(self):
        """Round-3 LOW 1 pin: the helper is exported via __all__ so
        autonomy_tools.py and other consumers can import it cleanly."""
        from kernos.kernel import coding_session_bridge as csb
        assert "normalize_investigation_outcome" in csb.__all__
