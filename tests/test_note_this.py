"""Pin tests for RESPONSE-FIDELITY-V1 Batch 1.2: note_this primitive.

Substrate-fidelity tests verifying:
  * fact path writes a KnowledgeEntry with provenance fields.
  * preference path writes a Preference with provenance.
  * rule path writes a CovenantRule with provenance.
  * No-op detection on identical fact (same instance_id|subject|content).
  * Returned ActionStateRecord matches the actual write.
  * Validation errors return failed ActionStateRecord without writing.

Resolves G.1 from the Phase 1 audit: the "I'll remember" pattern now
has a synchronous receipt-backed write path. Compaction-time
fact_harvest continues to run async; note_this adds the synchronous
receipt path that didn't exist before.
"""
from __future__ import annotations

import pytest

from kernos.kernel.note_this import handle_note_this, NOTE_THIS_TOOL
from kernos.kernel.state import (
    CovenantRule,
    KnowledgeEntry,
    Preference,
    StateStore,
)


# ---------------------------------------------------------------------------
# Fake StateStore — implements only the methods note_this needs.
# ---------------------------------------------------------------------------


class _FakeStateStore:
    """Minimal StateStore stub. Records each write so tests can
    inspect what landed."""

    def __init__(self):
        self.knowledge: dict[str, KnowledgeEntry] = {}
        self.knowledge_by_hash: dict[str, KnowledgeEntry] = {}
        self.preferences: dict[str, Preference] = {}
        self.rules: dict[str, CovenantRule] = {}
        self._raise_on_add: dict[str, Exception] = {}

    async def add_knowledge(self, entry: KnowledgeEntry) -> None:
        if "knowledge" in self._raise_on_add:
            raise self._raise_on_add["knowledge"]
        self.knowledge[entry.id] = entry
        if entry.content_hash:
            self.knowledge_by_hash[entry.content_hash] = entry

    async def get_knowledge_by_hash(
        self, instance_id: str, content_hash: str
    ) -> KnowledgeEntry | None:
        return self.knowledge_by_hash.get(content_hash)

    async def add_preference(self, pref: Preference) -> None:
        if "preference" in self._raise_on_add:
            raise self._raise_on_add["preference"]
        self.preferences[pref.id] = pref

    async def add_contract_rule(self, rule: CovenantRule) -> None:
        if "rule" in self._raise_on_add:
            raise self._raise_on_add["rule"]
        self.rules[rule.id] = rule


# ---------------------------------------------------------------------------
# Tool schema sanity
# ---------------------------------------------------------------------------


def test_tool_schema_shape():
    """Schema has the right surface: name, description, kind/content/
    subject/category in input_schema with kind+content required."""
    assert NOTE_THIS_TOOL["name"] == "note_this"
    schema = NOTE_THIS_TOOL["input_schema"]
    assert "kind" in schema["properties"]
    assert "content" in schema["properties"]
    assert "subject" in schema["properties"]
    assert "category" in schema["properties"]
    assert set(schema["required"]) == {"kind", "content"}
    # kind enum: fact / preference / rule
    assert set(schema["properties"]["kind"]["enum"]) == {
        "fact", "preference", "rule",
    }


# ---------------------------------------------------------------------------
# Fact path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fact_path_writes_knowledge_entry():
    state = _FakeStateStore()
    summary, record = await handle_note_this(
        state=state,  # type: ignore[arg-type]
        instance_id="inst-1",
        member_id="mem-1",
        active_space_id="space-1",
        turn_id="turn-1",
        kind="fact",
        content="The user works in Pacific time.",
        subject="work_schedule",
    )
    assert "Noted (fact)" in summary
    assert record.execution_state == "completed"
    assert record.surface == "memory"
    assert record.operation == "note_this"
    assert record.operation_class == "mutate"
    assert len(record.affected_objects) == 1
    entry_id = record.affected_objects[0]
    assert entry_id.startswith("know_")
    # Verify the actual write happened at substrate.
    entry = state.knowledge[entry_id]
    assert entry.subject == "work_schedule"
    assert entry.content == "The user works in Pacific time."
    assert entry.confidence == "stated"
    assert entry.source_event_id  # provenance attached
    assert entry.source_description == "user-noted via note_this"
    assert entry.owner_member_id == "mem-1"
    assert "user_noted" in entry.tags


