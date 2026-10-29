"""Labelled source replay for durable threads and separately regenerated narrative.

The source files and gold labels are fictional. Only model reasoning is scripted;
production compaction, source readers, persistence, and display remain real.
"""
import copy
import importlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kernos.kernel.compaction import CompactionService, CompactionState
from kernos.kernel.conversation_log import ConversationLogger
from kernos.kernel.spaces import ContextSpace
from kernos.kernel.tokens import EstimateTokenAdapter


FIXTURE = json.loads((Path(__file__).parent / "fixtures/living_state_replay.json").read_text())
INSTANCE = "fictional_replay"
SPACE = "studio"
MEMBER = "member_one"


def _source_key(source):
    return (
        source.get("space_id", SPACE),
        int(source.get("log_number", str(source.get("log", "log_000")).split("_")[-1])),
        int(source.get("turn_number", source.get("turn", 0))),
    )


def _alias(record, scenario):
    keys = {_source_key(source) for source in record.get("evidence", [])}
    matches = [alias for alias, item in scenario["items"].items()
               if _source_key(item["opening"]) in keys]
    return matches[0] if len(matches) == 1 else f"unknown:{record['id']}"


def _records(document):
    # A baseline without the register still reaches a behavioral assertion,
    # rather than failing test collection because its module does not exist.
    try:
        living_state = importlib.import_module("kernos.kernel.living_state")
    except ModuleNotFoundError:
        assert False, "Compaction did not persist a durable thread register."
    metadata = living_state.decode_state(document)
    assert metadata is not None, "Compaction did not persist a durable thread register."
    return living_state.thread_records(metadata)


def _metrics(*, expected_open, actual_open, expected_close, actual_close,
             expected_active, actual_active, displayed_tokens):
    """Separate errors that a mere 'no silent disappearance' score conceals."""
    expected_open, actual_open = set(expected_open), set(actual_open)
    expected_close, actual_close = set(expected_close), set(actual_close)
    expected_active, actual_active = set(expected_active), set(actual_active)
    return {
        "missed_openings": sorted(expected_open - actual_open),
        "false_openings": sorted(actual_open - expected_open),
        "wrong_closures": sorted(actual_close - expected_close),
        "missed_closures": sorted(expected_close - actual_close),
        "stale_items": sorted(actual_active - expected_active),
        "lost_active_items": sorted(expected_active - actual_active),
        "displayed_tokens": displayed_tokens,
    }


