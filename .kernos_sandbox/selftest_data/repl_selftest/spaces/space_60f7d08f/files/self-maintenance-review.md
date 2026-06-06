# Daily Self-Maintenance Review

_slice: `message-pipeline`_

Daily self-review of `message-pipeline` (health: minor_concerns).
Corrective notes:
  • Phase decomposition is incomplete: `assemble.py` still depends on private helper functions from `handler.py`, so handler/phase isolation is not fully achieved.
  • The phase order is duplicated in both `pipeline.py` and `phases/__init__.py`, which is minor redundancy and a maintenance footgun if the order ever changes.
One thoughtful evolution to consider: Move the remaining prompt/block assembly helpers out of `handler.py` into a small messages-local helper module so phases no longer import private handler internals.
Consider whether any of this is worth raising to the founder or proposing as a single minor improvement (through the normal gate). Thoughtful evolution, one step at a time — no obligation to act.
