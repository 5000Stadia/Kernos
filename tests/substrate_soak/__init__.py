"""SUBSTRATE-SELF-TEST-V1 (2026-05-26) — probe modules.

Each probe module exposes:
  * ``async def run_probe() -> ProbeResult`` — runs the probe
  * ``REQUIRED_BEHAVIORAL_KEYS: frozenset[str]`` — keys the probe
    populates in ``ProbeResult.behavioral_evidence``
  * ``REQUIRED_SUBSTRATE_KEYS: frozenset[str]`` — keys the probe
    populates in ``ProbeResult.substrate_evidence``

The ``SubstrateSoakRunner`` (in ``kernos.kernel.self_test_gate``)
dynamically imports each probe module by name, runs it against
fresh fixtures, and validates the result against the declared
required keys. Shallow evidence (sentinel-only values like
``{"ok": True}``) is rejected per AC2 even if the probe reported
``passed=True``.

The hand-listed enumeration of probe module names lives at
``kernos.kernel.self_test_gate.PROBE_MODULE_NAMES``. Adding a new
probe means (1) creating the module here, (2) adding its name to
that tuple.
"""
