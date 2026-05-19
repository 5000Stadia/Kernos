"""Substrate tests for bridge_watcher.

The bridge_watcher runs two background loops in the live bot:

  * Outbound: data/<instance>/coding_session_bridge/requests/ → ACPX
    dispatch → data/<instance>/coding_session_bridge/responses/
  * Inbound: data/<instance>/cc_inbox/ → read-only handlers →
    data/<instance>/cc_outbox/

These tests pin the substrate invariants:
  * O_CREAT|O_EXCL claim semantics — only one watcher processes a
    request, dedup is atomic
  * Path-traversal rejected on read_file / list_files (security
    boundary, not best-effort)
  * sqlite_query rejects non-SELECT / non-PRAGMA SQL (read-only
    enforcement)
  * Unknown kinds raise ValueError (caller-error surface)
  * _write_response_atomic does tmp+rename, never partial files

Live ACPX dispatch is mocked — the dispatch substrate has its own
test in test_acpx_adapter.py.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from kernos.kernel.external_agents import bridge_watcher
from kernos.kernel.external_agents.bridge_watcher import (
    _claim_request,
    _dispatch_inbound,
    _handle_inbound_request,
    _inbox_dir,
    _outbox_dir,
    _outbound_requests_dir,
    _outbound_responses_dir,
    _release_lock,
    _write_response_atomic,
)


# ===========================================================================
# Path helpers — directory derivation must be deterministic and per-instance
# ===========================================================================


class TestDirectoryDerivation:
    def test_outbound_dirs_are_per_instance(self, tmp_path):
        a = _outbound_requests_dir(str(tmp_path), "instance-A")
        b = _outbound_requests_dir(str(tmp_path), "instance-B")
        assert a != b
        assert "instance-A" in str(a)
        assert "instance-B" in str(b)
        assert a.parts[-2:] == ("coding_session_bridge", "requests")

    def test_outbound_responses_dir_pair(self, tmp_path):
        req = _outbound_requests_dir(str(tmp_path), "i1")
        resp = _outbound_responses_dir(str(tmp_path), "i1")
        # Sibling dirs under coding_session_bridge/
        assert req.parent == resp.parent
        assert resp.name == "responses"

    def test_inbox_outbox_pair(self, tmp_path):
        inbox = _inbox_dir(str(tmp_path), "i1")
        outbox = _outbox_dir(str(tmp_path), "i1")
        assert inbox.name == "cc_inbox"
        assert outbox.name == "cc_outbox"
        # Both directly under instance root
        assert inbox.parent == outbox.parent


# ===========================================================================
# O_CREAT|O_EXCL claim semantics — atomic dedup, no double-processing
# ===========================================================================


class TestClaimRequest:
    async def test_first_claim_succeeds(self, tmp_path):
        lock = tmp_path / "req-1.processing"
        assert await _claim_request(lock) is True
        assert lock.exists()
        # Lock content carries pid + started_at
        meta = json.loads(lock.read_text())
        assert "pid" in meta
        assert "started_at" in meta

    async def test_second_claim_blocked_when_lock_fresh(self, tmp_path):
        lock = tmp_path / "req-1.processing"
        first = await _claim_request(lock)
        second = await _claim_request(lock)
        assert first is True
        assert second is False  # blocked by fresh lock

    async def test_concurrent_claims_only_one_wins(self, tmp_path):
        # Race 10 concurrent claims for the same lock — exactly one
        # should win (atomicity guarantee of O_CREAT|O_EXCL).
        lock = tmp_path / "req-race.processing"
        results = await asyncio.gather(
            *[_claim_request(lock) for _ in range(10)]
        )
        winners = [r for r in results if r is True]
        losers = [r for r in results if r is False]
        assert len(winners) == 1
        assert len(losers) == 9

    async def test_release_lock_then_reclaim(self, tmp_path):
        lock = tmp_path / "req-1.processing"
        assert await _claim_request(lock) is True
        _release_lock(lock)
        assert not lock.exists()
        # After release another watcher can claim
        assert await _claim_request(lock) is True

    def test_release_missing_lock_no_error(self, tmp_path):
        # release should be idempotent — missing file is fine
        lock = tmp_path / "never-claimed.processing"
        _release_lock(lock)  # no exception


# ===========================================================================
# Atomic response write — partial files would race the bridge response
# emitter that watches for *.json appearance
# ===========================================================================


class TestWriteResponseAtomic:
    def test_writes_payload_as_json(self, tmp_path):
        path = tmp_path / "responses" / "req-1.json"
        payload = {"request_id": "req-1", "summary": "hello"}
        _write_response_atomic(path, payload)
        assert path.exists()
        assert json.loads(path.read_text()) == payload

    def test_creates_parent_dir(self, tmp_path):
        path = tmp_path / "deep" / "nested" / "responses" / "req.json"
        _write_response_atomic(path, {"x": 1})
        assert path.exists()

    def test_no_tmp_file_remains_after_success(self, tmp_path):
        path = tmp_path / "req.json"
        _write_response_atomic(path, {"x": 1})
        # tmp file must be renamed, not left dangling
        assert not (tmp_path / "req.json.tmp").exists()

    def test_unicode_preserved(self, tmp_path):
        path = tmp_path / "req.json"
        _write_response_atomic(path, {"text": "héllo 🌍 ⚡"})
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded["text"] == "héllo 🌍 ⚡"


# ===========================================================================
# Inbound dispatcher — read-only substrate, security boundaries
# ===========================================================================


class TestDispatchInboundFreeText:
    async def test_echoes_prompt(self, tmp_path):
        out = await _dispatch_inbound(
            kind="free_text",
            params={"prompt": "hello kernos"},
            data_dir=str(tmp_path), instance_id="test-inst",
        )
        assert out == {"echo": "hello kernos"}


class TestDispatchInboundInspectState:
    async def test_returns_data_root_status(self, tmp_path):
        # Create the instance root + bridge dir
        inst_root = tmp_path / "test-inst"
        (inst_root / "coding_session_bridge").mkdir(parents=True)

        out = await _dispatch_inbound(
            kind="inspect_state",
            params={},
            data_dir=str(tmp_path), instance_id="test-inst",
        )
        assert out["data_root_exists"] is True
        assert out["bridge_dir_exists"] is True
        assert "test-inst" in out["data_root"]


class TestDispatchInboundReadFile:
    async def test_reads_existing_file(self, tmp_path):
        inst_root = tmp_path / "i1"
        inst_root.mkdir()
        (inst_root / "hello.txt").write_text("world")

        out = await _dispatch_inbound(
            kind="read_file",
            params={"path": "hello.txt"},
            data_dir=str(tmp_path), instance_id="i1",
        )
        assert out["content"] == "world"
        assert out["truncated"] is False

    async def test_rejects_path_traversal(self, tmp_path):
        # Create a target outside the data_root that an attack would try to read
        (tmp_path / "secret.txt").write_text("not-for-cc")
        (tmp_path / "i1").mkdir()

        with pytest.raises(ValueError, match="escapes data_root"):
            await _dispatch_inbound(
                kind="read_file",
                params={"path": "../secret.txt"},
                data_dir=str(tmp_path), instance_id="i1",
            )

    async def test_rejects_empty_path(self, tmp_path):
        with pytest.raises(ValueError, match="requires params.path"):
            await _dispatch_inbound(
                kind="read_file", params={},
                data_dir=str(tmp_path), instance_id="i1",
            )

    async def test_rejects_missing_file(self, tmp_path):
        (tmp_path / "i1").mkdir()
        with pytest.raises(ValueError, match="does not exist"):
            await _dispatch_inbound(
                kind="read_file",
                params={"path": "nope.txt"},
                data_dir=str(tmp_path), instance_id="i1",
            )


class TestDispatchInboundListFiles:
    async def test_lists_directory_entries(self, tmp_path):
        inst_root = tmp_path / "i1"
        inst_root.mkdir()
        (inst_root / "a.txt").write_text("A")
        (inst_root / "b.txt").write_text("BB")
        (inst_root / "sub").mkdir()

        out = await _dispatch_inbound(
            kind="list_files",
            params={"path": ""},
            data_dir=str(tmp_path), instance_id="i1",
        )
        names = [e["name"] for e in out["entries"]]
        assert "a.txt" in names
        assert "b.txt" in names
        assert "sub" in names
        # File size present; dir size is None
        sub_entry = next(e for e in out["entries"] if e["name"] == "sub")
        assert sub_entry["is_dir"] is True
        assert sub_entry["size"] is None

    async def test_rejects_path_traversal(self, tmp_path):
        (tmp_path / "i1").mkdir()
        with pytest.raises(ValueError, match="escapes data_root"):
            await _dispatch_inbound(
                kind="list_files",
                params={"path": "../../etc"},
                data_dir=str(tmp_path), instance_id="i1",
            )

    async def test_rejects_non_directory(self, tmp_path):
        inst = tmp_path / "i1"
        inst.mkdir()
        (inst / "x.txt").write_text("x")
        with pytest.raises(ValueError, match="not a directory"):
            await _dispatch_inbound(
                kind="list_files",
                params={"path": "x.txt"},
                data_dir=str(tmp_path), instance_id="i1",
            )


class TestDispatchInboundSqliteQuery:
    @pytest.fixture
    def db_with_data(self, tmp_path):
        db_path = tmp_path / "instance.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE members (id TEXT, name TEXT)")
        conn.execute("INSERT INTO members VALUES ('m1', 'Alice')")
        conn.execute("INSERT INTO members VALUES ('m2', 'Bob')")
        conn.commit()
        conn.close()
        return tmp_path

    async def test_select_returns_rows(self, db_with_data):
        out = await _dispatch_inbound(
            kind="sqlite_query",
            params={"sql": "SELECT id, name FROM members ORDER BY id"},
            data_dir=str(db_with_data), instance_id="i1",
        )
        assert out["row_count"] == 2
        assert out["rows"][0] == {"id": "m1", "name": "Alice"}

    async def test_pragma_allowed(self, db_with_data):
        out = await _dispatch_inbound(
            kind="sqlite_query",
            params={"sql": "PRAGMA table_info(members)"},
            data_dir=str(db_with_data), instance_id="i1",
        )
        assert out["row_count"] >= 2  # id, name columns

    async def test_rejects_insert(self, db_with_data):
        with pytest.raises(ValueError, match="read-only"):
            await _dispatch_inbound(
                kind="sqlite_query",
                params={"sql": "INSERT INTO members VALUES ('m3', 'Eve')"},
                data_dir=str(db_with_data), instance_id="i1",
            )

    async def test_rejects_update(self, db_with_data):
        with pytest.raises(ValueError, match="read-only"):
            await _dispatch_inbound(
                kind="sqlite_query",
                params={"sql": "UPDATE members SET name='X' WHERE id='m1'"},
                data_dir=str(db_with_data), instance_id="i1",
            )

    async def test_rejects_delete(self, db_with_data):
        with pytest.raises(ValueError, match="read-only"):
            await _dispatch_inbound(
                kind="sqlite_query",
                params={"sql": "DELETE FROM members"},
                data_dir=str(db_with_data), instance_id="i1",
            )

    async def test_rejects_drop(self, db_with_data):
        with pytest.raises(ValueError, match="read-only"):
            await _dispatch_inbound(
                kind="sqlite_query",
                params={"sql": "DROP TABLE members"},
                data_dir=str(db_with_data), instance_id="i1",
            )

    async def test_rejects_empty_sql(self, db_with_data):
        with pytest.raises(ValueError, match="requires params.sql"):
            await _dispatch_inbound(
                kind="sqlite_query",
                params={"sql": ""},
                data_dir=str(db_with_data), instance_id="i1",
            )

    async def test_respects_limit(self, db_with_data):
        out = await _dispatch_inbound(
            kind="sqlite_query",
            params={"sql": "SELECT * FROM members", "limit": 1},
            data_dir=str(db_with_data), instance_id="i1",
        )
        assert out["row_count"] == 1


class TestDispatchInboundUnknownKind:
    async def test_unknown_kind_raises(self, tmp_path):
        with pytest.raises(ValueError, match="unknown kind"):
            await _dispatch_inbound(
                kind="exec_arbitrary_code",
                params={},
                data_dir=str(tmp_path), instance_id="i1",
            )


# ===========================================================================
# End-to-end inbound — request file → response file
# ===========================================================================


class TestHandleInboundRequest:
    async def test_writes_ok_response_for_free_text(self, tmp_path):
        inbox = tmp_path / "i1" / "cc_inbox"
        outbox = tmp_path / "i1" / "cc_outbox"
        inbox.mkdir(parents=True)
        outbox.mkdir(parents=True)

        req = inbox / "req-1.json"
        req.write_text(json.dumps({
            "kind": "free_text",
            "params": {"prompt": "ping"},
            "client": "test",
        }))

        await _handle_inbound_request(
            request_path=req, outbox_dir=outbox,
            data_dir=str(tmp_path), instance_id="i1",
        )

        resp_path = outbox / "req-1.json"
        assert resp_path.exists()
        resp = json.loads(resp_path.read_text())
        assert resp["status"] == "ok"
        assert resp["kind"] == "free_text"
        assert resp["result"] == {"echo": "ping"}

    async def test_writes_error_response_for_unknown_kind(self, tmp_path):
        inbox = tmp_path / "i1" / "cc_inbox"
        outbox = tmp_path / "i1" / "cc_outbox"
        inbox.mkdir(parents=True)
        outbox.mkdir(parents=True)

        req = inbox / "req-bad.json"
        req.write_text(json.dumps({"kind": "totally_unknown", "params": {}}))

        await _handle_inbound_request(
            request_path=req, outbox_dir=outbox,
            data_dir=str(tmp_path), instance_id="i1",
        )

        resp = json.loads((outbox / "req-bad.json").read_text())
        assert resp["status"] == "error"
        assert "unknown kind" in resp["error"]

    async def test_skips_if_response_already_exists(self, tmp_path):
        # If a prior watcher already produced the response, the
        # handler must not overwrite it (idempotency for restart
        # scenarios where the same request file appears again).
        inbox = tmp_path / "i1" / "cc_inbox"
        outbox = tmp_path / "i1" / "cc_outbox"
        inbox.mkdir(parents=True)
        outbox.mkdir(parents=True)

        req = inbox / "req-1.json"
        req.write_text(json.dumps({"kind": "free_text", "params": {"prompt": "x"}}))
        existing = outbox / "req-1.json"
        existing.write_text('{"status": "ok", "marker": "previous-run"}')

        await _handle_inbound_request(
            request_path=req, outbox_dir=outbox,
            data_dir=str(tmp_path), instance_id="i1",
        )
        # Existing response preserved
        assert json.loads(existing.read_text())["marker"] == "previous-run"
