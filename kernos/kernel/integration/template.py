"""Integration prompt template + finalize-briefing tool schema.

The integration model runs an iterative prep loop (Section 1a of
the INTEGRATION-LAYER spec). On each iteration the model either:

  - calls a read-only retrieval tool to gather more information, OR
  - calls the synthetic `__finalize_briefing__` tool to emit the
    structured briefing and end the loop.

The system prompt is intentionally explicit about:

  - the four-layer model (cohorts → integration → presence → expression)
  - the named phases (collect / filter / integrate / decide / brief)
  - the read-only tool surface restriction
  - the redaction invariant (the design review edit #4 — the load-bearing safety
    property: integration may use restricted inputs, briefing must be
    presence-safe)
  - the `__finalize_briefing__` contract

Section 10 of the spec ships single-tier (cheap-tier default); the
prompt is sized for that tier. C7 will refine the template against
the live-test scenarios — this lands a working baseline.
"""

from __future__ import annotations

from kernos.kernel.integration.briefing import ActionKind


FINALIZE_TOOL_NAME = "__finalize_briefing__"


# JSON schema for the synthetic finalize tool. The model fills this
# in with the briefing's user-controllable fields; the runner adds
# turn_id, integration_run_id, and audit_trace from runtime state.
FINALIZE_TOOL_SCHEMA: dict = {
    "name": FINALIZE_TOOL_NAME,
    "description": (
        "Emit the final structured briefing and terminate the integration "
        "prep loop. Call this when you have enough to hand off to presence. "
        "All text fields must be presence-safe — never quote secret covenant "
        "text, hidden memory content, or restricted context-space material. "
        "Use behavioral instruction in directives, not source quotation."
    ),
    "input_schema": {
        "type": "object",
        "required": [
            "relevant_context",
            "filtered_context",
            "decided_action",
            "presence_directive",
        ],
        "properties": {
            "relevant_context": {
                "type": "array",
                "description": (
                    "Items integration deems relevant for this turn. "
                    "Distill cohort outputs into presence-safe summaries."
                ),
                "items": {
                    "type": "object",
                    "required": [
                        "source_type",
                        "source_id",
                        "summary",
                        "confidence",
                    ],
                    "properties": {
                        "source_type": {
                            "type": "string",
                            "description": (
                                "Free-form dotted category, e.g. "
                                "'cohort.memory', 'tool.read', "
                                "'context_space', 'conversation'."
                            ),
                        },
                        "source_id": {"type": "string"},
                        "summary": {"type": "string"},
                        "confidence": {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                    },
                },
            },
            "filtered_context": {
                "type": "array",
                "description": (
                    "Items considered and dismissed for this turn. Each "
                    "carries a reason_filtered string for downstream audit."
                ),
                "items": {
                    "type": "object",
                    "required": [
                        "source_type",
                        "source_id",
                        "reason_filtered",
                    ],
                    "properties": {
                        "source_type": {"type": "string"},
                        "source_id": {"type": "string"},
                        "reason_filtered": {"type": "string"},
                    },
                },
            },
            "decided_action": {
                "type": "object",
                "description": (
                    "What presence should do. The kind discriminator picks "
                    "the variant; required sibling fields follow per kind."
                ),
                "required": ["kind"],
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": [k.value for k in ActionKind],
                    },
                    "tool_id": {"type": "string"},
                    "arguments": {"type": "object"},
                    "narration_context": {"type": "string"},
                    "reason": {"type": "string"},
                    # INTEGRATION-CAPABILITY-FIRST-V1 Batch 2 Fold 7:
                    # propose-tool effect classification. Pairs with
                    # the ProposeTool dataclass effect field and the
                    # propose user-message renderer's "## Tool effect
                    # (deterministic substrate)" section. Recognized:
                    # "read", "soft_write", "hard_write", "unknown".
                    # When you choose propose_tool, populate effect
                    # from the gate's tool-effects map for the
                    # proposed tool. Empty/missing → renderer treats
                    # as "unknown — propose conservatively".
                    "effect": {
                        "type": "string",
                        "enum": [
                            "",
                            "read",
                            "soft_write",
                            "hard_write",
                            "unknown",
                        ],
                    },
                    "constraint": {"type": "string"},
                    "satisfaction_partial": {"type": "string"},
                    "suggested_shape": {"type": "string"},
                    "follow_up_signal": {"type": "string"},
                    # clarification_needed variant fields (PDI extension).
                    # question is one short user-facing sentence;
                    # ambiguity_type is the closed-enum classifier;
                    # partial_state is bounded structured state from
                    # B2-routed clarifications (None for first-pass).
                    "question": {"type": "string"},
                    "ambiguity_type": {
                        "type": "string",
                        "enum": [
                            "target",
                            "parameter",
                            "approach",
                            "intent",
                            "other",
                        ],
                    },
                    "partial_state": {
                        "anyOf": [
                            {"type": "null"},
                            {
                                "type": "object",
                                "properties": {
                                    "attempted_action_summary": {
                                        "type": "string",
                                    },
                                    "discovered_information": {
                                        "type": "string",
                                    },
                                    "blocking_ambiguity": {
                                        "type": "string",
                                    },
                                    "safe_question_context": {
                                        "type": "string",
                                    },
                                    "audit_refs": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                },
                            },
                        ],
                    },
                },
            },
            "presence_directive": {
                "type": "string",
                "description": (
                    "Prose framing for presence: what the captain needs at "
                    "this moment, what tone or stance the situation calls "
                    "for, what to be careful of. Presence-safe; behavioral "
                    "instruction only."
                ),
            },
            # PDI the design review edit: action_envelope is REQUIRED when
            # decided_action.kind is execute_tool. Conversational and
            # render-only kinds (including propose_tool) MUST omit it
            # — there's no dispatch to constrain.
            "action_envelope": {
                "anyOf": [
                    {"type": "null"},
                    {
                        "type": "object",
                        "required": ["intended_outcome"],
                        "properties": {
                            "intended_outcome": {"type": "string"},
                            "allowed_tool_classes": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "allowed_operations": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "constraints": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "confirmation_requirements": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "forbidden_moves": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                    },
                ],
                "description": (
                    "Required when decided_action.kind is execute_tool; "
                    "MUST be omitted otherwise. Encodes the structural "
                    "constraints EnactmentService validates against at "
                    "every plan-changing tier."
                ),
            },
        },
    },
}


