"""Recovery and access checks for source-backed Living State revisions."""
import copy
import json
from unittest.mock import AsyncMock, MagicMock
from unittest.mock import patch

import pytest

from kernos.kernel.compaction import CompactionOutputError, CompactionState
from kernos.kernel.living_state import decode_state, metadata_block, thread_records
from kernos.kernel.retrieval import RetrievalService
from tests.test_living_state_replay import (
    FIXTURE, INSTANCE, MEMBER, SPACE, _advance, _log_text, _persist_log, _setup,
)


@pytest.fixture(autouse=True)
def split_enabled(monkeypatch):
    monkeypatch.delenv("KERNOS_MEMORY_THREAD_STATE", raising=False)


def _first(tmp_path):
    scenario = copy.deepcopy(FIXTURE["scenarios"][0])
    return scenario, _setup(tmp_path, scenario)


async def test_retry_after_document_commit_repairs_missing_counter_cache(tmp_path, monkeypatch):
    scenario, harness = _first(tmp_path)
    service, _, space, _, reasoner = harness
    original_save = service.save_state
    service.save_state = AsyncMock(side_effect=OSError("cache write interrupted"))
    with pytest.raises(OSError, match="cache write"):
        await _advance(harness, scenario, scenario["boundaries"][0])
    doc = await service.load_document(INSTANCE, SPACE, MEMBER)
    before = decode_state(doc)
    assert len(thread_records(before)) == 2
    service.save_state = original_save
    recovered = await service.load_state(INSTANCE, SPACE, MEMBER)
    assert recovered.global_compaction_number == 1
    assert recovered.document_budget == 180000 and recovered.conversation_headroom == 8000
    calls = len(reasoner.calls)
    await service.compact_from_log(INSTANCE, SPACE, space, _log_text(scenario["logs"][0]),
                                   1, recovered, MEMBER)
    assert len(reasoner.calls) == calls
    assert await service.load_document(INSTANCE, SPACE, MEMBER) == doc
    assert recovered.global_compaction_number == 1


async def test_retry_with_appended_turn_processes_only_new_suffix(tmp_path):
    scenario, harness = _first(tmp_path)
    service, _, _, state, reasoner = harness
    original_save = service.save_state
    service.save_state = AsyncMock(side_effect=OSError("after document"))
    with pytest.raises(OSError):
        await _advance(harness, scenario, scenario["boundaries"][0])
    service.save_state = original_save
    first = decode_state(await service.load_document(INSTANCE, SPACE, MEMBER))
    quote = "I sent the final workshop PDF to the attendees."
    scenario["logs"][0]["turns"].append(dict(timestamp="2026-04-02T09:00:00+00:00",
                                          role="user", text=quote))
    number = len(scenario["logs"][0]["turns"])
    boundary = dict(after_log="log_001", gold=dict(open=[],
        close=[dict(alias="send_handout", reason="completed", evidence=[dict(
            log="log_001", turn=number, role="user", quote=quote)])],
        active=["letter_pages"], narrative_required_details=[]))
    document, records = await _advance(harness, scenario, boundary)
    final = decode_state(document)
    assert len([e for e in final["events"] if e["op"] == "open"]) == 2
    assert len([e for e in final["events"] if e["op"] == "close"]) == 1
    assert final["events"][:2] == first["events"]
    assert final["last_compaction"]["frontier"]["turn"] == number
    assert state.global_compaction_number == 2
    assert [r["status"] for r in records].count("closed") == 1


async def test_changed_consumed_source_prefix_fails_without_replacing_memory(tmp_path):
    scenario, harness = _first(tmp_path)
    await _advance(harness, scenario, scenario["boundaries"][0])
    service, logger, space, state, reasoner = harness
    before = await service.load_document(INSTANCE, SPACE, MEMBER)
    scenario["logs"][0]["turns"][0]["text"] = "A source was edited after being consumed."
    text = _persist_log(logger, scenario["logs"][0])
    calls = len(reasoner.calls)
    with pytest.raises(CompactionOutputError, match="prefix changed"):
        await service.compact_from_log(INSTANCE, SPACE, space, text, 1, state, MEMBER)
    assert len(reasoner.calls) == calls
    assert await service.load_document(INSTANCE, SPACE, MEMBER) == before


