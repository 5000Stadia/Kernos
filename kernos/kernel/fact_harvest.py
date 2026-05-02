"""Checkpointed Fact Harvest — boundary-driven durable truth extraction.

Runs at compaction and space-switch boundaries. Two LLM calls:

1. PRIMARY — facts + sensitivity. Produces durable knowledge entries with
   add/update/reinforce verdicts and per-fact sensitivity classification.
   If this fails, the whole harvest produced no entries; we log and return.

2. SECONDARY — stewardship tension + operational insight. Runs only after
   primary succeeds. Independent try/except so failures do NOT cascade back
   into the primary path. A broken insight call never costs us the facts.

Every run emits FACT_HARVEST_OUTCOME at INFO with adds/updates/reinforces
plus primary_ok/secondary_ok flags so the dedup-fallback failure mode is
visible in logs (and the runtime trace, via the caller's collector).
"""
import json
import logging

from kernos.utils import utc_now

logger = logging.getLogger(__name__)


_PRIMARY_SYSTEM_PROMPT = """\
You are maintaining a durable fact store about a user. Given the current \
active facts and a new conversation span, harvest durable truths worth \
remembering long-term. Reconcile against existing facts.

Return JSON exactly matching this schema:
{
  "add": [{"content": "...", "archetype": "identity|structural|habitual|contextual", "confidence": "stated|inferred|observed", "subject": "user", "sensitivity": "open|contextual|personal"}],
  "update": [{"id": "know_xxx", "new_content": "...", "reason": "..."}],
  "reinforce": [{"id": "know_xxx"}]
}

FACT rules:
- Only durable truths worth remembering — not transient conversation or tasks
- Skip facts already accurately in the current store
- If a new statement updates an existing fact, use "update" with its id
- Use the user's actual statements as ground truth
- Return empty arrays if nothing durable was said

VALUES — extract what this person holds important. Consider multiple \
evidence channels, not just what was most eloquently said:
- Declared (they said it matters)
- Enacted (they repeatedly choose, protect, or sacrifice for it)
- Aspirational-but-unstable (they want it but struggle)
- Points of persistent regret or unresolved conflict
Use archetype "identity" for core values, "structural" for priorities, \
"habitual" for patterns. Only extract with real evidence — a stated \
preference is not a core value.

SENSITIVITY — classify every new fact:
  "open"       — general knowledge, fine to share (hobbies, public info)
  "contextual" — usable in reasoning but don't surface casually (work details, plans)
  "personal"   — private to this member, do not disclose to others \
(health, finances, relationships, emotional states)
When unsure, classify as "personal". Err conservative."""


_SECONDARY_SYSTEM_PROMPT = """\
You just helped harvest durable facts from a conversation. Now look at the \
same material through two further lenses. One call, two outputs.

Return JSON exactly matching this schema:
{"stewardship": "<sentence or empty>", "operational_insight": "<sentence or empty>"}

STEWARDSHIP — is there a tension between what this person says matters \
and what they're actually doing?

Classify any tension found:
- understandable_lapse — exhaustion, constraint, bad week. Let it go.
- unresolved_tradeoff — competing priorities, no clear right answer.
- value_transition — they're changing what matters. Give room.
- repeated_self_betrayal — persistent pattern contradicting stated values.
- insufficient_evidence — not enough signal.

Only "repeated_self_betrayal" with strong evidence warrants a note. A \
trusted friend speaks up when: the downside is meaningful, the pattern is \
non-trivial, the concern is grounded in observed history, and silence would \
feel negligent.

If no tension worth mentioning: set "stewardship" to "".
If yes: one warm sentence. A thought, not a diagnosis.

OPERATIONAL INSIGHT — is there something concrete this agent could DO \
(build, automate, anticipate, pre-stage, remind, take off their plate) \
that would genuinely reduce friction for them?

This is not pattern-reporting. Do not surface observations without ideas. \
A vague "I noticed X" is not worth surfacing. Only generate an insight if \
you have a concrete, actionable proposal.

Examples that qualify:
- "They rebuild the same spreadsheet headers every Thursday — I could \
build a template that pre-fills them"
- "They always check three things before approving invoices — I could \
do that first pass"

If no concrete idea: set "operational_insight" to "".
If yes: one sentence describing what and why it would help."""


_PRIMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "add": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "archetype": {"type": "string"},
                    "confidence": {"type": "string"},
                    "subject": {"type": "string"},
                    "sensitivity": {
                        "type": "string",
                        "enum": ["open", "contextual", "personal"],
                    },
                },
                "required": [
                    "content", "archetype", "confidence", "subject", "sensitivity",
                ],
                "additionalProperties": False,
            },
        },
        "update": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "new_content": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "new_content", "reason"],
                "additionalProperties": False,
            },
        },
        "reinforce": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["add", "update", "reinforce"],
    "additionalProperties": False,
}


_SECONDARY_SCHEMA = {
    "type": "object",
    "properties": {
        "stewardship": {"type": "string"},
        "operational_insight": {"type": "string"},
    },
    "required": ["stewardship", "operational_insight"],
    "additionalProperties": False,
}


async def harvest_facts(
    reasoning_service,
    state_store,
    events,
    instance_id: str,
    space_id: str,
    conversation_text: str,
    data_dir: str = "./data",
    member_id: str = "",
) -> dict:
    """Run boundary-driven fact harvest. Returns outcome dict.

    Outcome dict:
        {"adds": int, "updates": int, "reinforces": int,
         "primary_ok": bool, "secondary_ok": bool,
         "stewardship": bool, "insight": bool}

    Failures in primary → returns with primary_ok=False, all counts 0.
    Failures in secondary → primary still applied, secondary_ok=False.
    """
    outcome = {
        "adds": 0, "updates": 0, "reinforces": 0,
        "primary_ok": False, "secondary_ok": False,
        "stewardship": False, "insight": False,
    }
    if not conversation_text.strip() or not reasoning_service:
        logger.info(
            "FACT_HARVEST_OUTCOME: instance=%s space=%s SKIPPED (empty input)",
            instance_id, space_id,
        )
        return outcome

    # Load current facts for reconciliation.
    all_facts = await state_store.query_knowledge(
        instance_id, subject="user", active_only=True, limit=200,
        member_id=member_id,
    )
    if all_facts:
        facts_text = "\n".join(
            f'- [{e.id}] "{e.content}" ({e.lifecycle_archetype})'
            for e in all_facts
        )
    else:
        facts_text = "(no existing facts)"

    user_content = (
        f"CURRENT FACTS:\n{facts_text}\n\n"
        f"CONVERSATION SPAN TO HARVEST:\n{conversation_text}"
    )

    # --- PRIMARY: facts + sensitivity ---
    parsed = await _call_primary(reasoning_service, user_content)
    if parsed is None:
        logger.warning(
            "FACT_HARVEST_OUTCOME: instance=%s space=%s PRIMARY_FAILED — no entries written",
            instance_id, space_id,
        )
        return outcome
    outcome["primary_ok"] = True
    outcome["adds"] = await _apply_adds(
        state_store, instance_id, member_id, parsed.get("add", []),
    )
    outcome["updates"] = await _apply_updates(
        state_store, instance_id, parsed.get("update", []),
    )
    outcome["reinforces"] = await _apply_reinforces(
        state_store, instance_id, parsed.get("reinforce", []),
    )

    # --- SECONDARY: stewardship + insight ---
    try:
        secondary = await _call_secondary(reasoning_service, user_content)
        if secondary is not None:
            outcome["secondary_ok"] = True
            stewardship_text = (secondary.get("stewardship") or "").strip()
            insight_text = (secondary.get("operational_insight") or "").strip()
            if stewardship_text:
                outcome["stewardship"] = await _emit_whisper(
                    state_store, instance_id, space_id,
                    stewardship_text, "STEWARDSHIP",
                    "compaction harvest tension detection",
                    member_id=member_id,
                )
            if insight_text:
                outcome["insight"] = await _emit_whisper(
                    state_store, instance_id, space_id,
                    insight_text, "OPERATIONAL_INSIGHT",
                    "compaction harvest — concrete actionable idea",
                    member_id=member_id,
                )
    except Exception as exc:
        # Never let secondary failure hide primary success.
        logger.warning(
            "FACT_HARVEST_SECONDARY_FAILED: instance=%s space=%s error=%s",
            instance_id, space_id, exc,
        )

    logger.info(
        "FACT_HARVEST_OUTCOME: instance=%s space=%s adds=%d updates=%d "
        "reinforces=%d primary_ok=%s secondary_ok=%s stewardship=%s insight=%s",
        instance_id, space_id,
        outcome["adds"], outcome["updates"], outcome["reinforces"],
        outcome["primary_ok"], outcome["secondary_ok"],
        outcome["stewardship"], outcome["insight"],
    )
    return outcome


