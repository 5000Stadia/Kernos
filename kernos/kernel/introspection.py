"""State Introspection — unified views of what Kernos believes is true.

Two explicitly separate surfaces:
- User truth view: preferences, triggers, covenants, key facts, capabilities
- Operator state view: adds degraded services, legacy artifacts, reconciliation health
"""
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def build_user_truth_view(
    instance_id: str,
    state: Any,              # StateStore
    trigger_store: Any,      # TriggerStore | None
    registry: Any = None,    # CapabilityRegistry | None
    active_space_id: str = "",
) -> str:
    """Build the user-facing truth view.

    Answers: "What preferences are active? What's set up for me?"
    Concise, preference-first, no diagnostic clutter.
    """
    sections: list[str] = []

    # --- Active Preferences (headline) ---
    try:
        prefs = await state.query_preferences(instance_id, active_only=True)
        if prefs:
            lines = ["## Active Preferences"]
            for p in prefs:
                param_str = ""
                if p.parameters:
                    param_str = " — " + ", ".join(
                        f"{k}: {v}" for k, v in p.parameters.items()
                    )
                lines.append(f'- "{p.intent}"{param_str}')
                scope_note = f" (space: {p.context_space})" if p.scope != "global" else ""
                lines.append(f"  [{p.category}/{p.action}]{scope_note}")
                # Show linked derived objects
                if p.derived_trigger_ids:
                    for tid in p.derived_trigger_ids:
                        trigger_info = await _get_trigger_summary(trigger_store, instance_id, tid)
                        lines.append(f"  → Trigger: {trigger_info}")
                if p.derived_covenant_ids:
                    for cid in p.derived_covenant_ids:
                        lines.append(f"  → Covenant: {cid}")
            sections.append("\n".join(lines))
    except Exception as exc:
        logger.warning("Introspection: preferences query failed: %s", exc)

    # --- Active Triggers ---
    try:
        if trigger_store:
            triggers = await trigger_store.list_active(instance_id)
            linked = [t for t in triggers if t.source_preference_id]
            unlinked = [t for t in triggers if not t.source_preference_id]
            if unlinked:
                lines = ["## Active Triggers (standalone)"]
                for t in unlinked:
                    next_fire = f", next: {t.next_fire_at[:16]}" if t.next_fire_at else ""
                    lines.append(f"- {t.trigger_id}: \"{t.action_description}\""
                                 f" ({t.condition_type}{next_fire})")
                sections.append("\n".join(lines))
    except Exception as exc:
        logger.warning("Introspection: triggers query failed: %s", exc)

    # --- Active Covenants ---
    try:
        rules = await state.get_contract_rules(instance_id, active_only=True)
        if rules:
            lines = ["## Active Rules"]
            for r in rules:
                source_tag = f" [{r.source}]" if r.source != "default" else " [default]"
                linked_tag = f" (from pref {r.source_preference_id})" if getattr(r, "source_preference_id", "") else ""
                lines.append(f"- {r.rule_type}: {r.description}{source_tag}{linked_tag}")
            sections.append("\n".join(lines))
    except Exception as exc:
        logger.warning("Introspection: covenants query failed: %s", exc)

    # --- Key Facts (truth-oriented: high confidence, stated) ---
    try:
        knowledge = await state.query_knowledge(
            instance_id, active_only=True, limit=50,
        )
        # Filter to high-confidence, non-preference entries
        key_facts = [
            k for k in knowledge
            if k.confidence == "stated"
            and k.category in ("entity", "fact", "pattern")
            and k.lifecycle_archetype in ("identity", "structural", "habitual")
        ]
        if key_facts:
            lines = ["## Key Facts"]
            for f in key_facts[:15]:  # Cap at 15 for conciseness
                lines.append(f"- [{f.category}] {f.subject}: {f.content[:120]}")
            if len(key_facts) > 15:
                lines.append(f"  ... and {len(key_facts) - 15} more")
            sections.append("\n".join(lines))
    except Exception as exc:
        logger.warning("Introspection: knowledge query failed: %s", exc)

    # --- Pending Whispers (proactive insights awaiting surfacing) ---
    # The awareness evaluator produces/queues whispers, but there was no
    # read-side surface, so the proactive-awareness self-test could only say
    # "I have no way to check pending whispers." Expose the pending queue here
    # (read-only) so inspect_state can honestly report what's waiting.
    # (v1 self-test, 2026-06-08.)
    try:
        pending = await state.get_pending_whispers(instance_id)
        if pending:
            lines = ["## Pending Whispers"]
            for w in pending[:10]:
                _cls = getattr(w, "delivery_class", "") or "ambient"
                lines.append(f'- [{_cls}] {getattr(w, "insight_text", "")[:160]}')
            sections.append("\n".join(lines))
    except Exception as exc:
        logger.warning("Introspection: pending-whisper query failed: %s", exc)

    # --- Context Spaces ---
    try:
        spaces = await state.list_context_spaces(instance_id)
        active_spaces = [s for s in spaces if s.status == "active"]
        if active_spaces:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            lines = ["## Context Spaces"]
            current = next((s for s in active_spaces if s.id == active_space_id), None)
            if current is not None:
                cur_tag = f" [{current.space_type}]" if current.space_type == "system" else ""
                lines.append(f"Current space: {current.name}{cur_tag}")
            for s in sorted(active_spaces, key=lambda x: (x.depth, x.name)):
                if s.space_type == "system" and s.id != active_space_id:
                    continue  # Don't clutter the user view with system internals
                parent_note = ""
                if s.parent_id:
                    parent = next((p for p in active_spaces if p.id == s.parent_id), None)
                    if parent:
                        parent_note = f" (within {parent.name})"
                type_tag = " [default]" if s.is_default else ""
                desc = s.description[:100] if s.description else "No description"
                posture_note = f"\n  Style: {s.posture}" if s.posture else ""
                # Relative time
                age_note = ""
                if s.last_active_at:
                    try:
                        last = datetime.fromisoformat(s.last_active_at)
                        days = (now - last).days
                        if days == 0:
                            age_note = " — active today"
                        elif days == 1:
                            age_note = " — active yesterday"
                        elif days < 7:
                            age_note = f" — active {days} days ago"
                    except (ValueError, TypeError):
                        pass
                lines.append(f"- {s.name}{type_tag}{parent_note}{age_note}\n  {desc}{posture_note}")
            sections.append("\n".join(lines))
    except Exception as exc:
        logger.warning("Introspection: spaces query failed: %s", exc)

    # --- Connected Capabilities ---
    try:
        if registry:
            connected = registry.get_connected()
            if connected:
                lines = ["## Connected Capabilities"]
                for cap in connected:
                    lines.append(f"- {cap.display_name}: {len(cap.tools)} tools")
                sections.append("\n".join(lines))
    except Exception as exc:
        logger.warning("Introspection: capabilities query failed: %s", exc)

    if not sections:
        return "No active state found for this instance."

    return "\n\n".join(sections)


