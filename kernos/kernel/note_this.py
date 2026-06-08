"""note_this primitive — synchronous receipt-backed memory path.

RESPONSE-FIDELITY-V1 Batch 1.2 (2026-05-08).

Resolves G.1 from the Phase 1 audit: the "I'll remember" pattern
previously had no synchronous write path. Compaction-time fact_harvest
runs asynchronously after the turn ends; the agent's prose claim
"Got it, I'll remember that" had no substrate receipt to anchor it.

This primitive gives the agent a synchronous, receipt-backed surface
to record durable content. Single parametric tool with ``kind``
selecting the durable-object type:

    * ``kind="fact"`` → KnowledgeEntry (factual content/context)
    * ``kind="preference"`` → Preference (what the user wants)
    * ``kind="rule"`` → CovenantRule (genuinely behavioral rule;
      most rules still flow through the automatic-extractor path
      from conversational prose — note_this is the explicit
      synchronous-receipt path when the agent commits explicitly)

Returns: a string for the model's tool result + appends an
ActionStateRecord to the per-turn collector for the integration
runner to fold into AuditTrace at finalize time.

Async compaction-time harvesting continues in parallel — note_this
adds the synchronous-receipt path that didn't exist before; it
doesn't displace the async-opportunistic path.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any

from kernos.kernel.integration import ActionStateRecord
from kernos.kernel.state import (
    CovenantRule,
    KnowledgeEntry,
    Preference,
    StateStore,
    _enforcement_tier_for,
    classify_covenant_tier,
)
from kernos.utils import utc_now

logger = logging.getLogger(__name__)


NOTE_THIS_TOOL: dict = {
    "name": "note_this",
    "description": (
        "Record something the user wants preserved durably. Use this "
        "when the user says 'remember this', 'note that', 'add to my "
        "preferences', 'make this a rule' — when your conversational "
        "claim 'I'll remember' should anchor a real substrate write "
        "rather than rely on async compaction harvesting later. "
        "Synchronous receipt-backed write; the next turn's agent will "
        "see this in conversation history.\n\n"
        "Pick ``kind`` carefully:\n"
        "  - ``fact`` for factual content/context (\"I work in Pacific "
        "time\", \"my dog's name is Sasha\").\n"
        "  - ``preference`` for what the user wants Kernos to do "
        "(\"notify me 30 min before meetings\", \"keep responses "
        "short\").\n"
        "  - ``rule`` only when content is genuinely a behavioral "
        "rule (\"don't send messages after 10pm\"). Most behavioral "
        "rules still flow through the automatic-extractor path from "
        "your prose; use ``kind=rule`` only when you're explicitly "
        "committing to a substrate-level rule. Don't co-opt covenants "
        "for general fact-noting."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["fact", "preference", "rule"],
                "description": (
                    "Which durable-object type to write. fact → "
                    "KnowledgeEntry; preference → Preference; rule → "
                    "CovenantRule."
                ),
            },
            "content": {
                "type": "string",
                "description": (
                    "The text to record. For fact: the factual claim. "
                    "For preference: what the user wants. For rule: "
                    "the behavioral rule the agent should honor."
                ),
            },
            "subject": {
                "type": "string",
                "description": (
                    "Required for ``fact`` and ``preference``: a short "
                    "noun phrase identifying what this is about (e.g. "
                    "'work_schedule', 'food_preferences', 'reply_"
                    "length'). Used for retrieval and supersession. "
                    "Optional for ``rule`` (covenants don't use this "
                    "field directly)."
                ),
            },
            "category": {
                "type": "string",
                "description": (
                    "Optional category hint. For fact: 'fact' / "
                    "'entity' / 'pattern' (defaults to 'fact'). For "
                    "preference: 'notification' / 'behavior' / "
                    "'format' / 'access' / 'schedule' (defaults to "
                    "'behavior'). For rule: 'must' / 'must_not' / "
                    "'preference' / 'escalation' (defaults to 'must')."
                ),
            },
        },
        "required": ["kind", "content"],
        "additionalProperties": False,
    },
}


def _content_hash(instance_id: str, subject: str, content: str) -> str:
    """SHA256[:16] of (instance_id|subject|content) — matches the
    KnowledgeEntry dedup convention from state.KnowledgeEntry."""
    raw = f"{instance_id}|{subject}|{content}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _new_action_id() -> str:
    return f"act_{uuid.uuid4().hex[:12]}"


def _new_event_id() -> str:
    return f"evt_{uuid.uuid4().hex[:12]}"


def _new_object_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


async def handle_note_this(
    *,
    state: StateStore,
    instance_id: str,
    member_id: str,
    active_space_id: str,
    turn_id: str,
    kind: str,
    content: str,
    subject: str = "",
    category: str = "",
    embedding_service: Any = None,
    embedding_store: Any = None,
) -> tuple[str, ActionStateRecord]:
    """Dispatch note_this. Returns (user_visible_summary,
    ActionStateRecord). The summary string goes back to the agent as
    the tool result; the ActionStateRecord is appended to the per-turn
    collector by the caller (reasoning.execute_tool).

    Resolves to one of three durable-object writes:
      * fact → state.add_knowledge(KnowledgeEntry(...))
      * preference → state.add_preference(Preference(...))
      * rule → state.add_contract_rule(CovenantRule(...))

    No-op detection (per spec): when ``kind=fact`` and the same
    (instance_id, subject, content) already has an active KnowledgeEntry,
    no new write happens. Returns the existing entry's ActionStateRecord
    with execution_state=completed but the action_id reflects no-op.
    Other kinds rely on each subsystem's existing dedup (covenant
    creation has validate_covenant_set; preferences track supersedes).
    """
    if kind not in ("fact", "preference", "rule"):
        return (
            f"Error: kind must be one of fact/preference/rule (got {kind!r}).",
            _build_error_record(kind, "invalid_kind"),
        )
    if not content or not content.strip():
        return (
            "Error: content is required.",
            _build_error_record(kind, "empty_content"),
        )

    if kind in ("fact", "preference") and not subject.strip():
        return (
            f"Error: subject is required for kind={kind}.",
            _build_error_record(kind, "missing_subject"),
        )

    action_id = _new_action_id()
    source_event_id = _new_event_id()
    now = utc_now()

    if kind == "fact":
        return await _write_fact(
            state=state,
            instance_id=instance_id,
            member_id=member_id,
            action_id=action_id,
            source_event_id=source_event_id,
            now=now,
            content=content.strip(),
            subject=subject.strip(),
            category=(category or "fact").strip(),
            active_space_id=active_space_id,
            embedding_service=embedding_service,
            embedding_store=embedding_store,
        )
    if kind == "preference":
        return await _write_preference(
            state=state,
            instance_id=instance_id,
            action_id=action_id,
            source_event_id=source_event_id,
            now=now,
            content=content.strip(),
            subject=subject.strip(),
            category=(category or "behavior").strip(),
            turn_id=turn_id,
            active_space_id=active_space_id,
        )
    # kind == "rule"
    return await _write_rule(
        state=state,
        instance_id=instance_id,
        member_id=member_id,
        action_id=action_id,
        source_event_id=source_event_id,
        now=now,
        content=content.strip(),
        category=(category or "must").strip(),
        active_space_id=active_space_id,
    )


def _build_error_record(kind: str, reason: str) -> ActionStateRecord:
    """Build an ActionStateRecord for a validation-failure case."""
    return ActionStateRecord(
        action_id=_new_action_id(),
        surface="memory",
        operation="note_this",
        operation_class="mutate",
        authorization_state="not_required",
        execution_state="failed",
        user_visible_summary=f"note_this validation failed: {reason}",
        risk_level="low",
    )


async def _write_fact(
    *,
    state: StateStore,
    instance_id: str,
    member_id: str,
    action_id: str,
    source_event_id: str,
    now: str,
    content: str,
    subject: str,
    category: str,
    active_space_id: str,
    embedding_service: Any = None,
    embedding_store: Any = None,
) -> tuple[str, ActionStateRecord]:
    chash = _content_hash(instance_id, subject, content)
    existing = await state.get_knowledge_by_hash(instance_id, chash)
    if existing is not None and existing.active:
        # No-op: identical content already recorded.
        record = ActionStateRecord(
            action_id=action_id,
            surface="memory",
            operation="note_this",
            operation_class="mutate",
            authorization_state="not_required",
            execution_state="completed",
            receipt_refs=(),
            affected_objects=(existing.id,),
            user_visible_summary=(
                f"Already noted: {existing.subject} (no-op; existing "
                f"entry {existing.id})"
            ),
            risk_level="low",
        )
        summary = (
            f"Already in memory: {existing.subject} → {content[:80]} "
            f"(no-op; existing knowledge entry {existing.id})"
        )
        return summary, record

    entry_id = _new_object_id("know")
    entry = KnowledgeEntry(
        id=entry_id,
        instance_id=instance_id,
        category=category,
        subject=subject,
        content=content,
        confidence="stated",
        source_event_id=source_event_id,
        source_description="user-noted via note_this",
        created_at=now,
        last_referenced=now,
        tags=["user_noted"],
        active=True,
        content_hash=chash,
        owner_member_id=member_id,
        sensitivity="open",
        context_space=active_space_id,
        last_reinforced_at=now,
    )
    try:
        await state.add_knowledge(entry)
    except Exception as exc:
        logger.warning("note_this fact write failed: %s", exc)
        return (
            f"Error: could not record knowledge entry: {exc}",
            ActionStateRecord(
                action_id=action_id,
                surface="memory",
                operation="note_this",
                operation_class="mutate",
                authorization_state="not_required",
                execution_state="failed",
                user_visible_summary=f"note_this fact write failed: {exc}",
                risk_level="low",
            ),
        )

    # EMBED-ON-WRITE (②, 2026-06-08): the "embeddings computed on write"
    # contract (embeddings.py) is honored by the projector path, but note_this
    # writes KnowledgeEntries directly via add_knowledge, so noted facts had NO
    # embedding and were invisible to vector recall ("cerulean" returned
    # knowledge=0). Compute + store the embedding now so a freshly-noted fact is
    # semantically recallable — not only via the lexical fallback. Best-effort:
    # if no embedder is wired (e.g. VOYAGE_API_KEY unset) or it errors, the
    # lexical fallback still covers recall, so we never fail the write.
    if embedding_service is not None and embedding_store is not None:
        try:
            vec = await embedding_service.embed(content)
            if vec:
                await embedding_store.save(instance_id, entry_id, vec)
        except Exception as exc:  # pragma: no cover - best-effort
            logger.debug(
                "note_this embed-on-write failed for %s: %s", entry_id, exc
            )

    record = ActionStateRecord(
        action_id=action_id,
        surface="memory",
        operation="note_this",
        operation_class="mutate",
        authorization_state="not_required",
        execution_state="completed",
        receipt_refs=(source_event_id,),
        affected_objects=(entry_id,),
        user_visible_summary=(
            f"Noted as fact: {subject} → {content[:80]}"
        ),
        risk_level="low",
    )
    summary = f"Noted (fact). subject={subject} id={entry_id}"
    return summary, record


async def _write_preference(
    *,
    state: StateStore,
    instance_id: str,
    action_id: str,
    source_event_id: str,
    now: str,
    content: str,
    subject: str,
    category: str,
    turn_id: str,
    active_space_id: str,
) -> tuple[str, ActionStateRecord]:
    pref_id = _new_object_id("pref")
    pref = Preference(
        id=pref_id,
        instance_id=instance_id,
        intent=content,
        category=category,
        subject=subject,
        action="prefer",
        scope="global" if not active_space_id else active_space_id,
        context_space=active_space_id,
        status="active",
        created_at=now,
        updated_at=now,
        source_turn_id=turn_id,
    )
    try:
        await state.add_preference(pref)
    except Exception as exc:
        logger.warning("note_this preference write failed: %s", exc)
        return (
            f"Error: could not record preference: {exc}",
            ActionStateRecord(
                action_id=action_id,
                surface="memory",
                operation="note_this",
                operation_class="mutate",
                authorization_state="not_required",
                execution_state="failed",
                user_visible_summary=f"note_this preference write failed: {exc}",
                risk_level="low",
            ),
        )
    record = ActionStateRecord(
        action_id=action_id,
        surface="memory",
        operation="note_this",
        operation_class="mutate",
        authorization_state="not_required",
        execution_state="completed",
        receipt_refs=(source_event_id,),
        affected_objects=(pref_id,),
        user_visible_summary=(
            f"Noted as preference: {subject} → {content[:80]}"
        ),
        risk_level="low",
    )
    summary = f"Noted (preference). subject={subject} id={pref_id}"
    return summary, record


async def _write_rule(
    *,
    state: StateStore,
    instance_id: str,
    member_id: str,
    action_id: str,
    source_event_id: str,
    now: str,
    content: str,
    category: str,
    active_space_id: str,
) -> tuple[str, ActionStateRecord]:
    if category not in ("must", "must_not", "preference", "escalation"):
        category = "must"
    rule_id = _new_object_id("rule")
    # Codex review fold (2026-05-08): use the canonical tier/enforcement
    # classifiers from state.py so note_this(rule) doesn't diverge from
    # contract_parser / manage_covenants defaults. Previously hardcoded
    # tier="situational" + dataclass-default enforcement_tier="confirm",
    # which mis-classified must_not + escalation (should be pinned) and
    # preference (should be silent).
    rule = CovenantRule(
        id=rule_id,
        instance_id=instance_id,
        capability="general",
        rule_type=category,
        description=content,
        active=True,
        source="user_stated",
        source_event_id=source_event_id,
        created_at=now,
        updated_at=now,
        context_space=active_space_id or None,
        layer="practice",
        tier=classify_covenant_tier(category, "user_stated"),
        enforcement_tier=_enforcement_tier_for(category),
        member_id=member_id,
    )
    try:
        await state.add_contract_rule(rule)
    except Exception as exc:
        logger.warning("note_this rule write failed: %s", exc)
        return (
            f"Error: could not record covenant rule: {exc}",
            ActionStateRecord(
                action_id=action_id,
                surface="memory",
                operation="note_this",
                operation_class="mutate",
                authorization_state="not_required",
                execution_state="failed",
                user_visible_summary=f"note_this rule write failed: {exc}",
                risk_level="medium",  # rule mutations are higher-stakes
            ),
        )
    record = ActionStateRecord(
        action_id=action_id,
        surface="memory",
        operation="note_this",
        operation_class="mutate",
        authorization_state="not_required",
        execution_state="completed",
        receipt_refs=(source_event_id,),
        affected_objects=(rule_id,),
        user_visible_summary=(
            f"Noted as rule ({category}): {content[:80]}"
        ),
        risk_level="medium",
    )
    summary = f"Noted (rule). type={category} id={rule_id}"
    return summary, record


__all__ = ["NOTE_THIS_TOOL", "handle_note_this"]
