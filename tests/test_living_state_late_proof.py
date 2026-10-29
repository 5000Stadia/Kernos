"""Original evidence may settle an existing thread when it is recognized later.

All logs and quotations are fictional. The model selects source-table aliases;
real source collection and reconciliation retain the physical citations.
"""
import copy
import json

import pytest

from kernos.kernel.conversation_log import ConversationLogger
from kernos.kernel.living_state import ThreadReasoner, collect_sources, thread_records


SPACE = "observatory"
INSTANCE = "fictional_late_proof"
MEMBER = "fictional_reader"
OPENING = "I will render the lunar report as report.pdf."
RECEIPT = "render report state=completed | artifact=report.pdf"
CURRENT = "The next discussion concerns telescope paint."


def _turn(number, quote, *, speaker="user", space=SPACE, log=1,
          timestamp=None, channel="chat"):
    return dict(number=number, quote=quote, speaker=speaker, space=space, log=log,
                timestamp=timestamp or f"2026-06-01T09:{number:02d}:00+00:00",
                channel=channel)


async def _read_sources(tmp_path, turns):
    logger = ConversationLogger(str(tmp_path))
    grouped = {}
    for turn in turns:
        grouped.setdefault((turn["space"], turn["log"]), []).append(turn)
    texts = {}
    for (space, number), entries in grouped.items():
        entries.sort(key=lambda t: t["number"])
        assert [t["number"] for t in entries] == list(range(1, len(entries) + 1))
        text = "\n".join(
            f"[{t['timestamp']}] [{t['speaker']}] [{t['channel']}] {t['quote']}"
            for t in entries)
        directory = logger._logs_dir(INSTANCE, space, MEMBER)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"log_{number:03d}.txt").write_text(text)
        texts[space, number] = text
    current = max(number for space, number in grouped if space == SPACE)
    for space in {space for space, _ in grouped}:
        number = max(number for candidate, number in grouped if candidate == space)
        (logger._logs_dir(INSTANCE, space, MEMBER) / "meta.json").write_text(
            json.dumps(dict(current_log=number, current_log_tokens_est=0)))
    sources, _, _ = await collect_sources(
        logger, INSTANCE, MEMBER, SPACE, current, texts[SPACE, current],
        {space for space, _ in grouped})
    for source in sources:
        assert source["id"] == (
            f"{source['space_id']}/log_{source['log_number']:03d}/"
            f"turn_{source['turn_number']}@{source['start']}:{source['end']}")
        original = next(t for t in turns if
                        (t["space"], t["log"], t["number"]) ==
                        (source["space_id"], source["log_number"], source["turn_number"]))
        assert source["quote"] == original["quote"][source["start"]:source["end"]]
    return sources


def _find(sources, quote):
    matches = [s for s in sources if s["quote"] == quote]
    assert len(matches) == 1
    return matches[0]


class _Model:
    def __init__(self, *, opening=(), closing=(), kind="commitment",
                 reason="completed", verdict="supported"):
        self.opening = list(opening)
        self.closing = list(closing)
        self.kind, self.reason, self.verdict = kind, reason, verdict
        self.proposals, self.verifications = [], []

    @staticmethod
    def _labels(wanted, sources):
        labels = []
        for original in wanted:
            matches = [s for s in sources if all(s[key] == original[key] for key in
                       ("quote", "space_id", "log_number", "turn_number", "start", "end"))]
            assert len(matches) == 1, "The model must select an actually supplied source."
            assert matches[0]["id"].startswith("s")
            labels.append(matches[0]["id"])
        return labels

    async def complete_simple(self, *, user_content, output_schema, **kwargs):
        payload = json.loads(user_content)
        properties = set(output_schema["properties"])
        if properties == {"open", "close"}:
            self.proposals.append(payload)
            openings = ([dict(kind=self.kind, responsible="user",
                              evidence=self._labels(self.opening, payload["sources"]))]
                        if self.opening else [])
            closings = ([dict(id=payload["open_threads"][0]["id"], reason=self.reason,
                              evidence=self._labels(self.closing, payload["sources"]))]
                        if self.closing else [])
            return json.dumps(dict(open=openings, close=closings))
        assert properties == {"supported", "unsupported", "uncertain"}
        self.verifications.append(payload)
        result = dict(supported=[], unsupported=[], uncertain=[])
        result[self.verdict] = [c["id"] for c in payload["candidates"]]
        return json.dumps(result)