@pytest.mark.asyncio
async def test_fact_embed_on_write_stores_embedding():
    # ② embed-on-write: note_this honors the "computed on write" contract so
    # the fact is vector-recallable, not only via the lexical fallback.
    state = _FakeStateStore()

    class _Embed:
        async def embed(self, text):
            return [0.1, 0.2, 0.3]

    class _Store:
        def __init__(self):
            self.saved = {}
        async def save(self, instance_id, entry_id, vec):
            self.saved[(instance_id, entry_id)] = vec

    store = _Store()
    _, record = await handle_note_this(
        state=state, instance_id="inst-1", member_id="mem-1",
        active_space_id="space-1", turn_id="turn-1", kind="fact",
        content="My favorite test color is cerulean.", subject="favorite_color",
        embedding_service=_Embed(), embedding_store=store,
    )
    entry_id = record.affected_objects[0]
    assert ("inst-1", entry_id) in store.saved
    assert store.saved[("inst-1", entry_id)] == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_fact_write_succeeds_without_embedder():
    # Best-effort: no embedder wired (VOYAGE_API_KEY unset) → write still
    # succeeds; the lexical fallback covers recall.
    state = _FakeStateStore()
    _, record = await handle_note_this(
        state=state, instance_id="inst-1", member_id="mem-1",
        active_space_id="space-1", turn_id="turn-1", kind="fact",
        content="A fact with no embedder.", subject="topic",
    )
    assert record.execution_state == "completed"


@pytest.mark.asyncio
async def test_fact_no_op_detection_on_identical_content():
    """Same (instance_id, subject, content) → no second write; record
    reflects no-op + points at existing entry."""
    state = _FakeStateStore()
    # First write.
    _, record_1 = await handle_note_this(
        state=state,  # type: ignore[arg-type]
        instance_id="inst-1",
        member_id="mem-1",
        active_space_id="space-1",
        turn_id="turn-1",
        kind="fact",
        content="The dog's name is Sasha.",
        subject="pets",
    )
    assert len(state.knowledge) == 1

    # Second write with identical content → no-op.
    summary_2, record_2 = await handle_note_this(
        state=state,  # type: ignore[arg-type]
        instance_id="inst-1",
        member_id="mem-1",
        active_space_id="space-1",
        turn_id="turn-2",
        kind="fact",
        content="The dog's name is Sasha.",
        subject="pets",
    )
    # No new entry written.
    assert len(state.knowledge) == 1
    # But the record still claims completed; affected_objects points
    # at the existing entry (same id as the first write).
    assert record_2.execution_state == "completed"
    assert record_2.affected_objects == record_1.affected_objects
    assert "no-op" in summary_2.lower() or "Already" in summary_2


# ---------------------------------------------------------------------------
# Preference path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preference_path_writes_preference():
    state = _FakeStateStore()
    summary, record = await handle_note_this(
        state=state,  # type: ignore[arg-type]
        instance_id="inst-1",
        member_id="mem-1",
        active_space_id="space-1",
        turn_id="turn-1",
        kind="preference",
        content="Keep responses short unless I ask for depth.",
        subject="reply_length",
    )
    assert "Noted (preference)" in summary
    assert record.execution_state == "completed"
    pref_id = record.affected_objects[0]
    assert pref_id.startswith("pref_")
    pref = state.preferences[pref_id]
    assert pref.intent == "Keep responses short unless I ask for depth."
    assert pref.subject == "reply_length"
    assert pref.source_turn_id == "turn-1"
    assert pref.status == "active"


