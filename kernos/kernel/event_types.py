"""Event type definitions for the KERNOS event stream.

Hierarchical type strings enable filtered subscriptions: "message.*" or "tool.*"
without parsing payloads.
"""
from enum import Enum


class EventType(str, Enum):
    # Message lifecycle
    MESSAGE_RECEIVED = "message.received"
    MESSAGE_SENT = "message.sent"

    # Reasoning (LLM calls)
    REASONING_REQUEST = "reasoning.request"
    REASONING_RESPONSE = "reasoning.response"

    # Tool usage
    TOOL_CALLED = "tool.called"
    TOOL_RESULT = "tool.result"

    # Tenant lifecycle
    TENANT_PROVISIONED = "tenant.provisioned"

    # Capability changes
    CAPABILITY_CONNECTED = "capability.connected"
    CAPABILITY_DISCONNECTED = "capability.disconnected"
    CAPABILITY_ERROR = "capability.error"

    # Task lifecycle
    TASK_CREATED = "task.created"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"

    # Agent lifecycle
    AGENT_HATCHED = "agent.hatched"
    AGENT_BOOTSTRAP_GRADUATED = "agent.bootstrap_graduated"

    # Knowledge
    KNOWLEDGE_EXTRACTED = "knowledge.extracted"

    # System
    SYSTEM_STARTED = "system.started"
    SYSTEM_STOPPED = "system.stopped"
    HANDLER_ERROR = "handler.error"

    # --- Phase 2: Covenant lifecycle (Pillar B) ---
    COVENANT_EVALUATED = "covenant.evaluated"
    COVENANT_ACTION_STAGED = "covenant.action.staged"
    COVENANT_ACTION_APPROVED = "covenant.action.approved"
    COVENANT_ACTION_REJECTED = "covenant.action.rejected"
    COVENANT_ACTION_EXPIRED = "covenant.action.expired"
    COVENANT_RULE_GRADUATED = "covenant.rule.graduated"
    COVENANT_RULE_REGRESSED = "covenant.rule.regressed"
    COVENANT_RULE_CREATED = "covenant.rule.created"
    COVENANT_RULE_UPDATED = "covenant.rule.updated"
    COVENANT_RULE_MERGED = "covenant.rule.merged"
    COVENANT_RULE_REPLACED = "covenant.rule.replaced"
    COVENANT_CONTRADICTION_DETECTED = "covenant.contradiction.detected"

    # --- Phase 2: Entity resolution (Pillar A) ---
    ENTITY_CREATED = "entity.created"
    ENTITY_MERGED = "entity.merged"
    ENTITY_LINKED = "entity.linked"

    # --- Phase 2: Knowledge lifecycle (Pillar A) ---
    KNOWLEDGE_REINFORCED = "knowledge.reinforced"
    KNOWLEDGE_INVALIDATED = "knowledge.invalidated"
    KNOWLEDGE_DECAYED = "knowledge.decayed"

    # --- Phase 2: Context Spaces ---
    CONTEXT_SPACE_CREATED = "context.space.created"
    CONTEXT_SPACE_SWITCHED = "context.space.switched"
    CONTEXT_SPACE_SUSPENDED = "context.space.suspended"

    # --- Phase 2C: Compaction ---
    COMPACTION_TRIGGERED = "compaction.triggered"
    COMPACTION_COMPLETED = "compaction.completed"
    COMPACTION_ROTATION = "compaction.rotation"
    # CLEANUP-BATCH-V1 item 8: explicit receipt around compaction
    # follow-up processing. Distinguishes "ran with N commitments and
    # emitted M triggers" from "ran with empty input" from "raised."
    # Payload: status (succeeded|empty|failed), input_count,
    #          created_count, skipped_count, skip_reasons (list[str]),
    #          error (str, only on failed).
    COMPACTION_FOLLOW_UP_PROCESSED = "compaction.follow_up.processed"

    # CROSS_SPACE_REQUESTS_V1: emitted into target's event stream
    # whenever a kernel-dispatched cross-space request applies.
    # Surfaces in target's awareness preamble on next entry so the
    # target agent can answer "why is this here?" using only
    # target-local provenance + audit.
    # Payload: request_id, origin_space_id, target_space_id,
    #          action_kind, initiating_member_id, source_turn_id,
    #          work_order, receipt (full CrossSpaceReceipt dict).
    CROSS_SPACE_ACTION = "cross_space.action"

    # --- Phase 3D: Dispatch Interceptor ---
    DISPATCH_GATE = "dispatch.gate"
    # Payload: tool_name, effect, allowed, reason, method

    # --- Phase 3B+: MCP Installation ---
    TOOL_INSTALLED = "tool.installed"
    # Payload: capability_name, tool_count, universal
    TOOL_UNINSTALLED = "tool.uninstalled"
    # Payload: capability_name

    # POSTURE-CONFIGURATION-V1 (2026-05-22): emitted when the
    # /posture slash command mutates the persisted instance_posture
    # row. Lets the operator log + future friction observer track
    # posture drift over time.
    POSTURE_CHANGED = "posture.changed"
    # Payload: field ("posture_profile" | "gate_mode"), old,
    #          new, actor (member_id)

    # TOOL-REGISTRATION-AUTHORIZATION-V1 (2026-05-22): emitted when
    # an agent's register_tool call for a hard_write or
    # external_agent_read tool enters pending-operator-approval
    # state. The approval-on-approve callback emits TOOL_REGISTRATION_
    # APPROVED once the operator confirms.
    TOOL_REGISTRATION_PENDING = "tool.registration_pending"
    # Payload: name, classification, request_id, registration_hash,
    #          space_id
    TOOL_REGISTRATION_APPROVED = "tool.registration_approved"
    # Payload: name, classification, request_id, space_id, actor

    # LIVE-DISPATCH-UNBLOCKER-V1 Phase C (2026-05-22): emitted when
    # the live dispatch can't bind a tool call (unclassified, not
    # registered, evicted, etc.). Payload mirrors
    # BindingFailureDiagnostic.to_payload(). Lets operators trace
    # opaque "tool not found" symptoms to a structured attribution.
    TOOL_BINDING_FAILURE = "tool.binding_failure"
    # Payload: tool_id, status, expected_source, gate_class,
    #          last_registration_hash, reason_omitted, + tool-specific extras

    # --- Phase 3C: Proactive Awareness ---
    PROACTIVE_INSIGHT = "proactive.insight"
    # Payload: whisper_id, insight_text, delivery_class, source_space_id,
    #          target_space_id, knowledge_entry_id, reasoning_trace

    # --- CANVAS-V1 ---
    CANVAS_CREATED = "canvas.created"
    # Payload: canvas_id, name, scope, owner_member_id, member_ids
    CANVAS_PAGE_CREATED = "canvas.page.created"
    # Payload: canvas_id, page_path, type, state, writer_member_id
    CANVAS_PAGE_CHANGED = "canvas.page.changed"
    # Payload: canvas_id, page_path, type, state, prev_state, writer_member_id
    CANVAS_PAGE_STATE_CHANGED = "canvas.page.state_changed"
    # Payload: canvas_id, page_path, type, prev_state, new_state, writer_member_id
    CANVAS_PAGE_ARCHIVED = "canvas.page.archived"
    # Payload: canvas_id, page_path, writer_member_id
