# Implementation Notes

## Changes

- Added `docs/notes/soak-02.md` with exactly the three approved plain-text sentences.
- Added `tests/test_soak_02_note.py` with one substrate-fidelity test that checks reader-visible note intent and durable filesystem state together.
- Updated the existing loop-selftest dirty-scope allowlist so this approved soak-02 doc/test surface does not trip that guard.

## Acceptance Criteria Coverage

- AC1: `docs/notes/soak-02.md` exists.
- AC2: The note content is pinned exactly to the three approved sentences.
- AC3: No runtime, substrate, gate, memory, capability, or agent-flow code was changed.

## Verification

- Ran `python -m pytest tests/test_soak_02_note.py tests/test_loop_selftest_note.py -q` (`3 passed`).

STATUS: GREEN
