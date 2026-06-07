# Daily Self-Maintenance Review

_slice: `message-pipeline`_

Daily self-review of `message-pipeline` (health: minor_concerns).
Corrective notes:
  • ErrorBuffer.emit() assigns `ts` but never uses it; small dead code / stale formatting path.
  • Phase modules still import helper functions from `handler.py`, so the slice is decomposed but not fully isolated from handler internals.
One thoughtful evolution to consider: Extract a tiny shared `phase_runner(phases, ctx)` helper in `pipeline.py` and let the three entry points delegate to it, reducing duplication without changing behavior.
Consider whether any of this is worth raising to the founder or proposing as a single minor improvement (through the normal gate). Thoughtful evolution, one step at a time — no obligation to act.