class _ScriptedReasoning:
    """Propose real negative examples, then judge them by independent gold."""

    def __init__(self, scenario):
        self.scenario = scenario
        self.boundary = None
        self.calls = []
        self.generated_narratives = []
        self.rejected = []
        self.force_open_aliases = []
        self.uncertain_aliases = set()

    def _ref(self, sources, label, *, eligible=False):
        for source in sources:
            if _source_key(source) == _source_key(label) and label["quote"] == source["quote"]:
                if not eligible or source.get("eligible", True):
                    return source
        return None

    def _record(self, records, alias):
        return next((record for record in records if _alias(record, self.scenario) == alias), None)

    async def complete_simple(self, *, system_prompt, user_content, **kwargs):
        if not any(marker in system_prompt for marker in
                   ("THREAD_PROPOSER", "THREAD_VERIFIER", "SPLIT_HISTORIAN")):
            # Old compaction is a meaningful negative control: it rewrites an
            # empty narrative and consequently loses unresolved source items.
            return ("# New Ledger Entry\n\n- Synthetic conversation.\n\n"
                    "# Living State\n\nNo current summary.\n\nSEED_DEPTH: 5\n"
                    "RECURRING_WORKFLOWS: NONE\nFOLLOW_UPS: NONE\n")
        payload = json.loads(user_content)
        marker = next(marker for marker in
                      ("THREAD_PROPOSER", "THREAD_VERIFIER", "SPLIT_HISTORIAN")
                      if marker in system_prompt)
        self.calls.append((marker, copy.deepcopy(payload)))
        sources = payload.get("sources", [])
        gold = self.boundary["gold"]
        if marker == "THREAD_PROPOSER":
            records = payload.get("open_threads", [])
            openings, closings = [], []
            aliases = list(gold["open"]) + self.force_open_aliases
            if payload.get("migration"):
                aliases += self.scenario["initial"]["active"]
            for alias in dict.fromkeys(aliases):
                if self._record(records, alias):
                    continue
                item = self.scenario["items"][alias]
                source = self._ref(sources, item["opening"], eligible=True)
                if source:
                    openings.append({"kind": item["kind"], "responsible": "user",
                                     "evidence": [source["id"]]})
            for label in gold.get("non_openings", []):
                source = self._ref(sources, label, eligible=True)
                if source:
                    openings.append({"kind": "commitment", "responsible": label["role"],
                                     "evidence": [source["id"]]})
            for closed in gold["close"]:
                record = self._record(records, closed["alias"])
                evidence = [self._ref(sources, label) for label in closed["evidence"]]
                if record and all(evidence):
                    closings.append({"id": record["id"], "reason": closed["reason"],
                                     "evidence": [source["id"] for source in evidence]})
            for label in gold.get("non_closures", []):
                record = self._record(records, label["alias"])
                source = self._ref(sources, label)
                if record and source:
                    closings.append({"id": record["id"], "reason": "completed",
                                     "evidence": [source["id"]]})
            return json.dumps({"open": openings, "close": closings})
        if marker == "THREAD_VERIFIER":
            supported, unsupported, uncertain = [], [], []
            for candidate in payload["candidates"]:
                operation = candidate["op"]
                evidence = candidate.get("evidence", [])
                keys = {_source_key(source) for source in evidence}
                if operation == "open":
                    alias = _alias({"id": candidate["id"], "evidence": evidence}, self.scenario)
                    allowed = set(gold["open"]) | set(self.scenario["initial"]["active"])
                    accepted = alias in allowed
                    if alias in self.uncertain_aliases:
                        uncertain.append(candidate["id"])
                        continue
                elif operation == "close":
                    alias = _alias(candidate.get("thread", {}), self.scenario)
                    accepted = any(closed["alias"] == alias and
                                   {_source_key(source) for source in closed["evidence"]} <= keys
                                   for closed in gold["close"])
                else:
                    assert operation == "narrative", candidate
                    accepted = bool(evidence)
                if accepted:
                    supported.append(candidate["id"])
                else:
                    unsupported.append(candidate["id"])
                    self.rejected.append(copy.deepcopy(candidate))
            return json.dumps({"supported": supported, "unsupported": unsupported,
                               "uncertain": uncertain})
        assert marker == "SPLIT_HISTORIAN"
        serialized = json.dumps(payload)
        for previous in self.generated_narratives:
            assert previous not in serialized, "Previous narrative was recursively summarized."
        assert not {"previous_narrative", "previous_state", "living_state"} & payload.keys()
        # This is an independent response from source evidence, not an edit of
        # the previous narrative. The content labels allow semantic paraphrase.
        narrative = []
        for detail in gold.get("narrative_required_details", []):
            text = f"Boundary {self.boundary['after_log']}: {detail}"
            narrative.append({"text": text, "evidence": [source["id"] for source in sources]})
            self.generated_narratives.append(text)
        return json.dumps({"ledger_bullets": ["Synthetic source exchange recorded."],
                           "narrative": narrative, "seed_depth": 5,
                           "follow_ups": [], "recurring_workflows": []})


def _log_text(log):
    return "".join(f"[{turn['timestamp']}] [{turn['role']}] [test] {turn['text']}\n"
                   for turn in log["turns"])


def _persist_log(logger, log, *, space=SPACE, member=MEMBER):
    number = int(log["id"].split("_")[-1])
    directory = logger._logs_dir(INSTANCE, space, member)
    directory.mkdir(parents=True, exist_ok=True)
    text = _log_text(log)
    (directory / f"log_{number:03d}.txt").write_text(text)
    (directory / "meta.json").write_text(json.dumps({"current_log": number,
                                                    "current_log_tokens_est": 0}))
    return text


def _setup(tmp_path, scenario):
    reasoner = _ScriptedReasoning(scenario)
    service = CompactionService(state=MagicMock(), reasoning=reasoner,
                                token_adapter=EstimateTokenAdapter(), data_dir=str(tmp_path))
    logger = ConversationLogger(data_dir=str(tmp_path))
    space = ContextSpace(id=SPACE, instance_id=INSTANCE, name="Fictional studio")
    state = CompactionState(space_id=SPACE, document_budget=180000,
                            message_ceiling=100000, conversation_headroom=8000)
    for log in scenario["logs"]:
        if log.get("initial_source_only"):
            _persist_log(logger, log)
    if scenario["initial"]["narrative"]:
        directory = service._space_dir(INSTANCE, SPACE, MEMBER)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "active_document.md").write_text(
            "# Ledger\n\n## Compaction #1 (source: log_001)\n\n- Older exchange.\n\n"
            f"# Living State\n\n{scenario['initial']['narrative']}\n")
        state.compaction_number = state.global_compaction_number = 1
    return service, logger, space, state, reasoner


async def _advance(harness, scenario, boundary):
    service, logger, space, state, reasoner = harness
    reasoner.boundary = boundary
    log = next(log for log in scenario["logs"] if log["id"] == boundary["after_log"])
    text = _persist_log(logger, log)
    await service.compact_from_log(INSTANCE, SPACE, space, text,
                                   int(log["id"].split("_")[-1]), state,
                                   member_id=MEMBER)
    document = await service.load_document(INSTANCE, SPACE, MEMBER)
    return document, _records(document)