async def build_operator_state_view(
    instance_id: str,
    state: Any,
    trigger_store: Any,
    registry: Any = None,
) -> str:
    """Build the developer/operator state view.

    Answers: "Why did Kernos do that? What is stale? What's degraded?"
    Includes everything in user view plus diagnostic detail.
    """
    sections: list[str] = []

    # --- User truth view first ---
    user_view = await build_user_truth_view(instance_id, state, trigger_store, registry)
    sections.append(user_view)

    # --- Context Spaces ---
    try:
        spaces = await state.list_context_spaces(instance_id)
        if spaces:
            lines = ["## Context Spaces"]
            for s in spaces:
                status = "active" if s.status == "active" else s.status
                lines.append(
                    f"- {s.id}: \"{s.name}\" ({s.space_type}, {status},"
                    f" last: {s.last_active_at[:16] if s.last_active_at else 'never'})"
                )
            sections.append("\n".join(lines))
    except Exception as exc:
        logger.warning("Introspection: spaces query failed: %s", exc)

    # --- Legacy Unlinked Artifacts ---
    try:
        legacy_triggers = []
        if trigger_store:
            all_triggers = await trigger_store.list_all(instance_id)
            legacy_triggers = [
                t for t in all_triggers
                if not t.source_preference_id and t.status == "active"
            ]
        legacy_covenants = []
        try:
            all_rules = await state.get_contract_rules(instance_id, active_only=True)
            legacy_covenants = [
                r for r in all_rules
                if not getattr(r, "source_preference_id", "")
                and r.source != "default"
            ]
        except Exception:
            pass

        if legacy_triggers or legacy_covenants:
            lines = ["## Legacy Unlinked Artifacts"]
            for t in legacy_triggers:
                lines.append(f"- Trigger {t.trigger_id}: \"{t.action_description}\" (no preference)")
            for r in legacy_covenants:
                lines.append(f"- Covenant {r.id}: \"{r.description}\" (no preference)")
            sections.append("\n".join(lines))
    except Exception as exc:
        logger.warning("Introspection: legacy artifacts query failed: %s", exc)

    # --- Stale Reconciliation ---
    try:
        prefs = await state.query_preferences(instance_id, active_only=True)
        stale_items = []
        if trigger_store:
            for p in prefs:
                for tid in p.derived_trigger_ids:
                    t = await trigger_store.get(instance_id, tid)
                    if t and t.status not in ("active", "completed"):
                        stale_items.append(
                            f"- Pref {p.id} → Trigger {tid}: status={t.status} (expected active)"
                        )
        if stale_items:
            lines = ["## Stale Reconciliation"]
            lines.extend(stale_items)
            sections.append("\n".join(lines))
    except Exception as exc:
        logger.warning("Introspection: stale check failed: %s", exc)

    # --- Degraded Services ---
    try:
        if registry:
            from kernos.capability.registry import CapabilityStatus
            all_caps = registry.get_all()
            degraded = [
                c for c in all_caps
                if c.status in (CapabilityStatus.ERROR, CapabilityStatus.SUPPRESSED)
            ]
            if degraded:
                lines = ["## Degraded Services"]
                for c in degraded:
                    lines.append(f"- {c.display_name}: {c.status.value}")
                sections.append("\n".join(lines))
    except Exception as exc:
        logger.warning("Introspection: degraded services check failed: %s", exc)

    # --- Superseded Preferences (history note) ---
    try:
        superseded = await state.query_preferences(
            instance_id, status="superseded", active_only=False,
        )
        revoked = await state.query_preferences(
            instance_id, status="revoked", active_only=False,
        )
        inactive_count = len(superseded) + len(revoked)
        if inactive_count:
            sections.append(
                f"## Inactive Preferences\n{len(superseded)} superseded, {len(revoked)} revoked"
            )
    except Exception as exc:
        pass

    return "\n\n".join(sections)


async def _get_trigger_summary(
    trigger_store: Any, instance_id: str, trigger_id: str,
) -> str:
    """Get a one-line summary of a trigger for display."""
    if not trigger_store:
        return trigger_id
    try:
        t = await trigger_store.get(instance_id, trigger_id)
        if not t:
            return f"{trigger_id} (not found)"
        next_fire = f", next: {t.next_fire_at[:16]}" if t.next_fire_at else ""
        return f"{trigger_id} ({t.status}{next_fire})"
    except Exception:
        return f"{trigger_id} (query failed)"
