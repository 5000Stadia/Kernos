"""Per-turn cohort fan-out infrastructure.

First follow-on spec to INTEGRATION-LAYER-V1. This package builds
the fan-out runner that produces CohortOutput artifacts matching
the V1 schema. Real cohort adapters (gardener, memory, patterns,
covenant) come in subsequent specs and target this runner's
contract.

The runner is opt-in callable per spec acceptance criterion #13 —
nothing in the existing reasoning loop or message handler invokes
it yet. INTEGRATION-WIRE-LIVE later in the arc wires fan-out →
integration → presence into the production turn pipeline.

Failure isolation guarantee (narrowed per the design review edit #1):
async-task-per-cohort isolates yielding coroutines from each
other. The runner does NOT isolate against synchronous infinite
loops, CPU-bound work without await yield points, blocking I/O
without await, or memory exhaustion. v1 rejects sync callables at
registration; cohort authors wrapping sync work must explicitly
offload via loop.run_in_executor inside an async run callable.
"""

from kernos.kernel.cohorts.descriptor import (
    CohortContext,
    CohortDescriptor,
    CohortDescriptorError,
    CohortFanOutResult,
    CohortRunCallable,
    ContextSpaceRef,
    ExecutionMode,
    Turn,
)
from kernos.kernel.cohorts.redaction import (
    DEFAULT_TRUNCATE_AT,
    sanitize,
    sanitize_exception,
)
from kernos.kernel.cohorts.registry import CohortRegistry
from kernos.kernel.cohorts.runner import (
    AuditEmitter,
    CohortFanOutConfig,
    CohortFanOutRunner,
    build_integration_inputs_from_fan_out,
)
from kernos.kernel.cohorts.synthetic_test_cohort import (
    SyntheticBehaviour,
    make_synthetic_cohort,
)
from kernos.kernel.cohorts.gardener_cohort import (
    make_gardener_descriptor,
    register_gardener_cohort,
)
from kernos.kernel.cohorts.memory_cohort import (
    make_memory_descriptor,
    register_memory_cohort,
)
from kernos.kernel.cohorts.covenant_cohort import (
    make_covenant_descriptor,
    register_covenant_cohort,
)

__all__ = [
    "AuditEmitter",
    "CohortContext",
    "CohortDescriptor",
    "CohortDescriptorError",
    "CohortFanOutConfig",
    "CohortFanOutResult",
    "CohortFanOutRunner",
    "CohortRegistry",
    "CohortRunCallable",
    "ContextSpaceRef",
    "DEFAULT_TRUNCATE_AT",
    "ExecutionMode",
    "SyntheticBehaviour",
    "Turn",
    "build_integration_inputs_from_fan_out",
    "make_covenant_descriptor",
    "make_gardener_descriptor",
    "make_memory_descriptor",
    "make_synthetic_cohort",
    "register_covenant_cohort",
    "register_gardener_cohort",
    "register_memory_cohort",
    "sanitize",
    "sanitize_exception",
]