def _check_sources(records, scenario):
    logs = {int(log["id"].split("_")[-1]): log for log in scenario["logs"]}
    for record in records:
        alias = _alias(record, scenario)
        assert alias in scenario["items"], record
        opening = scenario["items"][alias]["opening"]
        assert record["text"] == opening["quote"]
        assert record["kind"] == scenario["items"][alias]["kind"]
        assert record["responsible"] == "user"
        for evidence in record["evidence"]:
            assert evidence["space_id"] == SPACE
            turn = logs[evidence["log_number"]]["turns"][evidence["turn_number"] - 1]
            assert turn["text"][evidence["start"]:evidence["end"]] == evidence["quote"]
            assert evidence["speaker"] == turn["role"]
            assert evidence["timestamp"] == turn["timestamp"]
        if record["status"] == "closed":
            label = next(closed for boundary in scenario["boundaries"]
                         for closed in boundary["gold"]["close"] if closed["alias"] == alias)
            closing = record.get("closing")
            assert closing, record
            assert closing["reason"] == label["reason"]
            actual = {_source_key(source) for source in closing["evidence"]}
            assert {_source_key(source) for source in label["evidence"]} <= actual


@pytest.fixture(autouse=True)
def default_thread_state(monkeypatch):
    monkeypatch.delenv("KERNOS_MEMORY_THREAD_STATE", raising=False)


@pytest.mark.parametrize("scenario", FIXTURE["scenarios"], ids=lambda scenario: scenario["id"])
async def test_labelled_replay_preserves_verbatim_threads_and_supported_lifecycle(tmp_path, scenario,
                                                                              record_property):
    harness = _setup(tmp_path, scenario)
    seen, previously_closed = set(), set()
    token_counts = []
    for index, boundary in enumerate(scenario["boundaries"]):
        document, records = await _advance(harness, scenario, boundary)
        _check_sources(records, scenario)
        aliases = {_alias(record, scenario) for record in records}
        active = {_alias(record, scenario) for record in records if record["status"] == "open"}
        closed = {_alias(record, scenario) for record in records if record["status"] == "closed"}
        displayed = await harness[0].load_context_document(INSTANCE, SPACE, member_id=MEMBER)
        tokens = await harness[0].adapter.count_tokens(displayed)
        token_counts.append(tokens)
        opened = aliases - seen
        if index == 0:
            opened -= set(scenario["initial"]["active"])
        metrics = _metrics(expected_open=boundary["gold"]["open"], actual_open=opened,
                           expected_close=[item["alias"] for item in boundary["gold"]["close"]],
                           actual_close=closed - previously_closed,
                           expected_active=boundary["gold"]["active"], actual_active=active,
                           displayed_tokens=tokens)
        record_property(boundary["after_log"], json.dumps(metrics, sort_keys=True))
        assert all(not value for key, value in metrics.items() if key != "displayed_tokens"), metrics
        assert tokens > 0
        for detail in boundary["gold"].get("narrative_required_details", []):
            assert detail in displayed
        for detail in boundary["gold"].get("narrative_must_not_present_as_current", []):
            assert detail not in service_state_display(harness[0], document)
        seen, previously_closed = aliases, closed
    assert len(token_counts) == len(scenario["boundaries"])
    assert any(marker == "SPLIT_HISTORIAN" for marker, _ in harness[4].calls)
    # Assistant-only closure evidence may be rejected by a deterministic
    # corroboration gate before verification. User-authored but semantically
    # unrelated evidence still requires the independent verifier's judgement.
    if any(boundary["gold"].get("non_openings")
           or any(label["role"] == "user" for label in boundary["gold"].get("non_closures", []))
           for boundary in scenario["boundaries"]):
        assert harness[4].rejected, "Negative proposals never reached independent verification."


def service_state_display(service, document):
    """Exclude the historical Ledger when judging present-tense narrative."""
    return service._extract_living_state(document)


@pytest.mark.parametrize("field, expected, actual", [
    ("missed_openings", ["needed"], []),
    ("false_openings", [], ["invented"]),
    ("wrong_closures", [], ["unsupported"]),
    ("missed_closures", ["finished"], []),
    ("stale_items", [], ["finished"]),
    ("lost_active_items", ["unresolved"], []),
])
def test_replay_metrics_distinguish_each_lifecycle_error(field, expected, actual):
    arguments = dict(expected_open=[], actual_open=[], expected_close=[], actual_close=[],
                     expected_active=[], actual_active=[], displayed_tokens=91)
    suffix = "open" if "opening" in field else "close" if "closure" in field else "active"
    arguments[f"expected_{suffix}"] = expected
    arguments[f"actual_{suffix}"] = actual
    metrics = _metrics(**arguments)
    assert len(metrics[field]) == 1
    assert all(not value for key, value in metrics.items()
               if key not in (field, "displayed_tokens"))
    assert metrics["displayed_tokens"] == 91


