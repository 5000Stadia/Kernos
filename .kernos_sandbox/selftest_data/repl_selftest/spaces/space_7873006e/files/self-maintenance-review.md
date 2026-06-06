# Daily Self-Maintenance Review

_slice: `message-pipeline`_

Daily self-review of `message-pipeline` (health: minor_concerns).
Corrective notes:
  • Phase modules still import helper builders from `handler.py` (`_build_*`, `_maybe_append_name_ask`), so the promised adapter/handler isolation is only partial and the phase package remains coupled to handler internals.
One thoughtful evolution to consider: Move the shared prompt/block-builder helpers out of `handler.py` into a tiny internal message-builder module so phases depend on a stable slice-local API instead of handler-private functions.
Consider whether any of this is worth raising to the founder or proposing as a single minor improvement (through the normal gate). Thoughtful evolution, one step at a time — no obligation to act.