async def _establish(sources, opening, *, kind="commitment"):
    state = dict(events=[], legacy_state="")
    model = _Model(opening=opening, kind=kind)
    await ThreadReasoner(model, "Prepare lunar reports", SPACE).reconcile(
        state, opening, sources, migration=False)
    records = thread_records(state)
    assert len(records) == 1 and records[0]["status"] == "open"
    assert records[0]["evidence"] == opening
    assert records[0]["text"] == "\n".join(s["quote"] for s in opening)
    return state


async def _settle(state, sources, chunk, proof, *, reason="completed", verdict="supported"):
    before = copy.deepcopy(state["events"][0])
    model = _Model(closing=proof, reason=reason, verdict=verdict)
    await ThreadReasoner(model, "Prepare lunar reports", SPACE).reconcile(
        state, chunk, sources, migration=False)
    assert state["events"][0] == before
    return thread_records(state)[0], model


def _assert_closed(record, proof, *, reason="completed"):
    assert record["status"] == "closed"
    assert record["closing"] == dict(op="close", id=record["id"], reason=reason, evidence=proof)
    assert [s["quote"] for s in record["closing"]["evidence"]] == [s["quote"] for s in proof]


@pytest.mark.parametrize("kind,reason,speaker,quote", [
    ("commitment", "completed", "system", RECEIPT),
    ("commitment", "cancelled", "user", "Cancel my plan to render the lunar report."),
    ("decision", "superseded", "user", "Replace the lunar report decision with a star chart."),
])
async def test_previously_read_proof_can_settle_an_existing_item(
        tmp_path, kind, reason, speaker, quote):
    sources = await _read_sources(tmp_path, [
        _turn(1, OPENING), _turn(2, quote, speaker=speaker), _turn(3, CURRENT)])
    opening, proof, current = (_find(sources, q) for q in (OPENING, quote, CURRENT))
    state = await _establish(sources, [opening], kind=kind)
    record, model = await _settle(state, sources, [current], [proof], reason=reason)
    supplied = next(s for s in model.proposals[0]["sources"] if s["quote"] == quote)
    assert supplied["eligible"] is False
    _assert_closed(record, [proof], reason=reason)
    assert len(model.verifications) == 1


async def test_current_completion_still_uses_the_original_receipt(tmp_path):
    sources = await _read_sources(tmp_path, [
        _turn(1, OPENING), _turn(2, RECEIPT, speaker="system")])
    opening, proof = (_find(sources, q) for q in (OPENING, RECEIPT))
    state = await _establish(sources, [opening])
    record, model = await _settle(state, sources, [proof], [proof])
    _assert_closed(record, [proof])
    assert len(model.verifications) == 1


async def test_old_opening_evidence_cannot_create_a_new_item(tmp_path):
    sources = await _read_sources(tmp_path, [_turn(1, OPENING), _turn(2, CURRENT)])
    opening, current = (_find(sources, q) for q in (OPENING, CURRENT))
    model = _Model(opening=[opening])
    state = dict(events=[], legacy_state="")
    await ThreadReasoner(model, "Prepare lunar reports", SPACE).reconcile(
        state, [current], sources, migration=False)
    assert thread_records(state) == []
    assert model.verifications == []