async def test_malformed_counter_cache_recovers_from_committed_revision(tmp_path, monkeypatch):
    scenario, harness = _first(tmp_path)
    await _advance(harness, scenario, scenario["boundaries"][0])
    service = harness[0]
    path = service._space_dir(INSTANCE, SPACE, MEMBER) / "state.json"
    path.write_text("{incomplete")
    monkeypatch.setenv("KERNOS_COMPACTION_THRESHOLD", "1234")
    recovered = await service.load_state(INSTANCE, SPACE, MEMBER)
    assert recovered.global_compaction_number == 1
    assert recovered.conversation_headroom == 8000
    assert recovered.document_budget == 180000
    assert recovered.compaction_threshold == 1234


async def test_current_cache_keeps_newer_backoff_and_runtime_counters(tmp_path):
    scenario, harness = _first(tmp_path)
    await _advance(harness, scenario, scenario["boundaries"][0])
    service, _, _, state, _ = harness
    state.consecutive_failures = 3
    state.last_compaction_failure_at = "2026-04-03T10:00:00+00:00"
    state.cumulative_new_tokens = 7234
    state.message_ceiling = 87000
    state.last_seed_depth = 17
    await service.save_state(INSTANCE, SPACE, state, MEMBER)
    loaded = await service.load_state(INSTANCE, SPACE, MEMBER)
    assert loaded.consecutive_failures == 3
    assert loaded.last_compaction_failure_at == state.last_compaction_failure_at
    assert loaded.cumulative_new_tokens == 7234
    assert loaded.message_ceiling == 87000
    assert loaded.last_seed_depth == 17


async def test_stale_valid_cache_is_repaired_from_committed_document(tmp_path):
    scenario, harness = _first(tmp_path)
    await _advance(harness, scenario, scenario["boundaries"][0])
    service = harness[0]
    path = service._space_dir(INSTANCE, SPACE, MEMBER) / "state.json"
    cached = json.loads(path.read_text())
    cached.update(global_compaction_number=0, compaction_number=0, cumulative_new_tokens=999)
    path.write_text(json.dumps(cached))
    loaded = await service.load_state(INSTANCE, SPACE, MEMBER)
    assert loaded.global_compaction_number == loaded.compaction_number == 1
    assert loaded.cumulative_new_tokens == 0


async def test_cache_ahead_of_document_is_not_silently_downgraded(tmp_path):
    from kernos.kernel.living_state import LivingStateError
    scenario, harness = _first(tmp_path)
    await _advance(harness, scenario, scenario["boundaries"][0])
    service, _, _, state, _ = harness
    state.global_compaction_number = 2
    await service.save_state(INSTANCE, SPACE, state, MEMBER)
    with pytest.raises(LivingStateError, match="ahead"):
        await service.load_state(INSTANCE, SPACE, MEMBER)


async def test_unknown_metadata_is_never_reset_to_empty_state(tmp_path):
    scenario, harness = _first(tmp_path)
    _, logger, space, state, _ = harness
    text = _persist_log(logger, scenario["logs"][0])
    service = harness[0]
    folder = service._space_dir(INSTANCE, SPACE, MEMBER)
    folder.mkdir(parents=True, exist_ok=True)
    doc = "# Ledger\n\n# Living State\n\nRetain me.\n<!-- KERNOS_LIVING_STATE_V99\n{}\n-->\n"
    (folder / "active_document.md").write_text(doc)
    with pytest.raises(CompactionOutputError, match="Unsupported"):
        await service.compact_from_log(INSTANCE, SPACE, space, text, 1, state, MEMBER)
    assert (folder / "active_document.md").read_text() == doc


