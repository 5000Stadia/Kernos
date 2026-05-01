"""Translation adapters for the unified trigger runtime.

Adapters in this package translate external descriptor / config
formats into the runtime's three-part :class:`TriggerPredicate`
model (event_selector + temporal_relation + dispatch_policy).
They live here — not on the runtime itself — so the runtime stays
agnostic to where its predicates come from.

C5a:
* :mod:`crb_compiler` — descriptor.triggers → TriggerPredicate.

C5b/C5c (later):
* :mod:`scheduler_adapter` — wraps shipped scheduler.py.
* :mod:`calendar_adapter` — wraps shipped calendar polling.
"""
from __future__ import annotations

from kernos.kernel.triggers.adapters.crb_compiler import (
    CompiledTrigger,
    compile_descriptor_triggers,
    compile_trigger_descriptor,
    derive_trigger_id,
)
from kernos.kernel.triggers.adapters.manage_schedule import (
    MANAGE_SCHEDULE_ACTION_NOTIFY,
    MANAGE_SCHEDULE_ACTION_TOOL_CALL,
    MANAGED_SCHEDULE_METADATA_KEY,
    is_managed_schedule_workflow,
    mint_managed_schedule_workflow_id,
    read_managed_schedule_metadata,
    register_managed_schedule_workflow,
    schedule_to_descriptor,
)


__all__ = [
    "CompiledTrigger",
    "MANAGE_SCHEDULE_ACTION_NOTIFY",
    "MANAGE_SCHEDULE_ACTION_TOOL_CALL",
    "MANAGED_SCHEDULE_METADATA_KEY",
    "compile_descriptor_triggers",
    "compile_trigger_descriptor",
    "derive_trigger_id",
    "is_managed_schedule_workflow",
    "mint_managed_schedule_workflow_id",
    "read_managed_schedule_metadata",
    "register_managed_schedule_workflow",
    "schedule_to_descriptor",
]