async def test_empty_state_negative_control_rejects_hypothetical_and_unaccepted_suggestion(tmp_path):
    original = copy.deepcopy(FIXTURE["scenarios"][0])
    scenario = {"id": "empty_control", "items": {}, "initial": {"active": [], "narrative": ""},
                "logs": [{"id": "log_001", "turns": original["logs"][0]["turns"][:2]}]}
    boundary = {"after_log": "log_001", "gold": {
        "open": [], "close": [], "active": [],
        "non_openings": original["boundaries"][0]["gold"]["non_openings"],
        "narrative_required_details": []}}
    scenario["boundaries"] = [boundary]
    harness = _setup(tmp_path, scenario)

    _, records = await _advance(harness, scenario, boundary)

    assert records == []
    assert len(harness[4].rejected) == 2


async def test_uncertain_verifier_verdict_does_not_open_a_thread(tmp_path):
    scenario = copy.deepcopy(FIXTURE["scenarios"][0])
    boundary = scenario["boundaries"][0]
    harness = _setup(tmp_path, scenario)
    harness[4].uncertain_aliases.add("send_handout")

    _, records = await _advance(harness, scenario, boundary)

    assert {_alias(record, scenario) for record in records} == {"letter_pages"}


async def test_source_collection_never_uses_other_member_legacy_or_unrelated_space(tmp_path):
    scenario = copy.deepcopy(FIXTURE["scenarios"][0])
    harness = _setup(tmp_path, scenario)
    decoy = {"id": "log_001", "turns": [{"timestamp": "2026-04-01T09:00:00+00:00",
             "role": "user", "text": "I'll do FORBIDDEN-FOREIGN-SOURCE work."}]}
    for space, member in ((SPACE, "member_two"), (SPACE, ""), ("unrelated", MEMBER)):
        _persist_log(harness[1], decoy, space=space, member=member)

    _, records = await _advance(harness, scenario, scenario["boundaries"][0])

    _check_sources(records, scenario)
    assert records
    for _, payload in harness[4].calls:
        assert "FORBIDDEN-FOREIGN-SOURCE" not in json.dumps(payload)


async def test_incidental_example_does_not_replace_purpose_relevant_state(tmp_path):
    scenario = copy.deepcopy(FIXTURE["scenarios"][0])
    boundary = scenario["boundaries"][0]
    harness = _setup(tmp_path, scenario)

    _, records = await _advance(harness, scenario, boundary)

    assert {_alias(record, scenario) for record in records} == {"send_handout", "letter_pages"}
    displayed = await harness[0].load_context_document(INSTANCE, SPACE, member_id=MEMBER)
    assert "The workshop teaches beginners how folds make paper structures stronger." in displayed
    for record in records:
        assert "orange ribbon" not in record["text"]
    # Dropping this unneeded detail earns no error and changes no lifecycle
    # label: cohesion is about preserving the work's intent and live threads.
    assert boundary["gold"]["incidental_details"]


async def test_display_cap_hides_overflow_without_closing_or_changing_stored_items(tmp_path):
    items, turns = {}, []
    for index in range(18):
        text = (f"I'll prepare COMMITMENT-{index:02d} using this checklist: "
                + "check each paper joint; " * 18).rstrip()
        alias = f"long_commitment_{index}"
        items[alias] = {"kind": "commitment", "opening": {
            "log": "log_001", "turn": index + 1, "role": "user", "quote": text}}
        turns.append({"timestamp": f"2026-04-13T09:{index:02d}:00+00:00", "role": "user", "text": text})
    boundary = {"after_log": "log_001", "gold": {
        "open": list(items), "close": [], "active": list(items), "narrative_required_details": []}}
    scenario = {"id": "display_overflow", "items": items,
                "initial": {"active": [], "narrative": ""},
                "logs": [{"id": "log_001", "turns": turns}], "boundaries": [boundary]}
    harness = _setup(tmp_path, scenario)

    document, records = await _advance(harness, scenario, boundary)
    living_state = importlib.import_module("kernos.kernel.living_state")
    metadata = living_state.decode_state(document)
    view = living_state.render_thread_view(metadata)

    assert len(view) <= 6000
    assert len(records) == len(items)
    assert all(record["status"] == "open" and not record.get("closing") for record in records)
    _check_sources(records, scenario)
    assert any(item["opening"]["quote"] not in view for item in items.values())
    assert "COMMITMENT-" in view