async def test_migration_resume_uses_pinned_batches_after_budget_change(tmp_path, monkeypatch):
    from kernos.kernel import living_state
    scenario, harness = _first(tmp_path)
    monkeypatch.setattr(living_state, "SOURCE_CHUNK_CHARS", 1800)
    reasoner = harness[4]
    original = reasoner.complete_simple
    proposer_calls = 0
    async def fail_second_chunk(**kw):
        nonlocal proposer_calls
        if "THREAD_PROPOSER" in kw["system_prompt"]:
            proposer_calls += 1
            if proposer_calls == 2:
                raise RuntimeError("interrupted migration")
        return await original(**kw)
    reasoner.complete_simple = fail_second_chunk
    with pytest.raises(CompactionOutputError, match="model operation failed"):
        await _advance(harness, scenario, scenario["boundaries"][0])
    service = harness[0]
    partial = decode_state(await service.load_document(INSTANCE, SPACE, MEMBER))
    assert partial["migration"]["cursor"] == 1
    assert not partial["migration"]["complete"]
    displayed = await service.load_context_document(INSTANCE, SPACE, member_id=MEMBER)
    assert "migration incomplete" in displayed
    assert await service.load_thread_evidence(INSTANCE, SPACE, MEMBER) == []
    batches = partial["migration"]["batches"]
    monkeypatch.setattr(living_state, "SOURCE_CHUNK_CHARS", 24000)
    reasoner.complete_simple = original
    document, records = await _advance(harness, scenario, scenario["boundaries"][0])
    final = decode_state(document)
    assert final["migration"]["batches"] == batches
    assert final["migration"]["complete"]
    assert len(records) == 2


async def test_migration_resume_on_later_log_keeps_original_fresh_items(tmp_path, monkeypatch):
    from kernos.kernel import living_state
    scenario, harness = _first(tmp_path)
    monkeypatch.setattr(living_state, "source_chunks", lambda sources: [[s] for s in sources])
    reasoner = harness[4]
    original = reasoner.complete_simple
    proposer_calls = 0
    async def interrupted(**kw):
        nonlocal proposer_calls
        if "THREAD_PROPOSER" in kw["system_prompt"]:
            proposer_calls += 1
            if proposer_calls == 2:
                raise RuntimeError("interrupt before original commitments")
        return await original(**kw)
    reasoner.complete_simple = interrupted
    with pytest.raises(CompactionOutputError):
        await _advance(harness, scenario, scenario["boundaries"][0])
    quote = "I'll reserve the workshop room."
    scenario["logs"][1]["turns"] = [dict(timestamp="2026-04-02T09:00:00+00:00",
                                           role="user", text=quote)]
    scenario["items"]["room"] = dict(kind="commitment", opening=dict(
        log="log_002", turn=1, role="user", quote=quote))
    boundary = copy.deepcopy(scenario["boundaries"][0])
    boundary["after_log"] = "log_002"
    boundary["gold"]["open"].append("room")
    boundary["gold"]["active"].append("room")
    reasoner.complete_simple = original
    document, records = await _advance(harness, scenario, boundary)
    assert len(records) == 3
    assert all(r["status"] == "open" for r in records)
    assert decode_state(document)["migration"]["complete"]


async def test_workflow_trailer_retains_existing_consumer_contract_and_retries(tmp_path):
    scenario, harness = _first(tmp_path)
    reasoner, service = harness[4], harness[0]
    original = reasoner.complete_simple
    workflow = dict(description="Prepare the workshop outline", count=3, trigger="new workshop")
    follow_up = dict(type="USER_COMMITMENT", description="send the PDF", due="soon", context="workshop")
    async def with_trailers(**kw):
        result = await original(**kw)
        if "SPLIT_HISTORIAN" in kw["system_prompt"]:
            assert set(kw["output_schema"]["properties"]["recurring_workflows"]["items"]["properties"]) == {"description", "count", "trigger"}
            result = json.loads(result)
            result.update(recurring_workflows=[workflow], follow_ups=[follow_up])
            return json.dumps(result)
        return result
    reasoner.complete_simple = with_trailers
    await _advance(harness, scenario, scenario["boundaries"][0])
    loaded = await service.load_state(INSTANCE, SPACE, MEMBER)
    assert loaded._recurring_workflows == [workflow]
    assert loaded._follow_ups == [follow_up]
    assert loaded._recurring_workflows[0].get("count", 0) >= 3