# ---------------------------------------------------------------------------
# Rule path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rule_path_writes_covenant_rule():
    state = _FakeStateStore()
    summary, record = await handle_note_this(
        state=state,  # type: ignore[arg-type]
        instance_id="inst-1",
        member_id="mem-1",
        active_space_id="space-1",
        turn_id="turn-1",
        kind="rule",
        content="Don't send messages to anyone after 10pm.",
        category="must_not",
    )
    assert "Noted (rule)" in summary
    assert record.execution_state == "completed"
    # Rule mutations are higher-stakes per the schema's risk_level field.
    assert record.risk_level == "medium"
    rule_id = record.affected_objects[0]
    assert rule_id.startswith("rule_")
    rule = state.rules[rule_id]
    assert rule.description == "Don't send messages to anyone after 10pm."
    assert rule.rule_type == "must_not"
    assert rule.source == "user_stated"
    assert rule.active is True


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_kind_returns_failed_record_without_writing():
    state = _FakeStateStore()
    summary, record = await handle_note_this(
        state=state,  # type: ignore[arg-type]
        instance_id="inst-1",
        member_id="mem-1",
        active_space_id="space-1",
        turn_id="turn-1",
        kind="not_a_kind",
        content="x",
    )
    assert "Error" in summary
    assert record.execution_state == "failed"
    # No write happened.
    assert len(state.knowledge) == 0
    assert len(state.preferences) == 0
    assert len(state.rules) == 0


@pytest.mark.asyncio
async def test_empty_content_returns_failed_record():
    state = _FakeStateStore()
    summary, record = await handle_note_this(
        state=state,  # type: ignore[arg-type]
        instance_id="inst-1",
        member_id="mem-1",
        active_space_id="space-1",
        turn_id="turn-1",
        kind="fact",
        content="",
        subject="x",
    )
    assert "Error" in summary
    assert record.execution_state == "failed"


@pytest.mark.asyncio
async def test_fact_without_subject_returns_failed_record():
    """Fact requires subject for retrieval + supersession."""
    state = _FakeStateStore()
    summary, record = await handle_note_this(
        state=state,  # type: ignore[arg-type]
        instance_id="inst-1",
        member_id="mem-1",
        active_space_id="space-1",
        turn_id="turn-1",
        kind="fact",
        content="something",
        subject="",
    )
    assert "subject" in summary.lower()
    assert record.execution_state == "failed"


@pytest.mark.asyncio
async def test_preference_without_subject_returns_failed_record():
    """Preference requires subject too."""
    state = _FakeStateStore()
    summary, record = await handle_note_this(
        state=state,  # type: ignore[arg-type]
        instance_id="inst-1",
        member_id="mem-1",
        active_space_id="space-1",
        turn_id="turn-1",
        kind="preference",
        content="something",
        subject="",
    )
    assert "subject" in summary.lower()
    assert record.execution_state == "failed"


@pytest.mark.asyncio
async def test_rule_without_subject_is_allowed():
    """Rules don't use the subject field directly (covenants have
    their own structure); subject is optional for kind=rule."""
    state = _FakeStateStore()
    summary, record = await handle_note_this(
        state=state,  # type: ignore[arg-type]
        instance_id="inst-1",
        member_id="mem-1",
        active_space_id="space-1",
        turn_id="turn-1",
        kind="rule",
        content="Don't ping me on weekends.",
        category="must_not",
    )
    assert record.execution_state == "completed"


# ---------------------------------------------------------------------------
# Substrate write failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_substrate_write_failure_returns_failed_record():
    """If the state store raises during the write, return a failed
    ActionStateRecord rather than letting the exception bubble."""
    state = _FakeStateStore()
    state._raise_on_add["knowledge"] = RuntimeError("disk full")

    summary, record = await handle_note_this(
        state=state,  # type: ignore[arg-type]
        instance_id="inst-1",
        member_id="mem-1",
        active_space_id="space-1",
        turn_id="turn-1",
        kind="fact",
        content="something",
        subject="x",
    )
    assert "Error" in summary
    assert "disk full" in summary
    assert record.execution_state == "failed"


# ---------------------------------------------------------------------------
# ActionStateRecord shape sanity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_carries_substrate_authoritative_summary():
    """The user_visible_summary on the record is substrate-authoritative
    (what was written, not what the renderer might paraphrase). The
    renderer in Batch 2 onward consumes this for receipt-grounded
    rendering."""
    state = _FakeStateStore()
    _, record = await handle_note_this(
        state=state,  # type: ignore[arg-type]
        instance_id="inst-1",
        member_id="mem-1",
        active_space_id="space-1",
        turn_id="turn-1",
        kind="fact",
        content="Pacific time",
        subject="work_tz",
    )
    assert "fact" in record.user_visible_summary.lower()
    assert "work_tz" in record.user_visible_summary
    assert record.receipt_refs  # source event id attached
