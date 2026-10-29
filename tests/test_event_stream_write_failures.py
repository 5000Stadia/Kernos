"""Real SQLite regressions for failed batches and continued writer progress."""
from __future__ import annotations

import asyncio
import sqlite3

import aiosqlite
import pytest

from kernos.kernel import event_stream


def make_event(event_id, **overrides):
    fields = dict(
        event_id=event_id,
        instance_id="inst_a",
        timestamp="2026-01-01T00:00:00+00:00",
        event_type="test.event",
        payload={"id": event_id},
    )
    fields.update(overrides)
    return event_stream.Event(**fields)


def durable_ids(path):
    # A separate connection distinguishes committed rows from partial inserts
    # visible only on the writer's connection.
    with sqlite3.connect(path / "instance.db") as db:
        return [row[0] for row in db.execute("SELECT event_id FROM events ORDER BY rowid")]


@pytest.fixture
async def writer(tmp_path):
    writer = event_stream._EventWriter()
    await writer.start(str(tmp_path), flush_interval_s=3600)
    yield writer
    await writer.stop()


@pytest.fixture
def committed_batches():
    batches = []

    def capture(batch):
        batches.append([event.event_id for event in batch])

    event_stream.register_post_flush_hook(capture)
    yield batches
    event_stream.unregister_post_flush_hook(capture)


async def test_partial_sql_failure_preserves_valid_neighbors_and_later_events(
    writer, tmp_path, committed_batches, caplog,
):
    writer.enqueue(make_event("initial"))
    await writer._flush_once()
    writer.enqueue(make_event("before"))
    writer.enqueue(make_event("bad-sql", event_type=None))
    writer.enqueue(make_event("after"))
    await writer._flush_once()

    assert durable_ids(tmp_path) == ["initial", "before", "after"]
    assert writer._db.in_transaction is False
    assert writer.queue_depth == 0
    assert "bad-sql" in caplog.text
    assert "NOT NULL constraint" in caplog.text
    writer.enqueue(make_event("fresh"))
    await writer._flush_once()
    assert durable_ids(tmp_path) == ["initial", "before", "after", "fresh"]
    assert committed_batches == [["initial"], ["before", "after"], ["fresh"]]


async def test_unencodable_payload_preserves_valid_neighbors_and_later_events(
    writer, tmp_path, committed_batches, caplog,
):
    writer.enqueue(make_event("initial"))
    await writer._flush_once()
    writer.enqueue(make_event("before"))
    writer.enqueue(make_event("bad-json", payload={"bad": object()}))
    writer.enqueue(make_event("after"))
    await writer._flush_once()

    assert durable_ids(tmp_path) == ["initial", "before", "after"]
    assert writer.queue_depth == 0
    assert "bad-json" in caplog.text
    assert "not JSON serializable" in caplog.text
    writer.enqueue(make_event("fresh"))
    await writer._flush_once()
    assert durable_ids(tmp_path) == ["initial", "before", "after", "fresh"]
    assert committed_batches == [["initial"], ["before", "after"], ["fresh"]]


@pytest.mark.parametrize("member_id", [{"bad": "binding"}, 2**100])
async def test_unbindable_row_does_not_block_valid_events(
    writer, tmp_path, committed_batches, caplog, member_id,
):
    writer.enqueue(make_event("before"))
    writer.enqueue(make_event("bad-binding", member_id=member_id))
    writer.enqueue(make_event("after"))
    await writer._flush_once()
    assert durable_ids(tmp_path) == ["before", "after"]
    assert writer.queue_depth == 0
    assert writer._db.in_transaction is False
    assert committed_batches == [["before", "after"]]
    assert "bad-binding" in caplog.text