# --- Internal calls ---


async def _call_primary(reasoning_service, user_content: str) -> dict | None:
    try:
        raw = await reasoning_service.complete_simple(
            system_prompt=_PRIMARY_SYSTEM_PROMPT,
            user_content=user_content,
            max_tokens=1024,
            prefer_cheap=True,
            output_schema=_PRIMARY_SCHEMA,
        )
        return json.loads(raw)
    except Exception as exc:
        logger.warning("FACT_HARVEST_PRIMARY_FAILED: error=%s", exc)
        return None


async def _call_secondary(reasoning_service, user_content: str) -> dict | None:
    try:
        raw = await reasoning_service.complete_simple(
            system_prompt=_SECONDARY_SYSTEM_PROMPT,
            user_content=user_content,
            max_tokens=300,
            prefer_cheap=True,
            output_schema=_SECONDARY_SCHEMA,
        )
        return json.loads(raw)
    except Exception as exc:
        logger.warning("FACT_HARVEST_SECONDARY_CALL_FAILED: error=%s", exc)
        return None


# --- Writers ---


async def _apply_adds(state_store, instance_id, member_id, items: list[dict]) -> int:
    import uuid
    from kernos.kernel.state import KnowledgeEntry
    count = 0
    for item in items:
        content = (item.get("content") or "").strip()
        if not content:
            continue
        entry = KnowledgeEntry(
            id=f"know_{int(uuid.uuid4().int)%10**16}_{uuid.uuid4().hex[:4]}",
            instance_id=instance_id,
            category="fact",
            subject=item.get("subject", "user"),
            content=content,
            confidence=item.get("confidence", "inferred"),
            source_event_id="",
            source_description="boundary_fact_harvest",
            created_at=utc_now(),
            last_referenced=utc_now(),
            tags=[],
            lifecycle_archetype=item.get("archetype", "structural"),
            valid_at=utc_now(),
            owner_member_id=member_id,
            sensitivity=item.get("sensitivity", "personal"),  # conservative default
        )
        await state_store.add_knowledge(entry)
        count += 1
        logger.info(
            "FACT_HARVEST_ADD: instance=%s sensitivity=%s content=%r",
            instance_id, entry.sensitivity, content[:80],
        )
    return count


async def _apply_updates(state_store, instance_id, items: list[dict]) -> int:
    count = 0
    for item in items:
        entry_id = (item.get("id") or "").strip()
        new_content = (item.get("new_content") or "").strip()
        if not entry_id or not new_content:
            continue
        await state_store.update_knowledge(
            instance_id, entry_id,
            {"content": new_content, "updated_at": utc_now()},
        )
        count += 1
        logger.info(
            "FACT_HARVEST_UPDATE: instance=%s id=%s content=%r",
            instance_id, entry_id, new_content[:80],
        )
    return count


async def _apply_reinforces(state_store, instance_id, items: list[dict]) -> int:
    count = 0
    for item in items:
        entry_id = (item.get("id") or "").strip()
        if not entry_id:
            continue
        await state_store.update_knowledge(
            instance_id, entry_id, {"last_referenced": utc_now()},
        )
        count += 1
        logger.info("FACT_HARVEST_REINFORCE: instance=%s id=%s", instance_id, entry_id)
    return count