async def test_remember_exposes_closed_evidence_and_uncited_legacy(tmp_path):
    scenario, harness = _first(tmp_path)
    scenario["initial"]["narrative"] = "Unresolved legacy ORCHID-DEVICE note without a supporting source."
    # A retained snapshot is evidence of what the summary said, never proof of truth.
    folder = harness[0]._space_dir(INSTANCE, SPACE, MEMBER)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "active_document.md").write_text(
        "# Ledger\n\n# Living State\n\n" + scenario["initial"]["narrative"] + "\n")
    for boundary in scenario["boundaries"]:
        await _advance(harness, scenario, boundary)
    state = MagicMock()
    state.get_context_space = AsyncMock(return_value=None)
    state.query_knowledge = AsyncMock(return_value=[])
    state.query_entity_nodes = AsyncMock(return_value=[])
    retrieval = RetrievalService(state, None, MagicMock(), harness[0], MagicMock())
    text = await retrieval.search(INSTANCE, "send final workshop PDF", SPACE, requesting_member_id=MEMBER)
    assert "closed commitment" in text and "completed" in text
    assert "log_003/turn_1" in text
    legacy = await retrieval.search(INSTANCE, "ORCHID DEVICE", SPACE, requesting_member_id=MEMBER)
    assert "ORCHID-DEVICE" in legacy and "Unverified" in legacy
    other = await retrieval.search(INSTANCE, "ORCHID DEVICE", SPACE, requesting_member_id="member_two")
    assert "ORCHID-DEVICE" not in other


async def test_multiple_recalled_thread_items_keep_status_with_their_quotations():
    state = MagicMock()
    state.get_context_space = AsyncMock(return_value=None)
    state.query_knowledge = AsyncMock(return_value=[])
    state.query_entity_nodes = AsyncMock(return_value=[])
    compaction = MagicMock()
    compaction.load_all_ledger_entries = AsyncMock(return_value=[])
    compaction.load_archived_living_states = AsyncMock(return_value=[])
    compaction.load_thread_evidence = AsyncMock(return_value=[
        dict(status="closed", label=f"closed commitment [thread_{i}]; completed [closing sources: studio/log_002/turn_{i + 1}]",
             text="Earlier context. " * 45 + f"QUOTE-{i} needle detail with evidence. " + "Later context. " * 2)
        for i in range(3)
    ])
    retrieval = RetrievalService(state, None, MagicMock(), compaction, MagicMock())
    text = await retrieval.search(INSTANCE, "needle detail", SPACE, requesting_member_id=MEMBER)
    assert "QUOTE-0" in text
    for i in range(3):
        if f"QUOTE-{i}" in text:
            assert f"closed commitment [thread_{i}]" in text
            assert f"studio/log_002/turn_{i + 1}" in text
    assert len(text) <= 6000


async def test_completion_proposer_and_verifier_receive_original_partial_progress(tmp_path):
    scenario, harness = _first(tmp_path)
    for boundary in scenario["boundaries"][:2]:
        await _advance(harness, scenario, boundary)
    reasoner = harness[4]
    reasoner.calls.clear()
    await _advance(harness, scenario, scenario["boundaries"][2])
    for marker in ("THREAD_PROPOSER", "THREAD_VERIFIER"):
        relevant = [payload for kind, payload in reasoner.calls if kind == marker]
        assert relevant
        assert any(source["log_number"] == 2 and source["turn_number"] == 1
                   and "draft is finished" in source["quote"]
                   and not source.get("eligible", False)
                   for payload in relevant for source in payload["sources"])
    historian = next(payload for kind, payload in reasoner.calls if kind == "SPLIT_HISTORIAN")
    assert len(historian["resolved_threads"]) == 1
    resolved = historian["resolved_threads"][0]
    assert resolved["status"] == "closed" and resolved["closing"]["reason"] == "completed"
    evidence_ids = {s["id"] for s in historian["sources"]}
    assert all(s["id"] in evidence_ids for s in resolved["evidence"] + resolved["closing"]["evidence"])


async def test_historical_migration_is_guided_by_legacy_but_new_span_is_fresh(tmp_path):
    scenario = copy.deepcopy(FIXTURE["scenarios"][2])
    harness = _setup(tmp_path, scenario)
    await _advance(harness, scenario, scenario["boundaries"][0])
    payload = next(payload for marker, payload in harness[4].calls if marker == "THREAD_PROPOSER")
    assert payload["migration"]
    assert payload["legacy_context"] == scenario["initial"]["narrative"]
    old = [s for s in payload["sources"] if s["log_number"] == 1]
    current = [s for s in payload["sources"] if s["log_number"] == 2]
    assert old and all(not s["fresh"] for s in old)
    assert current and all(s["fresh"] for s in current)