async def test_commit_failure_rolls_back_and_retries_without_false_hooks(
    writer, tmp_path, committed_batches, monkeypatch,
):
    real_commit = writer._db.commit

    async def fail_commit():
        raise aiosqlite.OperationalError("temporary commit failure")

    monkeypatch.setattr(writer._db, "commit", fail_commit)
    writer.enqueue(make_event("first"))
    writer.enqueue(make_event("second"))
    await writer._flush_once()

    assert durable_ids(tmp_path) == []
    async with writer._db.execute("SELECT COUNT(*) FROM events") as cursor:
        assert (await cursor.fetchone())[0] == 0
    assert writer._db.in_transaction is False
    assert [event.event_id for event in writer._queue] == ["first", "second"]
    assert committed_batches == []

    monkeypatch.setattr(writer._db, "commit", real_commit)
    writer.enqueue(make_event("fresh"))
    await writer._flush_once()
    assert durable_ids(tmp_path) == ["first", "second", "fresh"]
    assert writer.queue_depth == 0
    assert committed_batches == [["first", "second", "fresh"]]


async def test_database_failure_during_row_isolation_retries_valid_events(
    writer, tmp_path, committed_batches, monkeypatch, caplog,
):
    real_execute = writer._db.execute

    def fail_after_poison(sql, parameters=()):
        if sql.startswith("INSERT INTO events") and parameters[0] == "after":
            raise aiosqlite.OperationalError("temporary failure during isolation")
        return real_execute(sql, parameters)

    monkeypatch.setattr(writer._db, "execute", fail_after_poison)
    writer.enqueue(make_event("before"))
    writer.enqueue(make_event("bad-sql", event_type=None))
    writer.enqueue(make_event("after"))
    await writer._flush_once()

    assert durable_ids(tmp_path) == []
    assert writer._db.in_transaction is False
    assert [event.event_id for event in writer._queue] == ["before", "after"]
    assert committed_batches == []
    assert "bad-sql" in caplog.text
    assert "temporary failure during isolation" in caplog.text

    monkeypatch.setattr(writer._db, "execute", real_execute)
    writer.enqueue(make_event("fresh"))
    await writer._flush_once()
    assert durable_ids(tmp_path) == ["before", "after", "fresh"]
    assert writer.queue_depth == 0
    assert committed_batches == [["before", "after", "fresh"]]


async def test_concurrent_flush_cannot_commit_another_failed_batch(
    writer, tmp_path, committed_batches, monkeypatch,
):
    real_commit = writer._db.commit
    commit_entered = asyncio.Event()
    release_commit = asyncio.Event()
    attempts = 0

    async def delayed_failure():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            commit_entered.set()
            await release_commit.wait()
            raise aiosqlite.OperationalError("temporary commit failure")
        await real_commit()

    monkeypatch.setattr(writer._db, "commit", delayed_failure)
    writer.enqueue(make_event("first"))
    first_flush = asyncio.create_task(writer._flush_once())
    await asyncio.wait_for(commit_entered.wait(), timeout=1)
    writer.enqueue(make_event("fresh"))
    second_flush = asyncio.create_task(writer._flush_once())
    try:
        done, _ = await asyncio.wait({second_flush}, timeout=0.05)
        committed_before_release = durable_ids(tmp_path)
    finally:
        release_commit.set()
        await asyncio.gather(first_flush, second_flush)

    assert done == set()
    assert committed_before_release == []
    assert durable_ids(tmp_path) == ["first", "fresh"]
    assert writer.queue_depth == 0
    assert committed_batches == [["first", "fresh"]]


async def test_post_flush_hook_can_enqueue_and_flush(writer, tmp_path):
    observed = []

    async def emit_followup(batch):
        observed.append(([event.event_id for event in batch], durable_ids(tmp_path)))
        if batch[0].event_id == "first":
            writer.enqueue(make_event("followup"))
            await writer._flush_once()

    event_stream.register_post_flush_hook(emit_followup)
    try:
        writer.enqueue(make_event("first"))
        await asyncio.wait_for(writer._flush_once(), timeout=1)
    finally:
        event_stream.unregister_post_flush_hook(emit_followup)
    assert observed == [(["first"], ["first"]), (["followup"], ["first", "followup"])]
    assert writer.queue_depth == 0
