"""Task Engine — the kernel's execution layer.

All work in KERNOS flows through the Task Engine. For 1B.4, only
reactive-simple tasks exist: user message → reasoning → response.
The engine is the hook point for behavioral contract enforcement,
priority scheduling, and task decomposition in future phases.
"""
import logging
from kernos.utils import utc_now
from datetime import datetime, timezone

from kernos.kernel.event_types import EventType
from kernos.kernel.events import EventStream, emit_event
from kernos.kernel.reasoning import ReasoningRequest, ReasoningService
from kernos.kernel.task import Task, TaskStatus

logger = logging.getLogger(__name__)




class TaskEngine:
    """Routes work through the kernel.

    For 1B.4: reactive-simple pass-through only. Every message creates a Task,
    the engine executes it via ReasoningService, lifecycle events are emitted,
    and the populated task is returned.

    The engine does NOT catch reasoning errors — it emits task.failed then
    re-raises so the handler can produce the appropriate user-facing response.
    """

    def __init__(
        self,
        reasoning: ReasoningService,
        events: EventStream,
    ) -> None:
        self._reasoning = reasoning
        self._events = events

    async def execute(self, task: Task, request: ReasoningRequest) -> Task:
        """Execute a task. Returns the task with result and metrics populated.

        Emits task.created when execution begins and task.completed or
        task.failed when execution ends. Re-raises reasoning errors after
        emitting task.failed so the handler can handle user communication.
        """
        task.status = TaskStatus.RUNNING
        task.started_at = utc_now()

        # Emit task.created
        try:
            await emit_event(
                self._events,
                EventType.TASK_CREATED,
                task.instance_id,
                "task_engine",
                payload={
                    "task_id": task.id,
                    "task_type": task.type.value,
                    "priority": task.priority,
                    "source": task.source,
                    "conversation_id": task.conversation_id,
                },
            )
        except Exception as exc:
            logger.warning("Failed to emit task.created: %s", exc)

        try:
            # EXTERNAL-AGENT-CONSULTATION v1: mark this async task as a
            # conversational turn so the consult reentrancy guard
            # permits external-agent calls from the agent's tool loop
            # (default is UNKNOWN which blocks). The token resets on
            # exit; nested consults track depth via consult_depth.
            from kernos.kernel.external_agents.reentrancy import (
                CallingContext, set_calling_context, reset_calling_context,
            )
            _ext_token = set_calling_context(CallingContext.CONVERSATIONAL)
            try:
                result = await self._reasoning.reason(request)
            finally:
                reset_calling_context(_ext_token)

            task.status = TaskStatus.COMPLETED
            task.completed_at = utc_now()
            task.result_text = result.text
            task.input_tokens = result.input_tokens
            task.output_tokens = result.output_tokens
            task.estimated_cost_usd = result.estimated_cost_usd
            task.duration_ms = result.duration_ms
            task.tool_iterations = result.tool_iterations

            # Emit task.completed
            try:
                await emit_event(
                    self._events,
                    EventType.TASK_COMPLETED,
                    task.instance_id,
                    "task_engine",
                    payload={
                        "task_id": task.id,
                        "task_type": task.type.value,
                        "duration_ms": task.duration_ms,
                        "input_tokens": task.input_tokens,
                        "output_tokens": task.output_tokens,
                        "estimated_cost_usd": task.estimated_cost_usd,
                        "tool_iterations": task.tool_iterations,
                        "conversation_id": task.conversation_id,
                    },
                )
            except Exception as exc:
                logger.warning("Failed to emit task.completed: %s", exc)

            return task

        except Exception as exc:
            task.status = TaskStatus.FAILED
            task.completed_at = utc_now()
            task.error_message = str(exc)

            # Emit task.failed (best-effort)
            try:
                await emit_event(
                    self._events,
                    EventType.TASK_FAILED,
                    task.instance_id,
                    "task_engine",
                    payload={
                        "task_id": task.id,
                        "task_type": task.type.value,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                        "conversation_id": task.conversation_id,
                    },
                )
            except Exception as emit_exc:
                logger.warning("Failed to emit task.failed: %s", emit_exc)

            raise