async def _emit_whisper(
    state_store, instance_id: str, space_id: str,
    text: str, whisper_type: str, evidence: str,
    member_id: str = "",
) -> bool:
    """Create a stewardship/insight whisper from a harvest run.

    Passes owner_member_id through so the disclosure-gate-aware whisper
    surfacing can avoid leaking a member's stewardship to another member.
    The previous version of this function had the wrong field names
    (`whisper_type` / `supporting_evidence=str`) and silently failed on
    every call — so stewardship whispers weren't actually persisting.
    """
    try:
        from kernos.kernel.awareness import Whisper, generate_whisper_id
        whisper = Whisper(
            whisper_id=generate_whisper_id(),
            insight_text=text,
            delivery_class="ambient",
            source_space_id=space_id,
            target_space_id="",
            supporting_evidence=[evidence] if evidence else [],
            reasoning_trace=f"{whisper_type}: {evidence}",
            knowledge_entry_id="",
            foresight_signal=whisper_type,
            created_at=utc_now(),
            owner_member_id=member_id,
        )
        await state_store.save_whisper(instance_id, whisper)
        logger.info("%s_WHISPER: instance=%s member=%s text=%s",
                    whisper_type, instance_id, member_id or "(instance)", text[:80])
        return True
    except Exception as exc:
        logger.warning("%s_WHISPER_FAILED: %s", whisper_type, exc)
        return False


# --- Legacy compaction path (kept for backward compat) ---


async def process_harvest_results(
    harvest: list[dict],
    instance_id: str,
    space_id: str,
    state_store,
    events,
    member_id: str = "",
) -> int:
    """Process fact harvest results parsed from compaction output.

    LEGACY path — compaction emits a FACT_HARVEST: section with raw ADD/UPDATE/
    REINFORCE. This function applies them without sensitivity classification.
    The new primary path is harvest_facts() above; keep this for tests and
    the backfill case where compaction-parsed harvest still lands.
    """
    import uuid
    from kernos.kernel.state import KnowledgeEntry

    changes = 0
    for item in harvest:
        action = item.get("action", "")
        try:
            if action == "add":
                content = item.get("content", "").strip()
                if not content:
                    continue
                entry = KnowledgeEntry(
                    id=f"know_{int(uuid.uuid4().int)%10**16}_{uuid.uuid4().hex[:4]}",
                    instance_id=instance_id,
                    category="fact",
                    subject="user",
                    content=content,
                    confidence="inferred",
                    source_event_id="",
                    source_description="compaction_harvest",
                    created_at=utc_now(),
                    last_referenced=utc_now(),
                    tags=[],
                    lifecycle_archetype="structural",
                    context_space=space_id,
                    valid_at=utc_now(),
                    owner_member_id=member_id,
                )
                await state_store.add_knowledge(entry)
                changes += 1
                logger.info(
                    "FACT_HARVEST_ADD: instance=%s sensitivity=%s content=%r",
                    instance_id, entry.sensitivity, content[:80],
                )
            elif action == "update":
                entry_id = item.get("id", "").strip()
                content = item.get("content", "").strip()
                if entry_id and content:
                    await state_store.update_knowledge(
                        instance_id, entry_id,
                        {"content": content, "updated_at": utc_now()},
                    )
                    changes += 1
                    logger.info(
                        "FACT_HARVEST_UPDATE: instance=%s id=%s content=%r",
                        instance_id, entry_id, content[:80],
                    )
            elif action == "reinforce":
                entry_id = item.get("id", "").strip()
                if entry_id:
                    _existing = await state_store.get_knowledge_entry(instance_id, entry_id)
                    _ss = (_existing.storage_strength + 1.0) if _existing else 2.0
                    _rc = (_existing.reinforcement_count + 1) if _existing else 2
                    await state_store.update_knowledge(
                        instance_id, entry_id,
                        {
                            "last_referenced": utc_now(),
                            "last_reinforced_at": utc_now(),
                            "reinforcement_count": _rc,
                            "storage_strength": _ss,
                        },
                    )
                    logger.info(
                        "FACT_HARVEST_REINFORCE: instance=%s id=%s "
                        "storage_strength=%.1f reinforcement_count=%d",
                        instance_id, entry_id, _ss, _rc,
                    )
        except Exception as exc:
            logger.warning("FACT_HARVEST_ITEM: %s failed: %s", action, exc)

    if changes:
        logger.info(
            "COMPACTION_HARVEST_COMPLETE: instance=%s space=%s changes=%d",
            instance_id, space_id, changes,
        )
    return changes