async def test_model_cannot_supply_a_ledger_entry_header(tmp_path):
    scenario, harness = _first(tmp_path)
    reasoner = harness[4]
    original = reasoner.complete_simple
    historians = 0
    async def malformed_first_heading(**kw):
        nonlocal historians
        result = await original(**kw)
        if "SPLIT_HISTORIAN" in kw["system_prompt"]:
            historians += 1
            if historians == 1:
                result = json.loads(result)
                result["ledger_bullets"] = ["## Compaction #99 — invented boundary"]
                return json.dumps(result)
        return result
    reasoner.complete_simple = malformed_first_heading
    document, _ = await _advance(harness, scenario, scenario["boundaries"][0])
    assert historians == 2
    assert "Compaction #99" not in document
    assert len(harness[0]._parse_ledger_entries(document)) == 1


def test_quoted_header_text_never_becomes_an_entry_boundary(tmp_path):
    _, harness = _first(tmp_path)
    document = ("# Ledger\n\n## Compaction #1 (source: log_001)\n\n"
                "- Discussed the quoted text '## Compaction #99'.\n\n"
                "# Living State\n\nCurrent orientation.\n")
    entries = harness[0]._parse_ledger_entries(document)
    assert len(entries) == 1
    assert "quoted text" in entries[0]


def test_inline_living_state_label_never_truncates_ledger(tmp_path):
    _, harness = _first(tmp_path)
    first = ('## Compaction #1 (source: log_001)\n\n'
             '- Discussed the literal text "# Living State" in a document.')
    second = '## Compaction #2 (source: log_002)\n\n- A later topic.'
    document = "# Ledger\n\n" + first + "\n\n" + second + "\n\n# Living State\n\nCurrent.\n"
    assert harness[0]._parse_ledger_entries(document) == [first, second]
    assert harness[0]._extract_living_state(document) == "Current."


async def test_source_frontier_and_passages_share_one_parent_log_snapshot():
    from kernos.kernel.conversation_log import HistoricalTurn
    from kernos.kernel.living_state import collect_sources
    old = "[2026-04-01T09:00:00+00:00] [user] [test] First parent turn.\n"
    appended = "[2026-04-01T09:01:00+00:00] [user] [test] Later parent turn.\n"
    active = "[2026-04-01T10:00:00+00:00] [user] [test] Active turn.\n"
    logger = MagicMock()
    async def discovery(instance, space, member, **kw):
        # Discovery was read just before the parent's second turn arrived.
        return (HistoricalTurn(1, 1, "user", "2026-04-01T09:00:00+00:00",
                               "First parent turn." if space == "parent" else "Active turn.",
                               space, member),)
    logger.read_source_turns = discovery
    logger.read_log_text = AsyncMock(side_effect=lambda instance, space, number, **kw:
                                     old + appended if space == "parent" else active)
    sources, cutoffs, _ = await collect_sources(logger, INSTANCE, MEMBER, SPACE, 1,
                                               active, {SPACE, "parent"})
    parent = [s for s in sources if s["space_id"] == "parent"]
    assert cutoffs["parent/log_001"]["turn"] == 2
    assert [s["turn_number"] for s in parent] == [1, 2]
    assert parent[1]["quote"] == "Later parent turn."


async def test_many_ledger_topics_share_source_words_in_verifier():
    from kernos.kernel.living_state import ThreadReasoner
    from kernos.kernel.tokens import EstimateTokenAdapter
    sources = [dict(id=f"studio/log_001/turn_{i + 1}@0:324", quote=(f"PASSAGE{i:03d} " + "word " * 63),
                    space_id=SPACE, log_number=1, turn_number=i + 1, start=0, end=324,
                    speaker="user", timestamp="2026-04-01T09:00:00+00:00") for i in range(100)]
    payloads = []
    async def complete_simple(**kw):
        payload = json.loads(kw["user_content"])
        if "SPLIT_HISTORIAN" in kw["system_prompt"]:
            return json.dumps(dict(ledger_bullets=[f"Discussed topic {i}." for i in range(20)],
                                   narrative=[], seed_depth=5, follow_ups=[], recurring_workflows=[]))
        payloads.append(kw["user_content"])
        return json.dumps(dict(supported=[c["id"] for c in payload["candidates"]],
                               unsupported=[], uncertain=[]))
    reasoner = ThreadReasoner(MagicMock(complete_simple=complete_simple), "Workshop", SPACE)
    output = await reasoner.narrative(sources, dict(events=[], narrative=[]), [], "Workshop")
    assert len(output["ledger_bullets"]) == 20
    assert len(payloads) == 1
    assert payloads[0].count("PASSAGE000") == 1
    assert await EstimateTokenAdapter().count_tokens(payloads[0]) < 30000