@pytest.mark.parametrize("eligible", [False, True])
async def test_new_run_cannot_borrow_an_earlier_receipt_with_the_same_filename(tmp_path, eligible):
    sources = await _read_sources(tmp_path, [
        _turn(1, RECEIPT, speaker="system"),
        _turn(2, "Rerun the lunar report and render report.pdf again."), _turn(3, CURRENT)])
    proof = _find(sources, RECEIPT)
    opening = _find(sources, "Rerun the lunar report and render report.pdf again.")
    state = await _establish(sources, [opening])
    record, _ = await _settle(state, sources,
                              [proof] if eligible else [_find(sources, CURRENT)], [proof])
    assert record["status"] == "open"
    assert len(state["events"]) == 1


@pytest.mark.parametrize("eligible", [False, True])
async def test_the_opening_source_cannot_also_confirm_its_completion(tmp_path, eligible):
    sources = await _read_sources(tmp_path, [_turn(1, OPENING), _turn(2, CURRENT)])
    opening = _find(sources, OPENING)
    state = await _establish(sources, [opening])
    record, _ = await _settle(state, sources,
                              [opening] if eligible else [_find(sources, CURRENT)], [opening])
    assert record["status"] == "open"
    assert len(state["events"]) == 1


@pytest.mark.parametrize("verdict", ["unsupported", "uncertain"])
@pytest.mark.parametrize("eligible", [False, True])
async def test_old_proof_still_requires_a_supported_semantic_verdict(tmp_path, verdict, eligible):
    sources = await _read_sources(tmp_path, [
        _turn(1, OPENING), _turn(2, RECEIPT, speaker="system"), _turn(3, CURRENT)])
    state = await _establish(sources, [_find(sources, OPENING)])
    proof = _find(sources, RECEIPT)
    record, model = await _settle(state, sources,
                                  [proof] if eligible else [_find(sources, CURRENT)],
                                  [proof], verdict=verdict)
    assert record["status"] == "open"
    assert len(state["events"]) == 1
    if eligible:
        assert len(model.verifications) == 1
    assert all(c["op"] == "close" for p in model.verifications for c in p["candidates"])


@pytest.mark.parametrize("eligible", [False, True])
async def test_internal_user_dispatch_is_not_human_confirmation(tmp_path, eligible):
    quote = "The lunar report is complete and report.pdf is ready."
    sources = await _read_sources(tmp_path, [
        _turn(1, OPENING), _turn(2, quote, channel="internal"), _turn(3, CURRENT)])
    state = await _establish(sources, [_find(sources, OPENING)])
    proof = _find(sources, quote)
    record, _ = await _settle(state, sources,
                              [proof] if eligible else [_find(sources, CURRENT)], [proof])
    assert record["status"] == "open"
    assert len(state["events"]) == 1


@pytest.mark.parametrize("proof_is_later", [False, True])
@pytest.mark.parametrize("eligible", [False, True])
async def test_same_space_uses_physical_order_when_timestamps_regress(tmp_path, proof_is_later, eligible):
    entries = ([_turn(1, OPENING, timestamp="2026-06-02T09:00:00+00:00"),
                _turn(2, RECEIPT, speaker="system", timestamp="2026-06-01T09:00:00+00:00")]
               if proof_is_later else
               [_turn(1, RECEIPT, speaker="system", timestamp="2026-06-02T09:00:00+00:00"),
                _turn(2, OPENING, timestamp="2026-06-01T09:00:00+00:00")])
    sources = await _read_sources(tmp_path, entries + [_turn(3, CURRENT)])
    state = await _establish(sources, [_find(sources, OPENING)])
    proof = _find(sources, RECEIPT)
    record, _ = await _settle(state, sources,
                              [proof] if eligible else [_find(sources, CURRENT)], [proof])
    if proof_is_later:
        _assert_closed(record, [proof])
    else:
        assert record["status"] == "open"
        assert len(state["events"]) == 1


