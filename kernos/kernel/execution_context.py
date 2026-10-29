"""Shared prompt context for channel guidance, facts and self-directed plans."""

from typing import Any


PLATFORM_CONTEXT: dict[str, str] = {
    "sms": (
        "You are communicating via SMS. Keep responses very short — "
        "a few sentences max. No one wants a wall of text on their phone. "
        "If content is long (reports, detailed explanations, lists), "
        "offer to send it to Discord instead using send_to_channel. "
        "Use abbreviations where natural."
    ),
    "discord": (
        "You are communicating via Discord. Keep responses concise and clear; "
        "you can use a paragraph or two when the topic warrants it."
    ),
}

KNOWLEDGE_SOURCE_TAGS = {
    "identity": "stated", "habitual": "observed",
    "structural": "established", "episodic": "remembered",
    "contextual": "recent",
}


def format_execution_envelope(envelope: dict[str, Any] | None) -> str:
    """Describe running or paused plan context without driving plan progression."""
    if not envelope:
        return ""

    plan_id = envelope.get("plan_id", "?")
    step_id = envelope.get("step_id", "?")
    step_desc = envelope.get("step_description", "")
    if envelope.get("paused"):
        reason = envelope.get("paused_reason", "budget limit")
        reason_display = {
            "step_limit": "step limit",
            "token_budget": "token budget",
            "time_limit": "time limit",
        }.get(reason, reason)
        return (
            "PAUSED PLAN\n"
            f"Plan {plan_id} is paused at step {step_id} ({reason_display}). "
            f"Used {envelope.get('steps_used', 0)}/{envelope.get('budget_steps', '?')} steps.\n"
            f"Next step was: {step_desc}\n"
            "If the user wants to continue, call manage_plan with action='continue' "
            "and the same plan_id. The budget will be extended automatically.\n"
            "If the user wants to change limits, pass budget_override with "
            "max_steps, max_tokens, or max_time_s. Set to 0 for no limit."
        )

    budget_remaining = envelope.get("budget_steps", 0) - envelope.get("steps_used", 0)
    if envelope.get("is_final_step", False):
        instruction = (
            "This is the FINAL STEP. Your response will be sent directly to the user. "
            "Choose delivery: if the user is waiting for results, produce the full "
            "detailed deliverable (not a summary). If unclear, offer a short notice. "
            "If not useful to show now, produce no text (just complete the work). "
            "If delivery depends on an event, use manage_schedule instead."
        )
    else:
        instruction = (
            "Execute this step. The system advances the plan after this step completes."
        )
    return (
        "SELF-DIRECTED EXECUTION\n"
        f"Plan: {plan_id} | Step: {step_id} | Budget remaining: {budget_remaining} steps\n"
        f"Objective: {step_desc}\n"
        f"{instruction}"
    )