async def test_verifier_receives_proposed_classification_owner_and_close_reason():
    from kernos.kernel.living_state import ThreadReasoner
    source = dict(id="studio/log_001/turn_1@0:12", quote="Cancel that.", space_id=SPACE,
                  log_number=1, turn_number=1, start=0, end=12, speaker="user")
    async def complete_simple(**kw):
        payload = json.loads(kw["user_content"])
        opening, closing = payload["candidates"]
        assert opening["event"] == dict(op="open", id="item", kind="decision", responsible="user")
        assert closing["event"] == dict(op="close", id="item", reason="cancelled")
        assert "text" not in opening["event"] and "evidence" not in opening["event"]
        assert payload["sources"][0]["quote"] == "Cancel that."
        return json.dumps(dict(supported=["p0", "p1"], unsupported=[], uncertain=[]))
    candidates = [dict(id="p0", op="open", text="Cancel that.", evidence=[source], event=dict(
        op="open", id="item", kind="decision", responsible="user", text="Cancel that.", evidence=[source])),
        dict(id="p1", op="close", text="Cancel that.", evidence=[source], event=dict(
            op="close", id="item", reason="cancelled", evidence=[source]))]
    reasoner = ThreadReasoner(MagicMock(complete_simple=complete_simple), "Workshop", SPACE)
    assert await reasoner.verify(candidates, [source], []) == {"p0", "p1"}


@pytest.mark.parametrize("kind", ["decision", "commitment"])
async def test_internal_dispatch_cannot_create_a_human_choice_or_obligation(kind):
    from kernos.kernel.living_state import ThreadReasoner
    source = dict(id="studio/log_001/turn_1@0:18", quote="Use the north room.",
                  space_id=SPACE, log_number=1, turn_number=1, start=0, end=18,
                  speaker="user", channel="internal", fresh=True)
    async def complete_simple(**kw):
        payload = json.loads(kw["user_content"])
        if "THREAD_PROPOSER" in kw["system_prompt"]:
            return json.dumps(dict(open=[dict(kind=kind, responsible="user", evidence=[source["id"]])], close=[]))
        return json.dumps(dict(supported=[c["id"] for c in payload["candidates"]], unsupported=[], uncertain=[]))
    reasoner = ThreadReasoner(MagicMock(complete_simple=complete_simple), "Workshop", SPACE)
    state = dict(events=[], legacy_state="")
    await reasoner.reconcile(state, [source], [source], migration=False)
    assert state["events"] == []
    source["channel"] = "chat"
    await reasoner.reconcile(state, [source], [source], migration=False)
    assert len(state["events"]) == 1


async def test_internal_user_dispatch_cannot_confirm_completion_but_system_receipt_can():
    from kernos.kernel.living_state import ThreadReasoner
    opening = dict(id="studio/log_001/turn_1@0:17", quote="I'll send the PDF.",
                   space_id=SPACE, log_number=1, turn_number=1, start=0, end=17,
                   speaker="user", channel="chat")
    source = dict(id="studio/log_002/turn_1@0:17", quote="The PDF was sent.",
                  space_id=SPACE, log_number=2, turn_number=1, start=0, end=17,
                  speaker="user", channel="internal", fresh=True)
    state = dict(events=[dict(op="open", id="pdf", kind="commitment", responsible="user",
                              text=opening["quote"], evidence=[opening])])
    async def complete_simple(**kw):
        payload = json.loads(kw["user_content"])
        if "THREAD_PROPOSER" in kw["system_prompt"]:
            return json.dumps(dict(open=[], close=[dict(id="pdf", reason="completed", evidence=[source["id"]])]))
        return json.dumps(dict(supported=[c["id"] for c in payload["candidates"]], unsupported=[], uncertain=[]))
    reasoner = ThreadReasoner(MagicMock(complete_simple=complete_simple), "Workshop", SPACE)
    await reasoner.reconcile(state, [source], [source], migration=False)
    assert thread_records(state)[0]["status"] == "open"
    source.update(speaker="system", quote="[send] state=completed | delivered successfully")
    await reasoner.reconcile(state, [source], [source], migration=False)
    assert thread_records(state)[0]["status"] == "closed"