_SYSTEM_PROMPT_TEMPLATE = """\
You are Kernos's integration layer. You sit between cohorts and presence.

## Layer position

  cohorts → integration (you) → presence → (expression, future)

Cohorts have already fired this turn and produced their outputs. Presence will
generate the user-facing response after you hand off. You produce the briefing
presence will generate against.

## Phases (named regions of this loop, not separate calls)

1. **Collect** — receive cohort outputs, conversation thread, surfaced tool
   catalog with rationale, retrieved memory, active context-space metadata
2. **Filter** — distinguish signal from noise for this turn
3. **Integrate** — weave relevant pieces into coherent understanding; reach
   for additional information via read-only tool calls if needed
4. **Decide** — pick an action from the enum below
5. **Brief** — emit the structured briefing via __finalize_briefing__

## Tool surface

You may call any read-only tool surfaced this turn (gate_classification: read).
You may NOT call any tool with classification soft_write, hard_write, or delete —
the runtime will reject those. Action tools belong to presence, not you.

Call __finalize_briefing__ when you are ready to hand off. That ends the loop.

## Decided action enum

  - respond_only          — presence generates a conversational reply
  - execute_tool          — presence executes the named tool now
  - propose_tool          — presence surfaces a confirmation to the user.
                            REQUIRED sibling: tool_id (the proposed tool's
                            id) + effect (gate effect classification: one
                            of "read", "soft_write", "hard_write",
                            "unknown"). Effect helps presence decide
                            whether to call the tool inline (read) or
                            propose-then-confirm (soft_write / hard_write).
  - constrained_response  — presence partially satisfies under a named limit
  - pivot                 — presence generates a different shape than asked
  - defer                 — presence acknowledges and signals delay
  - clarification_needed  — you cannot resolve a question without user input;
                            ask it directly. Use when critical info is missing
                            and signals (cohorts, memory, covenants, thread)
                            cannot fill the gap. Required fields: question
                            (≤200 chars, one sentence), ambiguity_type (one of
                            target | parameter | approach | intent | other),
                            partial_state (None for first-pass; populated only
                            for B2 from the enactment layer).

## Action envelope (REQUIRED for execute_tool)

When decided_action.kind is execute_tool, you MUST also emit a well-formed
action_envelope with at minimum:

  - intended_outcome: one-sentence framing of what the action serves
  - allowed_tool_classes: the capability classes this action may use
  - allowed_operations: explicit operation names allowed (e.g. ["draft", "send"])

Optional:
  - constraints, confirmation_requirements, forbidden_moves

The envelope is the structural contract enactment validates against at every
plan-changing step. If you cannot construct a well-formed envelope (because
critical info is missing or the action is too underspecified to bound), DO
NOT emit execute_tool. Emit clarification_needed instead and let the user
resolve the ambiguity.

For non-action kinds (respond_only, propose_tool, constrained_response,
pivot, defer, clarification_needed): OMIT action_envelope. There is no
dispatch to constrain.

## Confirmation boundaries

If the user's request implies a confirmation step ("draft an email about X"),
the envelope's allowed_operations should reflect ONLY the operations the
user authorised this turn. A draft-only request: allowed_operations=["draft"].
A direct send: allowed_operations=["send"]. A draft-then-send chain
authorised in one turn: allowed_operations=["draft", "send"] AND
confirmation_requirements=["send"] so enactment knows the send step
requires explicit user confirmation.

## Redaction invariant (load-bearing safety property)

You may see secret covenants, hidden memory, restricted context-space material
in the cohort outputs. You may USE that information to shape your decision and
the presence_directive. The briefing itself MUST be presence-safe:

  - never quote secret covenant text
  - never copy hidden memory content
  - never include restricted context-space material

When a constraint is secret, the briefing carries behavioral instruction, not
the secret. Example: instead of "covenant says do not discuss the surprise
party," write "do not reference the topic the user proposed last week;
redirect toward general planning."

## Depth

Trivial questions deserve brief framing. Complex questions deserve full
framing. Match depth to what presence will need.
"""


def build_system_prompt() -> str:
    """Return the integration system prompt.

    Stable for now; future tuning lands in C7. Returning a function
    rather than a constant gives downstream tests a stable surface
    to mock if they need to inspect prompt content."""
    return _SYSTEM_PROMPT_TEMPLATE