@pytest.mark.parametrize("timestamp,verdict,expected", [
    ("2026-06-01T08:59:00+00:00", "supported", "open"),
    ("2026-06-01T09:00:00+00:00", "supported", "open"),
    ("2026-06-01T09:01:00+00:00", "supported", "closed"),
    ("2026-06-01T09:00:00+00:00", "unsupported", "open"),
    ("2026-06-01T09:01:00+00:00", "uncertain", "open"),
    ("2026-06-01T09:01:00", "supported", "open"),
    ("2026-06-01Tunknown", "supported", "open"),
])
@pytest.mark.parametrize("eligible", [False, True])
async def test_other_space_time_bounds_do_not_replace_semantic_verification(
        tmp_path, timestamp, verdict, expected, eligible):
    sources = await _read_sources(tmp_path, [
        _turn(1, OPENING, timestamp="2026-06-01T09:00:00+00:00"),
        _turn(1, RECEIPT, speaker="system", space="archive", timestamp=timestamp),
        _turn(2, CURRENT)])
    state = await _establish(sources, [_find(sources, OPENING)])
    proof = _find(sources, RECEIPT)
    record, model = await _settle(state, sources,
                                  [proof] if eligible else [_find(sources, CURRENT)],
                                  [proof], verdict=verdict)
    assert record["status"] == expected
    if expected == "closed":
        _assert_closed(record, [proof])
        assert len(model.verifications) == 1
    else:
        assert len(state["events"]) == 1


@pytest.mark.parametrize("proof_after_adoption", [False, True])
@pytest.mark.parametrize("eligible", [False, True])
async def test_completion_must_follow_every_source_that_established_the_item(
        tmp_path, proof_after_adoption, eligible):
    proposal = "Proposed decision: render a lunar report as report.pdf."
    adoption = "Yes, adopt that lunar report decision."
    middle = ([_turn(2, adoption), _turn(3, RECEIPT, speaker="system")]
              if proof_after_adoption else
              [_turn(2, RECEIPT, speaker="system"), _turn(3, adoption)])
    sources = await _read_sources(tmp_path, [
        _turn(1, proposal, speaker="assistant"), *middle, _turn(4, CURRENT)])
    opening = [_find(sources, proposal), _find(sources, adoption)]
    state = await _establish(sources, opening, kind="decision")
    proof = _find(sources, RECEIPT)
    record, _ = await _settle(state, sources,
                              [proof] if eligible else [_find(sources, CURRENT)], [proof])
    if proof_after_adoption:
        _assert_closed(record, [proof])
    else:
        assert record["status"] == "open"
        assert len(state["events"]) == 1


async def test_later_passage_in_the_same_turn_can_confirm_completion(tmp_path):
    confirmation = "The lunar report is now complete."
    sources = await _read_sources(tmp_path, [
        _turn(1, OPENING + " " + confirmation), _turn(2, CURRENT)])
    opening, proof = (_find(sources, q) for q in (OPENING, confirmation))
    assert opening["id"] == f"{SPACE}/log_001/turn_1@0:{len(OPENING)}"
    assert proof["start"] == len(OPENING) + 1
    state = await _establish(sources, [opening])
    record, _ = await _settle(state, sources, [_find(sources, CURRENT)], [proof])
    _assert_closed(record, [proof])


@pytest.mark.parametrize("eligible", [False, True])
async def test_old_receipt_and_later_assistant_status_cannot_jointly_confirm_a_new_run(
        tmp_path, eligible):
    claim = "The new lunar report has finished."
    sources = await _read_sources(tmp_path, [
        _turn(1, RECEIPT, speaker="system"), _turn(2, OPENING),
        _turn(3, claim, speaker="assistant"), _turn(4, CURRENT)])
    state = await _establish(sources, [_find(sources, OPENING)])
    proof = [_find(sources, RECEIPT), _find(sources, claim)]
    record, _ = await _settle(state, sources,
                              proof if eligible else [_find(sources, CURRENT)], proof)
    assert record["status"] == "open"
    assert len(state["events"]) == 1