async def test_authority_channel_is_preserved_and_consumed_prefix_changes_are_rejected(tmp_path):
    scenario, harness = _first(tmp_path)
    service, logger, space, state, _ = harness
    text = _persist_log(logger, scenario["logs"][0])
    turns = await logger.read_source_turns(INSTANCE, SPACE, MEMBER, include_current=True)
    assert all(turn.channel == "test" for turn in turns)
    await _advance(harness, scenario, scenario["boundaries"][0])
    original = await service.load_document(INSTANCE, SPACE, MEMBER)
    metadata = decode_state(original)
    assert all(s["channel"] == "test" for r in thread_records(metadata) for s in r["evidence"])
    changed = text.replace("[test]", "[internal]", 1)
    path = logger._logs_dir(INSTANCE, SPACE, MEMBER) / "log_001.txt"
    path.write_text(changed)
    with pytest.raises(CompactionOutputError, match="prefix changed"):
        await service.compact_from_log(INSTANCE, SPACE, space, changed, 1, state, MEMBER)
    assert await service.load_document(INSTANCE, SPACE, MEMBER) == original


async def test_ledger_repair_preserves_thread_metadata_byte_for_byte(tmp_path):
    scenario, harness = _first(tmp_path)
    for boundary in scenario["boundaries"][:2]:
        await _advance(harness, scenario, boundary)
    service, logger, space, _, _ = harness
    original = await service.load_document(INSTANCE, SPACE, MEMBER)
    block = metadata_block(original)
    entries = service._parse_ledger_entries(original)
    path = service._space_dir(INSTANCE, SPACE, MEMBER) / "active_document.md"
    path.write_text(service._render_document(entries[1:], service._extract_living_state(original)) + block)
    assert await service.rebuild_missing_entries(INSTANCE, SPACE, space, logger, member_id=MEMBER) == [1]
    repaired = await service.load_document(INSTANCE, SPACE, MEMBER)
    assert metadata_block(repaired) == block
    assert thread_records(decode_state(repaired)) == thread_records(decode_state(original))


async def test_split_compaction_retains_member_scoped_event_receipts(tmp_path):
    scenario, harness = _first(tmp_path)
    with patch("kernos.kernel.event_stream.emit", new=AsyncMock()) as emit:
        await _advance(harness, scenario, scenario["boundaries"][0])
    assert [call.args[1] for call in emit.call_args_list] == ["compaction.triggered", "compaction.completed"]
    assert all(call.kwargs["member_id"] == MEMBER and call.kwargs["space_id"] == SPACE
               for call in emit.call_args_list)


async def test_domain_and_child_briefings_never_consume_recovery_metadata(tmp_path):
    from kernos.kernel.compaction import domain_assessment_evidence
    from kernos.messages.handler import MessageHandler
    from kernos.kernel.spaces import ContextSpace
    scenario, harness = _first(tmp_path)
    document, _ = await _advance(harness, scenario, scenario["boundaries"][0])
    evidence = domain_assessment_evidence(document)
    assert "Last reconciled source" in evidence
    assert "KERNOS_LIVING_STATE" not in evidence
    assert not evidence.rstrip().endswith("-->")
    handler = MessageHandler.__new__(MessageHandler)
    handler.compaction = harness[0]
    child = ContextSpace(id="child", instance_id=INSTANCE, name="Child", parent_id=SPACE)
    handler.state = MagicMock()
    handler.state.list_child_spaces = AsyncMock(return_value=[child])
    handler.reasoning = MagicMock()
    handler.reasoning.complete_simple = AsyncMock(return_value="- Relevant current choice.")
    await handler._produce_child_briefings(INSTANCE, SPACE, harness[2], member_id=MEMBER)
    content = handler.reasoning.complete_simple.call_args.kwargs["user_content"]
    assert "letter-size" in content
    assert "KERNOS_LIVING_STATE" not in content
    assert '"version":1' not in content
