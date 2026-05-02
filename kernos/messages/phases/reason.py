"""Reason phase — invoke the principal agent via the task engine.

HANDLER-PIPELINE-DECOMPOSE. Verbatim port of ``MessageHandler._phase_reason``.
The smallest phase — constructs the Task + ReasoningRequest and hands
off to the engine.
"""
from __future__ import annotations

from kernos.kernel.reasoning import ReasoningRequest
from kernos.kernel.task import Task, TaskType, generate_task_id
from kernos.messages.phase_context import PhaseContext
from kernos.utils import utc_now


async def run(ctx: PhaseContext) -> PhaseContext:
    """Phase 4: Build ReasoningRequest, execute via task engine."""
    handler = ctx.handler
    ctx.task = Task(
        id=generate_task_id(), type=TaskType.REACTIVE_SIMPLE,
        instance_id=ctx.instance_id, conversation_id=ctx.conversation_id,
        source="user_message", input_text=ctx.message.content, created_at=utc_now(),
    )
    # Timezone: member profile → soul (legacy)
    _tz = (ctx.member_profile or {}).get("timezone", "") or ctx.soul.timezone

    # MODEL-AND-STATUS-V1: load any persisted (member, space) override
    # so ReasoningService can apply it via resolve_effective_chain.
    # None when no row exists or InstanceDB is unwired (legacy paths).
    model_override = None
    instance_db = getattr(handler, "_instance_db", None)
    if instance_db and ctx.member_id and ctx.active_space_id:
        try:
            model_override = await instance_db.get_model_override(
                instance_id=ctx.instance_id,
                member_id=ctx.member_id,
                space_id=ctx.active_space_id,
            )
        except Exception:
            model_override = None

    request = ReasoningRequest(
        instance_id=ctx.instance_id, conversation_id=ctx.conversation_id,
        system_prompt=ctx.system_prompt, messages=ctx.messages, tools=ctx.tools,
        system_prompt_static=ctx.system_prompt_static,
        system_prompt_dynamic=ctx.system_prompt_dynamic,
        model=handler.reasoning.main_model,
        trigger="user_message", active_space_id=ctx.active_space_id,
        member_id=ctx.member_id,
        input_text=ctx.message.content, active_space=ctx.active_space,
        user_timezone=_tz,
        trace=ctx.trace,
        model_override=model_override,
        # COGNITIVE-CONTEXT-V1 C3a: pass the typed packet through so
        # the decoupled path can route it via TurnRunnerInputs ->
        # IntegrationInputs -> Briefing.cognitive_context to the
        # renderer. None on legacy fixtures and pre-C3a callers.
        cognitive_context=getattr(ctx, "cognitive_context", None),
    )
    ctx.task = await handler.engine.execute(ctx.task, request)
    ctx.response_text = ctx.task.result_text
    return ctx
