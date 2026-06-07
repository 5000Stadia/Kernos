# Daily Self-Maintenance Review

_slice: `message-pipeline`_

Daily self-review of `message-pipeline` (health: minor_concerns).
Corrective notes:
  • Phase modules still lazily import several prompt/block helpers from handler.py, so the pipeline's adapter/handler isolation is only partial.
One thoughtful evolution to consider: Extract those pure prompt/block builders into a small messages/builders module so assemble/consequence no longer depend on handler internals.
Consider whether any of this is worth raising to the founder or proposing as a single minor improvement (through the normal gate). Thoughtful evolution, one step at a time — no obligation to act.
