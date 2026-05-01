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


__all__ = [
    "CompiledTrigger",
    "compile_descriptor_triggers",
    "compile_trigger_descriptor",
    "derive_trigger_id",
]
